#!/usr/bin/env python3
"""Held-out check of a post-hoc pattern: crypto rebounds in EU/US hours after TOPIX stress days.

The pattern was noticed on 2021-2026 data (docs/japan-crossmarket.md), so it is tested once,
with the rule fixed in that document, on 2017-08..2020-12, a period this repository had not
analysed. The 2021-2026 statistic is reported only for reference. Not a trading signal.
Outputs contain J-Quants-derived values and are refused inside this public repository.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtests.japan_crossmarket import (  # noqa: E402
    DAY, HOUR, JQ_DIR, cluster_diff_draws, day_number, expanding_tail, outside_repo, read_only, sha256_file,
    stable_seed,
)

HOLDOUT_DIR = Path("/mnt/e/Datas/market/holdout_pre2021/binance/spot/klines_1m")


@dataclass(frozen=True)
class Config:
    entry_hour_utc: int = 8  # J-Quants publishes TOPIX ~16:30 JST = 07:30 UTC
    exit_hour_utc: int = 19
    event_quantile: float = 0.05
    event_min_history: int = 250
    topix_max_gap_days: int = 7
    holdout: tuple[str, str] = ("2017-08-17", "2020-12-31")
    discovery: tuple[str, str] = ("2021-01-01", "2026-07-31")
    bootstrap: int = 4000
    seed: int = 20261020
    round_trip_bp: float = 14.0
    alpha: float = 0.05

    def validate(self) -> None:
        if not 0 <= self.entry_hour_utc < self.exit_hour_utc < 24:
            raise ValueError("entry must precede exit within the same UTC day")
        if not 0 < self.event_quantile < 0.5 or self.event_min_history < 1 or self.bootstrap < 1:
            raise ValueError("invalid event or bootstrap settings")
        if self.holdout[1] >= self.discovery[0]:
            raise ValueError("held-out period must end before the discovery period")


def topix_events(topix: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Same stress definition as japan_crossmarket: TOPIX log return at or below the expanding 5% quantile."""
    frame = topix[["date", "close"]].sort_values("date").reset_index(drop=True)
    day = day_number(frame.date)
    adjacent = np.r_[False, np.diff(day) <= config.topix_max_gap_days]
    frame["topix_ret"] = np.where(adjacent, np.log(frame.close / frame.close.shift()), np.nan)
    frame["event"], frame["threshold"] = expanding_tail(frame.topix_ret.to_numpy(), config.event_quantile,
                                                        True, config.event_min_history)
    frame["day"] = day
    return frame


def load_csv_opens(directory: Path) -> tuple[pd.DataFrame, dict[str, str], int]:
    files = sorted(directory.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"no kline CSVs in {directory}")
    parts = [pd.read_csv(path, header=None, usecols=[0, 1], names=["ts", "open"]) for path in files]
    frame = pd.concat(parts, ignore_index=True).astype({"ts": np.int64, "open": float})
    frame["ts"] = np.where(frame.ts > 10 ** 14, frame.ts // 1000, frame.ts)  # Binance 2025+ uses microseconds
    # The spot archive holds off-grid rows (2017-12-04..18, 2018-02-09..10); they are treated as missing.
    aligned = frame.ts % 60_000 == 0
    return _checked(frame.loc[aligned]), {path.name: sha256_file(path) for path in files}, int((~aligned).sum())


def load_db_opens(conn: sqlite3.Connection, symbol: str, start: str, end: str) -> pd.DataFrame:
    lo, hi = (int(x) * DAY for x in day_number([start, end]))
    query = "SELECT ts, open FROM klines WHERE venue='binance' AND market='spot' AND symbol=? AND ts>=? AND ts<?"
    return _checked(pd.read_sql_query(query, conn, params=(symbol, lo, hi + DAY)))


def _checked(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values("ts").reset_index(drop=True)
    if frame.ts.duplicated().any() or (frame.ts % 60_000 != 0).any():
        raise ValueError("kline timestamps must be unique UTC minute opens")
    return frame


def window_returns(events: pd.DataFrame, opens: pd.DataFrame, start: str, end: str, config: Config) -> pd.DataFrame:
    """Log return from the open at d entry_hour to the open at d exit_hour; NaN if either bar is missing."""
    lo, hi = day_number([start, end])
    frame = events.loc[(events.day >= lo) & (events.day <= hi)].copy()
    ts, price = opens.ts.to_numpy(np.int64), opens.open.to_numpy(float)

    def at(t):
        i = np.minimum(np.searchsorted(ts, t), len(ts) - 1)
        return np.where((ts[i] == t) & (price[i] > 0), price[i], np.nan)
    entry = at(frame.day.to_numpy() * DAY + config.entry_hour_utc * HOUR)
    exit_ = at(frame.day.to_numpy() * DAY + config.exit_hour_utc * HOUR)
    frame["window_ret"] = np.log(exit_ / entry)
    return frame.loc[np.isfinite(frame.window_ret)].reset_index(drop=True)


def evaluate(frame: pd.DataFrame, config: Config, seed: int) -> dict:
    """Event mean minus all-day mean, with a month-cluster bootstrap one-sided p (difference > 0)."""
    values, is_event = frame.window_ret.to_numpy(float), frame.event.to_numpy(bool)
    draws = cluster_diff_draws(values, is_event, frame.date.str[:7].to_numpy(), config.bootstrap, seed)
    event_mean = float(values[is_event].mean()) if is_event.any() else np.nan
    return {
        "days": len(values), "events": int(is_event.sum()), "first_date": frame.date.min(),
        "last_date": frame.date.max(), "event_mean": event_mean, "all_mean": float(values.mean()),
        "diff": event_mean - float(values.mean()),
        "diff_ci_low": float(np.quantile(draws, 0.025)) if len(draws) else np.nan,
        "diff_ci_high": float(np.quantile(draws, 0.975)) if len(draws) else np.nan,
        "p_greater": float((1 + np.sum(draws <= 0)) / (len(draws) + 1)) if len(draws) else np.nan,
        "event_mean_after_round_trip": event_mean - config.round_trip_bp / 10_000,
        "event_hit_rate": float((values[is_event] > 0).mean()) if is_event.any() else np.nan,
        "event_median": float(np.median(values[is_event])) if is_event.any() else np.nan,
    }


def run(jquants_db: Path, market_db: Path, holdout_dir: Path, output: Path, symbols: list[str], config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    with read_only(jquants_db) as conn:
        events = topix_events(pd.read_sql_query("SELECT date, close FROM topix_daily", conn), config)
    rows, ledgers, hashes, off_grid = [], [], {}, {}
    with read_only(market_db) as conn:
        for symbol in symbols:
            holdout_opens, hashes[symbol], off_grid[symbol] = load_csv_opens(holdout_dir / symbol)
            samples = {"holdout": window_returns(events, holdout_opens, *config.holdout, config),
                       "discovery": window_returns(events, load_db_opens(conn, symbol, *config.discovery),
                                                   *config.discovery, config)}
            for period, frame in samples.items():
                rows.append({"asset": symbol, "period": period,
                             **evaluate(frame, config, stable_seed(symbol, period, base=config.seed))})
                ledgers.append(frame.loc[frame.event].assign(asset=symbol, period=period)[
                    ["asset", "period", "date", "topix_ret", "threshold", "window_ret"]])
    table = pd.DataFrame(rows)
    btc = table.loc[(table.asset == "BTCUSDT") & (table.period == "holdout")]
    replicated = bool(len(btc) and btc.p_greater.iloc[0] < config.alpha and btc.event_mean_after_round_trip.iloc[0] > 0)
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "summary.csv", index=False, float_format="%.10g")
    pd.concat(ledgers, ignore_index=True).to_csv(output / "events.csv", index=False, float_format="%.10g")
    verdict = {"verdict": "replicated on the held-out period" if replicated else "not replicated on the held-out period",
               "rule": f"BTCUSDT held-out one-sided p<{config.alpha} and event mean after "
                       f"{config.round_trip_bp:g}bp round trip > 0",
               "results": json.loads(table.to_json(orient="records")), "off_grid_rows_dropped": off_grid}
    (output / "result.json").write_text(json.dumps(verdict, indent=2) + "\n")
    stat = market_db.stat()
    manifest = {"config": asdict(config), "symbols": symbols, "script_sha256": sha256_file(Path(__file__)),
                "inputs": {"jquants_db_sha256": sha256_file(jquants_db), "holdout_csv_sha256": hashes,
                           "off_grid_rows_dropped": off_grid,
                           "market_db": {"path": str(market_db), "byte_size": stat.st_size,
                                         "mtime_ns": stat.st_mtime_ns}},
                "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--market-db", type=Path, default=Path("/mnt/e/Datas/market/market.db"))
    parser.add_argument("--holdout-dir", type=Path, default=HOLDOUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/japan_session_reversal")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    args = parser.parse_args()
    verdict = run(args.jquants_db, args.market_db, args.holdout_dir, args.output_dir,
                  [s.strip() for s in args.symbols.split(",") if s.strip()], Config())
    print(json.dumps(verdict, indent=2))


if __name__ == "__main__":
    main()
