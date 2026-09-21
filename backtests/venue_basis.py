#!/usr/bin/env python3
"""Exploratory Binance/Bybit basis diagnostics; never an executable arbitrage model."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MINUTE = 60_000
HOUR = 60 * MINUTE
CUTOFF = 1785542400000  # 2026-08-01T00:00:00Z
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    threshold_bp: float = 10.0
    horizons_hours: tuple[int, ...] = (1, 4)
    max_wait_minutes: int = 2
    quantity: float = 1.0
    binance_fee_bp: float = 5.0
    bybit_fee_bp: float = 5.5
    slippage_bp: float = 2.0
    funding_interval_hours: int = 8
    funding_timestamp_tolerance_ms: int = 1_000

    def validate(self) -> None:
        positive = (self.threshold_bp, self.quantity)
        nonnegative = (self.binance_fee_bp, self.bybit_fee_bp, self.slippage_bp)
        if not all(np.isfinite(x) and x > 0 for x in positive):
            raise ValueError("threshold and quantity must be finite and positive")
        if not all(np.isfinite(x) and x >= 0 for x in nonnegative):
            raise ValueError("costs must be finite and nonnegative")
        if (not self.horizons_hours or any(not isinstance(x, int) or x <= 0 for x in self.horizons_hours)
                or self.max_wait_minutes < 1 or self.funding_interval_hours not in (1, 2, 4, 8)
                or not 0 <= self.funding_timestamp_tolerance_ms < MINUTE):
            raise ValueError("invalid horizon, wait tolerance, or funding schedule")


def prices(raw: pd.DataFrame) -> pd.DataFrame:
    frame = raw[["ts", "open", "close"]].copy()
    if frame.ts.duplicated().any() or frame.ts.isna().any() or (frame.ts % MINUTE != 0).any():
        raise ValueError("price timestamps must be unique UTC minute opens")
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.loc[~np.isfinite(frame[column]) | frame[column].le(0), column] = np.nan
    return frame.set_index("ts").sort_index()


def build_panel(binance: pd.DataFrame, bybit: pd.DataFrame, spot: pd.DataFrame,
                config: Config, end_ts: int = CUTOFF) -> tuple[pd.DataFrame, dict]:
    """Only synchronized, completed closes enter the signal; no forward fill."""
    config.validate()
    if end_ts > CUTOFF:
        raise ValueError("analysis must end no later than 2026-08-01 UTC")
    b, y, s = prices(binance), prices(bybit), prices(spot)
    joint = b.add_suffix("_binance").join(y.add_suffix("_bybit"), how="outer")
    complete = joint.index + MINUTE <= end_ts
    valid = joint.close_binance.notna() & joint.close_bybit.notna() & complete
    panel = joint.loc[valid].copy()
    panel["spot_close"] = s.close.reindex(panel.index)
    panel["signal_ts"] = panel.index + MINUTE
    panel["basis_bp"] = 10_000 * (panel.close_bybit / panel.close_binance - 1)
    panel["binance_spot_basis_bp"] = 10_000 * (panel.close_binance / panel.spot_close - 1)
    panel["extreme"] = panel.basis_bp.abs() >= config.threshold_bp
    contiguous = panel.index.to_series().diff().eq(MINUTE)
    same_side = np.sign(panel.basis_bp).eq(np.sign(panel.basis_bp.shift()))
    panel["episode_start"] = panel.extreme & ~(panel.extreme.shift(fill_value=False) & contiguous & same_side)
    # Outcomes are descriptive only; they never enter episode selection.
    for h in config.horizons_hours:
        future = panel.basis_bp.reindex(panel.index + h * HOUR).to_numpy()
        panel[f"basis_change_{h}h_bp"] = future - panel.basis_bp.to_numpy()
    coverage = {
        "binance_rows": len(b), "bybit_rows": len(y), "union_rows": len(joint),
        "common_completed_valid_closes": len(panel),
        "missing_or_invalid_binance_close": int(joint.close_binance.isna().sum()),
        "missing_or_invalid_bybit_close": int(joint.close_bybit.isna().sum()),
        "excluded_incomplete_closes": int((~complete).sum()),
        "missing_spot_reference": int(panel.spot_close.isna().sum()),
        "first_signal_ts": int(panel.signal_ts.min()) if len(panel) else None,
        "last_signal_ts": int(panel.signal_ts.max()) if len(panel) else None,
    }
    return panel.reset_index(), coverage


def funding_leg(funding: pd.DataFrame, venue_prices: pd.DataFrame, entry: int, exit_: int,
                direction: int, quantity: float, interval_hours: int,
                timestamp_tolerance_ms: int = 1_000) -> tuple[float, str, int]:
    """Require observed schedule coverage; holdings use [entry, exit) settlements.

    Price at each settlement is the venue's same-minute open, an explicit mark
    approximation. Unexpected/missing/changed schedules are unknown, not zero.
    """
    interval = interval_hours * HOUR
    first = (entry // interval) * interval
    last = ((exit_ + interval - 1) // interval) * interval
    expected_coverage = np.arange(first, last + 1, interval, dtype=np.int64)
    f = funding.set_index("ts").sort_index() if "ts" in funding.columns else funding
    if f.index.duplicated().any():
        return np.nan, "duplicate_funding", 0
    # API timestamps may differ a few milliseconds from the UTC schedule.
    # This matching checks coverage only; cash flows retain their actual times.
    scheduled = ((f.index.to_numpy() + interval // 2) // interval) * interval
    close_to_schedule = np.abs(f.index.to_numpy() - scheduled) <= timestamp_tolerance_ms
    eligible = f.loc[close_to_schedule].copy()
    eligible.index = scheduled[close_to_schedule]
    if eligible.index.duplicated().any():
        return np.nan, "duplicate_funding_schedule", 0
    observed = eligible.reindex(expected_coverage)
    if (observed[["rate", "interval_hours"]].isna().any().any()
            or not np.isfinite(observed.rate).all()
            or not observed.interval_hours.eq(interval_hours).all()):
        return np.nan, "funding_schedule_missing_or_changed", 0
    held = f[(f.index >= entry) & (f.index < exit_)]
    held_schedule = ((held.index.to_numpy() + interval // 2) // interval) * interval
    if (np.abs(held.index.to_numpy() - held_schedule) > timestamp_tolerance_ms).any():
        return np.nan, "off_schedule_funding", len(held)
    if held.empty:
        return 0.0, "complete_no_settlement_due", 0
    px = venue_prices.open.reindex((held.index.to_numpy() // MINUTE) * MINUTE)
    if px.isna().any() or not np.isfinite(px).all() or px.le(0).any():
        return np.nan, "settlement_price_missing", len(held)
    return float((-direction * quantity * px.to_numpy() * held.rate.to_numpy()).sum()), "complete_open_price_proxy", len(held)


def event_ledger(panel: pd.DataFrame, binance: pd.DataFrame, bybit: pd.DataFrame,
                 funding_binance: pd.DataFrame, funding_bybit: pd.DataFrame,
                 config: Config, end_ts: int = CUTOFF) -> pd.DataFrame:
    config.validate()
    if end_ts > CUTOFF:
        raise ValueError("historical boundary exceeded")
    b, y = prices(binance), prices(bybit)
    # Entry selection depends on opens only, never that minute's future close.
    opens = b[["open"]].rename(columns={"open": "binance"}).join(
        y[["open"]].rename(columns={"open": "bybit"}), how="inner").dropna()
    opens = opens[opens.index < end_ts]
    timestamps = opens.index.to_numpy()
    funding_binance = funding_binance.set_index("ts").sort_index()
    funding_bybit = funding_bybit.set_index("ts").sort_index()
    rows = []
    for event in panel.loc[panel.episode_start].itertuples(index=False):
        for h in config.horizons_hours:
            row = {"signal_ts": int(event.signal_ts), "signal_basis_bp": event.basis_bp,
                   "horizon_hours": h, "quantity": config.quantity, "status": "pending"}
            i = int(np.searchsorted(timestamps, event.signal_ts, side="right"))
            if i == len(timestamps) or timestamps[i] - event.signal_ts > config.max_wait_minutes * MINUTE:
                row["status"] = "entry_missing_or_wait_exceeded"
                rows.append(row)
                continue
            entry = int(timestamps[i])
            target = entry + h * HOUR
            row.update(entry_ts=entry, entry_wait_ms=entry - int(event.signal_ts), target_exit_ts=target)
            j = int(np.searchsorted(timestamps, target, side="left"))
            if j == len(timestamps) or timestamps[j] - target > config.max_wait_minutes * MINUTE:
                row["status"] = "exit_missing_or_wait_exceeded"
                rows.append(row)
                continue
            exit_ = int(timestamps[j])
            row.update(exit_ts=exit_, exit_wait_ms=exit_ - target)
            q = config.quantity
            db = 1 if event.basis_bp > 0 else -1
            denominator = q * (opens.iloc[i].binance + opens.iloc[i].bybit)
            row["entry_gross_notional"] = denominator
            for venue, direction, pf, ff, fee in (
                ("binance", db, b, funding_binance, config.binance_fee_bp),
                ("bybit", -db, y, funding_bybit, config.bybit_fee_bp),
            ):
                entry_px, exit_px = float(opens.iloc[i][venue]), float(opens.iloc[j][venue])
                funding, funding_state, count = funding_leg(ff, pf, entry, exit_, direction, q,
                    config.funding_interval_hours, config.funding_timestamp_tolerance_ms)
                held_times = ff.index[(ff.index >= entry) & (ff.index < exit_)].tolist()
                row.update({f"{venue}_direction": direction, f"{venue}_entry_px": entry_px,
                            f"{venue}_exit_px": exit_px,
                            f"{venue}_price_pnl": direction * q * (exit_px - entry_px),
                            f"{venue}_fee": q * (entry_px + exit_px) * fee / 10_000,
                            f"{venue}_slippage": q * (entry_px + exit_px) * config.slippage_bp / 10_000,
                            f"{venue}_funding_pnl": funding, f"{venue}_funding_status": funding_state,
                            f"{venue}_funding_count": count,
                            f"{venue}_funding_timestamps": json.dumps(held_times)})
            row["price_pnl"] = row["binance_price_pnl"] + row["bybit_price_pnl"]
            row["cost"] = sum(row[f"{v}_{c}"] for v in ("binance", "bybit") for c in ("fee", "slippage"))
            row["funding_pnl"] = row["binance_funding_pnl"] + row["bybit_funding_pnl"]
            row["net_pnl"] = row["price_pnl"] - row["cost"] + row["funding_pnl"]
            row["net_ret_on_gross_notional_bp"] = 10_000 * row["net_pnl"] / denominator
            row["status"] = "complete" if np.isfinite(row["funding_pnl"]) else "funding_unknown"
            rows.append(row)
    columns = ["signal_ts", "signal_basis_bp", "horizon_hours", "quantity", "status", "entry_ts",
               "entry_wait_ms", "target_exit_ts", "exit_ts", "exit_wait_ms", "entry_gross_notional"]
    for v in ("binance", "bybit"):
        columns.extend(f"{v}_{c}" for c in ("direction", "entry_px", "exit_px", "price_pnl", "fee",
                                          "slippage", "funding_pnl", "funding_status", "funding_count", "funding_timestamps"))
    columns.extend(("price_pnl", "cost", "funding_pnl", "net_pnl", "net_ret_on_gross_notional_bp"))
    return pd.DataFrame(rows, columns=columns)


def summarize(panel: pd.DataFrame, ledger: pd.DataFrame) -> dict:
    durations = []
    run = 0
    for row in panel.itertuples(index=False):
        if row.episode_start or not row.extreme:
            if run:
                durations.append(run)
            run = 0
        if row.extreme:
            run += 1
    if run:
        durations.append(run)
    quantiles = lambda x: {str(p): float(x.quantile(p)) if len(x.dropna()) else None for p in (.05, .5, .95)}
    summaries = []
    for horizon, group in ledger.groupby("horizon_hours"):
        complete = group[group.status.eq("complete")]
        summaries.append({"horizon_hours": int(horizon), "events": len(group),
                          "status_counts": group.status.value_counts().to_dict(),
                          "complete_events": len(complete),
                          "known_price_pnl_subtotal": float(group.price_pnl.sum()),
                          "known_cost_subtotal": float(group.cost.sum()),
                          "known_funding_pnl_subtotal": float(group.funding_pnl.sum()),
                          "known_price_pnl_events": int(group.price_pnl.notna().sum()),
                          "known_cost_events": int(group.cost.notna().sum()),
                          "known_funding_pnl_events": int(group.funding_pnl.notna().sum()),
                          "complete_price_pnl_subtotal": float(complete.price_pnl.sum()),
                          "complete_cost_subtotal": float(complete.cost.sum()),
                          "complete_funding_pnl_subtotal": float(complete.funding_pnl.sum()),
                          "complete_net_bp_quantiles": quantiles(complete.net_ret_on_gross_notional_bp),
                          "interpretation": "overlapping diagnostic events, not portfolio returns"})
    result = {"basis_bp_quantiles": quantiles(panel.basis_bp),
            "extreme_observed_minutes_quantiles": quantiles(pd.Series(durations, dtype=float)),
            "episodes": len(durations), "persistence_note": "observed runs; gaps and opposite signs split runs; edge runs may be censored",
            "events": summaries}
    for column in panel.columns:
        if column.startswith("basis_change_"):
            result[f"{column}_quantiles"] = quantiles(panel[column])
    return result


def load_prices(conn: sqlite3.Connection, venue: str, market: str, symbol: str,
                start: int, end: int) -> pd.DataFrame:
    return pd.read_sql_query("SELECT ts,open,close FROM klines WHERE venue=? AND market=? AND symbol=? "
                             "AND ts>=? AND ts<? ORDER BY ts", conn,
                             params=(venue, market, symbol, start, end))


def run(db: Path, output: Path, symbols: list[str], start: int, end: int, config: Config) -> dict:
    config.validate()
    if start >= end or end > CUTOFF or start % MINUTE or end % MINUTE:
        raise ValueError("require minute-aligned start < end <= 2026-08-01 UTC")
    if not symbols or any(s not in ("BTCUSDT", "ETHUSDT") for s in symbols):
        raise ValueError("initial study supports BTCUSDT and ETHUSDT only")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("output directory must be new or empty")
    db = db.resolve()
    before = db.stat()
    panels, ledgers, coverage, summary = [], [], {}, {}
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        for symbol in symbols:
            b = load_prices(conn, "binance", "perp", symbol, start, end)
            y = load_prices(conn, "bybit", "perp", symbol, start, end)
            s = load_prices(conn, "binance", "spot", symbol, start, end)
            fs = {}
            for venue in ("binance", "bybit"):
                fs[venue] = pd.read_sql_query("SELECT ts,rate,interval_hours FROM funding "
                    "WHERE venue=? AND symbol=? AND ts>=? AND ts<? ORDER BY ts", conn,
                    params=(venue, symbol, start - 8 * HOUR, min(end + 8 * HOUR, CUTOFF)))
            panel, coverage[symbol] = build_panel(b, y, s, config, end)
            ledger = event_ledger(panel, b, y, fs["binance"], fs["bybit"], config, end)
            summary[symbol] = summarize(panel, ledger)
            panel.insert(0, "symbol", symbol)
            ledger.insert(0, "symbol", symbol)
            panels.append(panel)
            ledgers.append(ledger)
    after = db.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("input database changed during analysis")
    manifest = {"config": asdict(config), "start_ts": start, "end_ts_exclusive": end,
                "historical_cutoff": CUTOFF, "coverage": coverage,
                "database": {"path": str(db), "bytes": before.st_size, "mtime_ns": before.st_mtime_ns},
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "interpretation": "Exploratory diagnostic, not executable arbitrage or confirmation of sealed hypotheses.",
                "signal": "exact synchronous completed minute closes; fixed threshold; no funding signal",
                "execution": "first common open strictly after signal; exit first common open at/after target",
                "funding": "[entry, exit) using actual stored settlement times; UTC schedule coverage tolerates configured millisecond jitter; missing is unknown; each venue settlement-minute open approximates mark",
                "costs": "explicit scenario assumptions, not calibrated fees/impact; no bid/ask or execution guarantee",
                "basis_denominator": "Q * (entry_px_binance + entry_px_bybit), gross notional not margin",
                "limitations": ["funding schedule changes or timestamp offsets beyond tolerance fail closed",
                    "no venue-price substitution, no liquidity/credit/transfer-risk model",
                    "episode outcomes overlap; do not sum as portfolio PnL",
                    "basis forward changes are outcomes and never used as signal inputs"]}
    output.mkdir(parents=True, exist_ok=True)
    pd.concat(panels, ignore_index=True).to_csv(output / "basis_panel.csv", index=False)
    pd.concat(ledgers, ignore_index=True).to_csv(output / "two_leg_events.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    manifest["outputs_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in sorted(output.iterdir()) if p.is_file()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/mnt/e/Datas/market/market.db"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results/venue_basis")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--start", default="2024-09-01")
    parser.add_argument("--end", default="2026-08-01")
    parser.add_argument("--threshold-bp", type=float, default=10)
    parser.add_argument("--max-wait-minutes", type=int, default=2)
    parser.add_argument("--quantity", type=float, default=1)
    parser.add_argument("--binance-fee-bp", type=float, default=5)
    parser.add_argument("--bybit-fee-bp", type=float, default=5.5)
    parser.add_argument("--slippage-bp", type=float, default=2)
    args = parser.parse_args()
    config = Config(threshold_bp=args.threshold_bp, max_wait_minutes=args.max_wait_minutes,
                    quantity=args.quantity, binance_fee_bp=args.binance_fee_bp,
                    bybit_fee_bp=args.bybit_fee_bp, slippage_bp=args.slippage_bp)
    start, end = (int(pd.Timestamp(v, tz="UTC").timestamp() * 1000) for v in (args.start, args.end))
    result = run(args.db, args.output_dir, args.symbols.split(","), start, end, config)
    print(json.dumps({"output_dir": str(args.output_dir), "coverage": result["coverage"]}))


if __name__ == "__main__":
    main()
