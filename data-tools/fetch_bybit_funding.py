#!/usr/bin/env python3
"""Fetch full Bybit linear perp funding rate history via REST, walking endTime cursor backwards.

Bybit v5 /v5/market/funding/history returns most-recent-first, max 200/page.
We page backwards using endTime = min(fundingRateTimestamp seen) - 1, until API returns empty list
or we pass below a safety floor (2019-01-01).

Output: one JSON file per symbol containing the full sorted (ascending time) list.
Idempotent: if final combined JSON already exists and --force not given, skip.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get_json, log

BASE = "/mnt/e/Datas/market"
URL = "https://api.bybit.com/v5/market/funding/history"
SLEEP_S = 0.2
FLOOR_MS = 1546300800000  # 2019-01-01T00:00:00Z


def out_path(symbol):
    d = os.path.join(BASE, "bybit/perp/funding", symbol)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}-funding-history.json")


def fetch_all(symbol):
    all_records = {}
    end_time = None
    page = 0
    while True:
        params = f"?category=linear&symbol={symbol}&limit=200"
        if end_time is not None:
            params += f"&endTime={end_time}"
        status, data = http_get_json(URL + params)
        page += 1
        if status != 200 or data is None or data.get("retCode") != 0:
            log(f"  bybit funding {symbol} page {page}: bad response status={status} data={str(data)[:200]}")
            break
        lst = data["result"]["list"]
        if not lst:
            log(f"  bybit funding {symbol} page {page}: empty list, stopping")
            break
        new_count = 0
        min_ts = None
        for rec in lst:
            ts = int(rec["fundingRateTimestamp"])
            if ts not in all_records:
                all_records[ts] = rec
                new_count += 1
            if min_ts is None or ts < min_ts:
                min_ts = ts
        log(f"  bybit funding {symbol} page {page}: got {len(lst)} recs (new={new_count}), min_ts={min_ts}, total_unique={len(all_records)}")
        if new_count == 0:
            log(f"  bybit funding {symbol}: no new records, stopping (reached start of history or dup loop)")
            break
        if min_ts <= FLOOR_MS:
            log(f"  bybit funding {symbol}: reached floor date, stopping")
            break
        end_time = min_ts - 1
        time.sleep(SLEEP_S)
    return sorted(all_records.values(), key=lambda r: int(r["fundingRateTimestamp"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    path = out_path(args.symbol)
    if os.path.exists(path) and not args.force:
        log(f"SKIP {args.symbol}: {path} already exists")
        return

    log(f"Fetching Bybit funding history for {args.symbol}")
    records = fetch_all(args.symbol)
    with open(path, "w") as f:
        json.dump(records, f)
    if records:
        log(f"DONE {args.symbol}: {len(records)} records, "
            f"range {records[0]['fundingRateTimestamp']}..{records[-1]['fundingRateTimestamp']} -> {path}")
    else:
        log(f"DONE {args.symbol}: 0 records -> {path}")


if __name__ == "__main__":
    main()
