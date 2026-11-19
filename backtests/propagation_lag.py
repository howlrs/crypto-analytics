#!/usr/bin/env python3
"""Exploratory event study: after a sharp BTC move, what is left for a follower once it can be traded?

Hypothesis H-010 (research_loop/hypotheses.json): a sharp move in the leader reaches thinner instruments
late. The payer would be whoever still quotes the follower at stale prices. This script measures, on
history before the research boundary only, how much of the follower's move remains after the first price
at which it could actually be bought or sold, and whether that covers a round trip.

Design fixed before looking at any outcome (docs/propagation-lag.md):

* Event: the leader's close-to-close return over ``window_minutes`` reaches +/- a threshold. The close of
  bar ``t`` is observable at ``t + 60s``; that is the decision time. After an event, no new event for
  ``refractory_minutes`` regardless of direction.
* Entry: the follower's bar open at decision time plus an entry delay; exit: the open ``horizon`` minutes
  later. Returns are signed by the leader's direction. Net return subtracts a taker fee and slippage on
  both sides. The primary cell is threshold 100bp, entry delay 1 minute, horizon 15 minutes, both
  directions pooled; every other cell is secondary.
* The leader's own return over the same entry/exit times is reported next to the follower's, so that a
  continued move of the whole market is not read as a delayed move of the follower.
* A missing bar is never filled. Events whose exit would reach the boundary are dropped, not shortened.

Nothing here is a validated strategy: the cells are many, the selection of the hypothesis came from one
observed episode, and one-minute bars do not show whether the entry price was obtainable in size.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

MINUTE = 60_000
CUTOFF = 1785542400000  # 2026-08-01T00:00:00Z, the research boundary (exclusive)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")


@dataclass(frozen=True)
class Instrument:
    name: str
    venue: str
    market: str
    symbol: str
    fee_bp: float = 5.0
    slippage_bp: float = 2.0

    @property
    def round_trip_cost_bp(self) -> float:
        return 2 * (self.fee_bp + self.slippage_bp)


LEADER = Instrument("BTC", "binance", "perp", "BTCUSDT")
FOLLOWERS = (
    Instrument("ETH", "binance", "perp", "ETHUSDT", fee_bp=5.0, slippage_bp=2.0),
    Instrument("HYPE", "bybit", "perp", "HYPEUSDT", fee_bp=5.5, slippage_bp=2.0),
)


@dataclass(frozen=True)
class Config:
    window_minutes: int = 5
    thresholds_bp: tuple[float, ...] = (50.0, 100.0, 150.0)
    refractory_minutes: int = 60
    entry_delays_minutes: tuple[int, ...] = (0, 1)
    horizons_minutes: tuple[int, ...] = (1, 3, 5, 10, 15, 30, 60)
    primary_threshold_bp: float = 100.0
    primary_delay_minutes: int = 1
    primary_horizon_minutes: int = 15
    bootstrap_samples: int = 2000
    seed: int = 20261119
    min_events: int = 30
    min_months: int = 6

    def validate(self) -> None:
        ints = (self.window_minutes, self.refractory_minutes, self.bootstrap_samples, self.min_events, self.min_months)
        if any(not isinstance(value, int) or value <= 0 for value in ints):
            raise ValueError("window, refractory, bootstrap and eligibility settings must be positive integers")
        if not self.thresholds_bp or any(not np.isfinite(x) or x <= 0 for x in self.thresholds_bp):
            raise ValueError("thresholds must be finite and positive")
        if not self.entry_delays_minutes or any(not isinstance(x, int) or x < 0 for x in self.entry_delays_minutes):
            raise ValueError("entry delays must be non-negative integers")
        if not self.horizons_minutes or any(not isinstance(x, int) or x <= 0 for x in self.horizons_minutes):
            raise ValueError("horizons must be positive integers")
        if (self.primary_threshold_bp not in self.thresholds_bp
                or self.primary_delay_minutes not in self.entry_delays_minutes
                or self.primary_horizon_minutes not in self.horizons_minutes):
            raise ValueError("the primary cell must be one of the evaluated cells")


def prices(raw: pd.DataFrame) -> pd.DataFrame:
    """Index one-minute bars by their open time; non-positive or non-finite prices become missing."""
    frame = raw[["ts", "open", "close"]].copy()
    if frame.ts.duplicated().any() or frame.ts.isna().any() or (frame.ts % MINUTE != 0).any():
        raise ValueError("price timestamps must be unique UTC minute opens")
    for column in ("open", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.loc[~np.isfinite(frame[column]) | frame[column].le(0), column] = np.nan
    return frame.set_index("ts").sort_index()


def detect_events(leader: pd.DataFrame, config: Config, threshold_bp: float, start_ts: int, end_ts: int) -> pd.DataFrame:
    """Leader moves of at least ``threshold_bp`` over the window, decided at the bar close.

    Uses only bars up to and including the event bar. Every close in the window must exist.
    """
    config.validate()
    if leader.empty:
        return pd.DataFrame(columns=["bar_ts", "decision_ts", "direction", "leader_window_bp"])
    grid = np.arange(int(leader.index.min()), int(leader.index.max()) + MINUTE, MINUTE)
    close = leader.close.reindex(grid)
    window = config.window_minutes
    complete = close.notna().rolling(window + 1).sum().eq(window + 1)
    move_bp = (close / close.shift(window) - 1) * 10_000
    candidates = move_bp[complete & move_bp.abs().ge(threshold_bp)]
    rows, blocked_until = [], None
    for bar_ts, value in candidates.items():
        decision_ts = int(bar_ts) + MINUTE
        if blocked_until is not None and decision_ts < blocked_until:
            continue
        # The refractory clock runs from every accepted event, including ones before ``start_ts``,
        # so the event list does not depend on where the requested period begins.
        blocked_until = decision_ts + config.refractory_minutes * MINUTE
        if start_ts <= decision_ts < end_ts:
            rows.append({"bar_ts": int(bar_ts), "decision_ts": decision_ts,
                         "direction": 1 if value > 0 else -1, "leader_window_bp": float(value)})
    return pd.DataFrame(rows, columns=["bar_ts", "decision_ts", "direction", "leader_window_bp"])


def outcomes(events: pd.DataFrame, leader: pd.DataFrame, follower_prices: pd.DataFrame, follower: Instrument,
             config: Config, threshold_bp: float, end_ts: int = CUTOFF) -> pd.DataFrame:
    """One row per event, entry delay and horizon. Incomplete rows are kept with a reason."""
    config.validate()
    if end_ts > CUTOFF:
        raise ValueError("end is after the research boundary")
    rows = []
    window_ms = config.window_minutes * MINUTE
    for event in events.itertuples(index=False):
        sign = int(event.direction)
        start_close = follower_prices.close.get(event.bar_ts - window_ms, np.nan)
        end_close = follower_prices.close.get(event.bar_ts, np.nan)
        follower_window_bp = sign * (end_close / start_close - 1) * 10_000 if np.isfinite(start_close * end_close) else np.nan
        for delay in config.entry_delays_minutes:
            entry_ts = int(event.decision_ts) + delay * MINUTE
            for horizon in config.horizons_minutes:
                exit_ts = entry_ts + horizon * MINUTE
                row = {"follower": follower.name, "threshold_bp": threshold_bp, "decision_ts": int(event.decision_ts),
                       "direction": sign, "leader_window_bp": sign * float(event.leader_window_bp),
                       "follower_window_bp": follower_window_bp, "entry_delay_min": delay, "horizon_min": horizon,
                       "entry_ts": entry_ts, "exit_ts": exit_ts, "entry_px": np.nan, "exit_px": np.nan,
                       "gross_bp": np.nan, "net_bp": np.nan, "leader_same_interval_bp": np.nan,
                       "complete": False, "reason": ""}
                if exit_ts >= end_ts:
                    row["reason"] = "exit_at_or_after_boundary"
                else:
                    entry_px = follower_prices.open.get(entry_ts, np.nan)
                    exit_px = follower_prices.open.get(exit_ts, np.nan)
                    if not np.isfinite(entry_px) or not np.isfinite(exit_px):
                        row["reason"] = "missing_follower_bar"
                    else:
                        gross = sign * (exit_px / entry_px - 1) * 10_000
                        row.update(entry_px=float(entry_px), exit_px=float(exit_px), gross_bp=float(gross),
                                   net_bp=float(gross - follower.round_trip_cost_bp), complete=True)
                        leader_entry = leader.open.get(entry_ts, np.nan)
                        leader_exit = leader.open.get(exit_ts, np.nan)
                        if np.isfinite(leader_entry) and np.isfinite(leader_exit):
                            row["leader_same_interval_bp"] = float(sign * (leader_exit / leader_entry - 1) * 10_000)
                rows.append(row)
    return pd.DataFrame(rows)


def _cluster_bootstrap(values: np.ndarray, months: np.ndarray, samples: int, rng: np.random.Generator):
    """Resample UTC months with replacement; returns (lower, upper, one-sided p that the mean is <= 0)."""
    labels, inverse = np.unique(months, return_inverse=True)
    sums = np.bincount(inverse, weights=values, minlength=len(labels))
    counts = np.bincount(inverse, minlength=len(labels)).astype(float)
    draws = rng.integers(0, len(labels), size=(samples, len(labels)))
    means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    lower, upper = np.quantile(means, [0.025, 0.975])
    return float(lower), float(upper), float((1 + np.sum(means <= 0)) / (samples + 1))


def _bh(p_values: pd.Series) -> pd.Series:
    """Benjamini-Hochberg q-values over the non-missing entries."""
    result = pd.Series(np.nan, index=p_values.index)
    known = p_values.dropna().sort_values()
    if known.empty:
        return result
    ranked = known.to_numpy() * len(known) / np.arange(1, len(known) + 1)
    result.loc[known.index] = np.minimum(1.0, np.minimum.accumulate(ranked[::-1])[::-1])
    return result


def summarize(ledger: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Per follower, threshold, direction, entry delay and horizon. ``both`` pools the two directions."""
    config.validate()
    rng = np.random.default_rng(config.seed)
    rows = []
    if ledger.empty:
        return pd.DataFrame(rows)
    frame = ledger.assign(month=pd.to_datetime(ledger.decision_ts, unit="ms", utc=True).dt.strftime("%Y-%m"))
    keys = ["follower", "threshold_bp", "entry_delay_min", "horizon_min"]
    for key, group in frame.groupby(keys, sort=True):
        for direction, part in (("both", group), ("up", group[group.direction > 0]), ("down", group[group.direction < 0])):
            done = part[part.complete]
            row = dict(zip(keys, key)) | {
                "direction": direction, "events": int(len(part)), "complete_events": int(len(done)),
                "incomplete_events": int(len(part) - len(done)), "active_months": int(done.month.nunique()),
                "mean_gross_bp": np.nan, "median_gross_bp": np.nan, "mean_net_bp": np.nan,
                "share_net_positive": np.nan, "mean_leader_same_interval_bp": np.nan,
                "mean_follower_window_bp": np.nan, "mean_leader_window_bp": np.nan,
                "net_ci95_low_bp": np.nan, "net_ci95_high_bp": np.nan, "p_one_sided_net_positive": np.nan,
                "status": "insufficient_sample"}
            if len(done):
                row.update(mean_gross_bp=float(done.gross_bp.mean()), median_gross_bp=float(done.gross_bp.median()),
                           mean_net_bp=float(done.net_bp.mean()), share_net_positive=float((done.net_bp > 0).mean()),
                           mean_leader_same_interval_bp=float(done.leader_same_interval_bp.mean()),
                           mean_follower_window_bp=float(done.follower_window_bp.mean()),
                           mean_leader_window_bp=float(done.leader_window_bp.mean()))
            if len(done) >= config.min_events and done.month.nunique() >= config.min_months:
                lower, upper, p_value = _cluster_bootstrap(done.net_bp.to_numpy(float), done.month.to_numpy(),
                                                           config.bootstrap_samples, rng)
                row.update(net_ci95_low_bp=lower, net_ci95_high_bp=upper, p_one_sided_net_positive=p_value,
                           status="evaluated")
            row["primary"] = bool(direction == "both" and row["threshold_bp"] == config.primary_threshold_bp
                                  and row["entry_delay_min"] == config.primary_delay_minutes
                                  and row["horizon_min"] == config.primary_horizon_minutes)
            rows.append(row)
    summary = pd.DataFrame(rows)
    summary["bh_q_all_cells"] = _bh(summary.p_one_sided_net_positive)
    return summary


def load_prices(conn: sqlite3.Connection, instrument: Instrument, start: int, end: int) -> pd.DataFrame:
    raw = pd.read_sql_query(
        "SELECT ts, open, close FROM klines WHERE venue=? AND market=? AND symbol=? AND ts>=? AND ts<? ORDER BY ts",
        conn, params=(instrument.venue, instrument.market, instrument.symbol, start, end))
    return prices(raw)


def run(db: Path, output: Path, start: int, end: int, config: Config,
        leader: Instrument = LEADER, followers: tuple[Instrument, ...] = FOLLOWERS) -> dict:
    config.validate()
    if start >= end or end > CUTOFF or start % MINUTE or end % MINUTE:
        raise ValueError("period must be minute-aligned, non-empty and end at or before the research boundary")
    if output.exists() and any(output.iterdir()):
        raise ValueError("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    before = db.stat()
    lookback = (config.window_minutes + config.refractory_minutes) * MINUTE
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        leader_prices = load_prices(conn, leader, start - lookback, end)
        follower_data = {item.name: load_prices(conn, item, start - lookback, end) for item in followers}
    after = db.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("database changed during the analysis")

    event_frames, ledgers, coverage = [], [], {}
    for threshold in config.thresholds_bp:
        events = detect_events(leader_prices, config, threshold, start, end)
        event_frames.append(events.assign(threshold_bp=threshold))
        for item in followers:
            data = follower_data[item.name]
            if data.empty:
                continue
            # Only events the follower could have been observed around: its history must have begun.
            eligible = events[events.bar_ts - config.window_minutes * MINUTE >= int(data.index.min())]
            ledgers.append(outcomes(eligible, leader_prices, data, item, config, threshold, end))
    for item in (leader, *followers):
        data = leader_prices if item is leader else follower_data[item.name]
        coverage[item.name] = {"venue": item.venue, "market": item.market, "symbol": item.symbol,
                               "bars": int(len(data)),
                               "first_ts": int(data.index.min()) if len(data) else None,
                               "last_ts": int(data.index.max()) if len(data) else None,
                               "round_trip_cost_bp": item.round_trip_cost_bp}
    events_all = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    ledger = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
    summary = summarize(ledger, config)
    events_all.to_csv(output / "events.csv", index=False)
    ledger.to_csv(output / "outcomes.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    manifest = {"script": str(Path(__file__).resolve().relative_to(ROOT)),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "config": asdict(config), "start_ts": start, "end_ts": end, "research_boundary_ts": CUTOFF,
                "leader": asdict(leader), "followers": [asdict(item) for item in followers], "coverage": coverage,
                "db": {"path": str(db), "size": before.st_size, "mtime_ns": before.st_mtime_ns},
                "events_by_threshold": {str(t): int((events_all.threshold_bp == t).sum()) if len(events_all) else 0
                                        for t in config.thresholds_bp},
                "caveats": ["exploratory; many cells; hypothesis chosen after one observed episode",
                            "one-minute bars: the open is not a guaranteed fill and size is not observed",
                            "costs are assumed taker fee and slippage, not measured fills; funding is ignored",
                            "the leader's same-interval return shows whether the whole market kept moving"]}
    manifest["outputs_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted(output.glob("*.csv"))}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"output_dir": str(output), "events_by_threshold": manifest["events_by_threshold"], "coverage": coverage}


def _parse_day(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").timestamp() * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, required=True, help="new or empty directory")
    parser.add_argument("--start", default="2021-01-02", help="UTC date, inclusive")
    parser.add_argument("--end", default="2026-08-01", help="UTC date, exclusive; not after 2026-08-01")
    parser.add_argument("--bootstrap-samples", type=int, default=Config.bootstrap_samples)
    parser.add_argument("--seed", type=int, default=Config.seed)
    args = parser.parse_args()
    config = Config(bootstrap_samples=args.bootstrap_samples, seed=args.seed)
    print(json.dumps(run(args.db, args.output_dir, _parse_day(args.start), _parse_day(args.end), config)))


if __name__ == "__main__":
    main()
