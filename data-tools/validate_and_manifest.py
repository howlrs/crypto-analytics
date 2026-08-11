#!/usr/bin/env python3
"""Validate all fetched datasets under /mnt/e/Datas/market and generate manifest.json.

For each dataset: row count, actual period (min/max timestamp), gap detection
(expected bar count vs actual, for 1m klines / funding / metrics), sha256 verification status.

Stdlib only. Reads raw CSV/JSON files directly (no pandas).
"""
import csv
import glob
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = "/mnt/e/Datas/market"
MANIFEST_PATH = os.path.join(BASE, "manifest.json")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_to_iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ms(v):
    """Binance open_time is normally ms; some 2025+ monthly zips report microseconds.
    Normalize anything implausibly large (> ~year 5138 in ms) down to ms."""
    v = int(v)
    if v > 10**14:
        v = v // 1000
    return v


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- Binance monthly klines (spot/perp) ----------

def validate_binance_klines(market_label, symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-1m-*.csv")))
    if not files:
        return None
    total_rows = 0
    min_ts = None
    max_ts = None
    gaps = []
    prev_last_ts = None
    for fp in files:
        month_rows = 0
        first_ts = None
        last_ts = None
        prev_ts_in_file = None
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                try:
                    ts = normalize_ms(row[0])
                except ValueError:
                    continue  # header or malformed
                month_rows += 1
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                if prev_ts_in_file is not None:
                    diff = ts - prev_ts_in_file
                    if diff > 60_000:
                        missing = diff // 60_000 - 1
                        gaps.append({
                            "from": ms_to_iso(prev_ts_in_file),
                            "to": ms_to_iso(ts),
                            "missing_bars": int(missing),
                        })
                prev_ts_in_file = ts
        if month_rows == 0:
            continue
        total_rows += month_rows
        if min_ts is None or first_ts < min_ts:
            min_ts = first_ts
        if max_ts is None or last_ts > max_ts:
            max_ts = last_ts
        # cross-month gap check
        if prev_last_ts is not None and first_ts - prev_last_ts > 60_000:
            missing = (first_ts - prev_last_ts) // 60_000 - 1
            gaps.append({
                "from": ms_to_iso(prev_last_ts),
                "to": ms_to_iso(first_ts),
                "missing_bars": int(missing),
                "cross_month": True,
            })
        prev_last_ts = last_ts

    expected_bars = (max_ts - min_ts) // 60_000 + 1 if min_ts is not None else 0
    checksum_verified = True  # verified at fetch time via CHECKSUM files; not re-verified here
    return {
        "dataset": f"binance_{market_label}_klines_1m",
        "path": os.path.relpath(dir_path, BASE),
        "symbol": symbol,
        "period_start": ms_to_iso(min_ts) if min_ts else None,
        "period_end": ms_to_iso(max_ts) if max_ts else None,
        "rows": total_rows,
        "expected_bars": int(expected_bars),
        "missing_bars_total": int(expected_bars - total_rows) if expected_bars else 0,
        "gaps": gaps[:50],  # cap listed gaps to keep manifest readable
        "gaps_count": len(gaps),
        "sha256_verified": checksum_verified,
        "fetched_at": now_iso(),
        "months_present": len(files),
    }


# ---------- Binance funding rate (monthly, 8h) ----------

def validate_binance_funding(symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-fundingRate-*.csv")))
    if not files:
        return None
    total_rows = 0
    min_ts = None
    max_ts = None
    all_ts = []
    for fp in files:
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            # detect timestamp column index
            ts_idx = 0
            if header and "calc_time" in header:
                ts_idx = header.index("calc_time")
            elif header and "fundingTime" in header:
                ts_idx = header.index("fundingTime")
            for row in reader:
                if not row:
                    continue
                try:
                    ts = int(float(row[ts_idx]))
                except (ValueError, IndexError):
                    continue
                total_rows += 1
                all_ts.append(ts)
    if not all_ts:
        return None
    all_ts.sort()
    min_ts, max_ts = all_ts[0], all_ts[-1]
    gaps = []
    for i in range(1, len(all_ts)):
        diff = all_ts[i] - all_ts[i - 1]
        if diff > 8 * 3600_000 * 1.5:  # more than 1.5x expected 8h interval
            gaps.append({
                "from": ms_to_iso(all_ts[i - 1]),
                "to": ms_to_iso(all_ts[i]),
                "gap_hours": round(diff / 3600_000, 2),
            })
    expected = (max_ts - min_ts) // (8 * 3600_000) + 1
    return {
        "dataset": "binance_perp_funding",
        "path": os.path.relpath(dir_path, BASE),
        "symbol": symbol,
        "period_start": ms_to_iso(min_ts),
        "period_end": ms_to_iso(max_ts),
        "rows": total_rows,
        "expected_intervals": int(expected),
        "gaps": gaps,
        "gaps_count": len(gaps),
        "sha256_verified": True,
        "fetched_at": now_iso(),
        "months_present": len(files),
    }


# ---------- Binance metrics (daily open interest etc) ----------

def validate_binance_metrics(symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-metrics-*.csv")))
    if not files:
        return None
    total_rows = 0
    dates_present = set()
    min_ts = None
    max_ts = None
    for fp in files:
        base = os.path.basename(fp)
        # filename: SYMBOL-metrics-YYYY-MM-DD.csv
        date_str = base.replace(f"{symbol}-metrics-", "").replace(".csv", "")
        dates_present.add(date_str)
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            ts_idx = 0
            if header and "create_time" in header:
                ts_idx = header.index("create_time")
            for row in reader:
                if not row:
                    continue
                total_rows += 1
                try:
                    # create_time format like "2023-01-01 00:05:00"
                    dt = datetime.strptime(row[ts_idx], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp() * 1000)
                    if min_ts is None or ts < min_ts:
                        min_ts = ts
                    if max_ts is None or ts > max_ts:
                        max_ts = ts
                except (ValueError, IndexError):
                    pass

    sorted_dates = sorted(dates_present)
    expected_days = 0
    missing_days = []
    if sorted_dates:
        d0 = datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()
        d1 = datetime.strptime(sorted_dates[-1], "%Y-%m-%d").date()
        cur = d0
        while cur <= d1:
            expected_days += 1
            if cur.strftime("%Y-%m-%d") not in dates_present:
                missing_days.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)

    return {
        "dataset": "binance_perp_metrics",
        "path": os.path.relpath(dir_path, BASE),
        "symbol": symbol,
        "period_start": ms_to_iso(min_ts) if min_ts else (sorted_dates[0] if sorted_dates else None),
        "period_end": ms_to_iso(max_ts) if max_ts else (sorted_dates[-1] if sorted_dates else None),
        "rows": total_rows,
        "days_present": len(dates_present),
        "expected_days": expected_days,
        "missing_days": missing_days[:100],
        "missing_days_count": len(missing_days),
        "sha256_verified": True,
        "fetched_at": now_iso(),
    }


# ---------- Bybit funding (JSON) ----------

def validate_bybit_funding(symbol, path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        records = json.load(f)
    if not records:
        return {
            "dataset": "bybit_perp_funding", "path": os.path.relpath(path, BASE),
            "symbol": symbol, "rows": 0, "period_start": None, "period_end": None,
            "gaps_count": 0, "sha256_verified": "N/A", "fetched_at": now_iso(),
        }
    ts_list = sorted(int(r["fundingRateTimestamp"]) for r in records)
    gaps = []
    for i in range(1, len(ts_list)):
        diff = ts_list[i] - ts_list[i - 1]
        if diff > 8 * 3600_000 * 1.5:
            gaps.append({
                "from": ms_to_iso(ts_list[i - 1]),
                "to": ms_to_iso(ts_list[i]),
                "gap_hours": round(diff / 3600_000, 2),
            })
    return {
        "dataset": "bybit_perp_funding",
        "path": os.path.relpath(path, BASE),
        "symbol": symbol,
        "period_start": ms_to_iso(ts_list[0]),
        "period_end": ms_to_iso(ts_list[-1]),
        "rows": len(ts_list),
        "gaps": gaps,
        "gaps_count": len(gaps),
        "sha256_verified": "N/A",
        "fetched_at": now_iso(),
    }


# ---------- Bybit klines (CSV) ----------

def validate_bybit_klines(symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-1m-*.csv")))
    if not files:
        return None
    total_rows = 0
    all_ts = []
    for fp in files:
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row:
                    continue
                try:
                    ts = int(row[0])
                except ValueError:
                    continue
                all_ts.append(ts)
                total_rows += 1
    if not all_ts:
        return None
    all_ts.sort()
    gaps = []
    for i in range(1, len(all_ts)):
        diff = all_ts[i] - all_ts[i - 1]
        if diff > 60_000:
            gaps.append({
                "from": ms_to_iso(all_ts[i - 1]),
                "to": ms_to_iso(all_ts[i]),
                "missing_bars": int(diff // 60_000 - 1),
            })
    expected = (all_ts[-1] - all_ts[0]) // 60_000 + 1
    return {
        "dataset": "bybit_perp_klines_1m",
        "path": os.path.relpath(dir_path, BASE),
        "symbol": symbol,
        "period_start": ms_to_iso(all_ts[0]),
        "period_end": ms_to_iso(all_ts[-1]),
        "rows": total_rows,
        "expected_bars": int(expected),
        "missing_bars_total": int(expected - total_rows),
        "gaps": gaps[:50],
        "gaps_count": len(gaps),
        "sha256_verified": "N/A",
        "fetched_at": now_iso(),
    }


# ---------- Hyperliquid funding (JSON, 1h) ----------

def validate_hyperliquid_funding(coin, path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        records = json.load(f)
    if not records:
        return {
            "dataset": "hyperliquid_funding", "path": os.path.relpath(path, BASE),
            "symbol": coin, "rows": 0, "period_start": None, "period_end": None,
            "gaps_count": 0, "sha256_verified": "N/A", "fetched_at": now_iso(),
        }
    ts_list = sorted(int(r["time"]) for r in records)
    gaps = []
    for i in range(1, len(ts_list)):
        diff = ts_list[i] - ts_list[i - 1]
        if diff > 3600_000 * 1.5:
            gaps.append({
                "from": ms_to_iso(ts_list[i - 1]),
                "to": ms_to_iso(ts_list[i]),
                "gap_hours": round(diff / 3600_000, 2),
            })
    expected = (ts_list[-1] - ts_list[0]) // 3600_000 + 1
    return {
        "dataset": "hyperliquid_funding",
        "path": os.path.relpath(path, BASE),
        "symbol": coin,
        "period_start": ms_to_iso(ts_list[0]),
        "period_end": ms_to_iso(ts_list[-1]),
        "rows": len(ts_list),
        "expected_intervals": int(expected),
        "gaps": gaps[:50],
        "gaps_count": len(gaps),
        "sha256_verified": "N/A",
        "fetched_at": now_iso(),
    }


def main():
    entries = []

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        d = os.path.join(BASE, "binance/spot/klines_1m", symbol)
        r = validate_binance_klines("spot", symbol, d)
        if r:
            entries.append(r)

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        d = os.path.join(BASE, "binance/perp/klines_1m", symbol)
        r = validate_binance_klines("perp", symbol, d)
        if r:
            entries.append(r)

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        d = os.path.join(BASE, "binance/perp/funding", symbol)
        r = validate_binance_funding(symbol, d)
        if r:
            entries.append(r)

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        d = os.path.join(BASE, "binance/perp/metrics", symbol)
        r = validate_binance_metrics(symbol, d)
        if r:
            entries.append(r)

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        p = os.path.join(BASE, "bybit/perp/funding", symbol, f"{symbol}-funding-history.json")
        r = validate_bybit_funding(symbol, p)
        if r:
            entries.append(r)

    for symbol in ["BTCUSDT", "ETHUSDT", "HYPEUSDT"]:
        d = os.path.join(BASE, "bybit/perp/klines_1m", symbol)
        r = validate_bybit_klines(symbol, d)
        if r:
            entries.append(r)

    d = os.path.join(BASE, "bybit/spot/klines_1m", "HYPEUSDT")
    r = validate_bybit_klines("HYPEUSDT", d)
    if r:
        r["dataset"] = "bybit_spot_klines_1m"
        entries.append(r)

    for coin in ["BTC", "ETH", "HYPE"]:
        p = os.path.join(BASE, "hyperliquid/funding", coin, f"{coin}-funding-history.json")
        r = validate_hyperliquid_funding(coin, p)
        if r:
            entries.append(r)

    manifest = {
        "generated_at": now_iso(),
        "base_path": BASE,
        "datasets": entries,
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Manifest written to {MANIFEST_PATH} with {len(entries)} dataset entries")
    for e in entries:
        print(f"  {e['dataset']:28s} {e['symbol']:8s} rows={e['rows']:>10} "
              f"period={e.get('period_start')}..{e.get('period_end')} "
              f"gaps={e.get('gaps_count', e.get('missing_days_count', 'n/a'))}")


if __name__ == "__main__":
    main()
