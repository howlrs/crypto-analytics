#!/usr/bin/env python3
"""Collect a short live HYPE spot/perp demand snapshot from Hyperliquid."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import websockets


INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
COINS = ("HYPE", "@107")


def info(payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        INFO_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def asset_context(coin: str) -> dict[str, Any]:
    if coin == "HYPE":
        meta, contexts = info({"type": "metaAndAssetCtxs"})
        index = next(i for i, asset in enumerate(meta["universe"]) if asset["name"] == coin)
        return contexts[index]
    _, contexts = info({"type": "spotMetaAndAssetCtxs"})
    return next(context for context in contexts if context.get("coin") == coin)


def book_metrics(book: dict[str, Any]) -> dict[str, Any]:
    bids, asks = book["levels"]
    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    mid = (best_bid + best_ask) / 2

    def side_notional(levels: list[dict[str, Any]], max_bps: float) -> float:
        total = 0.0
        for level in levels:
            px = float(level["px"])
            distance_bps = abs(px / mid - 1) * 10_000
            if distance_bps <= max_bps:
                total += px * float(level["sz"])
        return total

    depth: dict[str, Any] = {}
    for bps in (1, 5, 10, 25, 50):
        bid = side_notional(bids, bps)
        ask = side_notional(asks, bps)
        depth[str(bps)] = {
            "bid_usd": bid,
            "ask_usd": ask,
            "imbalance": (bid - ask) / (bid + ask) if bid + ask else None,
        }

    def largest(levels: list[dict[str, Any]]) -> list[dict[str, float]]:
        ranked = sorted(
            ({"px": float(x["px"]), "usd": float(x["px"]) * float(x["sz"])} for x in levels),
            key=lambda x: x["usd"],
            reverse=True,
        )
        return ranked[:3]

    return {
        "time_ms": book["time"],
        "mid": mid,
        "spread_bps": (best_ask - best_bid) / mid * 10_000,
        "depth": depth,
        "largest_bids": largest(bids),
        "largest_asks": largest(asks),
        "bid_range_bps": abs(float(bids[-1]["px"]) / mid - 1) * 10_000,
        "ask_range_bps": abs(float(asks[-1]["px"]) / mid - 1) * 10_000,
    }


def candles(coin: str, now_ms: int, interval: str = "1m", lookback_hours: int = 25) -> list[dict[str, Any]]:
    return info(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": now_ms - lookback_hours * 60 * 60 * 1000,
                "endTime": now_ms,
            },
        }
    )


def funding_history(coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= end_ms:
        page = info({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
        if not page:
            break
        rows.extend(page)
        next_cursor = int(page[-1]["time"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(page) < 500:
            break
    return rows


def candle_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    rows = sorted(rows, key=lambda x: x["t"])
    latest_close = float(rows[-1]["c"])
    out: dict[str, Any] = {"latest_close": latest_close, "bars": len(rows)}
    for label, minutes in (("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240), ("24h", 1440)):
        subset = rows[-minutes:] if len(rows) >= minutes else rows
        first_open = float(subset[0]["o"])
        out[label] = {
            "return_pct": (latest_close / first_open - 1) * 100,
            "volume_base": sum(float(x["v"]) for x in subset),
            "volume_usd_approx": sum(float(x["v"]) * float(x["c"]) for x in subset),
            "high": max(float(x["h"]) for x in subset),
            "low": min(float(x["l"]) for x in subset),
        }
    return out


def hourly_candle_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    rows = sorted(rows, key=lambda x: x["t"])
    latest_close = float(rows[-1]["c"])
    result: dict[str, Any] = {"latest_close": latest_close, "bars": len(rows)}
    for label, hours in (("7d", 24 * 7), ("30d", 24 * 30)):
        subset = rows[-hours:] if len(rows) >= hours else rows
        first_open = float(subset[0]["o"])
        result[label] = {
            "return_pct": (latest_close / first_open - 1) * 100,
            "high": max(float(x["h"]) for x in subset),
            "low": min(float(x["l"]) for x in subset),
            "volume_base": sum(float(x["v"]) for x in subset),
            "volume_usd_approx": sum(float(x["v"]) * float(x["c"]) for x in subset),
        }
    return result


def funding_summary(rows: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    result: dict[str, Any] = {"rows": len(rows)}
    for label, hours in (("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30)):
        rates = [float(x["fundingRate"]) for x in rows if int(x["time"]) >= now_ms - hours * 60 * 60 * 1000]
        if not rates:
            continue
        rates_sorted = sorted(rates)
        result[label] = {
            "observations": len(rates),
            "mean_hourly": statistics.fmean(rates),
            "median_hourly": statistics.median(rates),
            "annualized_mean_pct": statistics.fmean(rates) * 24 * 365 * 100,
            "positive_share": sum(rate > 0 for rate in rates) / len(rates),
            "negative_share": sum(rate < 0 for rate in rates) / len(rates),
            "min": rates_sorted[0],
            "max": rates_sorted[-1],
        }
    return result


def summarize_trades(trades: dict[str, dict[tuple[int, int], dict[str, Any]]], start_ms: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for coin in COINS:
        rows = sorted(trades[coin].values(), key=lambda x: x["time"])
        buy = sum(float(x["px"]) * float(x["sz"]) for x in rows if x["side"] == "B")
        sell = sum(float(x["px"]) * float(x["sz"]) for x in rows if x["side"] == "A")
        notionals = [
            {
                "time_ms": x["time"],
                "side": x["side"],
                "px": float(x["px"]),
                "size": float(x["sz"]),
                "usd": float(x["px"]) * float(x["sz"]),
            }
            for x in rows
        ]
        result[coin] = {
            "trades": len(rows),
            "first_time_ms": rows[0]["time"] if rows else None,
            "last_time_ms": rows[-1]["time"] if rows else None,
            "buy_usd": buy,
            "sell_usd": sell,
            "net_taker_usd": buy - sell,
            "buy_share": buy / (buy + sell) if buy + sell else None,
            "largest": sorted(notionals, key=lambda x: x["usd"], reverse=True)[:10],
            "capture_start_ms": start_ms,
        }
    return result


def summarize_books(samples: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for coin, rows in samples.items():
        entry: dict[str, Any] = {"samples": len(rows)}
        for field in ("mid", "spread_bps"):
            values = [float(x[field]) for x in rows]
            entry[field] = {
                "mean": statistics.fmean(values),
                "min": min(values),
                "max": max(values),
                "last": values[-1],
            }
        entry["depth"] = {}
        for bps in (1, 5, 10, 25, 50):
            key = str(bps)
            bid = [x["depth"][key]["bid_usd"] for x in rows]
            ask = [x["depth"][key]["ask_usd"] for x in rows]
            imbalance = [x["depth"][key]["imbalance"] for x in rows if x["depth"][key]["imbalance"] is not None]
            entry["depth"][key] = {
                "mean_bid_usd": statistics.fmean(bid),
                "mean_ask_usd": statistics.fmean(ask),
                "mean_imbalance": statistics.fmean(imbalance) if imbalance else None,
                "bid_dominant_share": sum(x > 0 for x in imbalance) / len(imbalance) if imbalance else None,
                "last_bid_usd": bid[-1],
                "last_ask_usd": ask[-1],
            }
        entry["last_largest_bids"] = rows[-1]["largest_bids"]
        entry["last_largest_asks"] = rows[-1]["largest_asks"]
        entry["last_visible_range_bps"] = {
            "bid": rows[-1]["bid_range_bps"],
            "ask": rows[-1]["ask_range_bps"],
        }
        result[coin] = entry
    return result


async def collect(duration: int, poll_seconds: float) -> dict[str, Any]:
    started_ms = int(time.time() * 1000)
    trades: dict[str, dict[tuple[int, int], dict[str, Any]]] = {coin: {} for coin in COINS}
    books: dict[str, list[dict[str, Any]]] = defaultdict(list)
    contexts_before = await asyncio.gather(*(asyncio.to_thread(asset_context, coin) for coin in COINS))
    candle_rows = await asyncio.gather(*(asyncio.to_thread(candles, coin, started_ms) for coin in COINS))
    hourly_rows = await asyncio.gather(
        *(asyncio.to_thread(candles, coin, started_ms, "1h", 24 * 31) for coin in COINS)
    )
    funding_rows = await asyncio.to_thread(
        funding_history, "HYPE", started_ms - 31 * 24 * 60 * 60 * 1000, started_ms
    )

    async def listen() -> None:
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
            for coin in COINS:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(5.0, max(0.1, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    continue
                message = json.loads(raw)
                if message.get("channel") != "trades":
                    continue
                for trade in message.get("data", []):
                    coin = trade.get("coin")
                    if coin in trades and int(trade["time"]) >= started_ms:
                        trades[coin][(int(trade["time"]), int(trade["tid"]))] = trade

    async def poll_books() -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            fetched = await asyncio.gather(
                *(asyncio.to_thread(info, {"type": "l2Book", "coin": coin, "nSigFigs": 5}) for coin in COINS)
            )
            for coin, book in zip(COINS, fetched, strict=True):
                books[coin].append(book_metrics(book))
            await asyncio.sleep(poll_seconds)

    await asyncio.gather(listen(), poll_books())
    ended_ms = int(time.time() * 1000)
    contexts_after = await asyncio.gather(*(asyncio.to_thread(asset_context, coin) for coin in COINS))

    context_output: dict[str, Any] = {}
    for coin, before, after in zip(COINS, contexts_before, contexts_after, strict=True):
        context_output[coin] = {"before": before, "after": after}
        if coin == "HYPE":
            mark_before = float(before["markPx"])
            mark_after = float(after["markPx"])
            oi_before = float(before["openInterest"])
            oi_after = float(after["openInterest"])
            context_output[coin]["change"] = {
                "mark_pct": (mark_after / mark_before - 1) * 100,
                "oi_base": oi_after - oi_before,
                "oi_usd_approx": (oi_after - oi_before) * mark_after,
                "oi_pct": (oi_after / oi_before - 1) * 100,
            }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capture": {"start_ms": started_ms, "end_ms": ended_ms, "duration_seconds": (ended_ms - started_ms) / 1000},
        "contexts": context_output,
        "candles": {coin: candle_summary(rows) for coin, rows in zip(COINS, candle_rows, strict=True)},
        "hourly_candles": {
            coin: hourly_candle_summary(rows) for coin, rows in zip(COINS, hourly_rows, strict=True)
        },
        "funding_history": funding_summary(funding_rows, started_ms),
        "trades": summarize_trades(trades, started_ms),
        "books": summarize_books(books),
        "notes": [
            "B/A is taker buy/sell direction.",
            "Hyperliquid l2Book is capped at 20 levels per side; wider depth can be understated.",
            "USD candle volume is an approximation using each bar close.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--book-poll", type=float, default=5.0)
    args = parser.parse_args()
    if args.duration < 10:
        parser.error("--duration must be at least 10 seconds")
    print(json.dumps(asyncio.run(collect(args.duration, args.book_poll)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
