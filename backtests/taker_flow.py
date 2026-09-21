#!/usr/bin/env python3
"""Causal hourly Binance spot/perpetual taker-flow feature set.

The source contains one-minute candles.  This program only emits an hour after
all of its 60 constituent candles have closed, uses prior completed hours for
standardisation, and joins OI/funding backward in time.  It is a feature
dataset, not a trading strategy or evidence of profitable predictability.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd


HOUR_MS = 3_600_000
MINUTE_MS = 60_000
ANALYSIS_CUTOFF_MS = int(pd.Timestamp("2026-08-01T00:00:00Z").timestamp() * 1000)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")
DEFAULT_OUT = REPO_ROOT / "results" / "taker_flow"


@dataclass(frozen=True)
class FlowConfig:
    venue: str = "binance"
    symbol: str = "BTCUSDT"
    start_ms: int | None = None
    end_ms: int = ANALYSIS_CUTOFF_MS
    standardize_hours: int = 168
    min_standardize_hours: int = 72
    oi_max_age_hours: int = 12
    funding_max_age_hours: int = 12
    flow_z_threshold: float = 1.0
    imbalance_threshold: float = 0.05
    spot_dominance_gap: float = 0.02
    oi_increase_threshold: float = 0.01
    oi_contracting_threshold: float = -0.01

    def validate(self) -> None:
        if self.venue != "binance" or self.symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("this MVP is scoped to Binance BTCUSDT or ETHUSDT")
        if self.end_ms > ANALYSIS_CUTOFF_MS:
            raise ValueError("analysis end must be on or before 2026-08-01T00:00:00Z")
        if self.start_ms is not None and self.start_ms >= self.end_ms:
            raise ValueError("start must be before end")
        if self.standardize_hours < 2 or not 2 <= self.min_standardize_hours <= self.standardize_hours:
            raise ValueError("standardisation history must be 2..window hours")
        if self.oi_max_age_hours < 0 or self.funding_max_age_hours < 0:
            raise ValueError("maximum ages must be non-negative")
        thresholds = (self.flow_z_threshold, self.imbalance_threshold, self.spot_dominance_gap,
                      self.oi_increase_threshold, self.oi_contracting_threshold)
        if not all(np.isfinite(value) for value in thresholds):
            raise ValueError("all thresholds must be finite")
        if self.flow_z_threshold < 0 or self.imbalance_threshold < 0 or self.spot_dominance_gap < 0:
            raise ValueError("flow thresholds must be non-negative")
        if self.oi_increase_threshold < 0 or self.oi_contracting_threshold > 0:
            raise ValueError("OI increase must be non-negative and contraction non-positive")


def parse_bound(value: str) -> int:
    """Parse an ISO UTC date/time or integer epoch milliseconds."""
    try:
        return int(value)
    except ValueError:
        stamp = pd.Timestamp(value, tz="UTC")
        return int(stamp.timestamp() * 1000)


def connect_read_only(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def load_klines(conn: sqlite3.Connection, cfg: FlowConfig) -> pd.DataFrame:
    where = "venue=? AND market IN ('spot', 'perp') AND symbol=? AND ts<?"
    params: list[object] = [cfg.venue, cfg.symbol, cfg.end_ms]
    if cfg.start_ms is not None:
        where += " AND ts>=?"
        params.append(cfg.start_ms)
    return pd.read_sql_query(
        f"""SELECT market, ts, close, quote_volume, taker_buy_quote FROM klines
            WHERE {where} ORDER BY market, ts""", conn, params=params
    )


def load_asof_source(conn: sqlite3.Connection, table: str, columns: str, cfg: FlowConfig) -> pd.DataFrame:
    if table == "oi_metrics":
        query = f"SELECT ts, {columns} FROM oi_metrics WHERE venue=? AND symbol=? AND ts<? ORDER BY ts"
    elif table == "funding":
        query = f"SELECT ts, {columns} FROM funding WHERE venue=? AND symbol=? AND ts<? ORDER BY ts"
    else:
        raise ValueError(f"unsupported source {table}")
    return pd.read_sql_query(query, conn, params=(cfg.venue, cfg.symbol, cfg.end_ms))


def aggregate_hourly(klines: pd.DataFrame) -> pd.DataFrame:
    """Aggregate only complete 60-minute windows; no partial-hour estimates."""
    frame = klines.copy().sort_values(["market", "ts"])
    if frame.empty:
        return pd.DataFrame(columns=["market", "ts", "bar_count", "close", "quote_volume", "taker_buy_quote"])
    frame["hour_ts"] = (frame["ts"].astype("int64") // HOUR_MS) * HOUR_MS
    valid_flow = (np.isfinite(frame["quote_volume"]) & np.isfinite(frame["taker_buy_quote"])
                  & frame["quote_volume"].ge(0) & frame["taker_buy_quote"].ge(0)
                  & frame["taker_buy_quote"].le(frame["quote_volume"]))
    frame["valid_flow"] = valid_flow
    frame["minute_aligned"] = frame["ts"].mod(MINUTE_MS).eq(0)
    frame["valid_quote"] = frame["quote_volume"].where(valid_flow)
    frame["valid_taker"] = frame["taker_buy_quote"].where(valid_flow)
    out = (frame.groupby(["market", "hour_ts"], as_index=False)
           .agg(bar_count=("ts", "size"), unique_minute_count=("ts", "nunique"), aligned_minute_count=("minute_aligned", "sum"), min_ts=("ts", "min"), max_ts=("ts", "max"),
                valid_flow_count=("valid_flow", "sum"), quote_volume=("valid_quote", "sum"),
                taker_buy_quote=("valid_taker", "sum"))
           .rename(columns={"hour_ts": "ts"}))
    close_at_end = frame.groupby(["market", "hour_ts"], as_index=False).tail(1)[["market", "hour_ts", "close"]]
    close_at_end = close_at_end.rename(columns={"hour_ts": "ts"})
    out = out.merge(close_at_end, on=["market", "ts"], how="left")
    expected_start = out["ts"]
    complete = ((out["bar_count"] == 60) & (out["unique_minute_count"] == 60)
                & (out["aligned_minute_count"] == 60)
                & (out["min_ts"] == expected_start) & (out["max_ts"] == expected_start + 59 * MINUTE_MS))
    out = out.loc[complete].copy()
    incomplete_values = out["valid_flow_count"] != 60
    out.loc[incomplete_values, ["quote_volume", "taker_buy_quote"]] = np.nan
    # A one-hour feature is decided after its final 1m candle has completed.
    # Thus `ts` is the decision timestamp (hour end), never the first candle's open.
    out["ts"] += HOUR_MS
    return out


def raw_input_coverage(klines: pd.DataFrame, cfg: FlowConfig, features: pd.DataFrame) -> dict[str, object]:
    """Audit source-hour eligibility, including hours dropped before feature creation."""
    if klines.empty:
        if cfg.start_ms is None:
            missing = 0
        else:
            first = cfg.start_ms // HOUR_MS * HOUR_MS
            missing = sum(1 for hour in range(first, cfg.end_ms, HOUR_MS) if hour + HOUR_MS < cfg.end_ms)
        return {"raw_min_ts": None, "raw_max_ts": None, "common_valid_start_utc": None,
                "common_valid_end_utc": None, "common_valid_hours": 0, "one_sided_valid_hours": 0,
                "both_incomplete_or_invalid_hours": 0, "all_missing_hours": missing}
    raw = klines.copy()
    raw["hour"] = raw["ts"].astype("int64") // HOUR_MS * HOUR_MS
    raw["valid"] = (raw["ts"].mod(MINUTE_MS).eq(0) & np.isfinite(raw["quote_volume"])
                    & np.isfinite(raw["taker_buy_quote"]) & raw["quote_volume"].ge(0)
                    & raw["taker_buy_quote"].ge(0) & raw["taker_buy_quote"].le(raw["quote_volume"]))
    grouped = raw.groupby(["market", "hour"], as_index=False).agg(
        count=("ts", "size"), unique=("ts", "nunique"), aligned=("valid", "size"), valid=("valid", "sum"),
        min_ts=("ts", "min"), max_ts=("ts", "max"),
    )
    grouped["complete"] = ((grouped["count"] == 60) & (grouped["unique"] == 60)
                           & (grouped["valid"] == 60) & (grouped["min_ts"] == grouped["hour"])
                           & (grouped["max_ts"] == grouped["hour"] + 59 * MINUTE_MS))
    grouped = grouped.loc[grouped["hour"] + HOUR_MS < cfg.end_ms].copy()
    pivot = grouped.pivot(index="hour", columns="market", values="complete").fillna(False)
    spot = pivot.get("spot", pd.Series(False, index=pivot.index)).astype(bool)
    perp = pivot.get("perp", pd.Series(False, index=pivot.index)).astype(bool)
    observed = set(pivot.index.astype(int))
    start = cfg.start_ms if cfg.start_ms is not None else int(raw["hour"].min())
    first_hour = start // HOUR_MS * HOUR_MS
    expected = set(range(first_hour, cfg.end_ms, HOUR_MS))
    # Hours begin at `hour`; a valid decision at hour+1h must be strictly in range.
    expected = {hour for hour in expected if hour + HOUR_MS < cfg.end_ms}
    common = spot & perp
    # Do not derive common bounds from the outer feature frame: that can contain
    # a spot-only hour followed by a perp-only hour with no shared valid hour.
    common_decisions = pd.Series(pivot.index[common].astype("int64") + HOUR_MS)
    return {
        "raw_min_ts": int(raw["ts"].min()), "raw_max_ts": int(raw["ts"].max()),
        "common_valid_start_utc": None if common_decisions.empty else pd.to_datetime(common_decisions.min(), unit="ms", utc=True).isoformat(),
        "common_valid_end_utc": None if common_decisions.empty else pd.to_datetime(common_decisions.max(), unit="ms", utc=True).isoformat(),
        "common_valid_hours": int(common.sum()), "one_sided_valid_hours": int((spot ^ perp).sum()),
        "both_incomplete_or_invalid_hours": int((~spot & ~perp).sum()),
        "all_missing_hours": int(len(expected - observed)),
    }


def _market_features(hourly: pd.DataFrame, market: str) -> pd.DataFrame:
    part = hourly.loc[hourly["market"] == market, ["ts", "bar_count", "close", "quote_volume", "taker_buy_quote"]].copy()
    part = part.rename(columns={
        "bar_count": f"{market}_bar_count", "close": f"{market}_close", "quote_volume": f"{market}_quote_volume",
        "taker_buy_quote": f"{market}_taker_buy_quote",
    })
    volume = part[f"{market}_quote_volume"]
    part[f"{market}_taker_net_quote"] = np.where(
        volume > 0, 2.0 * part[f"{market}_taker_buy_quote"] - volume, np.nan
    )
    # Zero volume is not directional information.  Keep the raw zero and mark its signal unavailable.
    part[f"{market}_taker_imbalance"] = np.where(volume > 0, part[f"{market}_taker_net_quote"] / volume, np.nan)
    return part


def causal_zscore(series: pd.Series, window: int, min_history: int, ts: pd.Series | None = None) -> pd.Series:
    """Z-score against prior *completed* observations only (the current row is excluded)."""
    if ts is None:
        prior = series.shift(1)
        mean = prior.rolling(window=window, min_periods=min_history).mean()
        std = prior.rolling(window=window, min_periods=min_history).std(ddof=0)
        return (series - mean) / std.where(std > 0)
    out = pd.Series(np.nan, index=series.index, dtype=float)
    starts = ts.diff().ne(HOUR_MS).cumsum()
    for _, segment in series.groupby(starts):
        prior = segment.shift(1)
        mean = prior.rolling(window=window, min_periods=min_history).mean()
        std = prior.rolling(window=window, min_periods=min_history).std(ddof=0)
        out.loc[segment.index] = (segment - mean) / std.where(std > 0)
    return out


def _asof_join(frame: pd.DataFrame, source: pd.DataFrame, value_columns: list[str], prefix: str,
               max_age_hours: int) -> pd.DataFrame:
    out = frame.sort_values("ts").copy()
    renamed = source.rename(columns={"ts": f"{prefix}_source_ts", **{c: f"{prefix}_{c}" for c in value_columns}})
    if renamed.empty:
        for col in [f"{prefix}_source_ts", *[f"{prefix}_{c}" for c in value_columns]]:
            out[col] = np.nan
    else:
        out = pd.merge_asof(out, renamed.sort_values(f"{prefix}_source_ts"), left_on="ts",
                            right_on=f"{prefix}_source_ts", direction="backward")
    out[f"{prefix}_age_hours"] = (out["ts"] - out[f"{prefix}_source_ts"]) / HOUR_MS
    any_missing = out[f"{prefix}_source_ts"].isna()
    stale = ~any_missing & (out[f"{prefix}_age_hours"] > max_age_hours)
    out[f"{prefix}_asof_state"] = np.select([any_missing, stale], ["missing", "stale"], default="available")
    return out


def build_features(klines: pd.DataFrame, oi: pd.DataFrame, funding: pd.DataFrame, cfg: FlowConfig) -> pd.DataFrame:
    cfg.validate()
    hourly = aggregate_hourly(klines)
    spot, perp = _market_features(hourly, "spot"), _market_features(hourly, "perp")
    frame = spot.merge(perp, on="ts", how="outer").sort_values("ts").reset_index(drop=True)
    # `end_ms` is exclusive for decision timestamps too. The final source hour can
    # end exactly at the research cutoff, but is not an in-bound decision.
    frame = frame.loc[frame["ts"] < cfg.end_ms].copy()
    frame["decision_ts"] = frame["ts"]
    for market in ("spot", "perp"):
        volume = frame[f"{market}_quote_volume"]
        state = np.select([volume.isna(), volume.eq(0)], ["unclassified_missing", "unclassified_zero_volume"], default="classified")
        frame[f"{market}_flow_state"] = state
        frame[f"{market}_taker_imbalance_z"] = causal_zscore(
            frame[f"{market}_taker_imbalance"], cfg.standardize_hours, cfg.min_standardize_hours, frame["ts"]
        )
        frame[f"{market}_close"] = pd.to_numeric(frame[f"{market}_close"], errors="coerce")
        valid_close = np.isfinite(frame[f"{market}_close"]) & frame[f"{market}_close"].gt(0)
        frame[f"{market}_price_state"] = np.where(valid_close, "available", "unclassified_invalid_or_missing")
        prior_close = frame[f"{market}_close"].shift(1)
        frame[f"{market}_price_change_1h"] = np.where(
            frame["ts"].diff().eq(HOUR_MS) & valid_close & np.isfinite(prior_close) & prior_close.gt(0), frame[f"{market}_close"] / prior_close - 1.0, np.nan
        )
        frame[f"{market}_zscore_state"] = np.select(
            [frame[f"{market}_flow_state"].ne("classified"), frame[f"{market}_taker_imbalance_z"].isna()],
            [frame[f"{market}_flow_state"], "unclassified_insufficient_history"], default="classified",
        )
    both_classified = (frame["spot_flow_state"] == "classified") & (frame["perp_flow_state"] == "classified")
    frame["flow_divergence"] = np.where(
        both_classified, frame["perp_taker_imbalance"] - frame["spot_taker_imbalance"], np.nan
    )
    frame["flow_divergence_z"] = causal_zscore(frame["flow_divergence"], cfg.standardize_hours, cfg.min_standardize_hours, frame["ts"])
    valid_basis_prices = (frame["spot_price_state"].eq("available") & frame["perp_price_state"].eq("available"))
    frame["basis"] = np.where(valid_basis_prices, frame["perp_close"] / frame["spot_close"] - 1.0, np.nan)
    frame["basis_state"] = np.where(valid_basis_prices, "classified", "unclassified_invalid_or_missing_price")
    frame["flow_divergence_z_state"] = np.select(
        [~both_classified, frame["flow_divergence_z"].isna()],
        ["unclassified_flow", "unclassified_insufficient_history"], default="classified",
    )
    frame = _asof_join(frame, oi, ["open_interest", "oi_value"], "oi", cfg.oi_max_age_hours)
    # Quantity and dollar-value availability remain separate: neither may substitute for the other.
    for col in ("open_interest", "oi_value"):
        value = pd.to_numeric(frame[f"oi_{col}"], errors="coerce")
        frame[f"oi_{col}"] = value
        frame[f"oi_{col}_state"] = np.select(
            [frame["oi_asof_state"].eq("missing"), frame["oi_asof_state"].eq("stale"), value.isna(), value.eq(0), ~np.isfinite(value) | value.lt(0)],
            ["missing", "stale", "missing_value", "unclassified_zero", "unclassified_invalid"], default="available",
        )
        previous_value = frame[f"oi_{col}"].shift(1)
        previous_state = frame[f"oi_{col}_state"].shift(1)
        frame[f"oi_{col}_change_1h"] = np.where(
            frame["ts"].diff().eq(HOUR_MS) & frame[f"oi_{col}_state"].eq("available") & previous_state.eq("available") & previous_value.gt(0),
            frame[f"oi_{col}"] / previous_value - 1.0, np.nan,
        )
    frame = _asof_join(frame, funding, ["rate", "interval_hours"], "funding", cfg.funding_max_age_hours)
    frame["funding_rate_state"] = np.select(
        [frame["funding_asof_state"].eq("missing"), frame["funding_asof_state"].eq("stale"), frame["funding_rate"].isna()],
        ["missing", "stale", "missing_value"], default="available",
    )
    required = (frame["spot_zscore_state"].eq("classified") & frame["perp_zscore_state"].eq("classified")
                & frame["oi_open_interest_state"].eq("available") & frame["oi_open_interest_change_1h"].notna())
    spot_buy = (frame["spot_taker_imbalance"].ge(cfg.imbalance_threshold)
                & frame["spot_taker_imbalance_z"].ge(cfg.flow_z_threshold)
                & (frame["spot_taker_imbalance"] - frame["perp_taker_imbalance"]).ge(cfg.spot_dominance_gap))
    perp_buy_oi = (frame["perp_taker_imbalance"].ge(cfg.imbalance_threshold)
                   & frame["perp_taker_imbalance_z"].ge(cfg.flow_z_threshold)
                   & frame["oi_open_interest_change_1h"].ge(cfg.oi_increase_threshold))
    oi_contracting = frame["oi_open_interest_change_1h"].le(cfg.oi_contracting_threshold)
    # The order makes contraction explicit even if flow happens to be buy-dominant.
    frame["flow_oi_state"] = np.select(
        [~required, oi_contracting, perp_buy_oi, spot_buy],
        ["unclassified", "oi_contracting", "perp_buy_oi_increase_consistent", "spot_buy_dominant_consistent"],
        default="mixed",
    )
    return frame


def _state_summary(series: pd.Series, ts: pd.Series) -> dict[str, dict[str, int]]:
    """Count state-hours and longest contiguous state duration, preserving operational gaps."""
    result: dict[str, dict[str, int]] = {}
    for state, count in series.value_counts(dropna=False).items():
        label = str(state)
        boundaries = series.ne(series.shift()) | ts.diff().ne(HOUR_MS)
        run_lengths = series.eq(state).groupby(boundaries.cumsum()).sum()
        result[label] = {"count": int(count), "hours": int(count), "max_contiguous_hours": int(run_lengths.max())}
    return result


def coverage(frame: pd.DataFrame, raw_coverage: dict[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"hour_rows": int(len(frame))}
    result["hour_start_utc"] = None if frame.empty else pd.to_datetime(frame.ts.min(), unit="ms", utc=True).isoformat()
    result["hour_end_utc"] = None if frame.empty else pd.to_datetime(frame.ts.max(), unit="ms", utc=True).isoformat()
    for col in ("spot_flow_state", "perp_flow_state", "spot_price_state", "perp_price_state", "spot_zscore_state", "perp_zscore_state",
                "flow_divergence_z_state", "oi_open_interest_state", "oi_oi_value_state", "funding_rate_state", "flow_oi_state"):
        result[col] = _state_summary(frame[col], frame["ts"])
    if raw_coverage is not None:
        result["raw_input"] = raw_coverage
    return result


def run(db_path: Path, output_dir: Path, cfg: FlowConfig) -> pd.DataFrame:
    cfg.validate()
    with connect_read_only(db_path) as conn:
        klines = load_klines(conn, cfg)
        frame = build_features(klines, load_asof_source(conn, "oi_metrics", "open_interest, oi_value", cfg),
                               load_asof_source(conn, "funding", "rate, interval_hours", cfg), cfg)
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / name for name in ("features.csv", "state_summary.csv", "run_manifest.json")]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing output: " + ", ".join(existing))
    frame.to_csv(output_dir / "features.csv", index=False)
    state_rows = []
    for state, values in _state_summary(frame["flow_oi_state"], frame["ts"]).items():
        state_rows.append({"state": state, **values})
    pd.DataFrame(state_rows, columns=["state", "count", "hours", "max_contiguous_hours"]).to_csv(output_dir / "state_summary.csv", index=False)
    manifest = {
        "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "settings": asdict(cfg), "analysis_cutoff_exclusive_utc": "2026-08-01T00:00:00Z",
        "coverage": coverage(frame, raw_input_coverage(klines, cfg, frame)),
        "disclaimers": [
            "Taker flow is inferred from candle taker-buy quote volume, not an order-book or trade-level audit; signed quote flow is not money entering or leaving the market.",
            "Missing, partial-hour, and zero-volume flow is unclassified; it is never treated as neutral or directional.",
            "OI quantity and OI value are separate fields, and neither measures directional capital flow. Each source is backward-as-of joined at the completed-hour end and its age/state is retained.",
            "OI and price alone cannot identify new longs, new shorts, or short covering.",
            "This is descriptive research data, not a trading recommendation or evidence of causality or profitability.",
        ],
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return frame


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--venue", default="binance")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", type=parse_bound)
    parser.add_argument("--end", type=parse_bound, default=ANALYSIS_CUTOFF_MS, help="exclusive; no later than 2026-08-01 UTC")
    parser.add_argument("--standardize-hours", type=int, default=168)
    parser.add_argument("--min-standardize-hours", type=int, default=72)
    parser.add_argument("--oi-max-age-hours", type=int, default=12)
    parser.add_argument("--funding-max-age-hours", type=int, default=12)
    parser.add_argument("--flow-z-threshold", type=float, default=1.0)
    parser.add_argument("--imbalance-threshold", type=float, default=0.05)
    parser.add_argument("--spot-dominance-gap", type=float, default=0.02)
    parser.add_argument("--oi-increase-threshold", type=float, default=0.01)
    parser.add_argument("--oi-contracting-threshold", type=float, default=-0.01)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = FlowConfig(venue=args.venue, symbol=args.symbol, start_ms=args.start, end_ms=args.end,
                     standardize_hours=args.standardize_hours, min_standardize_hours=args.min_standardize_hours,
                     oi_max_age_hours=args.oi_max_age_hours, funding_max_age_hours=args.funding_max_age_hours,
                     flow_z_threshold=args.flow_z_threshold, imbalance_threshold=args.imbalance_threshold,
                     spot_dominance_gap=args.spot_dominance_gap,
                     oi_increase_threshold=args.oi_increase_threshold, oi_contracting_threshold=args.oi_contracting_threshold)
    frame = run(args.db, args.output_dir, cfg)
    print(f"wrote {len(frame)} hourly rows to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
