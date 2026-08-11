#!/usr/bin/env python3
"""Fetch Binance USDS-M perp daily metrics (open interest etc), verify sha256, extract CSV.

Daily-only endpoint. Limited to >=2023-01-01 per spec.
Usage:
  python3 fetch_binance_metrics.py --symbol BTCUSDT --start 2023-01-01 --end 2026-07-31
"""
import argparse
import concurrent.futures
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get, verify_checksum, extract_single_csv_from_zip, day_range, log

BASE = "/mnt/e/Datas/market"
URL_TMPL = "https://data.binance.vision/data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{d}.zip"


def out_dir(symbol):
    d = os.path.join(BASE, "binance/perp/metrics", symbol)
    os.makedirs(d, exist_ok=True)
    return d


def fetch_one(symbol, d: date, force=False):
    ds = d.strftime("%Y-%m-%d")
    url = URL_TMPL.format(symbol=symbol, d=ds)
    csv_path = os.path.join(out_dir(symbol), f"{symbol}-metrics-{ds}.csv")
    if os.path.exists(csv_path) and not force:
        return ds, "skip_exists", True

    status, zbytes = http_get(url)
    if status == 404:
        return ds, "not_found", None
    if zbytes is None:
        return ds, "error_empty", None

    cstatus, cbytes = http_get(url + ".CHECKSUM")
    checksum_verified = verify_checksum(zbytes, cbytes.decode("utf-8", errors="replace")) if (cstatus == 200 and cbytes) else False

    try:
        csv_bytes = extract_single_csv_from_zip(zbytes)
    except Exception as e:
        return ds, f"error_extract:{e}", checksum_verified

    with open(csv_path, "wb") as f:
        f.write(csv_bytes)
    return ds, "ok", checksum_verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    sd = datetime.strptime(args.start, "%Y-%m-%d").date()
    ed = datetime.strptime(args.end, "%Y-%m-%d").date()
    days = list(day_range(sd, ed))
    log(f"Fetching metrics {args.symbol} for {len(days)} days ({args.start}..{args.end})")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, args.symbol, d, args.force): d for d in days}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            ds, status, checksum_ok = fut.result()
            results.append((ds, status, checksum_ok))
            done += 1
            if status != "skip_exists" or done % 200 == 0:
                log(f"  metrics {args.symbol} {ds}: {status} checksum_ok={checksum_ok} ({done}/{len(days)})")

    results.sort()
    ok = sum(1 for _, s, _ in results if s == "ok")
    skip = sum(1 for _, s, _ in results if s == "skip_exists")
    nf = sum(1 for _, s, _ in results if s == "not_found")
    err = sum(1 for _, s, _ in results if s not in ("ok", "skip_exists", "not_found"))
    log(f"DONE metrics {args.symbol}: ok={ok} skip={skip} not_found={nf} error={err}")


if __name__ == "__main__":
    main()
