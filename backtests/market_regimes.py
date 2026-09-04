#!/usr/bin/env python3
"""Causal market-regime and cross-asset risk analysis.

The existing backtests answer whether individual crypto signals worked.  This
script adds a different layer: *when* risk and return changed, how persistent
those environments were, and whether crypto shared downside risk with other
assets in each environment.

Regimes are observable at the end of UTC day t and are evaluated on the return
from t to t+1:

* trend: trailing ``--trend-days`` close-to-close return (default 90 days)
* volatility: annualised standard deviation of trailing ``--vol-days`` daily
  log returns (default 30 days)
* volatility boundary: a trailing median of volatility estimates, shifted by
  one day so the current estimate never sets its own boundary

This produces four states (up/down trend x low/high volatility).  Inference is
designed for time-series data rather than IID rows:

* circular moving-block bootstrap for regime-conditional mean return
* Newey-West/HAC inference for cross-asset beta
* Benjamini-Hochberg FDR correction across each family of tests

All market data is read from SQLite in read-only mode.  Results are descriptive
and diagnostic; no trading costs or execution assumptions are applied.

Run::

    python3 backtests/market_regimes.py

The default outputs are written to ``results/market_regimes/``.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from scipy import stats


DAY_MS = 24 * 60 * 60 * 1000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path("/mnt/e/Datas/market/market.db")
DEFAULT_OUTPUT_DIR = REPO_ROOT / "results" / "market_regimes"
REGIME_ORDER = (
    "uptrend_low_vol",
    "uptrend_high_vol",
    "downtrend_low_vol",
    "downtrend_high_vol",
)


@dataclass(frozen=True)
class RegimeConfig:
    """Parameters that fully determine regime construction and inference."""

    trend_days: int = 90
    vol_days: int = 30
    threshold_lookback_days: int = 730
    threshold_min_history_days: int = 252
    vol_quantile: float = 0.50
    bootstrap_samples: int = 2_000
    block_days: int = 14
    hac_lags: int = 5
    min_observations: int = 30
    seed: int = 20260904

    def validate(self) -> None:
        if self.trend_days < 2:
            raise ValueError("trend_days must be at least 2")
        if self.vol_days < 2:
            raise ValueError("vol_days must be at least 2")
        if self.threshold_lookback_days < self.threshold_min_history_days:
            raise ValueError("threshold lookback must be >= minimum history")
        if self.threshold_min_history_days < 10:
            raise ValueError("threshold minimum history must be at least 10 days")
        if not 0.0 < self.vol_quantile < 1.0:
            raise ValueError("vol_quantile must be strictly between 0 and 1")
        if self.bootstrap_samples < 1:
            raise ValueError("bootstrap_samples must be positive")
        if self.block_days < 1:
            raise ValueError("block_days must be positive")
        if self.hac_lags < 0:
            raise ValueError("hac_lags must be non-negative")
        if self.min_observations < 3:
            raise ValueError("min_observations must be at least 3")


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def comma_list(value: str) -> list[str]:
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected at least one comma-separated value")
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="market.db path")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory for CSV/JSON outputs"
    )
    parser.add_argument("--venue", default="binance")
    parser.add_argument("--market", default="perp")
    parser.add_argument("--symbols", type=comma_list, default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--anchor-symbol", default="BTCUSDT", help="regime used for pair conditioning")
    parser.add_argument("--index-source", default="fred")
    parser.add_argument(
        "--indexes",
        type=comma_list,
        default=["SP500", "NASDAQ100", "DJIA", "NIKKEI225"],
    )
    parser.add_argument("--start", help="inclusive UTC date (YYYY-MM-DD)")
    parser.add_argument("--end", help="inclusive UTC date (YYYY-MM-DD)")
    parser.add_argument("--trend-days", type=int, default=90)
    parser.add_argument("--vol-days", type=int, default=30)
    parser.add_argument("--threshold-lookback-days", type=int, default=730)
    parser.add_argument("--threshold-min-history-days", type=int, default=252)
    parser.add_argument("--vol-quantile", type=float, default=0.50)
    parser.add_argument("--bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--block-days", type=int, default=14)
    parser.add_argument(
        "--hac-lags",
        type=int,
        default=5,
        help="Newey-West lag count in consecutive common-return intervals",
    )
    parser.add_argument("--min-observations", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260904)
    return parser.parse_args(argv)


def connect_read_only(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _date_bound_ms(value: str, *, end: bool) -> int:
    stamp = pd.Timestamp(value, tz="UTC")
    if end:
        stamp += pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    return int(stamp.timestamp() * 1000)


def _shift_date(value: str, days: int) -> str:
    return (pd.Timestamp(value, tz="UTC") + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def load_daily_close(
    conn: sqlite3.Connection,
    venue: str,
    market: str,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
) -> pd.Series:
    """Load each complete UTC day's 23:59 close without loading all 1m rows."""

    filters = ["venue = ?", "market = ?", "symbol = ?"]
    params: list[object] = [venue, market, symbol]
    if start:
        filters.append("ts >= ?")
        params.append(_date_bound_ms(start, end=False))
    if end:
        filters.append("ts <= ?")
        params.append(_date_bound_ms(end, end=True))
    where = " AND ".join(filters)
    query = f"""
        WITH daily_last AS (
            SELECT CAST(ts / {DAY_MS} AS INTEGER) AS utc_day, MAX(ts) AS last_ts
            FROM klines
            WHERE {where}
            GROUP BY CAST(ts / {DAY_MS} AS INTEGER)
        )
        SELECT d.utc_day, d.last_ts, k.close
        FROM daily_last AS d
        JOIN klines AS k
          ON k.venue = ? AND k.market = ? AND k.symbol = ? AND k.ts = d.last_ts
        WHERE d.last_ts % {DAY_MS} = {DAY_MS - 60_000}
        ORDER BY d.utc_day
    """
    rows = pd.read_sql_query(query, conn, params=(*params, venue, market, symbol))
    if rows.empty:
        raise ValueError(f"no complete UTC-day kline data for {venue}/{market}/{symbol}")
    rows["date"] = pd.to_datetime(rows["utc_day"] * DAY_MS, unit="ms", utc=True)
    rows["close"] = pd.to_numeric(rows["close"], errors="coerce")
    rows = rows.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    if (rows["close"] <= 0).any():
        raise ValueError(f"non-positive close found for {symbol}")
    result = rows.set_index("date")["close"].sort_index().astype(float)
    result.name = symbol
    return result


def load_index_closes(
    conn: sqlite3.Connection,
    source: str,
    symbols: Sequence[str],
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Load available daily index closes; missing trading days stay missing."""

    if not symbols:
        return pd.DataFrame()
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'index_daily'"
    ).fetchone()
    if table_exists is None:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in symbols)
    filters = ["source = ?", f"symbol IN ({placeholders})"]
    params: list[object] = [source, *symbols]
    if start:
        filters.append("date >= ?")
        params.append(start)
    if end:
        filters.append("date <= ?")
        params.append(end)
    query = f"""
        SELECT date, symbol, close
        FROM index_daily
        WHERE {' AND '.join(filters)}
        ORDER BY date, symbol
    """
    rows = pd.read_sql_query(query, conn, params=params)
    if rows.empty:
        return pd.DataFrame()
    rows["date"] = pd.to_datetime(rows["date"], utc=True, errors="coerce")
    rows["close"] = pd.to_numeric(rows["close"], errors="coerce")
    rows = rows.dropna(subset=["date", "close"])
    rows = rows[rows["close"] > 0]
    return rows.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()


def build_regime_frame(close: pd.Series, symbol: str, config: RegimeConfig) -> pd.DataFrame:
    """Build a daily, causal regime panel for one asset.

    The volatility threshold at t is calculated from estimates ending at t-1.
    The regime itself may use close(t), and is paired only with return(t, t+1).
    Reindexing to calendar days makes every shift a day shift rather than an
    observation shift when an input dataset contains gaps.
    """

    config.validate()
    if close.empty:
        raise ValueError(f"empty close series for {symbol}")
    close = close.sort_index()
    close = close[~close.index.duplicated(keep="last")]
    if close.index.tz is None:
        close.index = close.index.tz_localize("UTC")
    else:
        close.index = close.index.tz_convert("UTC")
    calendar = pd.date_range(close.index.min().normalize(), close.index.max().normalize(), freq="D", tz="UTC")
    close = close.reindex(calendar)

    simple_return = close / close.shift(1) - 1.0
    log_return = np.log(close / close.shift(1))
    realised_vol = (
        log_return.rolling(config.vol_days, min_periods=config.vol_days).std(ddof=1) * math.sqrt(365.0)
    )
    vol_threshold = (
        realised_vol.shift(1)
        .rolling(
            config.threshold_lookback_days,
            min_periods=config.threshold_min_history_days,
        )
        .quantile(config.vol_quantile)
    )
    trend_return = close / close.shift(config.trend_days) - 1.0

    regime = pd.Series(pd.NA, index=calendar, dtype="string")
    ready = trend_return.notna() & realised_vol.notna() & vol_threshold.notna()
    up = trend_return >= 0.0
    high = realised_vol >= vol_threshold
    regime.loc[ready & up & ~high] = "uptrend_low_vol"
    regime.loc[ready & up & high] = "uptrend_high_vol"
    regime.loc[ready & ~up & ~high] = "downtrend_low_vol"
    regime.loc[ready & ~up & high] = "downtrend_high_vol"

    return pd.DataFrame(
        {
            "date": calendar,
            "symbol": symbol,
            "close": close.to_numpy(dtype=float),
            "return_1d": simple_return.to_numpy(dtype=float),
            "next_return_1d": (close.shift(-1) / close - 1.0).to_numpy(dtype=float),
            "trend_return": trend_return.to_numpy(dtype=float),
            "realized_vol_annualized": realised_vol.to_numpy(dtype=float),
            "causal_vol_threshold": vol_threshold.to_numpy(dtype=float),
            "regime": regime.to_numpy(),
        }
    )


def max_drawdown(returns: Iterable[float]) -> float:
    values = np.asarray(list(returns), dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return np.nan
    wealth = np.cumprod(1.0 + values)
    peaks = np.maximum.accumulate(np.r_[1.0, wealth])[1:]
    return float(np.min(wealth / peaks - 1.0))


def benjamini_hochberg(p_values: Sequence[float]) -> np.ndarray:
    """Benjamini-Hochberg adjusted q-values, preserving NaN positions."""

    values = np.asarray(p_values, dtype=float)
    result = np.full(values.shape, np.nan, dtype=float)
    valid_positions = np.flatnonzero(np.isfinite(values))
    if len(valid_positions) == 0:
        return result
    valid = np.clip(values[valid_positions], 0.0, 1.0)
    order = np.argsort(valid, kind="stable")
    ranked = valid[order] * len(valid) / np.arange(1, len(valid) + 1)
    adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
    result[valid_positions[order]] = np.minimum(adjusted, 1.0)
    return result


def circular_block_indices(n: int, block_size: int, rng: np.random.Generator) -> np.ndarray:
    """Sample n ordered positions as concatenated circular time-series blocks."""

    if n < 1:
        raise ValueError("n must be positive")
    block_size = min(max(1, block_size), n)
    blocks = math.ceil(n / block_size)
    starts = rng.integers(0, n, size=blocks)
    offsets = np.arange(block_size)
    return ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]


def calendar_block_indices(
    dates: Sequence[object], block_size: int, rng: np.random.Generator
) -> np.ndarray:
    """Block-sample within contiguous calendar segments, never across a gap."""

    date_index = pd.DatetimeIndex(pd.to_datetime(dates, utc=True))
    n = len(date_index)
    if n < 1:
        raise ValueError("dates must not be empty")
    gap = np.r_[True, (date_index[1:] - date_index[:-1]) != pd.Timedelta(days=1)]
    segment_ids = np.cumsum(gap) - 1
    segments = [np.flatnonzero(segment_ids == segment_id) for segment_id in np.unique(segment_ids)]
    if len(segments) == 1:
        return circular_block_indices(n, block_size, rng)
    probabilities = np.asarray([len(segment) for segment in segments], dtype=float)
    probabilities /= probabilities.sum()
    sampled: list[int] = []
    while len(sampled) < n:
        segment = segments[int(rng.choice(len(segments), p=probabilities))]
        width = min(max(1, block_size), len(segment))
        start = int(rng.integers(0, len(segment)))
        positions = segment[(start + np.arange(width)) % len(segment)]
        sampled.extend(int(position) for position in positions)
    return np.asarray(sampled[:n], dtype=int)


def bootstrap_regime_means(
    regimes: Sequence[str],
    returns: Sequence[float],
    labels: Sequence[str],
    samples: int,
    block_size: int,
    rng: np.random.Generator,
    dates: Sequence[object] | None = None,
) -> dict[str, dict[str, float]]:
    """Block-bootstrap CIs and a regime-centred two-sided null test."""

    regime_values = np.asarray(regimes, dtype=object)
    return_values = np.asarray(returns, dtype=float)
    valid = pd.notna(regime_values) & np.isfinite(return_values)
    date_values: pd.DatetimeIndex | None = None
    if dates is not None:
        parsed_dates = pd.DatetimeIndex(pd.to_datetime(dates, utc=True, errors="coerce"))
        if len(parsed_dates) != len(return_values):
            raise ValueError("dates, regimes, and returns must have equal length")
        valid &= ~pd.isna(parsed_dates)
        date_values = parsed_dates[valid]
    regime_values = regime_values[valid]
    return_values = return_values[valid]
    if len(return_values) == 0:
        return {
            label: {
                "ci_low": np.nan,
                "ci_high": np.nan,
                "p_value": np.nan,
                "valid_draw_share": np.nan,
            }
            for label in labels
        }

    observed = {
        label: float(return_values[regime_values == label].mean())
        if np.any(regime_values == label)
        else np.nan
        for label in labels
    }
    centred = return_values.copy()
    for label, mean in observed.items():
        if np.isfinite(mean):
            centred[regime_values == label] -= mean

    bootstrap = {label: np.full(samples, np.nan) for label in labels}
    null_bootstrap = {label: np.full(samples, np.nan) for label in labels}
    for draw in range(samples):
        positions = (
            calendar_block_indices(date_values, block_size, rng)
            if date_values is not None
            else circular_block_indices(len(return_values), block_size, rng)
        )
        sampled_regime = regime_values[positions]
        for label in labels:
            mask = sampled_regime == label
            if mask.any():
                bootstrap[label][draw] = return_values[positions][mask].mean()
                null_bootstrap[label][draw] = centred[positions][mask].mean()

    result: dict[str, dict[str, float]] = {}
    for label in labels:
        draws = bootstrap[label][np.isfinite(bootstrap[label])]
        null_draws = null_bootstrap[label][np.isfinite(null_bootstrap[label])]
        if not np.isfinite(observed[label]) or len(draws) == 0 or len(null_draws) == 0:
            result[label] = {
                "ci_low": np.nan,
                "ci_high": np.nan,
                "p_value": np.nan,
                "valid_draw_share": len(draws) / samples,
            }
            continue
        p_value = (1 + np.sum(np.abs(null_draws) >= abs(observed[label]))) / (len(null_draws) + 1)
        result[label] = {
            "ci_low": float(np.quantile(draws, 0.025)),
            "ci_high": float(np.quantile(draws, 0.975)),
            "p_value": float(min(1.0, p_value)),
            "valid_draw_share": len(draws) / samples,
        }
    return result


def regime_episodes(frame: pd.DataFrame) -> pd.DataFrame:
    """Return contiguous same-regime runs, splitting on missing calendar days."""

    valid = frame.dropna(subset=["regime"]).sort_values("date").copy()
    columns = [
        "symbol",
        "regime",
        "start_date",
        "end_date",
        "observations",
        "calendar_days",
        "cumulative_next_return",
        "max_drawdown",
    ]
    if valid.empty:
        return pd.DataFrame(columns=columns)
    day_gap = valid["date"].diff().dt.days.ne(1)
    state_change = valid["regime"].ne(valid["regime"].shift(1))
    valid["episode_id"] = (day_gap | state_change).cumsum()
    rows: list[dict[str, object]] = []
    for _, group in valid.groupby("episode_id", sort=False):
        outcome = group["next_return_1d"].dropna().to_numpy(dtype=float)
        cumulative = float(np.prod(1.0 + outcome) - 1.0) if len(outcome) else np.nan
        rows.append(
            {
                "symbol": group["symbol"].iloc[0],
                "regime": group["regime"].iloc[0],
                "start_date": group["date"].iloc[0],
                "end_date": group["date"].iloc[-1],
                "observations": len(group),
                "calendar_days": int((group["date"].iloc[-1] - group["date"].iloc[0]).days + 1),
                "cumulative_next_return": cumulative,
                "max_drawdown": max_drawdown(outcome),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def summarize_regimes(
    frame: pd.DataFrame, config: RegimeConfig, rng: np.random.Generator
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Summarise next-day outcomes and regime-specific calendar strategies."""

    symbol = str(frame["symbol"].iloc[0])
    observations = frame.dropna(subset=["regime", "next_return_1d"]).sort_values("date")
    bootstrap = bootstrap_regime_means(
        observations["regime"].to_numpy(),
        observations["next_return_1d"].to_numpy(),
        REGIME_ORDER,
        config.bootstrap_samples,
        config.block_days,
        rng,
        dates=observations["date"].to_numpy(),
    )
    episodes = regime_episodes(frame)
    total = len(observations)
    rows: list[dict[str, object]] = []
    for label in REGIME_ORDER:
        sample = observations.loc[observations["regime"] == label, "next_return_1d"].to_numpy(dtype=float)
        episode_lengths = episodes.loc[episodes["regime"] == label, "calendar_days"].to_numpy(dtype=float)
        enough = len(sample) >= config.min_observations
        mean = float(np.mean(sample)) if len(sample) else np.nan
        std = float(np.std(sample, ddof=1)) if len(sample) > 1 else np.nan
        var_5 = float(np.quantile(sample, 0.05)) if len(sample) else np.nan
        es_5 = float(np.mean(sample[sample <= var_5])) if len(sample) else np.nan

        first_state_date = frame.loc[frame["regime"].notna(), "date"].min()
        last_outcome_date = frame.loc[frame["next_return_1d"].notna(), "date"].max()
        calendar = frame[
            (frame["date"] >= first_state_date) & (frame["date"] <= last_outcome_date)
        ].sort_values("date")
        exposed = calendar["regime"].to_numpy() == label
        unknown_exposed_return = exposed & calendar["next_return_1d"].isna().to_numpy()
        strategy_returns = np.where(
            exposed,
            calendar["next_return_1d"].fillna(0.0).to_numpy(dtype=float),
            0.0,
        )
        years = (
            (calendar["date"].iloc[-1] - calendar["date"].iloc[0]).days + 1
        ) / 365.2425 if len(calendar) else np.nan
        if unknown_exposed_return.any():
            cagr = strategy_drawdown = np.nan
        else:
            ending_wealth = float(np.prod(1.0 + strategy_returns))
            cagr = (
                ending_wealth ** (1.0 / years) - 1.0
                if ending_wealth > 0 and np.isfinite(years) and years > 0
                else np.nan
            )
            strategy_drawdown = max_drawdown(strategy_returns)

        infer = bootstrap[label] if enough else {
            "ci_low": np.nan,
            "ci_high": np.nan,
            "p_value": np.nan,
            "valid_draw_share": np.nan,
        }
        rows.append(
            {
                "symbol": symbol,
                "regime": label,
                "n_evaluable_days": len(sample),
                "share_of_evaluable_classified_days": len(sample) / total if total else np.nan,
                "n_episodes": len(episode_lengths),
                "median_episode_days": float(np.median(episode_lengths)) if len(episode_lengths) else np.nan,
                "mean_next_return_bp": mean * 10_000.0,
                "bootstrap_ci_low_bp": infer["ci_low"] * 10_000.0,
                "bootstrap_ci_high_bp": infer["ci_high"] * 10_000.0,
                "bootstrap_p_value": infer["p_value"],
                "bootstrap_valid_draw_share": infer["valid_draw_share"],
                "median_next_return_bp": float(np.median(sample) * 10_000.0) if len(sample) else np.nan,
                "positive_next_day_share": float(np.mean(sample > 0.0)) if len(sample) else np.nan,
                "conditional_vol_annualized": std * math.sqrt(365.0) if np.isfinite(std) else np.nan,
                "conditional_sharpe_365": mean / std * math.sqrt(365.0)
                if np.isfinite(std) and std > 0
                else np.nan,
                "var_5pct_bp": var_5 * 10_000.0,
                "expected_shortfall_5pct_bp": es_5 * 10_000.0,
                "regime_only_strategy_cagr": cagr,
                "regime_only_strategy_max_drawdown": strategy_drawdown,
            }
        )
    return pd.DataFrame(rows), episodes


def transition_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Count only next-calendar-day transitions between observable regimes."""

    valid = frame.dropna(subset=["regime"]).sort_values("date").copy()
    if len(valid) < 2:
        return pd.DataFrame(columns=["symbol", "from_regime", "to_regime", "count", "probability"])
    next_regime = valid["regime"].shift(-1)
    next_date = valid["date"].shift(-1)
    pairs = valid.loc[(next_date - valid["date"]).dt.days.eq(1), ["symbol", "regime"]].copy()
    pairs["to_regime"] = next_regime.loc[pairs.index]
    counts = pairs.groupby(["symbol", "regime", "to_regime"], observed=True).size().rename("count").reset_index()
    if counts.empty:
        return pd.DataFrame(columns=["symbol", "from_regime", "to_regime", "count", "probability"])
    counts["probability"] = counts["count"] / counts.groupby(["symbol", "regime"])["count"].transform("sum")
    return counts.rename(columns={"regime": "from_regime"}).sort_values(
        ["symbol", "from_regime", "to_regime"]
    )


def newey_west_slope(x: Sequence[float], y: Sequence[float], max_lag: int) -> tuple[float, float, float]:
    """OLS slope, HAC standard error, and two-sided p-value with an intercept."""

    x_values = np.asarray(x, dtype=float)
    y_values = np.asarray(y, dtype=float)
    valid = np.isfinite(x_values) & np.isfinite(y_values)
    x_values = x_values[valid]
    y_values = y_values[valid]
    n = len(x_values)
    if n < 3 or np.var(x_values) <= 0:
        return np.nan, np.nan, np.nan
    design = np.column_stack([np.ones(n), x_values])
    xtx = design.T @ design
    if np.linalg.cond(xtx) > 1e12:
        return np.nan, np.nan, np.nan
    xtx_inverse = np.linalg.inv(xtx)
    coefficients = xtx_inverse @ design.T @ y_values
    residuals = y_values - design @ coefficients
    scores = design * residuals[:, None]
    meat = scores.T @ scores
    lag_limit = min(max(0, int(max_lag)), n - 2)
    for lag in range(1, lag_limit + 1):
        weight = 1.0 - lag / (lag_limit + 1.0)
        covariance = scores[lag:].T @ scores[:-lag]
        meat += weight * (covariance + covariance.T)
    covariance_matrix = xtx_inverse @ meat @ xtx_inverse
    covariance_matrix *= n / (n - design.shape[1])
    variance = float(covariance_matrix[1, 1])
    standard_error = math.sqrt(max(variance, 0.0))
    slope = float(coefficients[1])
    if standard_error <= 0:
        return slope, standard_error, np.nan
    t_stat = slope / standard_error
    p_value = float(2.0 * stats.t.sf(abs(t_stat), df=n - design.shape[1]))
    return slope, standard_error, p_value


def build_close_panel(
    regime_frames: dict[str, pd.DataFrame], index_closes: pd.DataFrame
) -> pd.DataFrame:
    """Combine crypto and index closes without filling non-trading days."""

    series: list[pd.Series] = []
    for symbol, frame in regime_frames.items():
        crypto = frame.set_index("date")["close"].rename(symbol)
        series.append(crypto)
    for symbol in index_closes.columns:
        series.append(index_closes[symbol].rename(symbol))
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1, sort=False).sort_index()


def cross_asset_dependence(
    close_panel: pd.DataFrame,
    regime_frame: pd.DataFrame,
    crypto_symbols: Sequence[str],
    config: RegimeConfig,
) -> pd.DataFrame:
    """Measure dependence over pair-matched forward close intervals.

    Each pair uses only dates on which both closes exist, and both returns span
    the same start/end dates.  Conditional rows use the anchor regime from the
    calendar day before the interval starts, so the state predates even the
    earliest market close on the start date.
    """

    columns = [
        "conditioning_symbol",
        "regime",
        "asset_x",
        "asset_y",
        "n_intervals",
        "regime_information_lag_calendar_days",
        "median_return_horizon_calendar_days",
        "max_return_horizon_calendar_days",
        "pearson_correlation",
        "spearman_correlation",
        "hac_beta_y_on_x",
        "hac_beta_standard_error",
        "hac_beta_p_value",
        "downside_correlation_x_bottom_20pct",
        "lower_tail_probability_y_given_x",
        "lower_tail_lift_vs_independence",
        "joint_lower_tail_share",
    ]
    if close_panel.empty:
        return pd.DataFrame(columns=columns)
    anchor = str(regime_frame["symbol"].iloc[0])
    regime = regime_frame.set_index("date")["regime"].rename("_regime")
    assets = list(close_panel.columns)
    crypto_set = set(crypto_symbols)
    pairs = [pair for pair in combinations(assets, 2) if crypto_set.intersection(pair)]
    rows: list[dict[str, object]] = []
    for asset_x, asset_y in pairs:
        common_closes = close_panel[[asset_x, asset_y]].dropna().sort_index()
        forward_returns = common_closes.shift(-1) / common_closes - 1.0
        horizon_days = (
            common_closes.index.to_series().shift(-1) - common_closes.index.to_series()
        ).dt.days.rename("_horizon_days")
        prior_regime_dates = common_closes.index - pd.Timedelta(days=1)
        known_regime = regime.reindex(prior_regime_dates)
        known_regime.index = common_closes.index
        pair_panel = forward_returns.join(horizon_days).join(known_regime)
        for label in ("all", *REGIME_ORDER):
            subset = pair_panel[[asset_x, asset_y, "_horizon_days", "_regime"]]
            if label != "all":
                subset = subset[subset["_regime"] == label]
            sample = subset.dropna(subset=[asset_x, asset_y, "_horizon_days"])
            n = len(sample)
            enough = n >= config.min_observations
            x = sample[asset_x].to_numpy(dtype=float)
            y = sample[asset_y].to_numpy(dtype=float)
            if enough and np.std(x, ddof=1) > 0 and np.std(y, ddof=1) > 0:
                pearson = float(np.corrcoef(x, y)[0, 1])
                spearman = float(stats.spearmanr(x, y).statistic)
                beta, beta_se, beta_p = newey_west_slope(x, y, config.hac_lags)
                x_20 = np.quantile(x, 0.20)
                downside = x <= x_20
                downside_corr = (
                    float(np.corrcoef(x[downside], y[downside])[0, 1])
                    if downside.sum() >= 10 and np.std(x[downside]) > 0 and np.std(y[downside]) > 0
                    else np.nan
                )
                x_10 = np.quantile(x, 0.10)
                y_10 = np.quantile(y, 0.10)
                x_tail = x <= x_10
                y_tail = y <= y_10
                conditional = float(np.mean(y_tail[x_tail])) if x_tail.any() else np.nan
                joint = float(np.mean(x_tail & y_tail))
                lift = conditional / 0.10 if np.isfinite(conditional) else np.nan
            else:
                pearson = spearman = beta = beta_se = beta_p = np.nan
                downside_corr = conditional = joint = lift = np.nan
            rows.append(
                {
                    "conditioning_symbol": anchor,
                    "regime": label,
                    "asset_x": asset_x,
                    "asset_y": asset_y,
                    "n_intervals": n,
                    "regime_information_lag_calendar_days": 1 if label != "all" else np.nan,
                    "median_return_horizon_calendar_days": float(sample["_horizon_days"].median())
                    if n
                    else np.nan,
                    "max_return_horizon_calendar_days": float(sample["_horizon_days"].max())
                    if n
                    else np.nan,
                    "pearson_correlation": pearson,
                    "spearman_correlation": spearman,
                    "hac_beta_y_on_x": beta,
                    "hac_beta_standard_error": beta_se,
                    "hac_beta_p_value": beta_p,
                    "downside_correlation_x_bottom_20pct": downside_corr,
                    "lower_tail_probability_y_given_x": conditional,
                    "lower_tail_lift_vs_independence": lift,
                    "joint_lower_tail_share": joint,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def build_findings(regime_summary: pd.DataFrame, dependence: pd.DataFrame) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for symbol, group in regime_summary.groupby("symbol", sort=False):
        valid = group.dropna(subset=["mean_next_return_bp"])
        if valid.empty:
            continue
        best = valid.loc[valid["mean_next_return_bp"].idxmax()]
        worst_tail = valid.loc[valid["expected_shortfall_5pct_bp"].idxmin()]
        findings.append(
            {
                "type": "best_mean_next_day_regime",
                "symbol": symbol,
                "regime": best["regime"],
                "mean_next_return_bp": best["mean_next_return_bp"],
                "fdr_q_value": best.get("fdr_q_value", np.nan),
            }
        )
        findings.append(
            {
                "type": "worst_expected_shortfall_regime",
                "symbol": symbol,
                "regime": worst_tail["regime"],
                "expected_shortfall_5pct_bp": worst_tail["expected_shortfall_5pct_bp"],
            }
        )
    crypto_pair = dependence[
        (dependence["regime"] == "all")
        & (dependence["asset_x"].str.contains("BTC", na=False))
        & (dependence["asset_y"].str.contains("ETH", na=False))
    ]
    if not crypto_pair.empty:
        row = crypto_pair.iloc[0]
        findings.append(
            {
                "type": "btc_eth_dependence",
                "pearson_correlation": row["pearson_correlation"],
                "lower_tail_probability_y_given_x": row["lower_tail_probability_y_given_x"],
                "lower_tail_lift_vs_independence": row["lower_tail_lift_vs_independence"],
            }
        )
    return findings


def write_outputs(
    output_dir: Path,
    daily: pd.DataFrame,
    summary: pd.DataFrame,
    transitions: pd.DataFrame,
    episodes: pd.DataFrame,
    dependence: pd.DataFrame,
    metadata: dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    daily.to_csv(output_dir / "daily_regimes.csv", index=False, float_format="%.10g")
    summary.to_csv(output_dir / "regime_summary.csv", index=False, float_format="%.10g")
    transitions.to_csv(output_dir / "transition_matrix.csv", index=False, float_format="%.10g")
    episodes.to_csv(output_dir / "regime_episodes.csv", index=False, float_format="%.10g")
    dependence.to_csv(output_dir / "cross_asset_dependence.csv", index=False, float_format="%.10g")
    with (output_dir / "analysis_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(_json_safe(metadata), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def run(args: argparse.Namespace) -> dict[str, object]:
    config = RegimeConfig(
        trend_days=args.trend_days,
        vol_days=args.vol_days,
        threshold_lookback_days=args.threshold_lookback_days,
        threshold_min_history_days=args.threshold_min_history_days,
        vol_quantile=args.vol_quantile,
        bootstrap_samples=args.bootstrap_samples,
        block_days=args.block_days,
        hac_lags=args.hac_lags,
        min_observations=args.min_observations,
        seed=args.seed,
    )
    config.validate()
    if args.anchor_symbol not in args.symbols:
        raise ValueError("anchor-symbol must be included in --symbols")
    if args.start and args.end and pd.Timestamp(args.start) > pd.Timestamp(args.end):
        raise ValueError("--start must be on or before --end")

    # Load enough observations before the requested reporting window to make
    # the first reported threshold causal and fully warmed up.  One extra day
    # after --end supplies the final requested day's forward return.
    warmup_days = max(
        config.trend_days,
        config.vol_days + config.threshold_lookback_days + 1,
    )
    load_start = _shift_date(args.start, -warmup_days) if args.start else None
    load_end = _shift_date(args.end, 1) if args.end else None

    started = time.monotonic()
    conn = connect_read_only(args.db)
    try:
        closes: dict[str, pd.Series] = {}
        for symbol in args.symbols:
            log(f"loading daily closes for {args.venue}/{args.market}/{symbol}")
            closes[symbol] = load_daily_close(
                conn, args.venue, args.market, symbol, start=load_start, end=load_end
            )
            log(f"  {symbol}: {len(closes[symbol])} UTC days")
        index_closes = load_index_closes(
            conn, args.index_source, args.indexes, start=args.start, end=args.end
        )
        missing_indexes = sorted(set(args.indexes) - set(index_closes.columns))
        if missing_indexes:
            log(f"  index data unavailable for: {', '.join(missing_indexes)}")
    finally:
        conn.close()

    rng = np.random.default_rng(config.seed)
    frames = {symbol: build_regime_frame(close, symbol, config) for symbol, close in closes.items()}
    for symbol, frame in frames.items():
        mask = pd.Series(True, index=frame.index)
        if args.start:
            mask &= frame["date"] >= pd.Timestamp(args.start, tz="UTC")
        if args.end:
            mask &= frame["date"] <= pd.Timestamp(args.end, tz="UTC")
        frames[symbol] = frame.loc[mask].reset_index(drop=True)
    summaries: list[pd.DataFrame] = []
    episode_frames: list[pd.DataFrame] = []
    transition_frames: list[pd.DataFrame] = []
    for symbol in args.symbols:
        log(f"bootstrapping regime outcomes for {symbol} ({config.bootstrap_samples} draws)")
        summary, episodes = summarize_regimes(frames[symbol], config, rng)
        summaries.append(summary)
        episode_frames.append(episodes)
        transition_frames.append(transition_table(frames[symbol]))

    daily = pd.concat([frames[symbol] for symbol in args.symbols], ignore_index=True)
    regime_summary = pd.concat(summaries, ignore_index=True)
    regime_summary["fdr_q_value"] = benjamini_hochberg(regime_summary["bootstrap_p_value"])
    episodes = pd.concat(episode_frames, ignore_index=True)
    transitions = pd.concat(transition_frames, ignore_index=True)

    closes_panel = build_close_panel(frames, index_closes)
    dependence = cross_asset_dependence(
        closes_panel, frames[args.anchor_symbol], args.symbols, config
    )
    if not dependence.empty:
        dependence["hac_beta_fdr_q_value"] = benjamini_hochberg(dependence["hac_beta_p_value"])

    coverage = {
        symbol: {
            "loaded_start": close.index.min(),
            "loaded_end": close.index.max(),
            "analysis_start": frames[symbol]["date"].min(),
            "analysis_end": frames[symbol]["date"].max(),
            "calendar_analysis_days": len(frames[symbol]),
            "observed_analysis_days": int(frames[symbol]["close"].notna().sum()),
            "missing_analysis_days": int(frames[symbol]["close"].isna().sum()),
            "classified_days": int(frames[symbol]["regime"].notna().sum()),
        }
        for symbol, close in closes.items()
    }
    index_coverage = {
        symbol: {
            "start": index_closes[symbol].dropna().index.min(),
            "end": index_closes[symbol].dropna().index.max(),
            "observations": int(index_closes[symbol].notna().sum()),
        }
        for symbol in index_closes.columns
        if index_closes[symbol].notna().any()
    }
    metadata: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc),
        "database": args.db.expanduser().resolve(),
        "price_source": {"venue": args.venue, "market": args.market},
        "configuration": asdict(config),
        "coverage": {"crypto": coverage, "indexes": index_coverage},
        "methodology": {
            "daily_close": "last 1m bar at 23:59 UTC; incomplete UTC days are excluded",
            "decision_timing": "regime uses data through UTC close t; outcome is close t to close t+1",
            "trend": f"trailing {config.trend_days}-calendar-day close return",
            "volatility": f"annualized standard deviation of {config.vol_days} daily log returns",
            "volatility_boundary": (
                f"{config.vol_quantile:.0%} quantile of prior volatility estimates over at most "
                f"{config.threshold_lookback_days} days; shifted one day"
            ),
            "regime_inference": (
                f"{config.bootstrap_samples}-draw circular moving-block bootstrap, "
                f"{config.block_days}-day blocks; regime-centred two-sided null"
            ),
            "dependence_inference": (
                f"pair-matched forward close intervals conditioned on the prior calendar day's "
                f"regime; OLS beta with Newey-West/HAC covariance over {config.hac_lags} "
                "consecutive common-return intervals"
            ),
            "multiple_testing": "Benjamini-Hochberg FDR within regime and dependence test families",
            "tail_definition": "asset-specific bottom decile within each reported pair/regime sample",
            "regime_only_strategy": (
                "hypothetical close-to-close exposure only after the named regime, cash otherwise; "
                "CAGR uses elapsed calendar time and includes no costs"
            ),
        },
        "limitations": [
            "Regimes are deterministic diagnostics, not latent-state estimates or trading recommendations.",
            "Index returns exist only on their source trading dates; crypto-index comparisons use overlapping dates.",
            "Daily closes occur at different clock times across venues, so same-date cross-market dependence is not fully synchronous.",
            "Tail estimates can be noisy in short regimes; sample counts and FDR-adjusted q-values must be inspected.",
            "HAC p-values use an asymptotic approximation and should be treated cautiously in short regimes.",
            "Daily closes do not model fees, slippage, intraday path, or liquidation constraints.",
        ],
        "findings": build_findings(regime_summary, dependence),
        "elapsed_seconds": time.monotonic() - started,
    }
    write_outputs(
        args.output_dir,
        daily,
        regime_summary,
        transitions,
        episodes,
        dependence,
        metadata,
    )

    display_columns = [
        "symbol",
        "regime",
        "n_evaluable_days",
        "mean_next_return_bp",
        "bootstrap_ci_low_bp",
        "bootstrap_ci_high_bp",
        "fdr_q_value",
        "expected_shortfall_5pct_bp",
        "regime_only_strategy_max_drawdown",
    ]
    print("\nREGIME OUTCOME SUMMARY")
    print(regime_summary[display_columns].to_string(index=False, float_format=lambda value: f"{value:,.4f}"))
    log(f"wrote six outputs to {args.output_dir} in {metadata['elapsed_seconds']:.1f}s")
    return metadata


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(parse_args(argv))
    except (FileNotFoundError, ValueError, sqlite3.DatabaseError) as exc:
        print(f"error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
