"""Causal early-warning diagnostics; see docs/early-warning.md for limitations."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import statistics
import time
from typing import Sequence

MINUTE_MS = 60_000


@dataclass(frozen=True)
class MinuteBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float | None = None
    taker_buy_quote: float | None = None
    received_ts: int | None = None


@dataclass(frozen=True)
class MarketSeries:
    venue: str
    market: str
    symbol: str
    bars: Sequence[MinuteBar]


@dataclass(frozen=True)
class ReferenceLevels:
    venue: str
    symbol: str
    support: float
    resistance: float
    known_at: int
    expires_at: int | None = None


@dataclass(frozen=True)
class WarningConfig:
    context_minutes: int = 60
    short_minutes: int = 15
    confirmation_minutes: int = 5
    confirmation_bars: int = 2
    pressure_imbalance: float = 0.10
    compression_ratio: float = 0.75
    near_resistance_atr: float = 1.0
    breakout_volume_ratio: float = 1.5
    reclaim_closes: int = 3
    max_age_seconds: int = 120


def _validate_config(config: WarningConfig) -> None:
    integers = (config.context_minutes, config.short_minutes, config.confirmation_minutes,
                config.confirmation_bars, config.reclaim_closes, config.max_age_seconds)
    if any(type(value) is not int or value <= 0 for value in integers):
        raise ValueError("window lengths, counts and max age must be positive integers")
    if config.context_minutes % config.short_minutes:
        raise ValueError("context_minutes must be a multiple of short_minutes")
    if config.context_minutes % config.confirmation_minutes:
        raise ValueError("context_minutes must be a multiple of confirmation_minutes")
    if config.reclaim_closes >= config.short_minutes:
        raise ValueError("reclaim_closes must be shorter than short_minutes")
    if not 0 < config.pressure_imbalance <= 1 or not 0 < config.compression_ratio < 1:
        raise ValueError("invalid imbalance or compression threshold")
    if not math.isfinite(config.near_resistance_atr) or config.near_resistance_atr <= 0:
        raise ValueError("near_resistance_atr must be finite and positive")
    if not math.isfinite(config.breakout_volume_ratio) or config.breakout_volume_ratio <= 0:
        raise ValueError("breakout_volume_ratio must be finite and positive")


def _available(series: MarketSeries, cutoff: int, mode: str) -> list[MinuteBar]:
    """Validate only the causal prefix; future payloads cannot change a past result."""
    selected = []
    for bar in series.bars:
        if type(bar.ts) is not int or bar.ts % MINUTE_MS:
            raise ValueError("bar.ts must be a minute-aligned integer epoch millisecond")
        if bar.ts + MINUTE_MS > cutoff:
            continue
        if mode == "observed":
            if bar.received_ts is None:
                continue
            if type(bar.received_ts) is not int or bar.received_ts < bar.ts + MINUTE_MS:
                raise ValueError("received_ts must not precede a completed candle")
            if bar.received_ts > cutoff:
                continue
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in prices):
            raise ValueError("OHLC prices must be finite and positive")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("invalid OHLC ordering")
        for value in (bar.quote_volume, bar.taker_buy_quote):
            if value is not None and (not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or value < 0):
                raise ValueError("volume must be finite and nonnegative, or null")
        if bar.taker_buy_quote is not None and (bar.quote_volume is None or
                                                bar.taker_buy_quote > bar.quote_volume):
            raise ValueError("taker buy quote must not exceed total quote volume")
        selected.append(bar)
    selected.sort(key=lambda bar: bar.ts)
    if any(a.ts == b.ts for a, b in zip(selected, selected[1:])):
        raise ValueError("duplicate minute timestamps are not allowed")
    return selected


def _window(bars: Sequence[MinuteBar], end: int, minutes: int) -> list[MinuteBar] | None:
    start = end - minutes * MINUTE_MS
    selected = [bar for bar in bars if start <= bar.ts < end]
    if len(selected) != minutes or any(bar.ts != start + i * MINUTE_MS
                                       for i, bar in enumerate(selected)):
        return None
    return selected


def _metrics(bars: Sequence[MinuteBar]) -> dict:
    volume = (sum(bar.quote_volume for bar in bars)
              if all(bar.quote_volume is not None for bar in bars) else None)
    buy = (sum(bar.taker_buy_quote for bar in bars)
           if all(bar.taker_buy_quote is not None for bar in bars) else None)
    imbalance = (2 * buy / volume - 1) if volume is not None and volume > 0 and buy is not None else None
    return {"return_fraction": bars[-1].close / bars[0].open - 1,
            "range": max(bar.high for bar in bars) - min(bar.low for bar in bars),
            "quote_volume": volume, "taker_imbalance": imbalance,
            "flow_status": "known" if imbalance is not None else "unknown"}


def evaluate(spot: MarketSeries, perp: MarketSeries | None = None, *, as_of_ms: int,
             generated_at_ms: int, mode: str = "observed", config: WarningConfig | None = None,
             reference: ReferenceLevels | None = None) -> dict:
    """Return a descriptive snapshot using only completed, available minutes.

    `observed` additionally requires received_ts <= as_of_ms for every bar.
    `market_time_replay` reconstructs market-time prefixes, never live eligibility.
    Historical states are diagnostics, not validated forecasts or trade signals.
    """
    config = config or WarningConfig()
    _validate_config(config)
    if mode not in ("observed", "market_time_replay"):
        raise ValueError("mode must be observed or market_time_replay")
    if any(type(ts) is not int or ts < 0 for ts in (as_of_ms, generated_at_ms)):
        raise ValueError("decision and generation times must be nonnegative epoch milliseconds")
    if generated_at_ms < as_of_ms:
        raise ValueError("generation time must not precede decision time")
    if spot.market != "spot" or not spot.venue or not spot.symbol:
        raise ValueError("primary series must identify a spot venue and symbol")
    if perp is not None and (perp.market != "perp" or perp.symbol != spot.symbol or not perp.venue):
        raise ValueError("optional perp series must have the same symbol as spot")
    if reference is not None:
        if reference.venue != spot.venue or reference.symbol != spot.symbol:
            raise ValueError("reference venue and symbol must match primary spot")
        if not (math.isfinite(reference.support) and math.isfinite(reference.resistance)
                and 0 < reference.support < reference.resistance):
            raise ValueError("invalid reference prices")
        if type(reference.known_at) is not int or not 0 <= reference.known_at <= as_of_ms:
            raise ValueError("reference must be known at or before decision time")
        if reference.expires_at is not None and (type(reference.expires_at) is not int or
                                                 reference.expires_at <= as_of_ms):
            raise ValueError("reference is expired or expiry timestamp is invalid")

    settings = asdict(config)
    # A caller-supplied historical timestamp cannot authorize live publication.
    # Replay remains a pure market-time calculation; observed mode checks the
    # actual local clock independently of both declared timestamps.
    wall_clock_ms = time.time_ns() // 1_000_000 if mode == "observed" else None
    publication_time = max(generated_at_ms, wall_clock_ms) if wall_clock_ms is not None else generated_at_ms
    clock_valid = wall_clock_ms is not None and generated_at_ms <= wall_clock_ms
    result = {"schema_version": 1, "rule_version": "early-warning-v1-unvalidated",
              "config": settings,
              "config_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest(),
              "mode": mode, "as_of_ms": as_of_ms, "generated_at_ms": generated_at_ms,
              "publication_checked_at_ms": wall_clock_ms,
              "state": "insufficient_data", "publishable_state": "insufficient_data",
              "usable_for_live": False, "flags": [], "counterevidence": [],
              "limitations": ["Descriptive rules; false-alarm rate and predictive edge are unvalidated.",
                              "No entry, exit, position sizing or 4–48-hour holding recommendation.",
                              "OHLCV/taker flow only; no order book, OI or liquidation confirmation."],
              "sources": {}, "metrics": {}}
    if mode == "market_time_replay":
        result["limitations"].append("Market-time reconstruction does not establish actual historical receipt.")
    bars = _available(spot, as_of_ms, mode)
    last_end = bars[-1].ts + MINUTE_MS if bars else None
    result["sources"]["spot"] = {"venue": spot.venue, "symbol": spot.symbol,
                                  "last_complete_end_ms": last_end,
                                  "available_bars": len(bars)}
    if not bars:
        result["counterevidence"].append("No completed, available spot candles at decision time.")
        return result
    result["price"] = bars[-1].close
    age = generated_at_ms - last_end
    publication_age = publication_time - last_end
    fresh = publication_age <= config.max_age_seconds * 1000
    result["freshness"] = {"price_age_seconds": age / 1000,
                           "publication_price_age_seconds": publication_age / 1000,
                           "decision_age_seconds": (generated_at_ms - as_of_ms) / 1000,
                           "within_limit": fresh}
    # Enough uninterrupted history for a pre-short-window volatility baseline.
    history_minutes = config.context_minutes + max(config.short_minutes,
        config.confirmation_minutes * config.confirmation_bars) + 2 * config.confirmation_minutes
    history = _window(bars, last_end, history_minutes)
    if history is None:
        result["counterevidence"].append(f"Need {history_minutes} consecutive complete spot minutes; gaps are not filled.")
        return result
    step = config.confirmation_minutes * MINUTE_MS
    block_end = last_end // step * step
    if reference is None:
        reference_end = block_end - config.confirmation_bars * step
        context = _window(history, reference_end, config.context_minutes)
        known_at = (max(reference_end, max(bar.received_ts for bar in context))
                    if mode == "observed" else reference_end)
        reference = ReferenceLevels(spot.venue, spot.symbol, min(bar.low for bar in context),
                                    max(bar.high for bar in context), known_at)
        result["reference_kind"] = "rolling_prior_context"
        result["limitations"].append("Rolling levels are stateless; supply fixed levels to track the same setup over time.")
    else:
        result["reference_kind"] = "fixed"
    result["reference"] = asdict(reference)
    if reference.support == reference.resistance:
        result["counterevidence"].append("Flat reference range cannot define a breakout setup.")
        return result
    short = _window(history, last_end, config.short_minutes)
    context = _window(history, last_end, config.context_minutes)
    result["metrics"]["spot"] = {"short": _metrics(short), "context": _metrics(context)}
    available_perp = ([bar for bar in _available(perp, as_of_ms, mode)
                       if bar.ts + MINUTE_MS <= last_end] if perp else [])
    perp_context = _window(available_perp, last_end, config.context_minutes)
    if perp:
        result["sources"]["perp"] = {"venue": perp.venue, "symbol": perp.symbol,
                                      "last_complete_end_ms": (available_perp[-1].ts + MINUTE_MS
                                                               if available_perp else None),
                                      "synchronized": perp_context is not None}
    if perp_context:
        result["metrics"]["perp"] = {"short": _metrics(perp_context[-config.short_minutes:]),
                                     "context": _metrics(perp_context)}
    else:
        result["counterevidence"].append("Synchronized perp context unavailable; not treated as neutral flow.")

    def flag(name: str, **evidence: object) -> None:
        result["flags"].append({"name": name, **evidence})

    for market, windows in result["metrics"].items():
        for window, values in windows.items():
            if values["taker_imbalance"] is None:
                result["counterevidence"].append(f"{market} {window} taker flow unknown.")
            elif (values["taker_imbalance"] <= -config.pressure_imbalance
                  and values["return_fraction"] >= 0):
                flag("sell_resilience", market=market, window=window,
                     return_fraction=values["return_fraction"], taker_imbalance=values["taker_imbalance"])
    # Compression baseline excludes the entire latest short window.
    short_start = last_end - config.short_minutes * MINUTE_MS
    preceding_ranges = [_metrics(_window(history, short_start - i * config.short_minutes * MINUTE_MS,
                                         config.short_minutes))["range"]
                        for i in range(config.context_minutes // config.short_minutes)]
    median_range = statistics.median(preceding_ranges)
    compression = _metrics(short)["range"] / median_range if median_range > 0 else None
    volatility_end = short_start // step * step
    true_ranges = []
    for i in range(config.context_minutes // config.confirmation_minutes):
        end = volatility_end - i * step
        group = _window(history, end, config.confirmation_minutes)
        previous = _window(history, end - step, 1)
        high, low = max(bar.high for bar in group), min(bar.low for bar in group)
        previous_close = previous[-1].close
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    scale = statistics.median(true_ranges)
    distance = reference.resistance - bars[-1].close
    near = 0 <= distance <= config.near_resistance_atr * scale if scale > 0 else False
    result["metrics"]["setup"] = {"compression_ratio": compression, "prior_median_true_range": scale,
                                    "resistance_distance": distance, "near_resistance": near}
    if near and compression is not None and compression <= config.compression_ratio:
        flag("compression_near_resistance", ratio=compression)
    for market in ("spot", "perp"):
        values = result["metrics"].get(market, {}).get("short", {})
        imbalance = values.get("taker_imbalance")
        if near and imbalance is not None and imbalance >= config.pressure_imbalance and values["return_fraction"] > 0:
            flag("buy_pressure_near_resistance", market=market, taker_imbalance=imbalance)

    # Only candles beginning after levels became known can confirm those levels.
    reclaim_window = [bar for bar in short if bar.ts >= reference.known_at]
    if (len(reclaim_window) > config.reclaim_closes
        and any(bar.close < reference.support for bar in reclaim_window[:-config.reclaim_closes])
        and all(bar.close > reference.support for bar in reclaim_window[-config.reclaim_closes:])):
        flag("support_reclaimed", closes=config.reclaim_closes, level=reference.support)
    confirmations = []
    for i in reversed(range(config.confirmation_bars)):
        end = block_end - i * step
        group = _window(history, end, config.confirmation_minutes)
        baseline = [_metrics(_window(history, end - j * step, config.confirmation_minutes))["quote_volume"]
                    for j in range(1, config.context_minutes // config.confirmation_minutes + 1)]
        volume = _metrics(group)["quote_volume"]
        typical = statistics.median(baseline) if all(v is not None for v in baseline) else None
        ratio = volume / typical if volume is not None and typical is not None and typical > 0 else None
        confirmations.append({"end_ms": end, "close": group[-1].close,
                              "after_reference_known": group[0].ts >= reference.known_at,
                              "above_resistance": group[-1].close > reference.resistance,
                              "volume_ratio": ratio})
    price_confirmed = all(item["above_resistance"] and item["after_reference_known"] for item in confirmations)
    volume_confirmed = any(item["volume_ratio"] is not None and
                           item["volume_ratio"] >= config.breakout_volume_ratio for item in confirmations)
    result["confirmation"] = {"bars": confirmations, "price_confirmed": price_confirmed,
                               "volume_confirmed": volume_confirmed,
                               "spot_flow_confirmation": result["metrics"]["spot"]["short"]["taker_imbalance"]}
    if price_confirmed:
        flag("resistance_close_confirmation", level=reference.resistance, volume_confirmed=volume_confirmed)
    if bars[-1].close < reference.support:
        state = "support_lost"
        result["counterevidence"].append("Latest spot close is below reference support; positive flags do not override it.")
    elif price_confirmed and bars[-1].close > reference.resistance:
        state = "price_breakout_confirmed"
        if not volume_confirmed:
            result["counterevidence"].append("Price confirmation lacks the configured volume expansion.")
    elif result["flags"]:
        state = "early_watch"
    else:
        state = "neutral"
    if price_confirmed and bars[-1].close <= reference.resistance:
        result["counterevidence"].append("Latest spot price has fallen back to/below confirmed resistance.")
        state = "breakout_faded" if bars[-1].close >= reference.support else "support_lost"
    result["state"] = state
    reference_current = reference.expires_at is None or reference.expires_at > publication_time
    result["usable_for_live"] = mode == "observed" and fresh and clock_valid and reference_current
    if mode == "market_time_replay":
        result["publishable_state"] = "historical_replay"
    elif not clock_valid:
        result["publishable_state"] = "invalid_clock"
        result["counterevidence"].append("Declared generation/decision time is ahead of the actual clock.")
    elif not reference_current:
        result["publishable_state"] = "expired_reference"
        result["counterevidence"].append("Reference expired before publication; historical state is not current.")
    else:
        result["publishable_state"] = state if fresh else "stale_data"
    if not fresh:
        result["counterevidence"].append("Price is stale at generation time; refresh before publishing a current assessment.")
    return result
