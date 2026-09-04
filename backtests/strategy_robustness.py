#!/usr/bin/env python3
"""Robustness checks for corrected event-study outputs (inputs are read-only).

This module deliberately refuses the historical event files which only contain
decision timestamps and returns.  Re-run the source studies after they emit
``entry_ts`` and ``exit_ts``: without those fields neither overlap purging nor
an embargoed out-of-sample split is auditable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REGIMES = ("uptrend_low_vol", "uptrend_high_vol", "downtrend_low_vol", "downtrend_high_vol")


@dataclass(frozen=True)
class Config:
    train_start: str = "2023-01-01"
    formal_year: int = 2025
    monitoring_year: int = 2026
    bootstrap_samples: int = 2_000
    seed: int = 20260904
    min_train_events: int = 30
    min_train_months: int = 12
    min_test_events: int = 10
    min_test_months: int = 5
    selection_fdr: float = 0.10


def stable_seed(*parts: object, base_seed: int = 0) -> int:
    """A process-independent RNG seed (unlike Python's salted hash())."""
    payload = "|".join(map(str, (base_seed, *parts))).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little") % (2**32)


def benjamini_hochberg(values: Iterable[float]) -> np.ndarray:
    p = np.asarray(list(values), dtype=float)
    out = np.full(len(p), np.nan)
    valid = np.isfinite(p)
    if not valid.any():
        return out
    x, idx, n = p[valid], np.argsort(p[valid]), valid.sum()
    ranked = x[idx] * n / np.arange(1, n + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    target = np.flatnonzero(valid)[idx]
    out[target] = np.minimum(adjusted, 1.0)
    return out


def benjamini_yekutieli(values: Iterable[float]) -> np.ndarray:
    p = np.asarray(list(values), dtype=float)
    n = int(np.isfinite(p).sum())
    return np.minimum(benjamini_hochberg(p) * (np.sum(1.0 / np.arange(1, n + 1)) if n else 1), 1.0)


def normalise_events(raw: pd.DataFrame, strategy: str, family: str) -> pd.DataFrame:
    """Enforce corrected schema and return a common event representation."""
    decision = "detect_ts" if "detect_ts" in raw else "ts" if "ts" in raw else None
    net = "net_ret_bp" if "net_ret_bp" in raw else "net_bp" if "net_bp" in raw else None
    if decision is None or net is None or "entry_ts" not in raw or "exit_ts" not in raw:
        raise ValueError(
            f"{strategy}: corrected event schema required (detect_ts/ts, entry_ts, exit_ts, net_ret_bp/net_bp). "
            "Re-run liq_reversion.py and crowding_signals.py with corrected event output first."
        )
    event = raw[[decision, "entry_ts", "exit_ts", net]].copy()
    event.columns = ["detect_ts", "entry_ts", "exit_ts", "net_bp"]
    for column in event.columns:
        event[column] = pd.to_numeric(event[column], errors="coerce")
    invalid = event.isna().any(axis=1) | ~np.isfinite(event).all(axis=1)
    if invalid.any():
        raise ValueError(f"{strategy}: {int(invalid.sum())} event rows contain missing or non-finite values")
    event = event.astype({"detect_ts": "int64", "entry_ts": "int64", "exit_ts": "int64"})
    if (event.entry_ts < event.detect_ts).any() or (event.exit_ts <= event.entry_ts).any():
        raise ValueError(f"{strategy}: timing validation failed (require entry_ts >= detect_ts and exit_ts > entry_ts)")
    event["strategy"], event["family"] = strategy, family
    return event


def greedy_purge(events: pd.DataFrame, strategy: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep the first chronological non-overlapping trade; retain an audit row."""
    if events.empty:
        name = strategy if strategy is not None else "<unknown>"
        return events.copy(), pd.DataFrame([{"strategy": name, "events_raw": 0,
                                              "events_kept": 0, "events_purged": 0}])
    ordered = events.sort_values(["entry_ts", "exit_ts", "detect_ts"], kind="stable").copy()
    keep, last_exit, purged = [], -np.inf, 0
    for i, row in ordered.iterrows():
        accepted = row.entry_ts >= last_exit
        keep.append(accepted)
        if accepted: last_exit = row.exit_ts
        else: purged += 1
    ordered["purge_kept"] = keep
    kept = ordered.loc[ordered.purge_kept].drop(columns="purge_kept").reset_index(drop=True)
    audit = pd.DataFrame([{"strategy": str(events.strategy.iloc[0]), "events_raw": len(events),
                           "events_kept": len(kept), "events_purged": purged}])
    return kept, audit


def join_prior_day_regime(events: pd.DataFrame, daily: pd.DataFrame, symbol: str) -> pd.DataFrame:
    regime = daily[daily.symbol.eq(symbol)].copy()
    regime["date"] = pd.to_datetime(regime.date, utc=True).dt.normalize()
    regime = regime[["date", "regime"]].drop_duplicates("date")
    result = events.copy()
    result["event_date"] = pd.to_datetime(result.detect_ts, unit="ms", utc=True).dt.normalize()
    result["regime_date"] = result.event_date - pd.Timedelta(days=1)
    result = result.merge(regime, left_on="regime_date", right_on="date", how="left").drop(columns="date")
    result["trend"] = np.where(result.regime.str.startswith("uptrend", na=False), "uptrend",
                         np.where(result.regime.str.startswith("downtrend", na=False), "downtrend", pd.NA))
    return result


def cluster_bootstrap(values: Sequence[float], months: Sequence[object], samples: int, seed: int) -> dict[str, float]:
    if samples < 1:
        raise ValueError("bootstrap samples must be positive")
    x, m = np.asarray(values, float), np.asarray(months).astype(str)
    valid = np.isfinite(x)
    x, m = x[valid], m[valid]
    if not len(x):
        return {
            "mean_bp": np.nan,
            "ci_low_bp": np.nan,
            "ci_high_bp": np.nan,
            "p_positive": np.nan,
            "p_two_sided": np.nan,
        }
    groups = [x[m == month] for month in np.unique(m)]
    group_sums = np.asarray([group.sum() for group in groups], dtype=float)
    group_counts = np.asarray([len(group) for group in groups], dtype=np.int64)
    cluster_count = len(groups)
    rng = np.random.default_rng(seed)
    selected = rng.integers(0, cluster_count, size=(samples, cluster_count))
    draws = group_sums[selected].sum(axis=1) / group_counts[selected].sum(axis=1)
    mean = float(x.mean())
    # Null draws resample month clusters after mean-centring the original series.
    null_sums = group_sums - mean * group_counts
    selected = rng.integers(0, cluster_count, size=(samples, cluster_count))
    null_draws = null_sums[selected].sum(axis=1) / group_counts[selected].sum(axis=1)
    return {"mean_bp": mean, "ci_low_bp": float(np.quantile(draws, .025)), "ci_high_bp": float(np.quantile(draws, .975)),
            "p_positive": float((1 + np.sum(null_draws >= mean)) / (samples + 1)),
            "p_two_sided": float((1 + np.sum(np.abs(null_draws) >= abs(mean))) / (samples + 1))}


def embargo_split(events: pd.DataFrame, year: int, horizon_ms: int, train_start: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp(f"{year}-01-01", tz="UTC").value // 1_000_000
    end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC").value // 1_000_000
    train_start_ms = pd.Timestamp(train_start, tz="UTC").value // 1_000_000
    train = events[(events.detect_ts >= train_start_ms) & (events.exit_ts < start - horizon_ms)]
    test = events[(events.entry_ts >= start + horizon_ms) & (events.exit_ts < end)]
    return train.copy(), test.copy()


def _horizon(events: pd.DataFrame) -> int:
    return int(max(0, (events.exit_ts - events.entry_ts).max())) if len(events) else 0


def _eligible(frame: pd.DataFrame, event_min: int, month_min: int) -> bool:
    return len(frame) >= event_min and frame.month.nunique() >= month_min


def _event_files(root: Path) -> list[tuple[Path, str, str, str, dict[str, object]]]:
    """Return corrected-source files, preserving every liq variant and A2/A3/B2/B3 split."""
    out: list[tuple[Path, str, str, str, dict[str, object]]] = []
    summary = root / "liq_reversion" / "summary.csv"
    if summary.exists():
        for record in pd.read_csv(summary).to_dict("records"):
            variant = record.pop("variant")
            out.append((root / "liq_reversion" / f"events_{variant}.csv", str(variant), "liq_reversion", str(variant).split("_")[0].upper() + "USDT", record))
    patterns = (("A2_contrarian_events_*.csv", "crowding_A2"), ("A3_momentum_events_*.csv", "crowding_A3"),
                ("B2_deleverage_events_*.csv", "crowding_B2"), ("B3_double_overheat_events_*.csv", "crowding_B3"))
    for pattern, family in patterns:
        for path in sorted((root / "crowding").glob(pattern)):
            symbol = "BTCUSDT" if "BTCUSDT" in path.name else "ETHUSDT"
            # B2 contains 24h/72h in one file and is divided after loading.
            out.append((path, path.stem, family, symbol, {}))
    return out


def load_strategies(results_root: Path, daily: pd.DataFrame) -> tuple[list[pd.DataFrame], pd.DataFrame]:
    strategies, audits = [], []
    for path, base, family, symbol, metadata in _event_files(results_root):
        if not path.exists(): raise FileNotFoundError(f"missing expected event file: {path}")
        raw = pd.read_csv(path)
        subsets = [(base, raw)]
        if family == "crowding_B2":
            if "horizon" not in raw: raise ValueError(f"{path}: B2 requires horizon for strategy normalisation")
            subsets = ([(f"{base}_{h}", d) for h, d in raw.groupby("horizon", sort=True)]
                       if not raw.empty else [(f"{base}_{h}", raw) for h in ("24h", "72h")])
        for strategy, subset in subsets:
            event = normalise_events(subset, strategy, family)
            event, audit = greedy_purge(event, strategy)
            event = join_prior_day_regime(event, daily, symbol)
            event["symbol"] = symbol
            event["month"] = pd.to_datetime(event.detect_ts, unit="ms", utc=True).dt.strftime("%Y-%m")
            safe_metadata = {f"meta_{key}": value for key, value in metadata.items()}
            audits.append(audit.assign(family=family, symbol=symbol, source_file=path.name, horizon_ms=_horizon(event), **safe_metadata))
            # Empty variants remain visible in the inventory but have no testable
            # statistic and therefore must not enter either FDR family.
            if not event.empty:
                strategies.append(event)
    return strategies, pd.concat(audits, ignore_index=True) if audits else pd.DataFrame()


def _adjust(frame: pd.DataFrame, pcol: str, eligible_col: str, prefix: str, global_groups: list[str]) -> pd.DataFrame:
    out = frame.copy()
    p = out[pcol].where(out[eligible_col])
    out[f"{prefix}_bh_q"] = p.groupby([out[c] for c in global_groups], dropna=False).transform(benjamini_hochberg)
    out[f"{prefix}_by_q"] = p.groupby([out[c] for c in global_groups], dropna=False).transform(benjamini_yekutieli)
    # Global BY is primary; family-level results are supplementary and explicit.
    out[f"{prefix}_family_bh_q"] = p.groupby([out[c] for c in [*global_groups, "family"]], dropna=False).transform(benjamini_hochberg)
    out[f"{prefix}_family_by_q"] = p.groupby([out[c] for c in [*global_groups, "family"]], dropna=False).transform(benjamini_yekutieli)
    return out


def run(config: Config, results_root: Path, regime_path: Path, output_dir: Path) -> dict[str, int]:
    if config.bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    if not 0 < config.selection_fdr <= 1:
        raise ValueError("selection_fdr must be in (0, 1]")
    minimums = (config.min_train_events, config.min_train_months,
                config.min_test_events, config.min_test_months)
    if any(value < 1 for value in minimums):
        raise ValueError("event and month eligibility minimums must be positive")
    if config.monitoring_year <= config.formal_year:
        raise ValueError("monitoring_year must be later than formal_year for an expanding fold")
    daily = pd.read_csv(regime_path)
    if not {"date", "symbol", "regime"}.issubset(daily): raise ValueError("daily_regimes.csv must contain date, symbol, regime")
    strategies, inventory = load_strategies(results_root, daily)
    if not strategies:
        raise ValueError("no non-empty corrected event strategies were found")
    folds = []
    for year, label in ((config.formal_year, "formal_oos"), (config.monitoring_year, "monitoring_ytd")):
        for event in strategies:
            horizon = _horizon(event)
            train, test = embargo_split(event, year, horizon, config.train_start)
            row = {"strategy": event.strategy.iloc[0], "family": event.family.iloc[0], "symbol": event.symbol.iloc[0], "fold_year": year, "fold_type": label, "horizon_ms": horizon,
                   "train_events": len(train), "train_months": train.month.nunique(), "test_events": len(test), "test_months": test.month.nunique()}
            row["train_eligible"] = _eligible(train, config.min_train_events, config.min_train_months)
            row["test_eligible"] = _eligible(test, config.min_test_events, config.min_test_months)
            row.update({f"train_{k}": v for k, v in cluster_bootstrap(train.net_bp, train.month, config.bootstrap_samples, stable_seed(row["strategy"], year, "train", base_seed=config.seed)).items()})
            row.update({f"test_{k}": v for k, v in cluster_bootstrap(test.net_bp, test.month, config.bootstrap_samples, stable_seed(row["strategy"], year, "test", base_seed=config.seed)).items()})
            folds.append(row)
    folds_df = pd.DataFrame(folds)
    # Selection is frozen from the training side only; test statistics never enter it.
    folds_df = _adjust(folds_df, "train_p_positive", "train_eligible", "train", ["fold_year"])
    folds_df = _adjust(folds_df, "test_p_positive", "test_eligible", "test", ["fold_year"])
    folds_df["primary_selection"] = (folds_df.train_eligible & (folds_df.train_mean_bp > 0)
                                      & (folds_df.train_by_q <= config.selection_fdr))
    folds_df["oos_positive"] = folds_df.test_eligible & (folds_df.test_mean_bp > 0)
    folds_df["oos_survives_selection"] = folds_df.primary_selection & folds_df.oos_positive
    folds_df["oos_fdr_significant"] = (folds_df.oos_positive
                                         & (folds_df.test_by_q <= config.selection_fdr))
    folds_df["selected_oos_fdr_significant"] = (folds_df.primary_selection
                                                  & folds_df.oos_fdr_significant)
    folds_df["monitoring_only"] = folds_df.fold_type.eq("monitoring_ytd")

    regime_rows = []
    for event in strategies:
        _, test = embargo_split(event, config.formal_year, _horizon(event), config.train_start)
        for bucket, part, scope in [("all", test, "primary"), ("uptrend", test[test.trend.eq("uptrend")], "primary"), ("downtrend", test[test.trend.eq("downtrend")], "primary")]:
            regime_rows.append(_regime_row(event, part, bucket, scope, config))
        for regime in REGIMES:
            regime_rows.append(_regime_row(event, test[test.regime.eq(regime)], regime, "exploratory", config))
    regimes_df = _adjust(pd.DataFrame(regime_rows), "p_positive", "eligible", "oos", ["scope"])
    formal_selection = folds_df.loc[folds_df.fold_year.eq(config.formal_year), ["strategy", "primary_selection"]]
    regimes_df = regimes_df.merge(formal_selection, on="strategy", how="left")
    regimes_df["selected_strategy"] = regimes_df.primary_selection.fillna(False)
    regimes_df["oos_fdr_significant"] = (regimes_df.eligible & (regimes_df.mean_bp > 0)
                                           & (regimes_df.oos_by_q <= config.selection_fdr))
    regimes_df["selected_oos_fdr_significant"] = (regimes_df.selected_strategy
                                                    & regimes_df.oos_fdr_significant)
    output_dir.mkdir(parents=True, exist_ok=True)
    inventory.to_csv(output_dir / "strategy_inventory.csv", index=False)
    folds_df.to_csv(output_dir / "walk_forward_folds.csv", index=False)
    family_df = (folds_df.groupby(["fold_year", "fold_type", "family"], as_index=False)
                 .agg(variants=("strategy", "nunique"), eligible_train_variants=("train_eligible", "sum"),
                      eligible_test_variants=("test_eligible", "sum"), selected_variants=("primary_selection", "sum"),
                      selected_with_positive_test=("oos_survives_selection", "sum"),
                      selected_with_fdr_test=("selected_oos_fdr_significant", "sum"),
                      median_train_mean_bp=("train_mean_bp", "median"), median_test_mean_bp=("test_mean_bp", "median"),
                      monitoring_only=("monitoring_only", "first")))
    family_df.to_csv(output_dir / "family_walk_forward.csv", index=False)
    regimes_df.to_csv(output_dir / "formal_oos_by_regime.csv", index=False)
    audit_totals = ({key: int(value) for key, value in
                     inventory[["events_raw", "events_kept", "events_purged"]].sum().to_dict().items()}
                    if len(inventory) else {})
    family_inventory = {
        str(family): {
            "normalized_strategies": int(len(group)),
            "events_raw": int(group.events_raw.sum()),
            "events_kept": int(group.events_kept.sum()),
            "events_purged": int(group.events_purged.sum()),
            "purge_rate": (float(group.events_purged.sum() / group.events_raw.sum())
                           if group.events_raw.sum() else None),
        }
        for family, group in inventory.groupby("family", sort=True)
    }
    formal = folds_df[folds_df.fold_year.eq(config.formal_year)]
    monitoring = folds_df[folds_df.fold_year.eq(config.monitoring_year)]
    primary_regimes = regimes_df[regimes_df.scope.eq("primary")]
    exploratory_regimes = regimes_df[regimes_df.scope.eq("exploratory")]

    def finite_min(series: pd.Series) -> float | None:
        value = pd.to_numeric(series, errors="coerce").min()
        return None if pd.isna(value) else float(value)

    formal_findings = {
        "train_eligible_strategies": int(formal.train_eligible.sum()),
        "test_eligible_strategies": int(formal.test_eligible.sum()),
        "train_raw_p_le_0_05_positive": int((formal.train_eligible & (formal.train_mean_bp > 0)
                                               & (formal.train_p_positive <= 0.05)).sum()),
        "train_global_bh_candidates": int((formal.train_eligible & (formal.train_mean_bp > 0)
                                             & (formal.train_bh_q <= config.selection_fdr)).sum()),
        "train_global_by_selected": int(formal.primary_selection.sum()),
        "train_min_global_bh_q": finite_min(formal.train_bh_q),
        "train_min_global_by_q": finite_min(formal.train_by_q),
        "test_positive_strategies": int(formal.oos_positive.sum()),
        "test_global_bh_significant": int((formal.oos_positive
                                             & (formal.test_bh_q <= config.selection_fdr)).sum()),
        "test_global_by_significant": int(formal.oos_fdr_significant.sum()),
        "test_min_global_by_q": finite_min(formal.test_by_q),
        "selected_with_positive_test": int(formal.oos_survives_selection.sum()),
        "selected_with_fdr_significant_test": int(formal.selected_oos_fdr_significant.sum()),
    }
    monitoring_findings = {
        "train_eligible_strategies": int(monitoring.train_eligible.sum()),
        "test_eligible_strategies": int(monitoring.test_eligible.sum()),
        "train_global_by_selected": int(monitoring.primary_selection.sum()),
        "train_min_global_by_q": finite_min(monitoring.train_by_q),
        "test_global_by_significant": int(monitoring.oos_fdr_significant.sum()),
        "test_min_global_by_q": finite_min(monitoring.test_by_q),
    }
    regime_findings = {
        "primary_eligible_rows": int(primary_regimes.eligible.sum()),
        "primary_global_by_significant": int(primary_regimes.oos_fdr_significant.sum()),
        "primary_min_global_by_q": finite_min(primary_regimes.oos_by_q),
        "exploratory_eligible_rows": int(exploratory_regimes.eligible.sum()),
        "exploratory_global_by_significant": int(exploratory_regimes.oos_fdr_significant.sum()),
        "exploratory_min_global_by_q": finite_min(exploratory_regimes.oos_by_q),
    }
    output_names = ["strategy_inventory.csv", "walk_forward_folds.csv", "family_walk_forward.csv",
                    "formal_oos_by_regime.csv", "analysis_summary.json"]
    summary = {"config": asdict(config),
               "source_event_files": int(inventory.source_file.nunique()),
               "normalized_strategies": int(len(inventory)),
               "analyzed_nonempty_strategies": len(strategies),
               "family_inventory": family_inventory,
               "folds": len(folds_df), "formal_oos_year": config.formal_year,
               "monitoring_year": config.monitoring_year, "monitoring_disclaimer": "2026 results are monitoring only, not formal OOS confirmation.",
               "overlap_purge": audit_totals,
               "overlap_purge_rate": (audit_totals.get("events_purged", 0) / audit_totals.get("events_raw", 1)
                                      if audit_totals.get("events_raw", 0) else None),
               "formal_findings": formal_findings,
               "monitoring_findings": monitoring_findings,
               "regime_findings": regime_findings,
               "formal_train_selected": int(formal.primary_selection.sum()),
               "formal_selected_oos_positive": int(formal.oos_survives_selection.sum()),
               "formal_selected_oos_fdr_significant": int(formal.selected_oos_fdr_significant.sum()),
               "primary_rule": f"eligible training rows, positive train mean, fold-global BY q <= {config.selection_fdr}",
               "embargo_rule": "Two-sided strategy-horizon embargo: train exits before fold_start-horizon; test entries start at fold_start+horizon and test exits remain inside the fold year.",
               "timing_rule": "Execution uses the first eligible bar open. entry_ts may equal detect_ts for liquidation events because detect_ts is the prior bar-close decision instant and the next bar opens at that same instant.",
               "oos_design_note": "The year split is a retrospective chronological holdout, not a prospectively sealed experiment.",
               "four_regime_note": "Four-regime results are exploratory because each split reduces event and month counts and expands the hypothesis family.",
               "outputs": output_names}
    (output_dir / "analysis_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return {"strategies": len(strategies), "folds": len(folds_df), "formal_rows": len(regimes_df)}


def _regime_row(event: pd.DataFrame, part: pd.DataFrame, bucket: str, scope: str, config: Config) -> dict[str, object]:
    stats = cluster_bootstrap(part.net_bp, part.month, config.bootstrap_samples, stable_seed(event.strategy.iloc[0], bucket, base_seed=config.seed))
    return {"strategy": event.strategy.iloc[0], "family": event.family.iloc[0], "symbol": event.symbol.iloc[0], "bucket": bucket, "scope": scope,
            "events": len(part), "months": part.month.nunique(), "eligible": _eligible(part, config.min_test_events, config.min_test_months), **stats}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results-root", type=Path, default=ROOT / "results")
    p.add_argument("--regimes", type=Path, default=ROOT / "results/market_regimes/daily_regimes.csv")
    p.add_argument("--output-dir", type=Path, default=ROOT / "results/strategy_robustness")
    p.add_argument("--train-start", default="2023-01-01")
    p.add_argument("--formal-year", type=int, default=2025)
    p.add_argument("--monitoring-year", type=int, default=2026)
    p.add_argument("--bootstrap-samples", type=int, default=2_000)
    p.add_argument("--seed", type=int, default=20260904)
    p.add_argument("--selection-fdr", type=float, default=.10)
    p.add_argument("--min-train-events", type=int, default=30); p.add_argument("--min-train-months", type=int, default=12)
    p.add_argument("--min-test-events", type=int, default=10); p.add_argument("--min-test-months", type=int, default=5)
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    config = Config(train_start=args.train_start, formal_year=args.formal_year, monitoring_year=args.monitoring_year,
                    bootstrap_samples=args.bootstrap_samples, seed=args.seed, min_train_events=args.min_train_events,
                    min_train_months=args.min_train_months, min_test_events=args.min_test_events, min_test_months=args.min_test_months,
                    selection_fdr=args.selection_fdr)
    print(run(config, args.results_root, args.regimes, args.output_dir))
