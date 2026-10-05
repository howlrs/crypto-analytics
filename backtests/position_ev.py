#!/usr/bin/env python3
"""Historical expected value of a fully specified position plan.

A plan fixes venue, market, side, entry (market or resting limit), holding
horizon, optional stop/take barriers, leverage and costs before any outcome is
measured.  The plan is replayed at every decision time on a fixed UTC grid, so
the result is the empirical outcome distribution of *that rule*, not of a
hand-picked chart example.  Optional ``--where`` conditions select decisions by
causal features (known strictly before the decision bar opens) and are compared
against the unconditional baseline on the same grid.

The market database is opened read-only.  All bars used for an outcome,
including the exit open, must precede 2026-08-01 UTC, the research boundary
shared with the sealed prospective validation.  Missing bars or funding
coverage make an outcome unknown; they are never filled with zero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")
CUTOFF_EXCLUSIVE_TS = int(pd.Timestamp("2026-08-01T00:00:00Z").value // 1_000_000)
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
FEATURE_LOOKBACK_MIN = 30 * 24 * 60
# One-way fee assumptions agreed for this repository (bp); slippage is separate.
DEFAULT_FEES_BP = {"binance": (5.0, 2.0), "bybit": (5.5, 2.0), "hyperliquid": (4.5, 1.5)}
FEATURES = ("ret_4h_bp", "ret_24h_bp", "ret_7d_bp", "rv_24h_bp", "rv_ratio_24h_30d",
            "range_pos_24h", "funding_8h_bp")
CONDITION_RE = re.compile(r"^\s*([a-z0-9_]+)\s*(<=|>=|<|>)\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")
MIN_TRADES = 30
MIN_MONTHS = 6
OUTCOME_COLUMNS = ["status", "reason", "entry_ts", "entry_px", "path_mae_bp", "path_mfe_bp", "ambiguous"] + [
    column for label in ("pess", "opt")
    for column in (f"exit_kind_{label}", f"exit_ts_{label}", f"gross_{label}_bp", f"cost_{label}_bp",
                   f"funding_{label}_bp", f"settlements_{label}", f"net_{label}_bp", f"equity_{label}_pct")]
MARKET_COLUMNS = ["market_status", "market_net_pess_bp", "market_net_opt_bp"]


@dataclass(frozen=True)
class Condition:
    feature: str
    op: str
    value: float

    def text(self) -> str:
        return f"{self.feature}{self.op}{self.value!r}"

    def mask(self, frame: pd.DataFrame) -> pd.Series:
        x = frame[self.feature]
        result = {"<": x < self.value, "<=": x <= self.value, ">": x > self.value, ">=": x >= self.value}[self.op]
        return result & x.notna()


def parse_condition(text: str) -> Condition:
    match = CONDITION_RE.match(text)
    if not match:
        raise ValueError(f"invalid condition {text!r}; expected e.g. 'ret_24h_bp>=200'")
    feature, op, raw = match.groups()
    if feature not in FEATURES:
        raise ValueError(f"unknown feature {feature!r}; choose from {', '.join(FEATURES)}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"condition value must be finite: {text!r}")
    return Condition(feature, op, value)


@dataclass(frozen=True)
class PositionPlan:
    venue: str
    market: str
    symbol: str
    side: str
    horizon_min: int
    stop_bp: float | None = None
    take_bp: float | None = None
    entry_offset_bp: float = 0.0
    entry_window_min: int = 0
    fill_through_bp: float = 1.0
    leverage: float = 1.0
    maintenance_margin_rate: float = 0.005
    taker_fee_bp: float = 5.0
    maker_fee_bp: float = 2.0
    slippage_bp: float = 2.0
    funding_venue: str | None = None
    funding_symbol: str | None = None
    step_min: int = 60
    start_ms: int | None = None
    end_ms: int = CUTOFF_EXCLUSIVE_TS

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def limit_entry(self) -> bool:
        return self.entry_offset_bp > 0

    @property
    def max_exit_offset_min(self) -> int:
        """Latest exit-open bar relative to the decision bar, before any outcome."""
        return (self.entry_window_min - 1 if self.limit_entry else 0) + self.horizon_min

    def validate(self) -> None:
        if self.side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        if self.market not in {"perp", "spot"}:
            raise ValueError("market must be perp or spot")
        if self.market == "spot" and (self.side == "short" or self.leverage != 1.0):
            raise ValueError("spot plans must be unlevered longs")
        if self.horizon_min < 1 or self.step_min < 1:
            raise ValueError("horizon and step must be at least one minute")
        for name in ("stop_bp", "take_bp"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or not 0 < value < 10_000):
                raise ValueError(f"{name} must be finite and in (0, 10000)")
        numbers = (self.entry_offset_bp, self.fill_through_bp, self.leverage, self.maintenance_margin_rate,
                   self.taker_fee_bp, self.maker_fee_bp, self.slippage_bp)
        if not all(math.isfinite(x) for x in numbers):
            raise ValueError("plan values must be finite")
        if min(self.entry_offset_bp, self.fill_through_bp, self.taker_fee_bp, self.maker_fee_bp,
               self.slippage_bp) < 0:
            raise ValueError("offsets and costs must be non-negative")
        if self.entry_offset_bp >= 10_000:
            raise ValueError("entry_offset_bp must be below 10000")
        if self.limit_entry and self.entry_window_min < 1:
            raise ValueError("a limit entry needs a positive entry window")
        if not self.limit_entry and self.entry_window_min:
            raise ValueError("entry window is only valid with a positive entry offset")
        if not 1.0 <= self.leverage <= 100.0:
            raise ValueError("leverage must be in [1, 100]")
        if not 0 <= self.maintenance_margin_rate < 1.0 / self.leverage:
            raise ValueError("maintenance margin rate must be non-negative and below 1/leverage")
        if self.end_ms > CUTOFF_EXCLUSIVE_TS:
            raise ValueError("end may not extend past the 2026-08-01 UTC research boundary")
        if self.start_ms is not None and self.start_ms >= self.end_ms:
            raise ValueError("start must precede end")

    def liquidation_px(self, entry_px: float) -> float:
        """Isolated linear-margin liquidation price (fees ignored); nan when unreachable."""
        lev, mmr = self.leverage, self.maintenance_margin_rate
        if self.sign > 0:
            px = entry_px * (1 - 1 / lev) / (1 - mmr)
            return px if px > 0 else np.nan
        return entry_px * (1 + 1 / lev) / (1 + mmr)


# --------------------------------------------------------------------------- data


@dataclass
class Grid:
    """Contiguous one-minute OHLC arrays; missing minutes are NaN."""
    base_ts: int
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    def __len__(self) -> int:
        return len(self.open)

    def index(self, ts: int) -> int:
        return (int(ts) - self.base_ts) // MINUTE_MS

    def ts(self, index: int) -> int:
        return self.base_ts + int(index) * MINUTE_MS

    def missing_between(self, first: int, last: int) -> int:
        """Number of missing or invalid minutes in ``[first, last]`` (indices)."""
        if not hasattr(self, "_missing_cum"):
            finite = np.isfinite(self.open) & np.isfinite(self.high) & np.isfinite(self.low)
            self._missing_cum = np.concatenate([[0], np.cumsum(~finite)])
        return int(self._missing_cum[last + 1] - self._missing_cum[first])


def build_grid(bars: pd.DataFrame) -> Grid:
    if bars.empty:
        raise ValueError("no bars for the requested market")
    ts = bars.ts.to_numpy(dtype=np.int64)
    if (ts % MINUTE_MS).any() or (np.diff(ts) <= 0).any():
        raise ValueError("bars must be unique, sorted and minute aligned")
    base = int(ts[0])
    size = int((ts[-1] - base) // MINUTE_MS) + 1
    out = {}
    for col in ("open", "high", "low", "close"):
        arr = np.full(size, np.nan)
        arr[(ts - base) // MINUTE_MS] = bars[col].to_numpy(dtype=float)
        out[col] = arr
    o, h, l, c = out["open"], out["high"], out["low"], out["close"]
    finite = np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    with np.errstate(invalid="ignore"):
        bad = finite & ((np.minimum(o, c) < l) | (np.maximum(o, c) > h) | (l <= 0))
    # Invalid OHLC rows are treated as missing so no outcome is built on them.
    for arr in (o, h, l, c):
        arr[bad] = np.nan
    return Grid(base, o, h, l, c)


@dataclass
class FundingSeries:
    ts: np.ndarray
    rate: np.ndarray
    interval_ms: np.ndarray
    cum_bad_gap: np.ndarray

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> "FundingSeries":
        frame = frame.dropna(subset=["ts", "rate"]).sort_values("ts")
        ts = frame.ts.to_numpy(dtype=np.int64)
        rate = frame.rate.to_numpy(dtype=float)
        interval = frame.interval_hours.to_numpy(dtype=float) * HOUR_MS
        bad = np.zeros(len(ts), dtype=bool)
        if len(ts) > 1:
            gaps = np.diff(ts).astype(float)
            later = interval[1:]
            bad[1:] = ~np.isfinite(later) | (later <= 0) | (gaps > 1.5 * later) | (gaps <= 0)
        bad |= ~np.isfinite(rate)
        return cls(ts, rate, interval, np.cumsum(bad))

    def latest_rate_8h_bp(self, decision_ts: np.ndarray) -> np.ndarray:
        """Last *settled* rate at or before each decision, normalised to 8 hours."""
        out = np.full(len(decision_ts), np.nan)
        if not len(self.ts):
            return out
        pos = np.searchsorted(self.ts, decision_ts, side="right") - 1
        ok = pos >= 0
        idx = pos[ok]
        fresh = decision_ts[ok] - self.ts[idx] <= 1.5 * self.interval_ms[idx]
        values = self.rate[idx] * 10_000 * (8 * HOUR_MS) / self.interval_ms[idx]
        out[np.flatnonzero(ok)[fresh]] = values[fresh]
        return out

    def coverage(self, start_ts: int, end_ts: int) -> str:
        """Empty when every settlement due in ``[start_ts, end_ts)`` is known.

        Requires a settlement before the window and no gap above 1.5 intervals
        through it.  After the last loaded row (rows at/after the run's end bound
        are never loaded) the window is covered only if it closes within one
        interval, i.e. before the next scheduled settlement can fall due.
        """
        prior = int(np.searchsorted(self.ts, start_ts, side="left")) - 1
        after = int(np.searchsorted(self.ts, end_ts, side="left"))
        if prior < 0:
            return "funding_out_of_range"
        if after >= len(self.ts):
            if end_ts - self.ts[-1] > self.interval_ms[-1]:
                return "funding_out_of_range"
            after = len(self.ts) - 1
        if self.cum_bad_gap[after] - self.cum_bad_gap[prior]:
            return "funding_gap"
        return ""

    def held(self, start_ts: int, end_ts: int) -> np.ndarray:
        """Indices of settlements inside ``[start_ts, end_ts)``."""
        return np.arange(int(np.searchsorted(self.ts, start_ts, side="left")),
                         int(np.searchsorted(self.ts, end_ts, side="left")))


def connect_read_only(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def default_funding_symbol(venue: str, symbol: str) -> str:
    if venue == "hyperliquid":
        return symbol.removesuffix("USDT").removesuffix("USDC")
    return symbol


def load_inputs(conn: sqlite3.Connection, plan: PositionPlan) -> tuple[Grid, FundingSeries | None]:
    lower = None if plan.start_ms is None else plan.start_ms - (FEATURE_LOOKBACK_MIN + 60) * MINUTE_MS
    where, params = "venue=? AND market=? AND symbol=? AND ts<?", [plan.venue, plan.market, plan.symbol, plan.end_ms]
    if lower is not None:
        where += " AND ts>=?"
        params.append(lower)
    bars = pd.read_sql_query(f"SELECT ts, open, high, low, close FROM klines WHERE {where} ORDER BY ts",
                             conn, params=params)
    if bars.empty:
        available = conn.execute("SELECT DISTINCT venue, market, symbol FROM klines ORDER BY 1, 2, 3").fetchall()
        raise ValueError(f"no klines for {plan.venue}/{plan.market}/{plan.symbol}; available: {available}")
    grid = build_grid(bars)
    funding = None
    if plan.funding_venue:
        frame = pd.read_sql_query(
            "SELECT ts, rate, interval_hours FROM funding WHERE venue=? AND symbol=? AND ts<? ORDER BY ts",
            conn, params=(plan.funding_venue, plan.funding_symbol, plan.end_ms))
        if frame.empty:
            raise ValueError(f"no funding for {plan.funding_venue}/{plan.funding_symbol}")
        funding = FundingSeries.from_frame(frame)
    return grid, funding


# ----------------------------------------------------------------------- features


def compute_features(grid: Grid, decision_idx: np.ndarray, funding: FundingSeries | None) -> pd.DataFrame:
    """Features at each decision bar using only bars that closed before it opened."""
    c = pd.Series(grid.close)
    prev = decision_idx - 1
    out = {}
    last = grid.close[prev] if len(prev) else np.array([])

    def lagged(minutes: int) -> np.ndarray:
        idx = prev - minutes
        res = np.full(len(idx), np.nan)
        ok = idx >= 0
        res[ok] = grid.close[idx[ok]]
        return res

    with np.errstate(invalid="ignore", divide="ignore"):
        for name, minutes in (("ret_4h_bp", 240), ("ret_24h_bp", 1440), ("ret_7d_bp", 10_080)):
            out[name] = (last / lagged(minutes) - 1) * 10_000
        lr60_sq = np.log(c / c.shift(60)) ** 2
        var_24h = lr60_sq.rolling(1440, min_periods=1440).mean().to_numpy()
        var_30d = lr60_sq.rolling(43_200, min_periods=43_200).mean().to_numpy()
        hi = pd.Series(grid.high).rolling(1440, min_periods=1440).max().to_numpy()
        lo = pd.Series(grid.low).rolling(1440, min_periods=1440).min().to_numpy()
        valid = prev >= 0
        take = lambda arr: np.where(valid, arr[np.clip(prev, 0, None)], np.nan)
        rv_24h = np.sqrt(24 * take(var_24h)) * 10_000
        rv_30d = np.sqrt(24 * take(var_30d)) * 10_000
        out["rv_24h_bp"] = rv_24h
        out["rv_ratio_24h_30d"] = rv_24h / rv_30d
        span = take(hi) - take(lo)
        out["range_pos_24h"] = np.where(span > 0, (last - take(lo)) / span, np.nan)
    decision_ts = grid.base_ts + decision_idx.astype(np.int64) * MINUTE_MS
    out["funding_8h_bp"] = (funding.latest_rate_8h_bp(decision_ts) if funding is not None
                            else np.full(len(decision_idx), np.nan))
    frame = pd.DataFrame(out, columns=list(FEATURES))
    return frame.replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------------- simulation


def simulate_path(grid: Grid, start: int, horizon: int, *, sign: int, entry_px: float, adverse_px: float,
                  take_px: float, partial_first: bool) -> dict:
    """Replay bars ``[start, start+horizon)`` and exit at the open of ``start+horizon``.

    ``partial_first`` marks a limit-fill bar: its open precedes the fill and
    its favourable extreme may too, so a favourable touch there is uncertain.
    Its adverse extreme is certain because price passed the limit to reach it.
    One-minute OHLC cannot order high and low; such bars are bracketed by a
    pessimistic (adverse first) and an optimistic (favourable first) outcome.
    """
    end = start + horizon
    if start < 0 or end >= len(grid):
        return {"reason": "path_out_of_range"}
    po, ph, pl = grid.open[start:end], grid.high[start:end], grid.low[start:end]
    exit_open = grid.open[end]
    if not (np.isfinite(po).all() and np.isfinite(ph).all() and np.isfinite(pl).all() and np.isfinite(exit_open)):
        return {"reason": "missing_path_bar"}
    with np.errstate(invalid="ignore"):
        if sign > 0:
            adv_open, fav_open = po <= adverse_px, po >= take_px
            adv_touch, fav_touch = pl <= adverse_px, ph >= take_px
            exit_adv, exit_fav = exit_open <= adverse_px, exit_open >= take_px
            adverse, favorable = pl / entry_px - 1, ph / entry_px - 1
        else:
            adv_open, fav_open = po >= adverse_px, po <= take_px
            adv_touch, fav_touch = ph >= adverse_px, pl <= take_px
            exit_adv, exit_fav = exit_open >= adverse_px, exit_open <= take_px
            adverse, favorable = -(ph / entry_px - 1), -(pl / entry_px - 1)
    fav_uncertain = np.zeros(len(po), dtype=bool)
    if partial_first:
        fav_open[0] = False
        fav_uncertain[0], fav_touch[0] = fav_touch[0], False
        favorable = favorable.copy()
        favorable[0] = 0.0
    terminal = sign * (exit_open / entry_px - 1)
    mae = min(0.0, float(adverse.min()), terminal)
    mfe = max(0.0, float(favorable.max()), terminal)

    def first(mask: np.ndarray) -> int:
        i = int(mask.argmax()) if len(mask) else 0
        return i if len(mask) and mask[i] else -1

    def resolve(i: int, optimistic: bool) -> tuple[str, int, float, bool]:
        """Return kind, absolute bar, raw exit price and whether the exit is at the bar open."""
        if i < 0:
            if exit_adv:
                return "adverse", end, float(exit_open), True
            if exit_fav:
                return "take", end, float(take_px), True
            return "time", end, float(exit_open), True
        if adv_open[i]:
            return "adverse", start + i, float(po[i]), True
        if fav_open[i]:
            # A resting take-profit limit fills at its own price on a gap.
            return "take", start + i, float(take_px), True
        fav = fav_touch[i] or (optimistic and fav_uncertain[i])
        if adv_touch[i] and not (optimistic and fav):
            return "adverse", start + i, float(adverse_px), False
        return "take", start + i, float(take_px), False

    events = adv_open | adv_touch | fav_open | fav_touch
    pess = resolve(first(events), False)
    opt = resolve(first(events | fav_uncertain), True)
    return {"reason": "", "pess": pess, "opt": opt, "path_mae_bp": 10_000 * mae, "path_mfe_bp": 10_000 * mfe}


def find_limit_fill(grid: Grid, start: int, window: int, *, sign: int, limit_px: float,
                    through_bp: float) -> tuple[int, str]:
    """First bar in ``[start, start+window)`` trading through the limit, else unfilled/unknown."""
    end = start + window
    if end > len(grid):
        return -1, "window_out_of_range"
    if sign > 0:
        touch = grid.low[start:end] <= limit_px * (1 - through_bp / 10_000)
    else:
        touch = grid.high[start:end] >= limit_px * (1 + through_bp / 10_000)
    missing = ~np.isfinite(grid.low[start:end]) | ~np.isfinite(grid.high[start:end])
    hit = int(touch.argmax()) if touch.any() else window
    if missing[:hit].any() or (hit < window and missing[hit]):
        return -1, "missing_window_bar"
    return (start + hit, "") if hit < window else (-1, "unfilled")


def evaluate_decision(grid: Grid, funding: FundingSeries | None, plan: PositionPlan, decision: int,
                      *, limit: bool) -> dict:
    """Outcome of one decision; ``limit=False`` replays the market-entry reference."""
    sign = plan.sign
    # Data sufficiency is decided for the whole planned window before any
    # outcome is known, so exits cannot select which decisions are "known".
    last = decision + plan.max_exit_offset_min
    if last >= len(grid):
        return {"status": "unknown", "reason": "path_out_of_range"}
    if grid.missing_between(decision, last):
        return {"status": "unknown", "reason": "missing_bar_in_plan_window"}
    if funding is not None:
        reason = funding.coverage(grid.ts(decision), grid.ts(last))
        if reason:
            return {"status": "unknown", "reason": reason}
    ref_px = grid.open[decision]
    if limit:
        entry_px = ref_px * (1 - sign * plan.entry_offset_bp / 10_000)
        fill, reason = find_limit_fill(grid, decision, plan.entry_window_min, sign=sign, limit_px=entry_px,
                                       through_bp=plan.fill_through_bp)
        if reason == "unfilled":
            return {"status": "unfilled", "reason": "unfilled"}
        if reason:
            return {"status": "unknown", "reason": reason}
        start, entry_cost = fill, plan.maker_fee_bp
        # Resting fill time inside the bar is unknown; settlements in that bar are not charged.
        held_from = grid.ts(fill) + MINUTE_MS
    else:
        entry_px, start, entry_cost = ref_px, decision, plan.taker_fee_bp + plan.slippage_bp
        held_from = grid.ts(decision)
    stop_px = np.nan if plan.stop_bp is None else entry_px * (1 - sign * plan.stop_bp / 10_000)
    take_px = np.nan if plan.take_bp is None else entry_px * (1 + sign * plan.take_bp / 10_000)
    liq_px = plan.liquidation_px(entry_px)
    # The nearer adverse level triggers first; equality is treated as liquidation.
    if np.isfinite(liq_px) and (not np.isfinite(stop_px) or sign * (liq_px - stop_px) >= 0):
        adverse_px, adverse_kind = liq_px, "liquidation"
    else:
        adverse_px, adverse_kind = stop_px, "stop"
    path = simulate_path(grid, start, plan.horizon_min, sign=sign, entry_px=entry_px, adverse_px=adverse_px,
                         take_px=take_px, partial_first=limit)
    if path["reason"]:
        return {"status": "unknown", "reason": path["reason"]}
    row = {"status": "complete", "reason": "", "entry_ts": grid.ts(start), "entry_px": entry_px,
           "path_mae_bp": path["path_mae_bp"], "path_mfe_bp": path["path_mfe_bp"]}
    for label in ("pess", "opt"):
        kind, bar, exit_px, at_open = path[label]
        if kind == "adverse":
            # A gap opening beyond the liquidation price liquidates even when the stop was nearer.
            beyond_liq = np.isfinite(liq_px) and sign * (exit_px - liq_px) <= 0
            kind = "liquidation" if beyond_liq else adverse_kind
        exit_ts = grid.ts(bar) + (0 if at_open else MINUTE_MS)
        funding_bp, n_settle = 0.0, 0
        if funding is not None and kind != "liquidation":
            held = funding.held(held_from, exit_ts)
            if len(held):
                marks = grid.open[[grid.index(t - t % MINUTE_MS) for t in funding.ts[held]]]
                if not np.isfinite(marks).all():
                    return {"status": "unknown", "reason": "funding_mark_missing"}
                funding_bp = float(sign * (funding.rate[held] * marks).sum() / entry_px * 10_000)
                n_settle = len(held)
        if kind == "liquidation":
            # Isolated margin is assumed fully lost; costs beyond the entry are not modelled.
            gross_bp, exit_cost = -10_000 / plan.leverage, 0.0
        else:
            gross_bp = sign * (exit_px / entry_px - 1) * 10_000
            exit_cost = plan.maker_fee_bp if kind == "take" else plan.taker_fee_bp + plan.slippage_bp
        net_bp = gross_bp - entry_cost - exit_cost - funding_bp
        row.update({f"exit_kind_{label}": kind, f"exit_ts_{label}": exit_ts, f"gross_{label}_bp": gross_bp,
                    f"cost_{label}_bp": entry_cost + exit_cost, f"funding_{label}_bp": funding_bp,
                    f"settlements_{label}": n_settle, f"net_{label}_bp": net_bp,
                    f"equity_{label}_pct": max(-100.0, plan.leverage * net_bp / 100)})
    # Any divergence between the bracketing replays (same-bar high/low order or
    # an uncertain favourable touch in a limit-fill bar) is reported as ambiguous.
    row["ambiguous"] = path["pess"][:2] != path["opt"][:2]
    return row


def decision_indices(grid: Grid, plan: PositionPlan) -> np.ndarray:
    """Grid decisions whose latest possible exit open precedes ``end_ms``."""
    step = plan.step_min * MINUTE_MS
    lo = grid.base_ts + FEATURE_LOOKBACK_MIN * MINUTE_MS if plan.start_ms is None else plan.start_ms
    lo = max(lo, grid.base_ts + MINUTE_MS)
    first = -(-lo // step) * step
    last_exit_open = min(plan.end_ms - MINUTE_MS, grid.ts(len(grid) - 1))
    last = last_exit_open - plan.max_exit_offset_min * MINUTE_MS
    if last < first:
        return np.array([], dtype=np.int64)
    ts = np.arange(first, last + 1, step, dtype=np.int64)
    return (ts - grid.base_ts) // MINUTE_MS


def build_ledger(grid: Grid, funding: FundingSeries | None, plan: PositionPlan,
                 conditions: Sequence[Condition]) -> pd.DataFrame:
    idx = decision_indices(grid, plan)
    if not len(idx):
        raise ValueError("no decision has a complete feature lookback and exit before the end bound")
    features = compute_features(grid, idx, funding)
    rows = []
    for decision in idx:
        row = evaluate_decision(grid, funding, plan, int(decision), limit=plan.limit_entry)
        if plan.limit_entry:
            ref = evaluate_decision(grid, funding, plan, int(decision), limit=False)
            row["market_status"] = ref["status"]
            row["market_net_pess_bp"] = ref.get("net_pess_bp", np.nan)
            row["market_net_opt_bp"] = ref.get("net_opt_bp", np.nan)
        rows.append(row)
    ledger = pd.DataFrame(rows).reindex(columns=OUTCOME_COLUMNS + (MARKET_COLUMNS if plan.limit_entry else []))
    ledger.insert(0, "decision_ts", grid.base_ts + idx.astype(np.int64) * MINUTE_MS)
    ledger.insert(1, "decision_utc", pd.to_datetime(ledger.decision_ts, unit="ms", utc=True)
                  .dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    ledger.insert(2, "month", pd.to_datetime(ledger.decision_ts, unit="ms", utc=True).dt.strftime("%Y-%m"))
    ledger = pd.concat([ledger, features.reset_index(drop=True)], axis=1)
    used = sorted({c.feature for c in conditions})
    ledger["features_known"] = ledger[used].notna().all(axis=1) if used else True
    matched = ledger.features_known.copy()
    for condition in conditions:
        matched &= condition.mask(ledger)
    ledger["matched"] = matched
    # Per-decision value: an unfilled limit earns zero; unknown outcomes stay missing.
    ledger["decision_net_pess_bp"] = np.where(ledger.status.eq("unfilled"), 0.0, ledger.net_pess_bp)
    ledger["decision_net_opt_bp"] = np.where(ledger.status.eq("unfilled"), 0.0, ledger.net_opt_bp)
    return ledger


# ---------------------------------------------------------------------- statistics


def stable_seed(*parts: object, base_seed: int = 0) -> int:
    payload = "|".join(map(str, (base_seed, *parts))).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little") % (2**32)


def month_bootstrap(values: np.ndarray, months: np.ndarray, samples: int, seed: int) -> tuple[float, float]:
    ok = np.isfinite(values)
    values, months = values[ok], months[ok]
    if len(values) < 2 or samples < 1:
        return np.nan, np.nan
    labels, inverse = np.unique(months, return_inverse=True)
    sums = np.bincount(inverse, weights=values)
    counts = np.bincount(inverse).astype(float)
    pick = np.random.default_rng(seed).integers(0, len(labels), size=(samples, len(labels)))
    draws = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))


def delta_bootstrap(base: pd.DataFrame, cond: pd.DataFrame, column: str, samples: int,
                    seed: int) -> tuple[float, float, int]:
    """Joint month-cluster bootstrap of ``mean(cond) - mean(base)``; cond is a subset of base."""
    b = base[np.isfinite(base[column])]
    c = cond[np.isfinite(cond[column])]
    labels = np.unique(b.month.to_numpy())
    if len(c) < 2 or len(labels) < 2 or samples < 1:
        return np.nan, np.nan, 0
    position = {m: i for i, m in enumerate(labels)}
    bi = b.month.map(position).to_numpy()
    ci = c.month.map(position).to_numpy()
    b_sum = np.bincount(bi, weights=b[column].to_numpy(), minlength=len(labels))
    b_cnt = np.bincount(bi, minlength=len(labels)).astype(float)
    c_sum = np.bincount(ci, weights=c[column].to_numpy(), minlength=len(labels))
    c_cnt = np.bincount(ci, minlength=len(labels)).astype(float)
    pick = np.random.default_rng(seed).integers(0, len(labels), size=(samples, len(labels)))
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = c_sum[pick].sum(axis=1) / c_cnt[pick].sum(axis=1) - b_sum[pick].sum(axis=1) / b_cnt[pick].sum(axis=1)
    draws = draws[np.isfinite(draws)]
    if not len(draws):
        return np.nan, np.nan, samples
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975)), samples - len(draws)


def non_overlapping(trades: pd.DataFrame) -> pd.DataFrame:
    """Greedy chronological half-open purge on realised (pessimistic) holding intervals."""
    if trades.empty:
        return trades
    ordered = trades.sort_values(["entry_ts", "decision_ts"], kind="stable")
    keep, last_exit = [], -np.inf
    for entry, exit_ in zip(ordered.entry_ts.to_numpy(), ordered.exit_ts_pess.to_numpy()):
        keep.append(entry >= last_exit)
        if keep[-1]:
            last_exit = exit_
    return ordered[np.asarray(keep)]


def ev_status(ci_low: float, ci_high: float, trades: int, months: int) -> str:
    if trades < MIN_TRADES or months < MIN_MONTHS or not np.isfinite(ci_low):
        return "insufficient_sample"
    if ci_low > 0:
        return "ci_above_zero"
    if ci_high < 0:
        return "ci_below_zero"
    return "ci_includes_zero"


def summarize(subset: pd.DataFrame, name: str, plan: PositionPlan, samples: int, seed: int,
              notional_usd: float | None) -> dict:
    known = subset[subset.status.isin(["complete", "unfilled"])]
    trades = subset[subset.status.eq("complete")]
    n_known, n_trades = len(known), len(trades)
    months = int(known.month.nunique())
    row: dict[str, object] = {
        "subset": name, "decisions": len(subset), "known_decisions": n_known,
        "unknown_decisions": int(subset.status.eq("unknown").sum()), "trades": n_trades,
        "unfilled": int(subset.status.eq("unfilled").sum()), "active_months": months,
        "fill_rate": n_trades / n_known if n_known else np.nan,
    }
    per_decision = known.decision_net_pess_bp.to_numpy(float)
    lo, hi = month_bootstrap(per_decision, known.month.to_numpy(), samples, stable_seed(name, "decision", base_seed=seed))
    row.update({"ev_per_decision_pess_bp": float(per_decision.mean()) if n_known else np.nan,
                "ev_per_decision_opt_bp": float(known.decision_net_opt_bp.mean()) if n_known else np.nan,
                "ev_ci_low_bp": lo, "ev_ci_high_bp": hi,
                "ev_status": ev_status(lo, hi, n_trades, months)})
    if notional_usd is not None:
        row["ev_per_decision_pess_usd"] = row["ev_per_decision_pess_bp"] * notional_usd / 10_000
    if n_trades:
        net = trades.net_pess_bp.to_numpy(float)
        wins, losses = net[net > 0], net[net <= 0]
        avg_win = float(wins.mean()) if len(wins) else np.nan
        avg_loss = float(-losses.mean()) if len(losses) else np.nan
        tail = np.sort(net)[:max(1, int(math.ceil(0.05 * len(net))))]
        kinds = trades.exit_kind_pess
        purged = non_overlapping(trades)
        row.update({
            "ev_per_trade_pess_bp": float(net.mean()), "ev_per_trade_opt_bp": float(trades.net_opt_bp.mean()),
            "median_net_bp": float(np.median(net)), "std_net_bp": float(net.std(ddof=1)) if n_trades > 1 else np.nan,
            "win_rate": float((net > 0).mean()), "avg_win_bp": avg_win, "avg_loss_bp": avg_loss,
            "payoff_ratio": avg_win / avg_loss if avg_loss and np.isfinite(avg_win) else np.nan,
            "breakeven_win_rate": avg_loss / (avg_win + avg_loss) if np.isfinite(avg_win) and np.isfinite(avg_loss) else np.nan,
            "profit_factor": (float(wins.sum() / -losses.sum()) if losses.sum() < 0
                              else (np.inf if len(wins) else np.nan)),
            "p05_net_bp": float(np.quantile(net, 0.05)), "cvar05_net_bp": float(tail.mean()),
            "take_rate": float(kinds.eq("take").mean()), "stop_rate": float(kinds.eq("stop").mean()),
            "time_exit_rate": float(kinds.eq("time").mean()), "liquidation_rate": float(kinds.eq("liquidation").mean()),
            "ambiguous_rate": float(trades.ambiguous.astype(bool).mean()),
            "mean_cost_bp": float(trades.cost_pess_bp.mean()), "mean_funding_bp": float(trades.funding_pess_bp.mean()),
            "mean_hold_hours": float(((trades.exit_ts_pess - trades.entry_ts) / HOUR_MS).mean()),
            "path_mae_p10_bp": float(trades.path_mae_bp.quantile(0.10)),
            "path_mae_p50_bp": float(trades.path_mae_bp.quantile(0.50)),
            "path_mfe_p50_bp": float(trades.path_mfe_bp.quantile(0.50)),
            "equity_mean_pct": float(trades.equity_pess_pct.mean()),
            "equity_p05_pct": float(trades.equity_pess_pct.quantile(0.05)),
            "nonoverlap_trades": len(purged), "nonoverlap_ev_pess_bp": float(purged.net_pess_bp.mean()),
        })
    if plan.limit_entry:
        market = subset[subset.market_status.eq("complete")]
        filled = market[market.status.eq("complete")]
        missed = market[market.status.eq("unfilled")]
        row.update({"market_ev_per_decision_pess_bp": float(market.market_net_pess_bp.mean()) if len(market) else np.nan,
                    "market_ev_when_filled_bp": float(filled.market_net_pess_bp.mean()) if len(filled) else np.nan,
                    "market_ev_when_unfilled_bp": float(missed.market_net_pess_bp.mean()) if len(missed) else np.nan})
    return row


def summarize_all(ledger: pd.DataFrame, plan: PositionPlan, conditions: Sequence[Condition], samples: int,
                  seed: int, notional_usd: float | None) -> pd.DataFrame:
    population = ledger[ledger.features_known]
    rows = [summarize(population, "baseline", plan, samples, seed, notional_usd)]
    if conditions:
        matched = population[population.matched]
        rows.append(summarize(matched, "conditional", plan, samples, seed, notional_usd))
        known = population.status.isin(["complete", "unfilled"])
        base, cond = population[known], matched[matched.status.isin(["complete", "unfilled"])]
        lo, hi, dropped = delta_bootstrap(base, cond, "decision_net_pess_bp", samples,
                                          stable_seed("delta", base_seed=seed))
        delta = rows[1]["ev_per_decision_pess_bp"] - rows[0]["ev_per_decision_pess_bp"]
        rows.append({"subset": "conditional_minus_baseline", "decisions": len(matched),
                     "known_decisions": len(cond), "trades": rows[1]["trades"],
                     "active_months": rows[1]["active_months"], "ev_per_decision_pess_bp": delta,
                     "ev_ci_low_bp": lo, "ev_ci_high_bp": hi, "bootstrap_draws_without_condition": dropped,
                     "ev_status": ev_status(lo, hi, int(rows[1]["trades"]), int(rows[1]["active_months"]))})
    return pd.DataFrame(rows)


def by_year(ledger: pd.DataFrame, conditions: Sequence[Condition]) -> pd.DataFrame:
    population = ledger[ledger.features_known].copy()
    population["year"] = population.month.str[:4]
    subsets = [("baseline", population)]
    if conditions:
        subsets.append(("conditional", population[population.matched]))
    rows = []
    for name, frame in subsets:
        for year, part in frame.groupby("year", sort=True):
            known = part[part.status.isin(["complete", "unfilled"])]
            trades = part[part.status.eq("complete")]
            kinds = trades.exit_kind_pess if len(trades) else pd.Series(dtype=str)
            rows.append({"subset": name, "year": year, "decisions": len(part), "known_decisions": len(known),
                         "trades": len(trades),
                         "ev_per_decision_pess_bp": float(known.decision_net_pess_bp.mean()) if len(known) else np.nan,
                         "ev_per_decision_opt_bp": float(known.decision_net_opt_bp.mean()) if len(known) else np.nan,
                         "win_rate": float((trades.net_pess_bp > 0).mean()) if len(trades) else np.nan,
                         "take_rate": float(kinds.eq("take").mean()) if len(trades) else np.nan,
                         "stop_rate": float(kinds.eq("stop").mean()) if len(trades) else np.nan})
    return pd.DataFrame(rows, columns=["subset", "year", "decisions", "known_decisions", "trades",
                                       "ev_per_decision_pess_bp", "ev_per_decision_opt_bp", "win_rate",
                                       "take_rate", "stop_rate"])


def feature_quantiles(ledger: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name in FEATURES:
        x = ledger[name].dropna()
        row = {"feature": name, "count": len(x)}
        for q in (0.05, 0.25, 0.5, 0.75, 0.95):
            row[f"p{int(q * 100):02d}"] = float(x.quantile(q)) if len(x) else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- I/O


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _db_state(db: Path) -> dict[str, int]:
    stat = db.stat()
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def run(db: Path, output_dir: Path, plan: PositionPlan, conditions: Sequence[Condition], *,
        samples: int = 2_000, seed: int = 20261006, notional_usd: float | None = None) -> pd.DataFrame:
    plan.validate()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("--output-dir must be new or empty")
    before = _db_state(db)
    with connect_read_only(db) as conn:
        grid, funding = load_inputs(conn, plan)
    ledger = build_ledger(grid, funding, plan, conditions)
    summary = summarize_all(ledger, plan, conditions, samples, seed, notional_usd)
    yearly = by_year(ledger, conditions)
    quantiles = feature_quantiles(ledger[ledger.features_known])
    if _db_state(db) != before:
        raise RuntimeError("market database changed during the run; results discarded")
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {"position_ledger.csv": ledger, "position_summary.csv": summary,
               "position_by_year.csv": yearly, "position_feature_quantiles.csv": quantiles}
    for name, frame in outputs.items():
        frame.to_csv(output_dir / name, index=False)
    manifest = {
        "format": 1, "tool": "backtests/position_ev.py", "script_sha256": _sha256(Path(__file__)),
        "plan": asdict(plan), "conditions": [c.text() for c in conditions],
        "cutoff_exclusive_ts": CUTOFF_EXCLUSIVE_TS, "db": str(db), "db_state": before,
        "bars": {"first_ts": grid.base_ts, "last_ts": grid.ts(len(grid) - 1),
                 "missing_minutes": int((~np.isfinite(grid.open)).sum())},
        "decisions": {"first_ts": int(ledger.decision_ts.min()) if len(ledger) else None,
                      "last_ts": int(ledger.decision_ts.max()) if len(ledger) else None,
                      "count": len(ledger)},
        "bootstrap": {"unit": "UTC month of decision", "samples": samples, "seed": seed},
        "interpretation": ("Historical replay of a fixed rule on an overlapping decision grid. "
                           "Not a forecast; conditions chosen after viewing results are exploratory."),
        "output_sha256": {name: _sha256(output_dir / name) for name in outputs},
    }
    (output_dir / "position_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                        encoding="utf-8")
    return summary


def parse_time(value: str) -> int:
    """Parse epoch milliseconds (12+ digits) or an ISO time; naive times are UTC."""
    text = value.strip()
    if text.isdigit() and len(text) >= 12:
        return int(text)
    try:
        stamp = pd.Timestamp(text)
    except ValueError as exc:
        raise ValueError(f"invalid time {value!r}") from exc
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return int(stamp.value // 1_000_000)


def _minutes(hours: float, name: str) -> int:
    minutes = hours * 60
    if not math.isfinite(minutes) or minutes < 0 or abs(minutes - round(minutes)) > 1e-9:
        raise ValueError(f"{name} must be a non-negative whole number of minutes")
    return int(round(minutes))


def plan_from_args(args: argparse.Namespace) -> PositionPlan:
    taker, maker = DEFAULT_FEES_BP.get(args.venue, (5.0, 2.0))
    if args.funding_venue == "none" or args.market == "spot":
        funding_venue = funding_symbol = None
        if args.market == "spot" and args.funding_venue not in (None, "none"):
            raise ValueError("spot positions do not pay funding; omit --funding-venue")
    else:
        funding_venue = args.funding_venue or args.venue
        funding_symbol = args.funding_symbol or default_funding_symbol(funding_venue, args.symbol)
    return PositionPlan(
        venue=args.venue, market=args.market, symbol=args.symbol.upper(), side=args.side,
        horizon_min=_minutes(args.horizon_hours, "--horizon-hours"), stop_bp=args.stop_bp, take_bp=args.take_bp,
        entry_offset_bp=args.entry_offset_bp, entry_window_min=_minutes(args.entry_window_hours, "--entry-window-hours"),
        fill_through_bp=args.fill_through_bp, leverage=args.leverage,
        maintenance_margin_rate=args.maintenance_margin_rate,
        taker_fee_bp=taker if args.taker_fee_bp is None else args.taker_fee_bp,
        maker_fee_bp=maker if args.maker_fee_bp is None else args.maker_fee_bp,
        slippage_bp=args.slippage_bp, funding_venue=funding_venue, funding_symbol=funding_symbol,
        step_min=args.step_minutes, start_ms=None if args.start is None else parse_time(args.start),
        end_ms=CUTOFF_EXCLUSIVE_TS if args.end is None else parse_time(args.end))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Historical expected value of a fixed position plan.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--venue", required=True, choices=sorted(DEFAULT_FEES_BP))
    p.add_argument("--market", default="perp", choices=["perp", "spot"])
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["long", "short"])
    p.add_argument("--horizon-hours", type=float, required=True)
    p.add_argument("--stop-bp", type=float)
    p.add_argument("--take-bp", type=float)
    p.add_argument("--entry-offset-bp", type=float, default=0.0,
                   help="resting limit distance from the decision open; 0 means market entry")
    p.add_argument("--entry-window-hours", type=float, default=0.0)
    p.add_argument("--fill-through-bp", type=float, default=1.0,
                   help="price must trade this far through the limit before a fill is assumed")
    p.add_argument("--leverage", type=float, default=1.0)
    p.add_argument("--maintenance-margin-rate", type=float, default=0.005)
    p.add_argument("--taker-fee-bp", type=float)
    p.add_argument("--maker-fee-bp", type=float)
    p.add_argument("--slippage-bp", type=float, default=2.0)
    p.add_argument("--funding-venue", choices=sorted(DEFAULT_FEES_BP) + ["none"])
    p.add_argument("--funding-symbol")
    p.add_argument("--where", action="append", default=[], help="causal condition, e.g. 'ret_24h_bp>=200'")
    p.add_argument("--start")
    p.add_argument("--end", help="exclusive bound on exits; may not exceed 2026-08-01 UTC")
    p.add_argument("--step-minutes", type=int, default=60)
    p.add_argument("--notional-usd", type=float)
    p.add_argument("--bootstrap-samples", type=int, default=2_000)
    p.add_argument("--seed", type=int, default=20261006)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = plan_from_args(args)
        conditions = [parse_condition(text) for text in args.where]
        if args.notional_usd is not None and not (math.isfinite(args.notional_usd) and args.notional_usd > 0):
            raise ValueError("--notional-usd must be positive")
        summary = run(args.db, args.output_dir, plan, conditions, samples=args.bootstrap_samples,
                      seed=args.seed, notional_usd=args.notional_usd)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")
    columns = ["subset", "decisions", "trades", "fill_rate", "ev_per_decision_pess_bp", "ev_per_decision_opt_bp",
               "ev_ci_low_bp", "ev_ci_high_bp", "win_rate", "stop_rate", "take_rate", "ev_status"]
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(summary.reindex(columns=columns).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"output={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
