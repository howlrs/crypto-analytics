#!/usr/bin/env python3
"""Fetch Hyperliquid funding history via POST /info fundingHistory, paging by startTime.

Response: list of {coin, fundingRate, premium, time} ascending by time, max 500/page (per docs).
We page forward using startTime = last_time + 1 until an empty/short page (no more new data).

Output: JSON file per coin.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_post_json, log

BASE = "/mnt/e/Datas/market"
URL = "https://api.hyperliquid.xyz/info"
SLEEP_S = 0.2
PAGE_SIZE = 500


def out_path(coin):
    d = os.path.join(BASE, "hyperliquid/funding", coin)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{coin}-funding-history.json")


def fetch_all(coin, start_time_ms):
    all_records = {}
    cur_start = start_time_ms
    page = 0
    now_ms = int(time.time() * 1000)
    while cur_start < now_ms:
        payload = {"type": "fundingHistory", "coin": coin, "startTime": cur_start}
        status, data = http_post_json(URL, payload)
        page += 1
        if status != 200 or data is None:
            log(f"  hyperliquid {coin} page {page}: bad response status={status}")
            break
        if not isinstance(data, list) or not data:
            log(f"  hyperliquid {coin} page {page}: empty list, stopping at cur_start={cur_start}")
            break
        max_ts = None
        new_count = 0
        for rec in data:
            ts = int(rec["time"])
            if ts not in all_records:
                new_count += 1
            all_records[ts] = rec
            if max_ts is None or ts > max_ts:
                max_ts = ts
        log(f"  hyperliquid {coin} page {page}: got {len(data)} recs (new={new_count}), max_ts={max_ts}, total={len(all_records)}")
        if new_count == 0:
            log(f"  hyperliquid {coin}: no new records, stopping")
            break
        next_start = max_ts + 1
        if next_start <= cur_start:
            log(f"  hyperliquid {coin}: no progress, stopping")
            break
        cur_start = next_start
        time.sleep(SLEEP_S)
    return sorted(all_records.values(), key=lambda r: int(r["time"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    ap.add_argument("--start", default="2023-01-01", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    path = out_path(args.coin)
    if os.path.exists(path) and not args.force:
        log(f"SKIP {args.coin}: {path} already exists")
        return

    sd = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(sd.timestamp() * 1000)

    log(f"Fetching Hyperliquid funding history for {args.coin} from {args.start}")
    records = fetch_all(args.coin, start_ms)
    with open(path, "w") as f:
        json.dump(records, f)
    if records:
        log(f"DONE {args.coin}: {len(records)} records, range {records[0]['time']}..{records[-1]['time']} -> {path}")
    else:
        log(f"DONE {args.coin}: 0 records -> {path}")


if __name__ == "__main__":
    main()
