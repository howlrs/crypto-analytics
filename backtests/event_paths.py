#!/usr/bin/env python3
"""Path-aware MAE/MFE audit for existing liquidation and crowding events.

The source event CSV remains the authority for endpoint execution and net return.
This tool reads the corresponding Binance perpetual one-minute OHLCV path only to
describe intra-trade excursion.  It never writes to the market database.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")
CUTOFF_TS = int(pd.Timestamp("2026-08-01T00:00:00Z").timestamp() * 1000)
MINUTE_MS = 60_000
SUMMARY_COLUMNS = ["strategy", "symbol", "direction", "events", "valid_path_count", "missing_path_count",
                   "mae_p05_bp", "mae_p50_bp", "mae_p95_bp", "mfe_p05_bp", "mfe_p50_bp", "mfe_p95_bp",
                   "underwater_minutes_p50", "any_barrier_rate", "stop_rate", "take_rate", "ambiguous_rate"]


def strategy_name(path: Path, row: pd.Series | None = None) -> str:
    """Return a source strategy key, retaining a row-level horizon when needed.

    B2 stores its 24h and 72h observations in the same filename.  Its horizon
    is therefore part of the event identity, rather than merely display
    metadata.  Other producers commonly encode the horizon in the filename;
    avoid duplicating it when they also retain a ``horizon`` column.
    """
    stem = path.stem
    strategy = stem.removeprefix("events_").replace("_events_", "_")
    if row is not None and "horizon" in row and pd.notna(row["horizon"]):
        horizon = str(row["horizon"]).strip()
        if horizon and horizon.lower() not in strategy.lower():
            strategy = f"{strategy}_{horizon}"
    return strategy


def symbol_for(path: Path) -> str:
    text = path.name.upper()
    if "BTC" in text:
        return "BTCUSDT"
    if "ETH" in text:
        return "ETHUSDT"
    raise ValueError("cannot infer BTCUSDT or ETHUSDT from filename")


def direction_for(path: Path, row: pd.Series) -> str:
    """Infer the actual directional leg from the documented source CSV formats."""
    name = path.name.lower()
    if "_long_" in name:
        return "long"
    if "_short_" in name:
        return "short"
    if "a2_contrarian" in name:
        return {"top": "short", "bottom": "long"}.get(str(row.get("leg", "")).lower(), "")
    if "a3_momentum" in name:
        return {"top": "long", "bottom": "short"}.get(str(row.get("leg", "")).lower(), "")
    if "b2_deleverage" in name:
        return "long"
    if "b3_double_overheat" in name:
        return "short"
    return ""


def _bp(change: float) -> float:
    return 10_000.0 * change


def analyze_path(bars: pd.DataFrame, *, entry_ts: int, exit_ts: int, direction: str,
                 stop_bp: float, take_bp: float) -> dict:
    """Measure an intratrade [entry, exit) path plus the terminal exit open.

    OHLC bars cannot order high and low within a minute.  A minute covering both
    barriers is therefore recorded as ``ambiguous`` rather than assigned
    an invented first hit.  Open crossings include gaps and are unambiguous.
    """
    if (not np.isfinite(stop_bp) or not np.isfinite(take_bp) or stop_bp <= 0 or take_bp <= 0 or
            stop_bp >= 10_000 or take_bp >= 10_000):
        return {"reason": "invalid_barrier"}
    path = bars[(bars.ts >= entry_ts) & (bars.ts < exit_ts)].copy()
    if path.empty or int(path.ts.iloc[0]) != entry_ts:
        return {"reason": "missing_entry_bar"}
    expected = np.arange(entry_ts, exit_ts, MINUTE_MS, dtype=np.int64)
    if len(path) != len(expected) or not np.array_equal(path.ts.to_numpy(dtype=np.int64), expected):
        return {"reason": "missing_path_bar"}
    values = path[["open", "high", "low", "close"]].to_numpy(float)
    if not np.isfinite(values).all() or (values <= 0).any():
        return {"reason": "invalid_ohlc"}
    if ((path.low > path[["open", "close"]].min(axis=1)) |
            (path[["open", "close"]].max(axis=1) > path.high)).any():
        return {"reason": "invalid_ohlc_order"}
    terminal = bars.loc[bars.ts == exit_ts, "open"]
    if len(terminal) != 1 or not np.isfinite(terminal.iloc[0]) or terminal.iloc[0] <= 0:
        return {"reason": "missing_or_invalid_exit_open"}
    entry = float(path.open.iloc[0])
    exit_open = float(terminal.iloc[0])
    if direction == "long":
        adverse = path.low.to_numpy(float) / entry - 1.0
        favorable = path.high.to_numpy(float) / entry - 1.0
        terminal_return = exit_open / entry - 1.0
        underwater = path.close.to_numpy(float) < entry
        stop_level, take_level = entry * (1 - stop_bp / 10_000), entry * (1 + take_bp / 10_000)
        stop_open = path.open.to_numpy(float) <= stop_level
        take_open = path.open.to_numpy(float) >= take_level
        stop_touch = path.low.to_numpy(float) <= stop_level
        take_touch = path.high.to_numpy(float) >= take_level
    elif direction == "short":
        # Strategy returns are linear in original entry notional, including shorts.
        adverse = -(path.high.to_numpy(float) / entry - 1.0)
        favorable = -(path.low.to_numpy(float) / entry - 1.0)
        terminal_return = -(exit_open / entry - 1.0)
        underwater = path.close.to_numpy(float) > entry
        stop_level, take_level = entry * (1 + stop_bp / 10_000), entry * (1 - take_bp / 10_000)
        stop_open = path.open.to_numpy(float) >= stop_level
        take_open = path.open.to_numpy(float) <= take_level
        stop_touch = path.high.to_numpy(float) >= stop_level
        take_touch = path.low.to_numpy(float) <= take_level
    else:
        return {"reason": "invalid_direction"}

    # The first available observation wins.  At an opening price only one side
    # can be crossed for positive barriers; the explicit branch preserves that.
    first_barrier_hit, barrier_ts, ambiguous_bar_ts = "none", np.nan, np.nan
    for i, ts in enumerate(path.ts.to_numpy(dtype=np.int64)):
        if stop_open[i]:
            first_barrier_hit, barrier_ts = "stop_open", int(ts); break
        if take_open[i]:
            first_barrier_hit, barrier_ts = "take_open", int(ts); break
        if stop_touch[i] and take_touch[i]:
            first_barrier_hit, barrier_ts, ambiguous_bar_ts = "ambiguous", int(ts), int(ts); break
        if stop_touch[i]:
            first_barrier_hit, barrier_ts = "stop_intrabar", int(ts); break
        if take_touch[i]:
            first_barrier_hit, barrier_ts = "take_intrabar", int(ts); break
    # Exit is terminal execution at its open only.  It may set an excursion or
    # a gap barrier, but its H/L/C never influence path diagnostics.
    if first_barrier_hit == "none":
        if (direction == "long" and exit_open <= stop_level) or (direction == "short" and exit_open >= stop_level):
            first_barrier_hit, barrier_ts = "stop_open", exit_ts
        elif (direction == "long" and exit_open >= take_level) or (direction == "short" and exit_open <= take_level):
            first_barrier_hit, barrier_ts = "take_open", exit_ts

    adverse_all = np.append(adverse, terminal_return)
    favorable_all = np.append(favorable, terminal_return)
    ts_all = np.append(path.ts.to_numpy(dtype=np.int64), exit_ts)
    # Baseline zero prevents a uniformly favorable (or adverse) path from
    # reporting an impossible MAE > 0 (or MFE < 0).
    mae_i, mfe_i = int(np.argmin(np.append(adverse_all, 0.0))), int(np.argmax(np.append(favorable_all, 0.0)))
    return {
        "reason": "", "entry_px": entry,
        "exit_open_px": exit_open,
        "mae_bp": _bp(float(np.append(adverse_all, 0.0)[mae_i])), "mae_ts": int(ts_all[mae_i]) if mae_i < len(ts_all) else entry_ts,
        "mfe_bp": _bp(float(np.append(favorable_all, 0.0)[mfe_i])), "mfe_ts": int(ts_all[mfe_i]) if mfe_i < len(ts_all) else entry_ts,
        "underwater_minutes": int(underwater.sum()), "path_minutes": int(len(path)),
        "first_barrier_hit": first_barrier_hit, "barrier_ts": barrier_ts, "ambiguous_bar_ts": ambiguous_bar_ts,
    }


def load_bars(db: Path, symbol: str, start: int, end: int) -> pd.DataFrame:
    uri = f"file:{db.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        return pd.read_sql_query(
            "SELECT ts, open, high, low, close FROM klines "
            "WHERE venue='binance' AND market='perp' AND symbol=? AND ts>=? AND ts<=? ORDER BY ts",
            con, params=(symbol, int(start), int(end)),
        )


def _source_net(row: pd.Series) -> tuple[object, float, str]:
    """Read the source endpoint return without allowing a malformed row to abort a file."""
    for field in ("net_bp", "net_ret_bp"):
        if field in row and pd.notna(row[field]):
            raw = row[field]
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                return raw, np.nan, "invalid_source_net"
            if not np.isfinite(value):
                return raw, np.nan, "invalid_source_net"
            return raw, value, ""
    return np.nan, np.nan, "missing_source_net"


def _source_price(row: pd.Series, field: str, label: str) -> tuple[float, str]:
    """Parse an optional source endpoint price, returning a row-level reason."""
    if field not in row or pd.isna(row[field]):
        return np.nan, ""
    try:
        value = float(row[field])
    except (TypeError, ValueError, OverflowError):
        return np.nan, f"invalid_source_{label}_price"
    if not np.isfinite(value) or value <= 0:
        return np.nan, f"invalid_source_{label}_price"
    return value, ""


def audit_file(path: Path, db: Path, stop_bp: float, take_bp: float) -> tuple[list[dict], list[dict]]:
    try:
        events = pd.read_csv(path)
        required = {"entry_ts", "exit_ts"}
        if not required.issubset(events.columns):
            raise ValueError("missing required entry_ts/exit_ts")
        symbol = symbol_for(path)
    except Exception as exc:
        record = {"source_file": str(path), "row_number": np.nan, "path_complete": False,
                  "reason": f"invalid_source:{exc}"}
        return [record], [record]
    metrics, rejected = [], []
    def reject(record: dict, reason: str) -> None:
        record = record | {"path_complete": False, "reason": reason}
        metrics.append(record)
        rejected.append(record)
    valid_rows: list[tuple[int, pd.Series, int, int, str, float, float, float]] = []
    for index, row in events.iterrows():
        source_net_raw, source_net, net_reason = _source_net(row)
        base = {"source_file": str(path), "strategy": strategy_name(path, row), "symbol": symbol,
                "row_number": int(index), "source_net_bp": source_net_raw}
        try:
            entry, exit_ = int(row.entry_ts), int(row.exit_ts)
        except (TypeError, ValueError, OverflowError):
            reject(base, "invalid_timestamp"); continue
        direction = direction_for(path, row)
        if entry < 0 or exit_ <= entry:
            reject(base | {"entry_ts": entry, "exit_ts": exit_}, "invalid_interval"); continue
        if exit_ >= CUTOFF_TS:
            reject(base | {"entry_ts": entry, "exit_ts": exit_}, "exit_after_cutoff"); continue
        if entry % MINUTE_MS or exit_ % MINUTE_MS:
            reject(base | {"entry_ts": entry, "exit_ts": exit_}, "unaligned_timestamp"); continue
        if not direction:
            reject(base | {"entry_ts": entry, "exit_ts": exit_}, "unknown_direction"); continue
        if net_reason:
            reject(base | {"entry_ts": entry, "exit_ts": exit_, "direction": direction}, net_reason); continue
        source_entry_px, entry_price_reason = _source_price(row, "entry_px", "entry")
        if entry_price_reason:
            reject(base | {"entry_ts": entry, "exit_ts": exit_, "direction": direction}, entry_price_reason); continue
        source_exit_px, exit_price_reason = _source_price(row, "exit_px", "exit")
        if exit_price_reason:
            reject(base | {"entry_ts": entry, "exit_ts": exit_, "direction": direction}, exit_price_reason); continue
        valid_rows.append((int(index), row, entry, exit_, direction, source_net, source_entry_px, source_exit_px))
    if not valid_rows:
        return metrics, rejected
    bars = load_bars(db, symbol, min(x[2] for x in valid_rows), max(x[3] for x in valid_rows))
    for index, row, entry, exit_, direction, source_net, source_entry_px, source_exit_px in valid_rows:
        source_net_raw, _, _ = _source_net(row)
        base = {"source_file": str(path), "strategy": strategy_name(path, row), "symbol": symbol,
                "row_number": index, "entry_ts": entry, "exit_ts": exit_, "direction": direction,
                "source_net_bp": source_net_raw}
        entry_open = bars.loc[bars.ts == entry, "open"]
        exit_open = bars.loc[bars.ts == exit_, "open"]
        if (len(entry_open) != 1 or len(exit_open) != 1 or not np.isfinite(entry_open.iloc[0]) or
                not np.isfinite(exit_open.iloc[0]) or entry_open.iloc[0] <= 0 or exit_open.iloc[0] <= 0):
            reject(base, "missing_or_invalid_endpoint_open"); continue
        for source_price, actual, label in ((source_entry_px, float(entry_open.iloc[0]), "entry"),
                                            (source_exit_px, float(exit_open.iloc[0]), "exit")):
            if np.isfinite(source_price) and not np.isclose(source_price, actual, rtol=0, atol=1e-9):
                reject(base | {"entry_px": float(entry_open.iloc[0]), "exit_open_px": float(exit_open.iloc[0])},
                       f"source_{label}_price_mismatch"); break
        else:
            sign = 1.0 if direction == "long" else -1.0
            reconstructed_gross_bp = sign * (float(exit_open.iloc[0]) / float(entry_open.iloc[0]) - 1.0) * 10_000.0
            if not np.isclose(reconstructed_gross_bp, source_net + 14.0, rtol=0, atol=1e-6):
                reject(base | {"entry_px": float(entry_open.iloc[0]), "exit_open_px": float(exit_open.iloc[0]),
                               "reconstructed_gross_bp": reconstructed_gross_bp}, "source_net_mismatch"); continue
            result = analyze_path(bars, entry_ts=entry, exit_ts=exit_, direction=direction,
                                  stop_bp=stop_bp, take_bp=take_bp)
            if result["reason"]:
                reject(base, result["reason"]); continue
            metrics.append(base | result | {"path_complete": True, "reconstructed_gross_bp": reconstructed_gross_bp})
            continue
    return metrics, rejected


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_outputs(ledger: list[dict], rejected: list[dict], output_dir: Path, *, inputs: list[Path], db: Path,
                  stop_bp: float, take_bp: float) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_columns = ["source_file", "strategy", "symbol", "row_number", "entry_ts", "exit_ts", "direction",
                      "source_net_bp", "reconstructed_gross_bp", "entry_px", "exit_open_px", "path_complete",
                      "mae_bp", "mae_ts", "mfe_bp", "mfe_ts", "underwater_minutes", "path_minutes",
                      "first_barrier_hit", "barrier_ts", "ambiguous_bar_ts", "reason"]
    rejected_columns = ["source_file", "strategy", "symbol", "row_number", "entry_ts", "exit_ts", "direction",
                        "source_net_bp", "path_complete", "reason"]
    ledger_df = pd.DataFrame(ledger).reindex(columns=ledger_columns)
    rejected_df = pd.DataFrame(rejected).reindex(columns=rejected_columns)
    ledger_df.to_csv(output_dir / "event_path_metrics.csv", index=False)
    rejected_df.to_csv(output_dir / "event_path_rejected.csv", index=False)
    if ledger_df.empty:
        summary = pd.DataFrame(columns=SUMMARY_COLUMNS)
    else:
        rows = []
        for keys, group in ledger_df.groupby(["strategy", "symbol", "direction"], sort=True, dropna=False):
            complete = group[group.path_complete.fillna(False).astype(bool)]
            status = complete.first_barrier_hit.astype(str)
            q = lambda column, p: float(complete[column].quantile(p)) if len(complete) else np.nan
            rows.append(dict(strategy=keys[0], symbol=keys[1], direction=keys[2], events=int(len(group)),
                             valid_path_count=int(len(complete)), missing_path_count=int(len(group) - len(complete)),
                             mae_p05_bp=q("mae_bp", .05), mae_p50_bp=q("mae_bp", .50), mae_p95_bp=q("mae_bp", .95),
                             mfe_p05_bp=q("mfe_bp", .05), mfe_p50_bp=q("mfe_bp", .50), mfe_p95_bp=q("mfe_bp", .95),
                             underwater_minutes_p50=q("underwater_minutes", .50),
                             any_barrier_rate=float((status != "none").mean()) if len(complete) else np.nan,
                             stop_rate=float(status.str.startswith("stop").mean()), take_rate=float(status.str.startswith("take").mean()),
                             ambiguous_rate=float((status == "ambiguous").mean())))
        summary = pd.DataFrame(rows).reindex(columns=SUMMARY_COLUMNS)
    summary.to_csv(output_dir / "event_path_summary.csv", index=False)
    db_stat = db.stat()
    output_names = ["event_path_metrics.csv", "event_path_rejected.csv", "event_path_summary.csv"]
    manifest = {"format": 1, "cutoff_exit_ts_exclusive": CUTOFF_TS, "db": str(db),
                "db_size_bytes": db_stat.st_size, "db_mtime_ns": db_stat.st_mtime_ns,
                "script_sha256": _sha256(Path(__file__)), "stop_bp": stop_bp,
                "take_bp": take_bp, "inputs": [{"path": str(p), "sha256": _sha256(p)} for p in inputs],
                "accepted_events": int(ledger_df.path_complete.fillna(False).astype(bool).sum()), "rejected_events": int(len(rejected_df)),
                "outputs": output_names,
                "output_sha256": {name: _sha256(output_dir / name) for name in output_names}}
    (output_dir / "event_path_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_inputs(files: list[str], patterns: list[str]) -> list[Path]:
    paths = [Path(x) for x in files]
    for pattern in patterns:
        paths.extend(Path(x) for x in glob.glob(pattern, recursive=True))
    unique = sorted({p.resolve() for p in paths})
    missing = [str(p) for p in unique if not p.is_file()]
    if missing:
        raise ValueError("event input does not exist: " + ", ".join(missing))
    if not unique:
        raise ValueError("supply --event-file and/or --event-glob")
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit MAE/MFE paths for existing BTC/ETH event CSVs.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--event-file", action="append", default=[], help="one event CSV (repeatable)")
    parser.add_argument("--event-glob", action="append", default=[], help="glob of event CSVs (repeatable)")
    parser.add_argument("--stop-bp", type=float, default=100.0)
    parser.add_argument("--take-bp", type=float, default=100.0)
    args = parser.parse_args(argv)
    if (not np.isfinite(args.stop_bp) or not np.isfinite(args.take_bp) or
            args.stop_bp <= 0 or args.take_bp <= 0 or args.stop_bp >= 10_000 or args.take_bp >= 10_000):
        parser.error("--stop-bp and --take-bp must be finite, positive, and below 10000")
    inputs = resolve_inputs(args.event_file, args.event_glob)
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise ValueError("--output-dir must be new or empty")
    ledger, rejected = [], []
    for path in inputs:
        ok, bad = audit_file(path, args.db, args.stop_bp, args.take_bp)
        ledger.extend(ok); rejected.extend(bad)
    write_outputs(ledger, rejected, args.output_dir, inputs=inputs, db=args.db, stop_bp=args.stop_bp, take_bp=args.take_bp)
    accepted = sum(bool(row.get("path_complete")) for row in ledger)
    print(f"accepted={accepted} rejected={len(rejected)} output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
