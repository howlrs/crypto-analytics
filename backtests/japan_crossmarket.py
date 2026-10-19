#!/usr/bin/env python3
"""Japan-session data (J-Quants) versus BTC/ETH forward risk: causal, exploratory diagnostics.

Primary question: does the 30-day ATM implied volatility of Nikkei 225 options (JIV) improve
out-of-sample forecasts of BTC/ETH realised volatility beyond crypto's own HAR terms and VIX?
Secondary and descriptive: crypto returns after Japanese stress days, and a volatility-target
overlay capped at 4x. The fixed design is in docs/japan-crossmarket.md. Not a trading signal.

Inputs are opened read-only. Outputs contain J-Quants-derived daily values, which are licensed
for private use, so writing them inside this public repository is refused.
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
from scipy import stats

MINUTE = 60_000
FIVE_MINUTES = 5 * MINUTE
HOUR = 60 * MINUTE
DAY = 24 * HOUR
CUTOFF = 1785542400000  # 2026-08-01T00:00:00Z
CUTOFF_DATE = "2026-08-01"
ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
HAR = ["log_rv1", "log_rv7", "log_rv30"]
MODELS = {"M0": HAR, "MV": HAR + ["log_vix"], "MJ": HAR + ["log_jiv"], "MVJ": HAR + ["log_vix", "log_jiv"]}
COMPARISONS = (("primary", "MVJ", "MV"), ("secondary", "MJ", "M0"), ("secondary", "MV", "M0"),
               ("reference", "M0", "MEAN"))  # HAR skill over the expanding mean of closed targets
EVENTS = ("event_topix", "event_jiv")
SHOCK_MONTH = "2024-08"  # the Japan-origin carry unwind; the verdict must survive without it


@dataclass(frozen=True)
class Config:
    decision_hour_utc: int = 19  # options-225 publish ~27:00 JST = 18:00 UTC, plus one hour
    horizons_days: tuple[int, ...] = (1, 3)
    min_days_to_sq: int = 7
    iv_target_days: int = 30
    vix_max_age_days: int = 5
    topix_max_gap_days: int = 7
    oos_start: str = "2023-01-01"
    min_train_rows: int = 250
    hac_lags_per_day: int = 5
    alpha: float = 0.05
    min_positive_years: int = 3
    event_quantile: float = 0.05
    event_min_history: int = 250
    bootstrap: int = 4000
    seed: int = 20261020
    vol_target: float = 0.5
    max_leverage: float = 4.0
    fee_bp: float = 5.0
    slippage_bp: float = 2.0

    def validate(self) -> None:
        if not 0 <= self.decision_hour_utc < 24 or not self.horizons_days:
            raise ValueError("decision hour must be 0-23 and horizons non-empty")
        if any(not isinstance(h, int) or h <= 0 for h in self.horizons_days):
            raise ValueError("horizons must be positive whole days")
        if self.min_days_to_sq < 1 or self.iv_target_days <= self.min_days_to_sq:
            raise ValueError("IV maturity settings are inconsistent")
        if not 0 < self.event_quantile < 0.5 or not 0 < self.alpha < 1:
            raise ValueError("quantile and alpha must be proper probabilities")
        if min(self.min_train_rows, self.event_min_history, self.bootstrap, self.hac_lags_per_day) < 1:
            raise ValueError("sample-size settings must be positive")
        if not (np.isfinite(self.vol_target) and self.vol_target > 0 and 0 < self.max_leverage <= 4):
            raise ValueError("vol target must be positive and leverage capped at 4x")
        if min(self.fee_bp, self.slippage_bp) < 0:
            raise ValueError("costs must be non-negative")

    @property
    def one_way_cost(self) -> float:
        return (self.fee_bp + self.slippage_bp) / 10_000


def day_number(dates) -> np.ndarray:
    return np.asarray(dates, dtype="datetime64[D]").astype(np.int64)


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("J-Quants-derived outputs must stay outside the public repository")
    return resolved


def iv30(atm: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Constant-maturity ATM IV from the two nearest expiries with enough days left.

    Total variance is linear in calendar days between them, and extrapolated when both lie
    beyond the target, as the pre-2014 VIX did after rolling past a near-term expiry. This
    avoids the jump a flat fallback makes on roll days. A lone eligible expiry is used as is.
    """
    usable = (atm.days_to_sq >= config.min_days_to_sq) & np.isfinite(atm.atm_iv) & (atm.atm_iv > 0)
    target, rows = config.iv_target_days, []
    for day, group in atm.loc[usable].sort_values("days_to_sq").groupby("date", sort=True):
        tau, sigma = group.days_to_sq.to_numpy(float)[:2], group.atm_iv.to_numpy(float)[:2]
        if len(tau) == 1:
            rows.append((day, sigma[0], "single", tau[0], tau[0]))
            continue
        w = (target - tau[0]) / (tau[1] - tau[0])
        total = (1 - w) * sigma[0] ** 2 * tau[0] + w * sigma[1] ** 2 * tau[1]
        method = "interpolated" if tau[0] <= target <= tau[1] else "extrapolated"
        rows.append((day, np.sqrt(total / target) if total > 0 else np.nan, method, tau[0], tau[1]))
    return pd.DataFrame(rows, columns=["date", "jiv", "jiv_method", "jiv_near_days", "jiv_far_days"])


def expanding_tail(values: np.ndarray, quantile: float, lower: bool, min_history: int) -> tuple[np.ndarray, np.ndarray]:
    """Flag x[i] beyond the quantile of strictly earlier finite values."""
    flags, thresholds, history = np.zeros(len(values), bool), np.full(len(values), np.nan), []
    for i, x in enumerate(np.asarray(values, float)):
        if np.isfinite(x):
            if len(history) >= min_history:
                thresholds[i] = np.quantile(history, quantile)
                flags[i] = x <= thresholds[i] if lower else x >= thresholds[i]
            history.append(x)
    return flags, thresholds


def japan_panel(topix: pd.DataFrame, jiv: pd.DataFrame, vix: pd.DataFrame, config: Config) -> pd.DataFrame:
    """One row per TSE trading day d, everything known at T = d + decision_hour_utc."""
    frame = topix[["date", "close"]].rename(columns={"close": "topix_close"}).sort_values("date")
    frame = frame.merge(jiv, on="date", how="left").reset_index(drop=True)
    day = day_number(frame.date)
    adjacent = np.r_[False, np.diff(day) <= config.topix_max_gap_days]
    frame["topix_ret"] = np.where(adjacent, np.log(frame.topix_close / frame.topix_close.shift()), np.nan)
    frame["jiv_chg"] = np.where(adjacent, np.log(frame.jiv / frame.jiv.shift()), np.nan)
    # VIX of the latest US date strictly before d: at T the US session of d is still open.
    v = vix.loc[np.isfinite(vix.vix) & (vix.vix > 0)].sort_values("date")
    v_day, v_value = day_number(v.date), v.vix.to_numpy(float)
    idx = np.searchsorted(v_day, day, side="left") - 1
    found = idx >= 0
    age = np.where(found, day - v_day[np.maximum(idx, 0)], np.iinfo(np.int64).max)
    frame["vix"] = np.where(found & (age <= config.vix_max_age_days), v_value[np.maximum(idx, 0)], np.nan)
    frame["decision_ts"] = day * DAY + config.decision_hour_utc * HOUR
    frame["event_topix"], frame["topix_threshold"] = expanding_tail(
        frame.topix_ret.to_numpy(), config.event_quantile, True, config.event_min_history)
    frame["event_jiv"], frame["jiv_chg_threshold"] = expanding_tail(
        frame.jiv_chg.to_numpy(), 1 - config.event_quantile, False, config.event_min_history)
    frame["log_vix"], frame["log_jiv"] = np.log(frame.vix), np.log(frame.jiv)
    return frame


def validate_bars(raw: pd.DataFrame) -> pd.DataFrame:
    bars = raw[["ts", "open", "high", "low", "close"]].astype({"ts": np.int64}).sort_values("ts")
    if bars.ts.duplicated().any() or (bars.ts % MINUTE != 0).any():
        raise ValueError("bar timestamps must be unique UTC minute opens")
    for column in ("open", "high", "low", "close"):
        bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars.loc[~(bars[column] > 0), column] = np.nan
    return bars.reset_index(drop=True)


class RealizedVariance:
    """Prefix sums of squared 5-minute log returns, indexed by each return's end time."""

    def __init__(self, bars: pd.DataFrame):
        end = bars.ts.to_numpy(np.int64) + MINUTE
        grid = end % FIVE_MINUTES == 0
        end, close = end[grid], bars.close.to_numpy(float)[grid]
        valid = (np.diff(end) == FIVE_MINUTES) & np.isfinite(close[1:]) & np.isfinite(close[:-1])
        with np.errstate(invalid="ignore", divide="ignore"):
            r2 = np.log(close[1:] / close[:-1]) ** 2
        self.end = end[1:][valid]
        self.cum = np.r_[0.0, np.cumsum(r2[valid])]

    def vol(self, start, stop) -> np.ndarray:
        """Annualised volatility over (start, stop]; NaN unless every 5-minute return is present."""
        start, stop = np.asarray(start, np.int64), np.asarray(stop, np.int64)
        i0 = np.searchsorted(self.end, start, side="right")
        i1 = np.searchsorted(self.end, stop, side="right")
        span = np.where(stop > start, stop - start, 1)
        ok = ((stop > start) & (start % FIVE_MINUTES == 0) & (stop % FIVE_MINUTES == 0)
              & (i1 - i0 == span // FIVE_MINUTES))
        variance = (self.cum[i1] - self.cum[i0]) * 365 * DAY / span
        with np.errstate(invalid="ignore"):
            return np.where(ok & (variance > 0), np.sqrt(variance), np.nan)


def path_outcomes(bars: pd.DataFrame, starts: np.ndarray, horizon_ms: int) -> pd.DataFrame:
    """Entry at the open of the bar starting at T, exit at the open of the bar starting at T+h."""
    ts = bars.ts.to_numpy(np.int64)
    o, h, lo = (bars[c].to_numpy(float) for c in ("open", "high", "low"))
    out = np.full((len(starts), 4), np.nan)
    for k, start in enumerate(np.asarray(starts, np.int64)):
        i0, i1 = np.searchsorted(ts, [start, start + horizon_ms])
        if (start + horizon_ms > CUTOFF - MINUTE or i1 >= len(ts) or ts[i0] != start
                or ts[i1] != start + horizon_ms or i1 - i0 != horizon_ms // MINUTE):
            continue
        out[k] = o[i0], o[i1], np.min(lo[i0:i1]), np.max(h[i0:i1])
    frame = pd.DataFrame(out, columns=["entry", "exit", "low", "high"])
    return pd.DataFrame({"ret": np.log(frame.exit / frame.entry), "low": frame.low / frame.entry - 1,
                         "high": frame.high / frame.entry - 1, "entry": frame.entry})


def asset_panel(japan: pd.DataFrame, bars: pd.DataFrame, config: Config) -> pd.DataFrame:
    rv = RealizedVariance(bars)
    out = japan.loc[(japan.decision_ts >= bars.ts.iloc[0]) & (japan.decision_ts < CUTOFF)].copy()
    t = out.decision_ts.to_numpy(np.int64)
    for days in (1, 7, 30):
        out[f"rv{days}"] = rv.vol(t - days * DAY, t)
        out[f"log_rv{days}"] = np.log(out[f"rv{days}"])
    for h in config.horizons_days:
        stop = t + h * DAY
        out[f"fwd_vol_{h}d"] = np.where(stop <= CUTOFF, rv.vol(t, np.minimum(stop, CUTOFF)), np.nan)
        out[f"target_{h}d"] = np.log(out[f"fwd_vol_{h}d"])
        path = path_outcomes(bars, t, h * DAY)
        for column in ("ret", "low", "high"):
            out[f"fwd_{column}_{h}d"] = path[column].to_numpy()
        if h == min(config.horizons_days):
            out["entry_price"] = path.entry.to_numpy()
    return out.reset_index(drop=True)


def oos_forecasts(panel: pd.DataFrame, horizon: int, config: Config) -> pd.DataFrame:
    """Expanding OLS refit at every decision on rows whose target window closed by then."""
    target = f"target_{horizon}d"
    columns = sorted({c for features in MODELS.values() for c in features})
    data = panel.loc[np.isfinite(panel[columns + [target]]).all(axis=1)].sort_values("decision_ts")
    t, y = data.decision_ts.to_numpy(np.int64), data[target].to_numpy(float)
    design = {m: np.column_stack([np.ones(len(data)), data[f].to_numpy(float)]) for m, f in MODELS.items()}
    start = int(day_number([config.oos_start])[0]) * DAY
    rows = []
    for k in np.flatnonzero(t >= start):
        train = t + horizon * DAY <= t[k]
        n = int(train.sum())
        if n < config.min_train_rows:
            continue
        row = {"date": data.date.iloc[k], "decision_ts": int(t[k]), "target": y[k], "n_train": n,
               "pred_MEAN": float(y[train].mean()), "s2_MEAN": float(y[train].var(ddof=1))}
        for name, x in design.items():
            beta = np.linalg.lstsq(x[train], y[train], rcond=None)[0]
            resid = y[train] - x[train] @ beta
            row[f"pred_{name}"] = float(x[k] @ beta)
            row[f"s2_{name}"] = float(resid @ resid / (n - x.shape[1]))
        rows.append(row)
    return pd.DataFrame(rows)


def in_sample_fit(panel: pd.DataFrame, horizon: int, config: Config) -> list[dict]:
    """Full-sample MVJ coefficients with Newey-West t statistics (descriptive, not a test)."""
    target, features = f"target_{horizon}d", MODELS["MVJ"]
    data = panel.loc[np.isfinite(panel[features + [target]]).all(axis=1)]
    x = np.column_stack([np.ones(len(data)), data[features].to_numpy(float)])
    y = data[target].to_numpy(float)
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    resid = y - x @ beta
    scores, lags = x * resid[:, None], config.hac_lags_per_day * horizon
    meat = scores.T @ scores
    for j in range(1, min(lags, len(y) - 1) + 1):
        gamma = scores[j:].T @ scores[:-j]
        meat += (1 - j / (lags + 1)) * (gamma + gamma.T)
    bread = np.linalg.inv(x.T @ x)
    se = np.sqrt(np.diag(bread @ meat @ bread))
    r2 = 1 - resid.var() / y.var()
    return [{"horizon_days": horizon, "term": term, "coef": float(b), "hac_t": float(b / e), "n": len(y),
             "r2": float(r2)} for term, b, e in zip(["const"] + features, beta, se)]


def newey_west_se(x: np.ndarray, lags: int) -> float:
    d = np.asarray(x, float) - np.mean(x)
    n = len(d)
    s = d @ d / n
    for j in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - j / (lags + 1)) * (d[j:] @ d[:-j]) / n
    return float(np.sqrt(max(s, 0.0) / n))


def clark_west(y, small, big, lags: int) -> tuple[float, float]:
    """One-sided test that the larger nested model forecasts better (Clark and West, 2007)."""
    y, small, big = (np.asarray(v, float) for v in (y, small, big))
    if len(y) < 2:
        return np.nan, np.nan
    adjusted = (y - small) ** 2 - ((y - big) ** 2 - (small - big) ** 2)
    se = newey_west_se(adjusted, lags)
    if not se > 0:
        return np.nan, np.nan
    t = float(adjusted.mean() / se)
    return t, float(stats.norm.sf(t))


def benjamini_hochberg(values) -> np.ndarray:
    p = np.asarray(values, float)
    out, valid = np.full(len(p), np.nan), np.isfinite(p)
    if valid.any():
        x = p[valid]
        order = np.argsort(x)
        ranked = x[order] * len(x) / np.arange(1, len(x) + 1)
        adjusted = np.minimum.accumulate(ranked[::-1])[::-1]
        out[np.flatnonzero(valid)[order]] = np.minimum(adjusted, 1.0)
    return out


def oos_r2(y, small, big) -> float:
    base = np.sum((np.asarray(y) - small) ** 2)
    return float(1 - np.sum((np.asarray(y) - big) ** 2) / base) if base > 0 else np.nan


def qlike(y, pred, s2) -> float:
    """QLIKE on variance; a lognormal log-vol forecast implies E[vol^2] = exp(2 pred + 2 s2)."""
    ratio = np.exp(2 * np.asarray(y)) / np.exp(2 * np.asarray(pred) + 2 * np.asarray(s2))
    return float(np.mean(ratio - np.log(ratio) - 1))


def forecast_tests(oos: dict[tuple[str, int], pd.DataFrame], config: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    tests, years = [], []
    for (asset, h), f in oos.items():
        if f.empty:
            continue
        y, year, month = f.target.to_numpy(), f.date.str[:4].to_numpy(), f.date.str[:7].to_numpy()
        for family, big, small in COMPARISONS:
            ps, pb = f[f"pred_{small}"].to_numpy(), f[f"pred_{big}"].to_numpy()
            t, p = clark_west(y, ps, pb, config.hac_lags_per_day * h)
            by_year = {yr: oos_r2(y[year == yr], ps[year == yr], pb[year == yr]) for yr in np.unique(year)}
            without = {m: oos_r2(y[month != m], ps[month != m], pb[month != m]) for m in np.unique(month)}
            worst = min(without, key=without.get)
            keep = month != SHOCK_MONTH
            tests.append({
                "asset": asset, "horizon_days": h, "family": family, "model": big, "baseline": small,
                "n_oos": len(y), "first_date": f.date.iloc[0], "last_date": f.date.iloc[-1],
                "mse_baseline": float(np.mean((y - ps) ** 2)), "mse_model": float(np.mean((y - pb) ** 2)),
                "oos_r2": oos_r2(y, ps, pb), "cw_t": t, "cw_p": p,
                "qlike_baseline": qlike(y, ps, f[f"s2_{small}"]), "qlike_model": qlike(y, pb, f[f"s2_{big}"]),
                "positive_years": int(sum(v > 0 for v in by_year.values())), "years": len(by_year),
                "oos_r2_without_shock_month": oos_r2(y[keep], ps[keep], pb[keep]),
                "min_leave_one_month_r2": without[worst], "most_influential_month": worst,
            })
            years += [{"asset": asset, "horizon_days": h, "model": big, "baseline": small, "year": yr,
                       "n": int((year == yr).sum()), "oos_r2": v} for yr, v in by_year.items()]
    frame = pd.DataFrame(tests)
    if frame.empty:
        return frame, pd.DataFrame(years)
    frame["q_value"] = frame.groupby("family").cw_p.transform(benjamini_hochberg)
    frame["supports_jiv"] = ((frame.family == "primary") & (frame.q_value < config.alpha) & (frame.oos_r2 > 0)
                             & (frame.positive_years >= config.min_positive_years)
                             & (frame.oos_r2_without_shock_month > 0))
    return frame, pd.DataFrame(years)


def overlay(forecasts: pd.DataFrame, bars: pd.DataFrame, config: Config) -> tuple[pd.DataFrame, list[dict]]:
    """Long at min(4x, target / forecast vol), rebalanced at each decision; funding excluded."""
    f = forecasts.sort_values("decision_ts").reset_index(drop=True)
    ts, opens = bars.ts.to_numpy(np.int64), bars.open.to_numpy(float)
    idx = np.searchsorted(ts, f.decision_ts.to_numpy(np.int64))
    ok = (idx < len(ts)) & (ts[np.minimum(idx, len(ts) - 1)] == f.decision_ts.to_numpy())
    price = np.where(ok, opens[np.minimum(idx, len(ts) - 1)], np.nan)
    periods = f[["date", "decision_ts"]].iloc[:-1].copy()
    periods["return"] = price[1:] / price[:-1] - 1
    leverage = {"constant_1x": np.ones(len(f))}
    for model in ("M0", "MV", "MVJ"):
        forecast_vol = np.exp(f[f"pred_{model}"] + f[f"s2_{model}"] / 2).to_numpy()
        leverage[model] = np.clip(config.vol_target / forecast_vol, 0, config.max_leverage)
    summary = []
    years = (f.decision_ts.iloc[-1] - f.decision_ts.iloc[0]) / (365 * DAY) if len(f) > 1 else np.nan
    for name, lev in leverage.items():
        lev = lev[:-1]
        turnover = np.abs(np.diff(np.r_[0.0, lev]))
        net = lev * periods["return"].to_numpy() - config.one_way_cost * turnover
        periods[f"lev_{name}"], periods[f"net_{name}"] = lev, net
        valid = np.isfinite(net)
        x = net[valid]
        per_year = len(x) / years if years and years > 0 else np.nan
        equity = np.cumprod(1 + x)
        summary.append({
            "policy": name, "periods": len(x), "ann_return": float(np.mean(x) * per_year) if len(x) else np.nan,
            "ann_vol": float(np.std(x, ddof=1) * np.sqrt(per_year)) if len(x) > 1 else np.nan,
            "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)) if len(x) else np.nan,
            "mean_leverage": float(np.mean(lev[valid])) if len(x) else np.nan,
            "max_leverage": float(np.max(lev[valid])) if len(x) else np.nan,
            "mean_turnover": float(np.mean(turnover[valid])) if len(x) else np.nan,
        })
        row = summary[-1]
        row["sharpe"] = row["ann_return"] / row["ann_vol"] if row["ann_vol"] and row["ann_vol"] > 0 else np.nan
    return periods, summary


def decluster(ts: np.ndarray, flags: np.ndarray, gap_ms: int) -> np.ndarray:
    keep, last = np.zeros(len(ts), bool), None
    for i in np.flatnonzero(flags):
        if last is None or ts[i] - last >= gap_ms:
            keep[i], last = True, ts[i]
    return keep


def cluster_diff_draws(values: np.ndarray, is_event: np.ndarray, months: np.ndarray,
                       samples: int, seed: int) -> np.ndarray:
    """Month-cluster bootstrap draws of mean(event) - mean(all); draws without events are dropped."""
    labels, inverse = np.unique(months, return_inverse=True)
    all_sum = np.bincount(inverse, weights=values, minlength=len(labels))
    all_n = np.bincount(inverse, minlength=len(labels)).astype(float)
    ev_sum = np.bincount(inverse, weights=np.where(is_event, values, 0.0), minlength=len(labels))
    ev_n = np.bincount(inverse, weights=is_event.astype(float), minlength=len(labels))
    pick = np.random.default_rng(seed).integers(0, len(labels), size=(samples, len(labels)))
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = ev_sum[pick].sum(1) / ev_n[pick].sum(1) - all_sum[pick].sum(1) / all_n[pick].sum(1)
    return draws[np.isfinite(draws)]


def cluster_bootstrap_diff(values: np.ndarray, is_event: np.ndarray, months: np.ndarray,
                           samples: int, seed: int) -> tuple[float, float, float]:
    """Percentile CI and two-sided p for mean(event) - mean(all) from month-cluster draws."""
    draws = cluster_diff_draws(values, is_event, months, samples, seed)
    if not len(draws):
        return np.nan, np.nan, np.nan
    p = 2 * min((1 + np.sum(draws <= 0)) / (len(draws) + 1), (1 + np.sum(draws >= 0)) / (len(draws) + 1))
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975)), float(min(1.0, p))


def stable_seed(*parts: object, base: int = 0) -> int:
    digest = hashlib.sha256("|".join(map(str, (base, *parts))).encode()).digest()
    return int.from_bytes(digest[:8], "little") % 2 ** 32


def event_study(panels: dict[str, pd.DataFrame], config: Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary, by_year, ledgers = [], [], []
    for asset, panel in panels.items():
        for h in config.horizons_days:
            ret, low = f"fwd_ret_{h}d", f"fwd_low_{h}d"
            sample = panel.loc[np.isfinite(panel[ret])].sort_values("decision_ts").reset_index(drop=True)
            values, months = sample[ret].to_numpy(float), sample.date.str[:7].to_numpy()
            for event in EVENTS:
                keep = decluster(sample.decision_ts.to_numpy(np.int64), sample[event].to_numpy(bool), h * DAY)
                chosen = sample.loc[keep]
                low_ci, high_ci, p = cluster_bootstrap_diff(values, keep, months, config.bootstrap,
                                                            stable_seed(asset, h, event, base=config.seed))
                mean_event = float(chosen[ret].mean()) if keep.any() else np.nan
                summary.append({
                    "asset": asset, "horizon_days": h, "event": event, "events": int(keep.sum()),
                    "raw_flags": int(sample[event].sum()), "days": len(sample),
                    "mean_event_ret": mean_event, "mean_all_ret": float(values.mean()),
                    "diff": mean_event - float(values.mean()), "diff_ci_low": low_ci, "diff_ci_high": high_ci,
                    "p_two_sided": p, "event_hit_rate": float((chosen[ret] > 0).mean()) if keep.any() else np.nan,
                    "mean_event_ret_after_round_trip": mean_event - 2 * config.one_way_cost,
                    "mean_event_low": float(chosen[low].mean()) if keep.any() else np.nan,
                    "mean_all_low": float(sample[low].mean()),
                })
                for year, group in chosen.groupby(chosen.date.str[:4]):
                    by_year.append({"asset": asset, "horizon_days": h, "event": event, "year": year,
                                    "events": len(group), "mean_event_ret": float(group[ret].mean())})
                ledgers.append(chosen.assign(asset=asset, horizon_days=h, event=event)[
                    ["asset", "horizon_days", "event", "date", "decision_ts", "topix_ret", "jiv_chg", ret, low]
                ].rename(columns={ret: "fwd_ret", low: "fwd_low"}))
    frame = pd.DataFrame(summary)
    if not frame.empty:
        frame["q_value"] = benjamini_hochberg(frame.p_two_sided)
    events = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
    return frame, pd.DataFrame(by_year), events


def load_bars(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    query = """SELECT ts, open, high, low, close FROM klines
               WHERE venue='binance' AND market='perp' AND symbol=? AND ts<? ORDER BY ts"""
    return validate_bars(pd.read_sql_query(query, conn, params=(symbol, CUTOFF)))


def load_vix(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, dtype=str)
    if list(raw.columns) != ["observation_date", "VIXCLS"]:
        raise ValueError("expected a FRED VIXCLS CSV")
    return pd.DataFrame({"date": raw.observation_date, "vix": pd.to_numeric(raw.VIXCLS, errors="coerce")})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def before_cutoff(frame: pd.DataFrame) -> pd.DataFrame:
    """Japan inputs are trimmed to the same research boundary as the crypto data."""
    return frame.loc[frame.date < CUTOFF_DATE].reset_index(drop=True)


def data_checks(japan: pd.DataFrame, calendar: pd.DataFrame, atm: pd.DataFrame, jiv: pd.DataFrame,
                config: Config) -> dict:
    first, last = japan.date.min(), japan.date.max()
    business = set(calendar.loc[calendar.hol_div.isin(["1", "2"]) & calendar.date.between(first, last), "date"])
    near = atm.loc[atm.days_to_sq >= config.min_days_to_sq].sort_values("days_to_sq").groupby("date").first()
    joined = jiv.set_index("date").join(near.base_vol, how="inner").dropna()
    return {
        "topix_first": first, "topix_last": last, "topix_days": len(japan),
        "calendar_business_days_without_topix": len(business - set(japan.date)),
        "topix_days_not_business_days": len(set(japan.date) - business) if business else None,
        "topix_days_without_jiv": int(japan.jiv.isna().sum()),
        "jiv_method_share": jiv.jiv_method.value_counts(normalize=True).round(4).to_dict(),
        "corr_jiv_vs_jpx_base_vol": float(np.corrcoef(joined.jiv, joined.base_vol)[0, 1]) if len(joined) > 2 else None,
    }


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, float_format="%.10g")


def read_only(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)


def run(market_db: Path, jquants_db: Path, vix_csv: Path, output: Path, symbols: list[str], config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    with read_only(jquants_db) as conn:
        topix = before_cutoff(pd.read_sql_query("SELECT date, close FROM topix_daily ORDER BY date", conn))
        atm = before_cutoff(pd.read_sql_query("SELECT date, days_to_sq, atm_iv, base_vol FROM n225_option_atm", conn))
        calendar = pd.read_sql_query("SELECT date, hol_div FROM trading_calendar", conn)
    jiv = iv30(atm, config)
    japan = japan_panel(topix, jiv, load_vix(vix_csv), config)
    panels, oos, overlays, overlay_summary, coefficients = {}, {}, [], [], []
    with read_only(market_db) as conn:
        for symbol in symbols:
            bars = load_bars(conn, symbol)
            panels[symbol] = asset_panel(japan, bars, config)
            for h in config.horizons_days:
                oos[(symbol, h)] = oos_forecasts(panels[symbol], h, config)
                coefficients += [dict(asset=symbol, **row) for row in in_sample_fit(panels[symbol], h, config)]
            periods, rows = overlay(oos[(symbol, min(config.horizons_days))], bars, config)
            overlays.append(periods.assign(asset=symbol))
            overlay_summary += [dict(asset=symbol, **row) for row in rows]
            del bars
    tests, by_year = forecast_tests(oos, config)
    events_summary, events_by_year, events = event_study(panels, config)
    output.mkdir(parents=True, exist_ok=True)
    outputs = {
        "panel.csv": pd.concat([p.assign(asset=s) for s, p in panels.items()], ignore_index=True),
        "oos_forecasts.csv": pd.concat([f.assign(asset=s, horizon_days=h) for (s, h), f in oos.items()],
                                       ignore_index=True),
        "forecast_tests.csv": tests, "forecast_by_year.csv": by_year,
        "in_sample_coefficients.csv": pd.DataFrame(coefficients),
        "overlay_periods.csv": pd.concat(overlays, ignore_index=True),
        "overlay_summary.csv": pd.DataFrame(overlay_summary),
        "event_summary.csv": events_summary, "event_by_year.csv": events_by_year, "events.csv": events,
    }
    for name, frame in outputs.items():
        write_csv(frame, output / name)
    primary = tests.loc[tests.family == "primary"] if not tests.empty else tests
    summary = {
        "question": "Does Nikkei 225 30-day ATM IV add out-of-sample information on BTC/ETH realised "
                    "volatility beyond crypto HAR terms and VIX?",
        "verdict": ("JIV adds robust incremental information" if not primary.empty and primary.supports_jiv.any()
                    else "no robust incremental information from JIV"),
        "decision_rule": f"primary BH q<{config.alpha}, OOS R2>0, positive in >={config.min_positive_years} "
                         f"calendar years and without {SHOCK_MONTH}",
        "primary": json.loads(primary.to_json(orient="records")) if not primary.empty else [],
        "data_checks": data_checks(japan, calendar, atm, jiv, config),
        "event_cells_q_below_alpha": int((events_summary.q_value < config.alpha).sum()) if len(events_summary) else 0,
        "notes": ["Retrospective, exploratory; not a sealed prospective test.",
                  "Outputs include J-Quants-derived daily values: private use only, do not publish or commit."],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=True) + "\n")
    stat = Path(market_db).stat()
    manifest = {
        "config": asdict(config), "symbols": symbols, "cutoff_ts": CUTOFF,
        "inputs": {"jquants_db_sha256": sha256_file(jquants_db), "vix_csv_sha256": sha256_file(vix_csv),
                   "market_db": {"path": str(market_db), "byte_size": stat.st_size, "mtime_ns": stat.st_mtime_ns}},
        "script_sha256": sha256_file(Path(__file__)),
        "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--market-db", type=Path, default=Path("/mnt/e/Datas/market/market.db"))
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--vix-csv", type=Path, default=Path("/mnt/e/Datas/market/indices/fred_VIXCLS.csv"))
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/japan_crossmarket")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    parser.add_argument("--oos-start", default=Config.oos_start)
    parser.add_argument("--bootstrap", type=int, default=Config.bootstrap)
    args = parser.parse_args()
    config = Config(oos_start=args.oos_start, bootstrap=args.bootstrap)
    summary = run(args.market_db, args.jquants_db, args.vix_csv, args.output_dir,
                  [s.strip() for s in args.symbols.split(",") if s.strip()], config)
    print(json.dumps({k: summary[k] for k in ("verdict", "decision_rule", "data_checks")}, indent=2))


if __name__ == "__main__":
    main()
