#!/usr/bin/env python3
"""A risk card for each liquid TSE stock, kept for after the J-Quants data is gone.

Hyperliquid's xyz market lists new Asian stock perps often. Sizing the KIOXIA and SOFTBANK carry took ten
years of TSE prices and earnings dates (docs/japan-perp-forward.md section 2), which will not be on this
machine after the cancellation. These cards hold the same aggregates, one row per stock, for the
stocks that could plausibly list next. Each row has jump sizes, session statistics and the leverage the
registered rule would give (backtests/japan_earnings_risk.py), and no dated values.

The table lives in RETAINED_DIR and is not published. TSE daily limits cap what these prices show; a perp has
no limit, so treat every jump here as a lower bound (KIOXIA's perp rose +32% across a close when the TSE
close rose +17.7%).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests import japan_earnings_risk as earnings  # noqa: E402
from backtests.japan_positioning import read_only  # noqa: E402
from backtests.japan_retained import RETAINED_DIR, write_summary, write_table  # noqa: E402

JQ_DIR = Path("/mnt/e/Datas/market/jquants")
TOP_STOCKS = 500
ETFS = ("13210", "13060")  # Nikkei 225 and TOPIX: the index level of a card, not a listing candidate
TRADING_DAYS = 245
BETA_DAYS = 750
TURNOVER_DAYS = 250
MIN_DAYS = 250


def session_returns(bars: pd.DataFrame) -> pd.DataFrame:
    """Overnight and intraday log returns; the overnight leg is adjusted by the day's split factor."""
    b = bars.sort_values("date").reset_index(drop=True)
    prev = b.close.shift() * b.adj_factor.fillna(1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return pd.DataFrame({"date": b.date, "overnight": np.log(b.open / prev), "intraday": np.log(b.close / b.open)}).replace([np.inf, -np.inf], np.nan)


def rule_leverage(worst: float, config: earnings.Config) -> float:
    chosen = earnings.choose_leverage(worst, config)
    return np.nan if chosen is None else chosen


def card(bars: pd.DataFrame, topix: pd.Series, reactions: set[str], config: earnings.Config) -> dict | None:
    moves = earnings.daily_moves(bars)
    if len(moves) < MIN_DAYS:
        return None
    sessions = session_returns(bars)
    on_earnings = moves.date.isin(reactions)
    r = moves.set_index("date").ret_close
    joined = pd.concat([np.log1p(r), np.log(topix).diff()], axis=1, keys=["stock", "topix"]).dropna().tail(BETA_DAYS)
    beta = float(np.cov(joined.stock, joined.topix)[0, 1] / joined.topix.var()) if len(joined) >= MIN_DAYS else np.nan
    worst_up = float(moves.rise_high.max())
    worst_down = float(-moves.fall_low.min())
    sign_share = lambda x: float(x.mean())  # noqa: E731
    return {
        "days": int(len(moves)),
        "median_turnover_250d_jpy": float(bars.sort_values("date").turnover.tail(TURNOVER_DAYS).median()),
        "worst_rise_from_prev_close": worst_up, "worst_fall_from_prev_close": worst_down,
        "p99_abs_close_change": float(moves.ret_close.abs().quantile(0.99)), "mean_abs_close_change": float(moves.ret_close.abs().mean()),
        "overnight_mean_annual": float(sessions.overnight.mean() * TRADING_DAYS), "intraday_mean_annual": float(sessions.intraday.mean() * TRADING_DAYS),
        "overnight_vol_annual": float(sessions.overnight.std() * np.sqrt(TRADING_DAYS)), "intraday_vol_annual": float(sessions.intraday.std() * np.sqrt(TRADING_DAYS)),
        "earnings_days": int(on_earnings.sum()),
        "earnings_mean_abs_change": float(moves.ret_close[on_earnings].abs().mean()) if on_earnings.any() else np.nan,
        "other_days_mean_abs_change": float(moves.ret_close[~on_earnings].abs().mean()),
        "limit_up_share": sign_share(bars.upper_limit.fillna(0) == 1), "limit_down_share": sign_share(bars.lower_limit.fillna(0) == 1),
        "beta_topix_750d": beta,
        "rule_leverage_short": rule_leverage(worst_up, config), "rule_leverage_long": rule_leverage(worst_down, config),
    }


def choose_universe(conn: sqlite3.Connection) -> pd.DataFrame:
    last = conn.execute("SELECT max(date) FROM equities_daily").fetchone()[0]
    start = conn.execute("SELECT date FROM (SELECT DISTINCT date FROM equities_daily ORDER BY date DESC LIMIT ?) ORDER BY date LIMIT 1", (TURNOVER_DAYS,)).fetchone()[0]
    active = pd.read_sql_query("SELECT code, co_name_en, mkt_nm, s33_nm FROM equities_master_history WHERE prod_cat='011' AND mkt_nm IN ('プライム','スタンダード','グロース') AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)", conn, params=(last, last))
    active = active.drop_duplicates("code", keep="last")
    turnover = pd.read_sql_query("SELECT code, turnover FROM equities_daily WHERE date>=?", conn, params=(start,)).groupby("code").turnover.median()
    chosen = active.assign(turnover=active.code.map(turnover)).sort_values("turnover", ascending=False).head(TOP_STOCKS)
    etfs = pd.read_sql_query(f"SELECT DISTINCT code, co_name_en, mkt_nm, s33_nm FROM equities_master_history WHERE code IN ({','.join('?' * len(ETFS))})", conn, params=ETFS).drop_duplicates("code", keep="last")
    return pd.concat([chosen.drop(columns="turnover"), etfs], ignore_index=True)


def run(jquants_db: Path, directory: Path) -> dict:
    config = earnings.Config()
    config.validate()
    with read_only(jquants_db) as conn:
        universe = choose_universe(conn)
        codes = universe.code.tolist()
        marks = ",".join("?" * len(codes))
        bars = pd.read_sql_query(f"SELECT date, code, open, high, low, close, adj_factor, turnover, upper_limit, lower_limit FROM equities_daily WHERE code IN ({marks}) ORDER BY code, date", conn, params=codes)
        filings = pd.read_sql_query(f"SELECT code, disc_date, disc_time, doc_type FROM fins_summary WHERE code IN ({marks})", conn, params=codes)
        topix = pd.read_sql_query("SELECT date, close FROM topix_daily WHERE close IS NOT NULL", conn).set_index("date").close
    business = np.array(sorted(bars.date.unique()), dtype=object)
    rows = []
    by_code = dict(tuple(filings.groupby("code")))
    for code, part in bars.groupby("code"):
        reactions = set()
        if code in by_code:
            reactions = set(earnings.earnings_days(by_code[code], business).reaction)
        values = card(part, topix, reactions, config)
        if values is not None:
            rows.append({"code": code, **values})
    cards = universe.merge(pd.DataFrame(rows), on="code").sort_values("median_turnover_250d_jpy", ascending=False)
    target = write_table(cards, "risk_cards", ["code"], "days", directory)
    fits = cards.rule_leverage_short.notna()
    summary = {"stocks": int(len(cards)), "rule": {"leverages": list(config.leverages), "buffer": config.buffer},
               "short_side_leverage_counts": cards.rule_leverage_short.value_counts(dropna=False).rename(str).to_dict(),
               "no_leverage_fits_short": int((~fits).sum()),
               "median_worst_rise": float(cards.worst_rise_from_prev_close.median()),
               "median_earnings_vs_other_days": float((cards.earnings_mean_abs_change / cards.other_days_mean_abs_change).median())}
    summary["short_side_leverage_counts"] = {str(k): int(v) for k, v in summary["short_side_leverage_counts"].items()}
    write_summary(summary, "risk_cards", directory)
    return {"path": str(target), **summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--directory", type=Path, default=RETAINED_DIR)
    args = parser.parse_args()
    print(json.dumps(run(args.jquants_db, args.directory), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
