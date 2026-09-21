#!/usr/bin/env python3
"""Causal execution-capacity sensitivity for existing BTC/ETH event studies.

The source event files are read-only inputs.  This program re-prices their
already-costed returns using only quote volume known before each execution bar.
It deliberately keeps rows which cannot be executed so capacity failures do
not disappear from the reported sample.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")
CUTOFF_EXCLUSIVE_TS = int(pd.Timestamp("2026-08-01T00:00:00Z").value // 1_000_000)
MINUTE_MS = 60_000
WINDOW_MINUTES = 60
SOURCE_ROUND_TRIP_BP = 14.0


@dataclass(frozen=True)
class Scenario:
    name: str
    notional_usd: float
    max_participation: float
    fee_bp_one_way: float
    slippage_bp_one_way: float
    impact_coefficient_bp: float

    def validate(self) -> None:
        if not self.name:
            raise ValueError("scenario name must not be empty")
        values = (self.notional_usd, self.max_participation, self.fee_bp_one_way,
                  self.slippage_bp_one_way, self.impact_coefficient_bp)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("scenario values must be finite")
        if self.notional_usd <= 0 or self.max_participation <= 0:
            raise ValueError("notional_usd and max_participation must be positive")
        if min(self.fee_bp_one_way, self.slippage_bp_one_way, self.impact_coefficient_bp) < 0:
            raise ValueError("cost values must be non-negative")


DEFAULT_SCENARIOS = (
    Scenario("baseline_10k_14bp", 10_000.0, 0.10, 5.0, 2.0, 0.0),
    Scenario("impact_100k", 100_000.0, 0.10, 5.0, 2.0, 10.0),
    Scenario("impact_1m", 1_000_000.0, 0.10, 5.0, 2.0, 10.0),
)


def validate_scenarios(scenarios: Iterable[Scenario]) -> tuple[Scenario, ...]:
    result = tuple(scenarios)
    if not result:
        raise ValueError("at least one scenario is required")
    for scenario in result:
        scenario.validate()
    if len({scenario.name for scenario in result}) != len(result):
        raise ValueError("scenario names must be unique")
    return result


def load_scenarios(path: Path | None) -> tuple[Scenario, ...]:
    if path is None:
        return validate_scenarios(DEFAULT_SCENARIOS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        scenarios = tuple(Scenario(**item) for item in payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid scenario config {path}: expected a JSON list of Scenario objects") from exc
    return validate_scenarios(scenarios)


def _symbol_for(path: Path, frame: pd.DataFrame) -> str:
    if "symbol" in frame and frame.symbol.dropna().nunique() == 1:
        value = str(frame.symbol.dropna().iloc[0]).upper()
        if value in {"BTCUSDT", "ETHUSDT"}:
            return value
    upper = path.name.upper()
    for symbol in ("BTCUSDT", "ETHUSDT"):
        if symbol in upper:
            return symbol
    # liq_reversion variants use ``events_btc_...`` while crowding filenames
    # carry the full exchange symbol.
    if "_BTC_" in upper or upper.startswith("BTC_"):
        return "BTCUSDT"
    if "_ETH_" in upper or upper.startswith("ETH_"):
        return "ETHUSDT"
    raise ValueError(f"cannot infer BTCUSDT or ETHUSDT from {path}; add a single symbol column")


def expand_event_files(values: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        matches = [Path(p) for p in glob.glob(value)]
        if matches:
            paths.extend(matches)
        elif Path(value).is_file():
            paths.append(Path(value))
        else:
            raise FileNotFoundError(f"event input does not exist or match a glob: {value}")
    unique = sorted({p.resolve() for p in paths})
    if not unique:
        raise ValueError("at least one event CSV is required")
    return unique


def load_events(paths: Iterable[Path], *, cutoff_exclusive_ts: int = CUTOFF_EXCLUSIVE_TS) -> pd.DataFrame:
    """Read corrected event files and fail closed outside the sealed history."""
    blocks: list[pd.DataFrame] = []
    for path in paths:
        raw = pd.read_csv(path)
        decision = "detect_ts" if "detect_ts" in raw else "ts" if "ts" in raw else None
        net = "net_ret_bp" if "net_ret_bp" in raw else "net_bp" if "net_bp" in raw else None
        required = [decision, "entry_ts", "exit_ts", net]
        if any(column is None or column not in raw for column in required):
            raise ValueError(f"{path}: require detect_ts/ts, entry_ts, exit_ts, and net_ret_bp/net_bp")
        frame = raw[[decision, "entry_ts", "exit_ts", net]].copy()
        frame.columns = ["detect_ts", "entry_ts", "exit_ts", "source_net_bp"]
        for column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame.isna().any().any() or not np.isfinite(frame.to_numpy(dtype=float)).all():
            raise ValueError(f"{path}: event timestamps and net return must be finite")
        frame = frame.astype({"detect_ts": "int64", "entry_ts": "int64", "exit_ts": "int64"})
        if (frame.entry_ts < frame.detect_ts).any() or (frame.exit_ts <= frame.entry_ts).any():
            raise ValueError(f"{path}: invalid event timing")
        if (frame.exit_ts >= cutoff_exclusive_ts).any():
            raise ValueError(f"{path}: exit_ts must be strictly before 2026-08-01T00:00:00Z")
        frame["symbol"] = _symbol_for(path, raw)
        frame["source_file"] = path.name
        # A basename is useful for display but is not a stable unique source ID:
        # two independently supplied directories may contain the same filename.
        source_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        frame["event_id"] = [f"{source_key}:{i}" for i in range(len(frame))]
        blocks.append(frame)
    return pd.concat(blocks, ignore_index=True) if blocks else pd.DataFrame()


def load_quote_volume(db_path: Path, events: pd.DataFrame) -> dict[str, pd.Series]:
    """Load only the quote-volume span needed for the event executions, read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        output: dict[str, pd.Series] = {}
        for symbol, group in events.groupby("symbol", sort=True):
            start = int(min(group.entry_ts.min(), group.exit_ts.min()) - WINDOW_MINUTES * MINUTE_MS)
            end = int(max(group.entry_ts.max(), group.exit_ts.max()))
            frame = pd.read_sql_query(
                """SELECT ts, quote_volume FROM klines
                   WHERE venue='binance' AND market='perp' AND symbol=? AND ts BETWEEN ? AND ?
                   ORDER BY ts""",
                conn, params=(symbol, start, end),
            )
            output[str(symbol)] = pd.Series(frame.quote_volume.to_numpy(dtype=float), index=frame.ts.astype("int64"))
    finally:
        conn.close()
    return output


def volume_audit(quotes: pd.Series, execution_ts: int) -> dict[str, float | str]:
    """Return a causal 60-bar median and a separate same-bar audit observation."""
    prior_index = np.arange(execution_ts - WINDOW_MINUTES * MINUTE_MS, execution_ts, MINUTE_MS, dtype=np.int64)
    prior = quotes.reindex(prior_index).to_numpy(dtype=float)
    actual = quotes.reindex([execution_ts]).to_numpy(dtype=float)[0]
    valid_prior = len(prior) == WINDOW_MINUTES and np.isfinite(prior).all() and (prior > 0).all()
    historical = float(np.median(prior)) if valid_prior else np.nan
    actual_ok = bool(np.isfinite(actual) and actual > 0)
    return {
        "historical_quote_volume": historical,
        "historical_status": "known" if valid_prior else "unknown_zero_or_missing",
        "actual_quote_volume": float(actual) if np.isfinite(actual) else np.nan,
        "actual_status": "known" if actual_ok else "unknown_zero_or_missing",
    }


def _leg_cost(scenario: Scenario, historical_volume: float) -> tuple[float, float]:
    participation = scenario.notional_usd / historical_volume
    # Square-root impact is deliberately a capacity proxy, not a claim about
    # observed order-book execution.  Coefficient units are bp at Q/V = 1.
    impact = scenario.impact_coefficient_bp * math.sqrt(participation)
    return participation, impact


def evaluate_scenario(events: pd.DataFrame, quotes: dict[str, pd.Series], scenario: Scenario) -> pd.DataFrame:
    """Reprice every input row; skipped entry and failed exit rows stay in the ledger."""
    scenario.validate()
    if events.empty:
        return pd.DataFrame({column: pd.Series(dtype=dtype) for column, dtype in {
            "event_id": "str", "scenario": "str", "execution_status": "str", "hypothetical_priced": "bool",
            "entry_capacity_status": "str", "exit_capacity_status": "str", "scenario_net_bp": "float64",
        }.items()})
    rows: list[dict[str, object]] = []
    for event in events.to_dict("records"):
        entry = volume_audit(quotes[event["symbol"]], int(event["entry_ts"]))
        exit_ = volume_audit(quotes[event["symbol"]], int(event["exit_ts"]))
        row = dict(event, scenario=scenario.name, **{f"entry_{k}": v for k, v in entry.items()},
                   **{f"exit_{k}": v for k, v in exit_.items()})
        row["source_implied_gross_bp"] = float(event["source_net_bp"]) + SOURCE_ROUND_TRIP_BP
        for leg in ("entry", "exit"):
            historical = float(row[f"{leg}_historical_quote_volume"])
            known = row[f"{leg}_historical_status"] == "known"
            if known:
                participation, impact = _leg_cost(scenario, historical)
                row[f"{leg}_historical_participation"] = participation
                row[f"{leg}_fee_bp"] = scenario.fee_bp_one_way
                row[f"{leg}_slippage_bp"] = scenario.slippage_bp_one_way
                row[f"{leg}_impact_bp"] = impact
                row[f"{leg}_cost_bp"] = scenario.fee_bp_one_way + scenario.slippage_bp_one_way + impact
                row[f"{leg}_capacity_status"] = "within_cap" if participation <= scenario.max_participation else "cap_breach"
            else:
                row[f"{leg}_historical_participation"] = np.nan
                row[f"{leg}_fee_bp"] = np.nan
                row[f"{leg}_slippage_bp"] = np.nan
                row[f"{leg}_impact_bp"] = np.nan
                row[f"{leg}_cost_bp"] = np.nan
                row[f"{leg}_capacity_status"] = "unknown_zero_or_missing"
            actual = float(row[f"{leg}_actual_quote_volume"])
            row[f"{leg}_actual_participation"] = scenario.notional_usd / actual if np.isfinite(actual) and actual > 0 else np.nan
        priced = (row["entry_historical_status"] == "known" and row["exit_historical_status"] == "known")
        row["hypothetical_priced"] = priced
        if priced:
            row["scenario_total_cost_bp"] = float(row["entry_cost_bp"] + row["exit_cost_bp"])
            row["scenario_net_bp"] = float(row["source_implied_gross_bp"] - row["scenario_total_cost_bp"])
        entry_ok = row["entry_capacity_status"] == "within_cap"
        exit_ok = row["exit_capacity_status"] == "within_cap"
        if not entry_ok:
            row["execution_status"] = "skipped_entry"
        elif not exit_ok:
            row["execution_status"] = ("exit_capacity_breach" if row["exit_capacity_status"] == "cap_breach"
                                       else "incomplete_exit")
        else:
            row["execution_status"] = "complete"
        if not priced:
            row["scenario_total_cost_bp"] = np.nan
            row["scenario_net_bp"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def summarize(ledger: pd.DataFrame, scenarios: Iterable[Scenario], *, reference_scenario: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Produce complete-only subtotals and explicitly labelled incomplete totals."""
    rows = []
    reference_name = reference_scenario or next(iter(scenarios)).name
    baseline = ledger[ledger.scenario.eq(reference_name)].set_index("event_id")
    if baseline.empty and not ledger.empty:
        raise ValueError(f"reference scenario is absent: {reference_name}")
    complete_all = ledger.groupby("event_id").execution_status.apply(lambda x: bool((x == "complete").all()))
    common_ids = set(complete_all[complete_all].index)
    common_rows = []
    for scenario in scenarios:
        data = ledger[ledger.scenario.eq(scenario.name)]
        completed = data[data.execution_status.eq("complete")]
        priced = data[data.hypothetical_priced]
        entry_skips = int(data.execution_status.eq("skipped_entry").sum())
        exit_incomplete = int(data.execution_status.isin(["exit_capacity_breach", "incomplete_exit"]).sum())
        rows.append({
            "scenario": scenario.name, "events_input": int(len(data)), "events_complete": int(len(completed)),
            "entry_skipped": entry_skips, "exit_incomplete": exit_incomplete,
            "entry_skip_rate": entry_skips / len(data) if len(data) else np.nan,
            "exit_breach_rate": int(data.exit_capacity_status.eq("cap_breach").sum()) / len(data) if len(data) else np.nan,
            "entry_unknown": int(data.entry_capacity_status.eq("unknown_zero_or_missing").sum()),
            "entry_cap_breach": int(data.entry_capacity_status.eq("cap_breach").sum()),
            "exit_unknown": int(data.exit_capacity_status.eq("unknown_zero_or_missing").sum()),
            "exit_cap_breach": int(data.exit_capacity_status.eq("cap_breach").sum()),
            "known_hypothetical_events": int(len(priced)),
            "known_hypothetical_net_sum_bp": float(priced.scenario_net_bp.sum()) if len(priced) else np.nan,
            "known_hypothetical_net_mean_bp": float(priced.scenario_net_bp.mean()) if len(priced) else np.nan,
            "full_hypothetical_net_mean_bp": float(priced.scenario_net_bp.mean()) if len(priced) == len(data) else np.nan,
            "net_result_label": "full_hypothetical_all_targets" if len(priced) == len(data) else "incomplete_overall_net",
            "unpriced_events": int(len(data) - len(priced)),
        })
        common = completed[completed.event_id.isin(common_ids)].set_index("event_id")
        baseline_common = baseline.reindex(common.index)
        common_rows.append({
            "scenario": scenario.name, "common_complete_events": int(len(common)),
            "common_set_net_mean_bp": float(common.scenario_net_bp.mean()) if len(common) else np.nan,
            "common_set_delta_vs_baseline_bp": float((common.scenario_net_bp - baseline_common.scenario_net_bp).mean()) if len(common) else np.nan,
        })
    return pd.DataFrame(rows), pd.DataFrame(common_rows)


def run(event_files: Iterable[str], db_path: Path, output_dir: Path, scenarios: Iterable[Scenario] = DEFAULT_SCENARIOS,
        *, cutoff_exclusive_ts: int = CUTOFF_EXCLUSIVE_TS, reference_scenario: str | None = None) -> dict[str, int]:
    if cutoff_exclusive_ts > CUTOFF_EXCLUSIVE_TS:
        raise ValueError("cutoff cannot be later than 2026-08-01T00:00:00Z")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory must be absent or empty: {output_dir}")
    paths = expand_event_files(event_files)
    scenario_list = validate_scenarios(scenarios)
    events = load_events(paths, cutoff_exclusive_ts=cutoff_exclusive_ts)
    db_path = Path(db_path).resolve()
    before = db_path.stat()
    quotes = load_quote_volume(db_path, events)
    after = db_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("read-only input database changed during analysis")
    ledger = pd.concat([evaluate_scenario(events, quotes, scenario) for scenario in scenario_list], ignore_index=True)
    reference = reference_scenario or scenario_list[0].name
    summary, common = summarize(ledger, scenario_list, reference_scenario=reference)
    source_cost_scenarios = [s for s in scenario_list if (s.fee_bp_one_way, s.slippage_bp_one_way, s.impact_coefficient_bp) == (5.0, 2.0, 0.0)]
    for source_cost in source_cost_scenarios:
        baseline = ledger[(ledger.scenario == source_cost.name) & ledger.hypothetical_priced]
        if len(baseline) and not np.allclose(baseline.scenario_net_bp, baseline.source_net_bp, rtol=0, atol=1e-10):
            raise AssertionError("5bp fee + 2bp slippage baseline must reproduce source net return")
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger.to_csv(output_dir / "execution_ledger.csv", index=False)
    summary.to_csv(output_dir / "scenario_summary.csv", index=False)
    common.to_csv(output_dir / "common_set_deltas.csv", index=False)
    output_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted(output_dir.iterdir()) if path.is_file()}
    volume_hashes = {}
    for symbol, series in quotes.items():
        payload = pd.DataFrame({"ts": series.index.to_numpy(dtype="int64"), "quote_volume": series.to_numpy(dtype=float)})
        volume_hashes[symbol] = hashlib.sha256(payload.to_csv(index=False).encode("utf-8")).hexdigest()
    manifest = {"cutoff_exclusive_ts": cutoff_exclusive_ts, "source_round_trip_cost_bp": SOURCE_ROUND_TRIP_BP,
                "prior_completed_minutes": WINDOW_MINUTES, "event_files": [str(p) for p in paths],
                "event_file_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                "database": {"path": str(db_path), "bytes": before.st_size, "mtime_ns": before.st_mtime_ns,
                             "before": {"bytes": before.st_size, "mtime_ns": before.st_mtime_ns},
                             "after": {"bytes": after.st_size, "mtime_ns": after.st_mtime_ns}},
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "quote_volume_sha256": volume_hashes, "outputs_sha256": output_hashes,
                "reference_scenario": reference, "impact_model": "per-leg coefficient_bp * sqrt(notional_usd / prior_60m_median_quote_volume)",
                "disclaimer": "Hypothetical capacity proxy; not observed order-book impact or fill evidence.",
                "scenarios": [asdict(s) for s in scenario_list], "outputs": ["execution_ledger.csv", "scenario_summary.csv", "common_set_deltas.csv"]}
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"events": int(len(events)), "ledger_rows": int(len(ledger)), "scenarios": int(len(scenario_list))}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Causal execution-capacity sensitivity for corrected BTC/ETH event CSVs.")
    parser.add_argument("--event-files", nargs="+", required=True, help="Explicit event CSV paths and/or shell-style globs (quoted).")
    parser.add_argument("--db", required=True, type=Path, help="Read-only market.db path.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scenario-config", type=Path, help="JSON list of scenario objects.")
    parser.add_argument("--reference-scenario", help="Scenario used for common-set deltas; defaults to the first.")
    parser.add_argument("--cutoff-exclusive-ts", type=int, default=CUTOFF_EXCLUSIVE_TS,
                        help="May tighten but never extend the 2026-08-01 UTC cap.")
    args = parser.parse_args(argv)
    result = run(args.event_files, args.db, args.output_dir, load_scenarios(args.scenario_config),
                 cutoff_exclusive_ts=args.cutoff_exclusive_ts, reference_scenario=args.reference_scenario)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
