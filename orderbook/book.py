"""Order-book arithmetic shared by the collector and the analysis (standard library only).

Prices are quote currency per unit and sizes are base units; notionals are
``px * size`` in quote currency (USDT/USDC treated as USD).  A side's
*coverage* is the distance from mid (bp) within which the stored levels are
known to be complete.  Depth or impact that would need levels beyond it is
reported as unknown rather than extrapolated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

Level = tuple  # (px, size) or (px, size, order_count)


@dataclass(frozen=True)
class Book:
    bids: list   # best (highest) first
    asks: list   # best (lowest) first
    bid_coverage_bp: float
    ask_coverage_bp: float

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bp(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid * 10_000

    def side(self, side: str) -> tuple[list, float]:
        if side == "bid":
            return self.bids, self.bid_coverage_bp
        if side == "ask":
            return self.asks, self.ask_coverage_bp
        raise ValueError("side must be bid or ask")


def parse_levels(raw: Iterable, *, descending: bool) -> list:
    """Convert exchange levels to floats and reject unsorted, duplicate or non-positive rows."""
    levels = []
    for item in raw:
        if isinstance(item, dict):  # Hyperliquid {"px", "sz", "n"}
            level = (float(item["px"]), float(item["sz"]), int(item["n"]))
        else:
            level = (float(item[0]), float(item[1]))
        if not (math.isfinite(level[0]) and math.isfinite(level[1]) and level[0] > 0 and level[1] > 0):
            raise ValueError("non-positive or non-finite level")
        levels.append(level)
    for previous, current in zip(levels, levels[1:]):
        if (current[0] >= previous[0]) if descending else (current[0] <= previous[0]):
            raise ValueError("levels are not strictly ordered")
    return levels


def distance_bp(px: float, mid: float) -> float:
    return abs(px / mid - 1) * 10_000


def side_coverage_bp(levels: Sequence, mid: float, *, exhausted: bool) -> float:
    """Completeness of one fetched side: the whole side if the venue returned fewer
    levels than requested, otherwise up to the furthest returned level."""
    if exhausted or not levels:
        return math.inf
    return distance_bp(levels[-1][0], mid)


def truncate(levels: Sequence, mid: float, coverage_bp: float, *, band_bp: float = 0.0,
             max_levels: int = 0) -> tuple[list, float]:
    """Keep levels within ``band_bp`` of mid and at most ``max_levels``; return the new coverage."""
    kept = list(levels)
    coverage = coverage_bp
    if band_bp > 0:
        kept = [level for level in kept if distance_bp(level[0], mid) <= band_bp]
        coverage = min(coverage, band_bp)
    if max_levels > 0 and len(kept) > max_levels:
        kept = kept[:max_levels]
        coverage = min(coverage, distance_bp(kept[-1][0], mid))
    return kept, coverage


def make_book(bids: list, asks: list, *, bid_coverage_bp: float, ask_coverage_bp: float) -> Book:
    if not bids or not asks:
        raise ValueError("empty book side")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("crossed or locked book")
    return Book(bids, asks, bid_coverage_bp, ask_coverage_bp)


def depth_usd(book: Book, side: str, band_bp: float) -> float:
    """Resting notional within ``band_bp`` of mid; nan when the band exceeds coverage."""
    levels, coverage = book.side(side)
    if band_bp > coverage:
        return math.nan
    mid = book.mid
    return sum(px * size for px, size, *_ in levels if distance_bp(px, mid) <= band_bp)


def imbalance(bid_usd: float, ask_usd: float) -> float:
    total = bid_usd + ask_usd
    if not (math.isfinite(total) and total > 0):
        return math.nan
    return (bid_usd - ask_usd) / total


def impact_bp(book: Book, side: str, notional: float) -> float:
    """Average execution cost versus mid (bp, half-spread included) of a market order
    of ``notional`` that consumes ``side`` ("ask" for a buy, "bid" for a sell).

    Only levels inside coverage are usable; nan when they are insufficient.
    """
    levels, coverage = book.side(side)
    mid = book.mid
    remaining, base = notional, 0.0
    for px, size, *_ in levels:
        if distance_bp(px, mid) > coverage:
            break
        take = min(remaining, px * size)
        base += take / px
        remaining -= take
        if remaining <= 1e-9 * notional:
            vwap = notional / base
            return (vwap / mid - 1) * 10_000 if side == "ask" else (1 - vwap / mid) * 10_000
    return math.nan


def walls(book: Book, *, band_bp: float, multiple: float, min_usd: float, min_share: float = 0.0) -> list[dict]:
    """Large resting levels within ``min(band_bp, coverage)`` of mid.

    A wall needs at least ``min_usd``, ``multiple`` times the side's median level
    notional in that range, and ``min_share`` of the side's notional in that range.
    The share test keeps books with many dust levels from flagging ordinary levels.
    """
    mid = book.mid
    found = []
    for side in ("bid", "ask"):
        levels, coverage = book.side(side)
        limit = min(band_bp, coverage)
        inside = [(px, size, *rest) for px, size, *rest in levels if distance_bp(px, mid) <= limit]
        if len(inside) < 3:
            continue
        notionals = sorted(px * size for px, size, *_ in inside)
        middle = len(notionals) // 2
        median = notionals[middle] if len(notionals) % 2 else (notionals[middle - 1] + notionals[middle]) / 2
        total = sum(notionals)
        threshold = max(min_usd, multiple * median, min_share * total)
        for px, size, *rest in inside:
            usd = px * size
            if usd >= threshold:
                found.append({"side": side, "px": px, "usd": usd, "distance_bp": distance_bp(px, mid),
                              "share": usd / total, "orders": rest[0] if rest else None, "median_level_usd": median})
    return found
