#!/usr/bin/env python3
"""Do crowded leveraged longs lose? A test of the project's profit source in an independent market.

Pre-registered in docs/jquants-closing-research.md (analysis D). The adopted BTC/ETH funding capture rests
on one claim: leveraged long demand overpays, and that is who loses to the strategy. Crypto has five years of
one regime; the Japanese margin balances cover ten years and about 3,000 stocks, each week, with the size of
the leveraged long book known stock by stock. Not a trading plan: Japanese stock long-short is not on the
project's venues.

D1  The margin long book: weights are the long balances; alpha against the market and size. Expected < 0.
D2  Stock level: Fama-MacBeth slope of next week's return on the long balance as a share of market cap,
    with size, turnover, reversal, momentum, volatility and sector controls. Expected < 0.
D3  The same with the four-week change of that share. Expected < 0.
Descriptive: deciles, the margin short book, subsets, and D2 after market stress (the analogue of the
liquidation reversion the project rejected in crypto; expected > 0, a rebound of the crowded names).

Margin balances are known two TSE days after the reference day, around 16:30 JST, so a decision is made at
the close of the third TSE day. Returns are split-adjusted price returns: the dividend data is outside the
plan, which understates the low-balance names and works against D2 and D3. Outputs hold J-Quants-derived
values and stay outside the repository, except the test statistics in RETAINED_DIR.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests.japan_positioning import benjamini_hochberg, decision_days, outside_repo, read_only, sha256_file  # noqa: E402
from backtests.japan_retained import write_summary  # noqa: E402

JQ_DIR = Path("/mnt/e/Datas/market/jquants")
CUTOFF_DATE = "2026-08-01"
SPLIT_DATE = "2022-01-01"
SHOCK_MONTHS = ("2020-03", "2024-08")
STOCK_CATEGORY = "011"  # domestic common stock
EXCLUDED_MARKETS = ("その他", "TOKYO PRO MARKET")
LENDABLE = "貸借"
WEEKS_PER_YEAR = 50  # decisions per year after holidays are close to this; only used to annualise


@dataclass(frozen=True)
class Config:
    publication_lag: int = 3  # TSE days from the reference day to the first close after publication
    min_price: float = 100.0
    min_market_cap: float = 5e9
    min_turnover: float = 2e7  # median JPY over the last 20 days
    momentum_days: int = 250
    reversal_days: int = 20
    change_weeks: int = 4
    tail: float = 0.01  # cross-sectional winsorisation of the signal and controls
    tail_return: float = 0.005
    min_stocks: int = 200
    hac_lags: int = 4
    alpha: float = 0.05
    stress_return: float = -0.04  # TOPIX over the last five TSE days
    deciles: int = 10

    def validate(self) -> None:
        if self.publication_lag < 3:
            raise ValueError("the balance is published after the close of the second TSE day")
        if self.change_weeks < 1 or self.reversal_days >= self.momentum_days or self.deciles < 2:
            raise ValueError("window settings are inconsistent")
        if not 0 < self.tail < 0.5 or not 0 < self.tail_return < 0.5 or not 0 < self.alpha < 1:
            raise ValueError("quantile or test settings out of range")


def weekly_schedule(business: np.ndarray, references: pd.DataFrame, lag: int) -> pd.DataFrame:
    """One row per reference day that ends an ISO week: the decision is `lag` TSE days after it and never
    before the TSE day after the published date. The balance has daily rows from 2026-09-28; they are skipped."""
    week_ends = set(decision_days(pd.Series(business)).date)
    r = references.loc[references.date.isin(week_ends)].drop_duplicates("date").sort_values("date").reset_index(drop=True)
    index = np.searchsorted(business, r.date)
    known = np.where(r.pub_date.notna(), np.searchsorted(business, r.pub_date.fillna(""), side="right"), 0)
    position = np.maximum(index + lag, known)
    fits = position < len(business)
    out = pd.DataFrame({"reference": r.date[fits], "decision": business[position[fits]]}).reset_index(drop=True)
    out["next_decision"] = out.decision.shift(-1)
    return out


def adjusted_log_prices(close: pd.DataFrame, adj_factor: pd.DataFrame) -> pd.DataFrame:
    """Wide log prices on the latest share basis, carried over days without trades."""
    return (np.log(close.where(close > 0)) - np.log(adj_factor.fillna(1.0)).cumsum()).ffill()


def winsorize(x: np.ndarray, tail: float) -> np.ndarray:
    lo, hi = np.nanquantile(x, [tail, 1 - tail])
    return np.clip(x, lo, hi)


def standardize(x: np.ndarray, tail: float) -> np.ndarray:
    w = winsorize(x, tail)
    return (w - np.nanmean(w)) / np.nanstd(w)


def cross_section_slope(y: np.ndarray, signal: np.ndarray, controls: list[np.ndarray], sectors: np.ndarray, config: Config) -> float:
    """Slope of return on the standardized signal in one week. Removing sector means equals sector dummies."""
    columns = [signal, *controls]
    ok = np.isfinite(y) & np.logical_and.reduce([np.isfinite(c) for c in columns]) & pd.notna(sectors)
    if ok.sum() < config.min_stocks:
        return np.nan
    X = np.column_stack([standardize(c[ok], config.tail) for c in columns])
    Y = winsorize(y[ok], config.tail_return)
    group = pd.Series(sectors[ok])
    X = X - pd.DataFrame(X).groupby(group.to_numpy()).transform("mean").to_numpy()
    Y = Y - pd.Series(Y).groupby(group.to_numpy()).transform("mean").to_numpy()
    return float(np.linalg.lstsq(X, Y, rcond=None)[0][0])


def decile_spread(y: np.ndarray, signal: np.ndarray, config: Config) -> float:
    """Equal-weighted raw return of the highest signal decile minus the lowest."""
    ok = np.isfinite(y) & np.isfinite(signal)
    if ok.sum() < config.min_stocks:
        return np.nan
    rank = pd.Series(signal[ok]).rank(pct=True, method="first").to_numpy()
    width = 1 / config.deciles
    return float(y[ok][rank > 1 - width].mean() - y[ok][rank <= width].mean())


def book_return(y: np.ndarray, value: np.ndarray) -> float:
    ok = np.isfinite(y) & np.isfinite(value) & (value > 0)
    return float((y[ok] * value[ok]).sum() / value[ok].sum()) if ok.any() else np.nan


def ols_hac(y: np.ndarray, X: np.ndarray, lags: int) -> tuple[np.ndarray, np.ndarray]:
    """Coefficients and Newey-West (Bartlett) standard errors; X holds its own intercept column."""
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    xu = X * (y - X @ beta)[:, None]
    s = xu.T @ xu
    for j in range(1, min(lags, len(y) - 1) + 1):
        g = xu[j:].T @ xu[:-j]
        s += (1 - j / (lags + 1)) * (g + g.T)
    return beta, np.sqrt(np.maximum(np.diag(xtx_inv @ s @ xtx_inv), 0))


def mean_test(x: np.ndarray, config: Config) -> tuple[float, float, float]:
    x = x[np.isfinite(x)]
    beta, se = ols_hac(x, np.ones((len(x), 1)), config.hac_lags)
    return float(beta[0]), float(se[0]), float(beta[0] / se[0]) if se[0] > 0 else np.nan


def book_alpha(frame: pd.DataFrame, column: str, config: Config) -> dict:
    p = frame.dropna(subset=[column, "market", "equal"])
    y = p[column].to_numpy()
    size = (p.equal - p.market).to_numpy()
    beta, se = ols_hac(y, np.column_stack([np.ones(len(p)), p.market.to_numpy(), size]), config.hac_lags)
    return {"weeks": int(len(p)), "alpha_weekly": float(beta[0]), "se_hac": float(se[0]),
            "t_hac": float(beta[0] / se[0]), "alpha_annual": float(beta[0] * WEEKS_PER_YEAR),
            "beta_market": float(beta[1]), "beta_size": float(beta[2]), "mean_excess_annual": float((y - p.market.to_numpy()).mean() * WEEKS_PER_YEAR)}


def split_masks(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    months = frame.decision.str[:7]
    return {"early": (frame.decision < SPLIT_DATE).to_numpy(), "late": (frame.decision >= SPLIT_DATE).to_numpy(),
            "calm": ~months.isin(SHOCK_MONTHS).to_numpy()}


def primary_family(weeks: pd.DataFrame, config: Config) -> pd.DataFrame:
    rows = []
    mask = split_masks(weeks)
    alpha = book_alpha(weeks, "long_book", config)
    row = {"test": "D1_long_book_alpha", "expected_sign": -1, "weeks": alpha["weeks"], "estimate": alpha["alpha_weekly"],
           "se_hac": alpha["se_hac"], "t_hac": alpha["t_hac"]}
    for name, m in mask.items():
        row[f"estimate_{name}"] = book_alpha(weeks.loc[m], "long_book", config)["alpha_weekly"]
    rows.append(row)
    for label, column in (("D2_balance_level", "slope_level"), ("D3_balance_change", "slope_change")):
        estimate, se, t = mean_test(weeks[column].to_numpy(float), config)
        row = {"test": label, "expected_sign": -1, "weeks": int(weeks[column].notna().sum()), "estimate": estimate, "se_hac": se, "t_hac": t}
        for name, m in mask.items():
            row[f"estimate_{name}"] = mean_test(weeks.loc[m, column].to_numpy(float), config)[0]
        rows.append(row)
    out = pd.DataFrame(rows)
    out["p_one_sided"] = stats.norm.sf(out.expected_sign * out.t_hac)
    out["q_bh"] = benjamini_hochberg(out.p_one_sided)
    same = lambda col: np.sign(out[col]) == out.expected_sign  # noqa: E731
    out["supports"] = (out.q_bh < config.alpha) & same("estimate_early") & same("estimate_late") & same("estimate_calm")
    return out


def load_wide(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    bars = pd.read_sql_query("SELECT date, code, close, adj_factor, market_cap, turnover FROM equities_daily ORDER BY date", conn)
    out = {c: bars.pivot(index="date", columns="code", values=c) for c in ("close", "adj_factor", "market_cap", "turnover")}
    return out


def membership(master: pd.DataFrame, day: str, codes: pd.Index) -> pd.DataFrame:
    """The master record in force on `day`, per code, so a stock's market and sector are as of then."""
    m = master.loc[(master.valid_from <= day) & (master.valid_to.isna() | (master.valid_to >= day))]
    return m.drop_duplicates("code", keep="last").set_index("code").reindex(codes)


def weekly_cross_sections(wide: dict[str, pd.DataFrame], margin: pd.DataFrame, master: pd.DataFrame, schedule: pd.DataFrame,
                          topix: pd.Series, config: Config) -> pd.DataFrame:
    close, cap, turnover = wide["close"], wide["market_cap"], wide["turnover"]
    log_price = adjusted_log_prices(close, wide["adj_factor"])
    returns = log_price.diff()
    days = close.index.to_numpy()
    codes = close.columns
    longs = margin.pivot(index="date", columns="code", values="long_shares").reindex(columns=codes)
    shorts = margin.pivot(index="date", columns="code", values="short_shares").reindex(columns=codes)
    history: dict[str, np.ndarray] = {}
    rows = []
    for w in schedule.itertuples():
        d = np.searchsorted(days, w.decision)
        if d < config.momentum_days or pd.isna(w.next_decision):
            history[w.reference] = None
            continue
        ref_close, ref_cap = close.loc[w.reference].to_numpy(float), cap.loc[w.reference].to_numpy(float)
        long_value = longs.loc[w.reference].to_numpy(float) * ref_close
        short_value = shorts.loc[w.reference].to_numpy(float) * ref_close
        with np.errstate(divide="ignore", invalid="ignore"):
            share = long_value / ref_cap
        history[w.reference] = share
        earlier = schedule.reference.iloc[w.Index - config.change_weeks] if w.Index >= config.change_weeks else None
        change = share - history[earlier] if earlier is not None and history.get(earlier) is not None else np.full(len(codes), np.nan)
        m = membership(master, w.decision, codes)
        now = log_price.iloc[d].to_numpy(float)
        y = np.expm1(log_price.loc[w.next_decision].to_numpy(float) - now)
        recent_turnover = np.nanmedian(turnover.iloc[d - 19:d + 1].to_numpy(float), axis=0)
        stock = (m.prod_cat == STOCK_CATEGORY).to_numpy() & ~m.mkt_nm.isin(EXCLUDED_MARKETS).to_numpy()
        tradable = np.isfinite(close.iloc[d].to_numpy(float))
        universe = (stock & tradable & (close.iloc[d].to_numpy(float) >= config.min_price)
                    & (cap.iloc[d].to_numpy(float) >= config.min_market_cap) & (recent_turnover >= config.min_turnover)
                    & np.isfinite(share))
        with np.errstate(divide="ignore", invalid="ignore"):
            controls = [np.log(cap.iloc[d].to_numpy(float)), np.log(recent_turnover), now - log_price.iloc[d - config.reversal_days].to_numpy(float),
                        log_price.iloc[d - config.reversal_days].to_numpy(float) - log_price.iloc[d - config.momentum_days].to_numpy(float),
                        np.nanstd(returns.iloc[d - 19:d + 1].to_numpy(float), axis=0)]
        keep = lambda x: np.where(universe, x, np.nan)  # noqa: E731
        sectors = m.s33.where(universe).to_numpy()
        kept_controls = [keep(c) for c in controls]
        row = {"reference": w.reference, "decision": w.decision, "stocks": int(universe.sum()),
               "slope_level": cross_section_slope(keep(y), keep(share), kept_controls, sectors, config),
               "slope_change": cross_section_slope(keep(y), keep(change), kept_controls, sectors, config),
               "decile_level": decile_spread(keep(y), keep(share), config), "decile_change": decile_spread(keep(y), keep(change), config)}
        lendable = universe & (m.mrgn_nm == LENDABLE).to_numpy()
        row["slope_level_lendable"] = cross_section_slope(np.where(lendable, y, np.nan), np.where(lendable, share, np.nan),
                                                          [np.where(lendable, c, np.nan) for c in controls], m.s33.where(lendable).to_numpy(), config)
        big = universe & (cap.iloc[d].to_numpy(float) >= np.nanmedian(np.where(universe, cap.iloc[d].to_numpy(float), np.nan)))
        row["slope_level_large"] = cross_section_slope(np.where(big, y, np.nan), np.where(big, share, np.nan),
                                                       [np.where(big, c, np.nan) for c in controls], m.s33.where(big).to_numpy(), config)
        row["long_book"], row["short_book"] = book_return(np.where(stock, y, np.nan), long_value), book_return(np.where(stock, y, np.nan), short_value)
        cap_now = np.where(stock & tradable, cap.iloc[d].to_numpy(float), np.nan)
        row["market"] = book_return(y, cap_now)
        row["equal"] = float(np.nanmean(np.where(np.isfinite(cap_now), y, np.nan)))
        row["top_decile_minus_universe"] = decile_top(keep(y), keep(share), config)
        row["topix_5d"] = float(topix.reindex(days).iloc[d] / topix.reindex(days).iloc[d - 5] - 1)
        rows.append(row)
    return pd.DataFrame(rows)


def decile_top(y: np.ndarray, signal: np.ndarray, config: Config) -> float:
    ok = np.isfinite(y) & np.isfinite(signal)
    if ok.sum() < config.min_stocks:
        return np.nan
    rank = pd.Series(signal[ok]).rank(pct=True, method="first").to_numpy()
    return float(y[ok][rank > 1 - 1 / config.deciles].mean() - y[ok].mean())


def describe(weeks: pd.DataFrame, config: Config) -> dict:
    out = {}
    for column in ("decile_level", "decile_change", "top_decile_minus_universe", "slope_level_lendable", "slope_level_large"):
        m, s, t = mean_test(weeks[column].to_numpy(float), config)
        out[column] = {"weekly_mean": m, "annual_mean": m * WEEKS_PER_YEAR, "t_hac": t}
    out["short_book"] = book_alpha(weeks, "short_book", config)
    out["long_book"] = book_alpha(weeks, "long_book", config)
    stress = (weeks.topix_5d <= config.stress_return).to_numpy()
    slopes = weeks.slope_level.to_numpy(float)
    for name, mask in (("after_stress", stress), ("otherwise", ~stress)):
        m, s, t = mean_test(slopes[mask], config)
        out[f"slope_level_{name}"] = {"weeks": int(np.isfinite(slopes[mask]).sum()), "weekly_mean": m, "t_hac": t}
    return out


def by_year(weeks: pd.DataFrame) -> pd.DataFrame:
    frame = weeks.assign(year=weeks.decision.str[:4])
    return frame.groupby("year").agg(weeks=("slope_level", "count"), slope_level=("slope_level", "mean"),
                                     slope_change=("slope_change", "mean"), decile_level=("decile_level", "sum"),
                                     long_minus_market=("long_book", "sum"), market=("market", "sum")).reset_index()


def run(jquants_db: Path, output: Path, config: Config) -> dict:
    config.validate()
    warnings.filterwarnings("ignore", message="(All-NaN slice|Degrees of freedom|Mean of empty)", category=RuntimeWarning)
    output = outside_repo(output)
    with read_only(jquants_db) as conn:
        wide = load_wide(conn)
        margin = pd.read_sql_query("SELECT date, pub_date, code, long_shares, short_shares FROM margin_weekly WHERE date < ?", conn, params=(CUTOFF_DATE,))
        master = pd.read_sql_query("SELECT code, valid_from, valid_to, s33, mkt_nm, mrgn_nm, prod_cat FROM equities_master_history ORDER BY code, valid_from", conn)
        topix = pd.read_sql_query("SELECT date, close FROM topix_daily WHERE close IS NOT NULL", conn).set_index("date").close
    business = wide["close"].index[wide["close"].notna().any(axis=1)].to_numpy()
    for key in wide:
        wide[key] = wide[key].loc[business]
    schedule = weekly_schedule(business, margin[["date", "pub_date"]].drop_duplicates("date"), config.publication_lag)
    weeks = weekly_cross_sections(wide, margin, master, schedule, topix, config)
    last_ok = schedule.loc[schedule.next_decision < CUTOFF_DATE, "decision"]
    weeks = weeks.loc[weeks.decision.isin(last_ok)].reset_index(drop=True)
    tests = primary_family(weeks, config)
    output.mkdir(parents=True, exist_ok=True)
    weeks.to_csv(output / "weekly.csv", index=False)
    tests.to_csv(output / "tests.csv", index=False)
    summary = {"question": "Do crowded leveraged longs lose? (Japan margin balances, weekly)",
               "verdict": "supported" if tests.supports.all() else ("partly_supported" if tests.supports.any() else "not_supported"),
               "config": asdict(config), "period": [weeks.decision.iloc[0], weeks.decision.iloc[-1]], "weeks": len(weeks),
               "median_stocks": float(weeks.stocks.median()), "tests": tests.to_dict("records"), "descriptive": describe(weeks, config),
               "by_year": by_year(weeks).to_dict("records")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float) + "\n")
    (output / "manifest.json").write_text(json.dumps({"inputs": {str(jquants_db): sha256_file(jquants_db)}}, indent=1) + "\n")
    write_summary(summary, "margin_crowding")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--output", type=Path, default=JQ_DIR / "results/japan_margin_crowding")
    args = parser.parse_args()
    out = run(args.jquants_db, args.output, Config())
    print(json.dumps({k: out[k] for k in ("verdict", "period", "weeks", "median_stocks")}, ensure_ascii=False))
    print(pd.DataFrame(out["tests"]).round(6).T.to_string())
    print(json.dumps(out["descriptive"], indent=1, default=float))


if __name__ == "__main__":
    main()
