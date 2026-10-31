#!/usr/bin/env python3
"""Fetch funding history and candles for Hyperliquid HIP-3 (xyz) perps that track Japanese prices.

  xyz:JP225     Nikkei 225, quanto (JPY index level settled in USDC)
  xyz:KIOXIA    TSE 285A converted to USD at the live USD/JPY rate
  xyz:SOFTBANK  TSE 9984 converted to USD
  xyz:JPY       USD/JPY, used for the FX leg

candleSnapshot only serves the most recent 5000 candles per interval, so every run merges new
candles into the stored file by open time and never drops older ones. Funding is paged from
--start. Files go to /mnt/e/Datas/market/hyperliquid/xyz/ with ':' replaced by '_'
(NTFS forbids ':'). Public data; stdlib only.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http_post_json, log, now_iso
from fetch_hyperliquid_funding import fetch_all

URL = "https://api.hyperliquid.xyz/info"
BASE = "/mnt/e/Datas/market/hyperliquid/xyz"
COINS = ("xyz:JP225", "xyz:KIOXIA", "xyz:SOFTBANK", "xyz:JPY")
INTERVALS = {"15m": 900_000, "1h": 3_600_000}
MAX_CANDLES = 5000


def stem(coin):
    return coin.replace(":", "_")


def merge_candles(old, new):
    """Union by open time; a re-served candle replaces the stored one (the last bar may have been open)."""
    rows = {int(c["t"]): c for c in old}
    rows.update({int(c["t"]): c for c in new})
    return [rows[t] for t in sorted(rows)]


def fetch_candles(coin, interval, now_ms):
    span = MAX_CANDLES * INTERVALS[interval]
    status, data = http_post_json(URL, {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval, "startTime": now_ms - span, "endTime": now_ms}})
    if status != 200 or not isinstance(data, list):
        raise RuntimeError(f"candleSnapshot {coin} {interval}: status {status}")
    return data


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as handle:
        return json.load(handle)


def write_json(path, value):
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as handle:
        json.dump(value, handle)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--start", default="2025-12-01", help="first funding day (YYYY-MM-DD, UTC)")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    os.makedirs(args.base, exist_ok=True)
    start_ms = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    manifest_path = os.path.join(args.base, "manifest.json")
    manifest = read_json(manifest_path, {})
    for coin in [c.strip() for c in args.coins.split(",") if c.strip()]:
        now_ms = int(time.time() * 1000)
        funding = fetch_all(coin, start_ms)
        write_json(os.path.join(args.base, f"{stem(coin)}-funding.json"), funding)
        entry = {"funding_rows": len(funding), "fetched_at": now_iso()}
        for interval in INTERVALS:
            path = os.path.join(args.base, f"{stem(coin)}-candles-{interval}.json")
            merged = merge_candles(read_json(path, []), fetch_candles(coin, interval, now_ms))
            write_json(path, merged)
            entry[f"candles_{interval}"] = len(merged)
            time.sleep(0.2)
        manifest[coin] = entry
        log(f"{coin}: {json.dumps(entry)}")
    # Open interest has no public history, so keep a dated snapshot per run for capacity notes.
    status, ctx = http_post_json(URL, {"type": "metaAndAssetCtxs", "dex": "xyz"})
    if status == 200 and isinstance(ctx, list) and len(ctx) == 2:
        snapshot = {a["name"]: {k: c.get(k) for k in ("markPx", "oraclePx", "openInterest", "dayNtlVlm", "funding")}
                    for a, c in zip(ctx[0]["universe"], ctx[1]) if a["name"] in COINS}
        manifest.setdefault("ctx_snapshots", {})[now_iso()] = snapshot
    write_json(manifest_path, manifest)


if __name__ == "__main__":
    main()
