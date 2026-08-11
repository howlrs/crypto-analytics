#!/usr/bin/env python3
"""Fetch Binance spot & USDS-M perp 1m klines (monthly zips), verify sha256, extract CSV.

Idempotent: skips months whose extracted CSV already exists (unless --force).
Usage:
  python3 fetch_binance_klines.py --market spot --symbol BTCUSDT --start 2021-01 --end 2026-07
  python3 fetch_binance_klines.py --market perp --symbol ETHUSDT --start 2021-01 --end 2026-07
"""
import argparse
import concurrent.futures
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_get, verify_checksum, extract_single_csv_from_zip, month_range, log

BASE = "/mnt/e/Datas/market"

MARKET_URL = {
    "spot": "https://data.binance.vision/data/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{ym}.zip",
    "perp": "https://data.binance.vision/data/futures/um/monthly/klines/{symbol}/1m/{symbol}-1m-{ym}.zip",
}
MARKET_DIR = {
    "spot": "binance/spot/klines_1m",
    "perp": "binance/perp/klines_1m",
}


def out_dir(market, symbol):
    d = os.path.join(BASE, MARKET_DIR[market], symbol)
    os.makedirs(d, exist_ok=True)
    return d


def fetch_one(market, symbol, year, month, force=False):
    ym = f"{year:04d}-{month:02d}"
    url = MARKET_URL[market].format(symbol=symbol, ym=ym)
    csv_path = os.path.join(out_dir(market, symbol), f"{symbol}-1m-{ym}.csv")
    if os.path.exists(csv_path) and not force:
        return ym, "skip_exists", True

    status, zbytes = http_get(url)
    if status == 404:
        return ym, "not_found", None
    if zbytes is None:
        return ym, "error_empty", None

    checksum_verified = None
    cstatus, cbytes = http_get(url + ".CHECKSUM")
    if cstatus == 200 and cbytes:
        checksum_verified = verify_checksum(zbytes, cbytes.decode("utf-8", errors="replace"))
    else:
        checksum_verified = False

    try:
        csv_bytes = extract_single_csv_from_zip(zbytes)
    except Exception as e:
        return ym, f"error_extract:{e}", checksum_verified

    with open(csv_path, "wb") as f:
        f.write(csv_bytes)

    return ym, "ok", checksum_verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["spot", "perp"], required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM")
    ap.add_argument("--end", required=True, help="YYYY-MM")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    sy, sm = map(int, args.start.split("-"))
    ey, em = map(int, args.end.split("-"))
    months = list(month_range(sy, sm, ey, em))

    log(f"Fetching {args.market} {args.symbol} klines for {len(months)} months "
        f"({args.start}..{args.end}), workers={args.workers}")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(fetch_one, args.market, args.symbol, y, m, args.force): (y, m)
            for y, m in months
        }
        for fut in concurrent.futures.as_completed(futs):
            ym, status, checksum_ok = fut.result()
            results.append((ym, status, checksum_ok))
            log(f"  {args.market}/{args.symbol} {ym}: {status} checksum_ok={checksum_ok}")

    results.sort()
    ok = sum(1 for _, s, _ in results if s == "ok")
    skip = sum(1 for _, s, _ in results if s == "skip_exists")
    nf = sum(1 for _, s, _ in results if s == "not_found")
    err = sum(1 for _, s, _ in results if s not in ("ok", "skip_exists", "not_found"))
    log(f"DONE {args.market}/{args.symbol}: ok={ok} skip={skip} not_found={nf} error={err}")
    if nf:
        log("  not_found months: " + ", ".join(ym for ym, s, _ in results if s == "not_found"))
    if err:
        log("  error months: " + ", ".join(f"{ym}({s})" for ym, s, _ in results if s not in ("ok", "skip_exists", "not_found")))


if __name__ == "__main__":
    main()
