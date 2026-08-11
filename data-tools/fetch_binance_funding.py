#!/usr/bin/env python3
"""Fetch Binance USDS-M perp funding rate monthly zips, verify sha256, extract CSV.

Usage:
  python3 fetch_binance_funding.py --symbol BTCUSDT --start 2021-01 --end 2026-07
"""
import argparse
import concurrent.futures
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get, verify_checksum, extract_single_csv_from_zip, month_range, log

BASE = "/mnt/e/Datas/market"
URL_TMPL = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{ym}.zip"


def out_dir(symbol):
    d = os.path.join(BASE, "binance/perp/funding", symbol)
    os.makedirs(d, exist_ok=True)
    return d


def fetch_one(symbol, year, month, force=False):
    ym = f"{year:04d}-{month:02d}"
    url = URL_TMPL.format(symbol=symbol, ym=ym)
    csv_path = os.path.join(out_dir(symbol), f"{symbol}-fundingRate-{ym}.csv")
    if os.path.exists(csv_path) and not force:
        return ym, "skip_exists", True

    status, zbytes = http_get(url)
    if status == 404:
        return ym, "not_found", None
    if zbytes is None:
        return ym, "error_empty", None

    cstatus, cbytes = http_get(url + ".CHECKSUM")
    checksum_verified = verify_checksum(zbytes, cbytes.decode("utf-8", errors="replace")) if (cstatus == 200 and cbytes) else False

    try:
        csv_bytes = extract_single_csv_from_zip(zbytes)
    except Exception as e:
        return ym, f"error_extract:{e}", checksum_verified

    with open(csv_path, "wb") as f:
        f.write(csv_bytes)
    return ym, "ok", checksum_verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    sy, sm = map(int, args.start.split("-"))
    ey, em = map(int, args.end.split("-"))
    months = list(month_range(sy, sm, ey, em))
    log(f"Fetching funding {args.symbol} for {len(months)} months")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, args.symbol, y, m, args.force): (y, m) for y, m in months}
        for fut in concurrent.futures.as_completed(futs):
            ym, status, checksum_ok = fut.result()
            results.append((ym, status, checksum_ok))
            log(f"  funding {args.symbol} {ym}: {status} checksum_ok={checksum_ok}")

    results.sort()
    ok = sum(1 for _, s, _ in results if s == "ok")
    skip = sum(1 for _, s, _ in results if s == "skip_exists")
    nf = sum(1 for _, s, _ in results if s == "not_found")
    err = sum(1 for _, s, _ in results if s not in ("ok", "skip_exists", "not_found"))
    log(f"DONE funding {args.symbol}: ok={ok} skip={skip} not_found={nf} error={err}")
    if nf:
        log("  not_found: " + ", ".join(ym for ym, s, _ in results if s == "not_found"))


if __name__ == "__main__":
    main()
