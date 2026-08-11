#!/usr/bin/env python3
"""Fetch Bybit SPOT 1m klines via REST for a given date range.

Same backward-paging logic as fetch_bybit_klines.py (which is linear/perp-only
and hardcoded to bybit/perp/klines_1m/). This variant targets category=spot
and writes to bybit/spot/klines_1m/<SYMBOL>/. Added for HYPEUSDT spot history
(existing BTCUSDT/ETHUSDT perp fetch script left untouched to avoid regressions).

Bybit v5 /v5/market/kline: interval=1 (1min), max 1000 bars/request, returns descending
by time and does not paginate forward from start. We page BACKWARDS from end.

Output: CSV per symbol with columns open_time,open,high,low,close,volume,turnover
Idempotent: skip if final CSV already exists.
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get_json, log

BASE = "/mnt/e/Datas/market"
URL = "https://api.bybit.com/v5/market/kline"
SLEEP_S = 0.2
LIMIT = 1000
BAR_MS = 60_000


def out_path(symbol, start_ms, end_ms):
    d = os.path.join(BASE, "bybit/spot/klines_1m", symbol)
    os.makedirs(d, exist_ok=True)
    sd = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    ed = datetime.fromtimestamp((end_ms - 1) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(d, f"{symbol}-1m-{sd}_to_{ed}.csv")


def fetch_range(symbol, start_ms, end_ms):
    all_bars = {}
    cur_end = end_ms - 1
    page = 0
    while cur_end >= start_ms:
        params = f"?category=spot&symbol={symbol}&interval=1&start={start_ms}&end={cur_end}&limit={LIMIT}"
        status, data = http_get_json(URL + params)
        page += 1
        if status != 200 or data is None or data.get("retCode") != 0:
            log(f"  bybit spot kline {symbol} page {page}: bad response status={status} data={str(data)[:200]}")
            break
        lst = data["result"]["list"]
        if not lst:
            log(f"  bybit spot kline {symbol} page {page}: empty, stopping at cur_end={cur_end}")
            break
        min_ts = None
        for rec in lst:
            ts = int(rec[0])
            all_bars[ts] = rec
            if min_ts is None or ts < min_ts:
                min_ts = ts
        log(f"  bybit spot kline {symbol} page {page}: got {len(lst)} bars, min_ts={min_ts}, total={len(all_bars)}")
        next_end = min_ts - BAR_MS
        if next_end >= cur_end:
            log(f"  bybit spot kline {symbol}: no progress, stopping")
            break
        cur_end = next_end
        time.sleep(SLEEP_S)
    return sorted(all_bars.values(), key=lambda r: int(r[0]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, exclusive)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sd = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ed = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(sd.timestamp() * 1000)
    end_ms = int(ed.timestamp() * 1000)

    path = out_path(args.symbol, start_ms, end_ms)
    if os.path.exists(path) and not args.force:
        log(f"SKIP {args.symbol}: {path} already exists")
        return

    log(f"Fetching Bybit spot 1m klines {args.symbol} {args.start}..{args.end}")
    bars = fetch_range(args.symbol, start_ms, end_ms)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open_time", "open", "high", "low", "close", "volume", "turnover"])
        for rec in bars:
            w.writerow(rec)

    if bars:
        log(f"DONE {args.symbol}: {len(bars)} bars -> {path}")
    else:
        log(f"DONE {args.symbol}: 0 bars -> {path}")


if __name__ == "__main__":
    main()
