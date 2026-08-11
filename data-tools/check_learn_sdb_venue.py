#!/usr/bin/env python3
"""Determine which venue/market learn.sdb's 2024-09 OHLCV data most likely originates from.

learn.sdb: /mnt/e/Datas/learn.sdb, table `ohlcv`, columns include timestamp (TEXT,
format '2024.09.01 00:01'), open, high, low, close, volume. A stray header row
(timestamp='timestamp') is present and must be excluded.

Compares learn.sdb close prices against same-minute close prices from:
  - Binance spot BTCUSDT 1m klines (2024-09)
  - Binance perp  BTCUSDT 1m klines (2024-09)
  - Bybit  perp   BTCUSDT 1m klines (2024-09, subset of full month if only partial fetched)
(and same for ETHUSDT, in case learn.sdb turns out to be ETH not BTC)

Metric: mean absolute error (MAE) and max absolute error on matched timestamps.
Lowest MAE + high match-rate wins.
"""
import csv
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE = "/mnt/e/Datas/market"
SDB = "/mnt/e/Datas/learn.sdb"
SQLITE = os.path.expanduser("~/.local/bin/sqlite3")


def load_learn_sdb():
    """Return dict: 'YYYY-MM-DD HH:MM:SS' -> close (float), for 2024-09 range."""
    cmd = [
        SQLITE, "-csv", "-noheader", SDB,
        "SELECT timestamp, close FROM ohlcv WHERE timestamp != 'timestamp' "
        "AND timestamp LIKE '2024.09%';",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    result = {}
    for line in out.strip().splitlines():
        if not line:
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2:
            continue
        ts_raw, close_raw = parts
        # '2024.09.01 00:01' -> '2024-09-01 00:01:00'
        try:
            dt = datetime.strptime(ts_raw.strip('"'), "%Y.%m.%d %H:%M")
        except ValueError:
            continue
        key = dt.strftime("%Y-%m-%d %H:%M:00")
        try:
            result[key] = float(close_raw)
        except ValueError:
            continue
    return result


def load_binance_klines_close(symbol, market):
    """Return dict: 'YYYY-MM-DD HH:MM:SS' -> close, for 2024-09 csv."""
    subdir = "spot" if market == "spot" else "perp"
    path = os.path.join(BASE, f"binance/{subdir}/klines_1m/{symbol}/{symbol}-1m-2024-09.csv")
    if not os.path.exists(path):
        return {}
    result = {}
    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            try:
                ts_ms = int(row[0])
                close = float(row[4])
            except (ValueError, IndexError):
                continue
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            key = dt.strftime("%Y-%m-%d %H:%M:00")
            result[key] = close
    return result


def load_bybit_klines_close(symbol):
    files = glob.glob(os.path.join(BASE, f"bybit/perp/klines_1m/{symbol}/{symbol}-1m-*.csv"))
    result = {}
    for path in files:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row:
                    continue
                try:
                    ts_ms = int(row[0])
                    close = float(row[4])
                except (ValueError, IndexError):
                    continue
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                key = dt.strftime("%Y-%m-%d %H:%M:00")
                result[key] = close
    return result


def compare(learn_map, ref_map, label):
    common_keys = sorted(set(learn_map) & set(ref_map))
    if not common_keys:
        return {"label": label, "matched": 0, "mae": None, "max_abs_err": None, "mean_rel_err_bps": None}
    abs_errs = []
    rel_errs = []
    for k in common_keys:
        a, b = learn_map[k], ref_map[k]
        abs_errs.append(abs(a - b))
        if b != 0:
            rel_errs.append(abs(a - b) / b * 10000)  # bps
    mae = sum(abs_errs) / len(abs_errs)
    max_err = max(abs_errs)
    mean_rel_bps = sum(rel_errs) / len(rel_errs) if rel_errs else None
    return {
        "label": label,
        "matched": len(common_keys),
        "learn_rows": len(learn_map),
        "ref_rows": len(ref_map),
        "mae": round(mae, 6),
        "max_abs_err": round(max_err, 6),
        "mean_rel_err_bps": round(mean_rel_bps, 4) if mean_rel_bps is not None else None,
    }


def main():
    print(f"Loading learn.sdb 2024-09 close prices from {SDB} ...")
    learn_map = load_learn_sdb()
    print(f"  learn.sdb rows (2024-09, header excluded): {len(learn_map)}")
    if learn_map:
        sample_k = sorted(learn_map)[0]
        print(f"  sample: {sample_k} -> close={learn_map[sample_k]}")

    candidates = []
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        for market, loader in [
            ("binance_spot", lambda s=symbol: load_binance_klines_close(s, "spot")),
            ("binance_perp", lambda s=symbol: load_binance_klines_close(s, "perp")),
            ("bybit_perp", lambda s=symbol: load_bybit_klines_close(s)),
        ]:
            ref_map = loader()
            label = f"{market}:{symbol}"
            result = compare(learn_map, ref_map, label)
            candidates.append(result)
            print(f"  compared {label}: matched={result['matched']} mae={result['mae']} "
                  f"max_abs_err={result['max_abs_err']} mean_rel_err_bps={result['mean_rel_err_bps']}")

    # rank by mae among those with reasonable match rate (>= 50% of learn rows)
    min_match = len(learn_map) * 0.5
    ranked = [c for c in candidates if c["mae"] is not None and c["matched"] >= min_match]
    ranked.sort(key=lambda c: c["mae"])

    print("\n=== RANKING (by MAE, matched >= 50% of learn.sdb rows) ===")
    for c in ranked:
        print(f"  {c['label']:20s} mae={c['mae']:<12} matched={c['matched']}/{len(learn_map)} "
              f"mean_rel_err_bps={c['mean_rel_err_bps']}")

    verdict = ranked[0] if ranked else None
    if verdict:
        print(f"\nVERDICT: learn.sdb most closely matches {verdict['label']} "
              f"(MAE={verdict['mae']}, {verdict['matched']} matched bars, "
              f"mean_rel_err={verdict['mean_rel_err_bps']} bps)")
    else:
        print("\nVERDICT: no candidate had sufficient match rate to determine venue")

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "learn_sdb_rows_2024_09": len(learn_map),
        "candidates": candidates,
        "ranking": ranked,
        "verdict": verdict,
    }
    out_path = os.path.join(BASE, "learn_sdb_venue_check.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResult written to {out_path}")


if __name__ == "__main__":
    main()
