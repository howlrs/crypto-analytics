#!/usr/bin/env python3
"""Do TSE crypto-linked stocks lead BTC and ETH?

Pre-registered in docs/jquants-closing-research.md (analysis C). PR #14 and #15 tested Japanese macro and
positioning data against crypto. The direct link was left: Japanese retail buys BTC exposure through
listed stocks (the tax on a stock gain is lower than on crypto), and treasury companies turn that demand
into BTC purchases. If the demand comes first, the part of a TSE close-to-close move that BTC and TOPIX do
not explain should carry over to BTC in the next 24 hours.

Each stock's residual on BTC (same clock window) and TOPIX uses betas fitted on the previous 60 TSE days
only, and is divided by the fit's residual standard deviation. A basket averages the stocks' residuals. The
target is the BTC or ETH log return from that TSE close to the next one, Binance perp, before the research
boundary 2026-08-01. Inputs are read-only; outputs hold J-Quants-derived values and stay outside the
repository, except the test statistics in RETAINED_DIR.
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
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests.japan_positioning import benjamini_hochberg, newey_west_slope, outside_repo, read_only, rule_pnl, sha256_file  # noqa: E402
from backtests.japan_retained import write_summary  # noqa: E402

JQ_DIR = Path("/mnt/e/Datas/market/jquants")
MARKET_DB = Path("/mnt/e/Datas/market/market.db")
HOUR = 3_600_000
CUTOFF_MS = 1785542400000  # 2026-08-01T00:00:00Z, the research boundary and the end of market.db
LONGER_SESSION = "2024-11-05"  # the TSE close moved from 15:00 to 15:30 JST
# code -> first day it counts. Fixed from public knowledge before any return was computed.
BASKETS = {
    "treasury": {"33500": "2024-05-13",  # Metaplanet adopts BTC as its treasury strategy
                 "38250": "2024-09-26",  # Remixpoint's first BTC purchase
                 "31890": "2025-04-01"},  # ANAP: the earliest purchase found in its disclosures is 2025-04
    "exchange": {"86980": "2021-01-04",  # Monex Group (Coincheck)
                 "84730": "2021-01-04",  # SBI Holdings (SBI VC Trade, BITPoint)
                 "94490": "2021-01-04"},  # GMO Internet Group (GMO Coin)
}
ASSETS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
SHOCK_MONTHS = ("2022-05", "2022-11", "2024-08")


@dataclass(frozen=True)
class Config:
    window: int = 60
    min_obs: int = 40
    hac_lags: int = 5
    alpha: float = 0.05
    cost_bp: float = 5.0  # per unit of position turnover in a BTC or ETH perp
    min_days: int = 120

    def validate(self) -> None:
        if not 3 <= self.min_obs <= self.window:
            raise ValueError("window settings are inconsistent")
        if self.hac_lags < 1 or not 0 < self.alpha < 1 or self.cost_bp < 0 or self.min_days < 30:
            raise ValueError("test settings out of range")


def midnight_ms(dates: pd.Series) -> np.ndarray:
    days = pd.to_datetime(pd.Series(dates).reset_index(drop=True)).to_numpy("datetime64[D]").astype(np.int64)
    return days * 24 * HOUR


def tse_clock(dates: pd.Series) -> pd.DataFrame:
    """UTC milliseconds of each session: the open at 09:00 JST is midnight UTC, the close moved in 2024-11."""
    d = pd.Series(dates).reset_index(drop=True)
    opens = midnight_ms(d)
    closes = opens + np.where(d < LONGER_SESSION, 6 * HOUR, 6 * HOUR + HOUR // 2)
    out = pd.DataFrame({"date": d, "open_ms": opens, "close_ms": closes})
    out["prev_close_ms"] = out.close_ms.shift()
    out["next_close_ms"] = out.close_ms.shift(-1)
    out["next_open_ms"] = out.open_ms.shift(-1)
    return out


def adjusted_log_close(bars: pd.DataFrame) -> pd.Series:
    """Log close on today's share basis; on a split day adj_factor scales yesterday's close to today's."""
    b = bars.sort_values("date")
    level = np.log(b.close.astype(float).where(b.close > 0))
    return (level - np.log(b.adj_factor.astype(float).fillna(1.0)).cumsum()).ffill().set_axis(b.date)


def standardized_residuals(y: np.ndarray, x: np.ndarray, window: int, min_obs: int) -> np.ndarray:
    """y[t] minus its fit on x[t], with coefficients from the previous `window` rows, over that fit's residual sd."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    if x.ndim == 1:
        x = x[:, None]
    out = np.full(len(y), np.nan)
    for t in range(len(y)):
        if not (np.isfinite(y[t]) and np.isfinite(x[t]).all()):
            continue
        lo = max(0, t - window)
        ok = np.isfinite(y[lo:t]) & np.isfinite(x[lo:t]).all(axis=1)
        if ok.sum() < min_obs:
            continue
        A = np.column_stack([np.ones(ok.sum()), x[lo:t][ok]])
        beta, *_ = np.linalg.lstsq(A, y[lo:t][ok], rcond=None)
        res = y[lo:t][ok] - A @ beta
        sd = np.sqrt(res @ res / (len(res) - A.shape[1]))
        if sd > 0:
            out[t] = (y[t] - np.r_[1.0, x[t]] @ beta) / sd
    return out


def basket_signal(panel: pd.DataFrame, closes: dict[str, pd.Series], starts: dict[str, str], config: Config) -> np.ndarray:
    """Mean over the stocks that count that day. Days before a stock's start stay out of its fits too."""
    x = panel[["btc_window", "topix_ret"]].to_numpy(float)
    rows = []
    for code, start in starts.items():
        stock = closes[code].reindex(panel.date).diff().to_numpy(float, copy=True)
        stock[(panel.date < start).to_numpy()] = np.nan
        rows.append(standardized_residuals(stock, x, config.window, config.min_obs))
    matrix = np.vstack(rows)
    count = np.isfinite(matrix).sum(axis=0)
    with np.errstate(invalid="ignore"):
        return np.where(count > 0, np.nansum(matrix, axis=0) / np.maximum(count, 1), np.nan)


def evaluate(panel: pd.DataFrame, signal: str, target: str, config: Config, sign: int = 1) -> dict:
    z, y = panel[signal].to_numpy(float), panel[target].to_numpy(float)
    ok = np.isfinite(z) & np.isfinite(y)
    row = {"signal": signal, "target": target, "expected_sign": sign, "days": int(ok.sum())}
    if ok.sum() < config.min_days:
        return row
    used = panel.loc[ok]
    middle = used.date.iloc[len(used) // 2]
    early = ok & (panel.date < middle).to_numpy()
    late = ok & (panel.date >= middle).to_numpy()
    calm = ok & ~panel.date.str[:7].isin(SHOCK_MONTHS).to_numpy()
    beta, se = newey_west_slope(y[ok], z[ok], config.hac_lags)
    t = beta / se if se > 0 else np.nan
    pnl = rule_pnl(z[ok], np.expm1(y[ok]), sign, config.cost_bp)
    row.update({"first": used.date.iloc[0], "last": used.date.iloc[-1], "split": middle, "slope": beta, "se_hac": se,
                "t_hac": t, "p_one_sided": float(stats.norm.sf(sign * t)), "corr": float(np.corrcoef(z[ok], y[ok])[0, 1]),
                "slope_early": newey_west_slope(y[early], z[early], config.hac_lags)[0],
                "slope_late": newey_west_slope(y[late], z[late], config.hac_lags)[0],
                "slope_calm": newey_west_slope(y[calm], z[calm], config.hac_lags)[0],
                "rule_mean_daily": float(pnl.mean()), "rule_sharpe_annual": float(pnl.mean() / pnl.std(ddof=1) * np.sqrt(245))})
    return row


def primary_family(rows: list[dict], config: Config) -> pd.DataFrame:
    out = pd.DataFrame(rows)
    out["q_bh"] = benjamini_hochberg(out.p_one_sided)
    same = lambda col: np.sign(out[col]) == out.expected_sign  # noqa: E731
    out["supports"] = ((out.q_bh < config.alpha) & same("slope_early") & same("slope_late") & same("slope_calm")
                       & (out.rule_mean_daily > 0))
    return out


def load_opens(conn: sqlite3.Connection, symbol: str, stamps: np.ndarray) -> np.ndarray:
    """The perp's open at each minute; NaN when the minute is missing or the stamp is unknown."""
    wanted = sorted({int(x) for x in stamps[np.isfinite(stamps)]})
    found: dict[int, float] = {}
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        marks = ",".join("?" * len(chunk))
        found.update(conn.execute(f"SELECT ts, open FROM klines WHERE venue='binance' AND market='perp' AND symbol=? "
                                  f"AND ts IN ({marks})", [symbol, *chunk]).fetchall())
    return np.array([found.get(int(x), np.nan) if np.isfinite(x) else np.nan for x in stamps])


def build_panel(conn_jq: sqlite3.Connection, conn_market: sqlite3.Connection) -> tuple[pd.DataFrame, dict[str, pd.Series]]:
    topix = pd.read_sql_query("SELECT date, close FROM topix_daily WHERE close IS NOT NULL ORDER BY date", conn_jq)
    panel = tse_clock(topix.date)
    panel["topix_ret"] = np.log(topix.close).diff().to_numpy()
    for asset, symbol in ASSETS.items():
        at_close = load_opens(conn_market, symbol, panel.close_ms.to_numpy(float))
        panel[f"{asset}_window"] = np.log(at_close / np.r_[np.nan, at_close[:-1]])
        panel[f"{asset}_next"] = np.log(np.r_[at_close[1:], np.nan] / at_close)
        panel[f"{asset}_overnight"] = np.log(load_opens(conn_market, symbol, panel.next_open_ms.to_numpy(float)) / at_close)
    late = panel.next_close_ms >= CUTOFF_MS
    panel.loc[late, [f"{a}_{w}" for a in ASSETS for w in ("next", "overnight")]] = np.nan
    codes = sorted({c for b in BASKETS.values() for c in b})
    marks = ",".join("?" * len(codes))
    bars = pd.read_sql_query(f"SELECT date, code, close, adj_factor FROM equities_daily WHERE code IN ({marks})", conn_jq, params=codes)
    return panel, {code: adjusted_log_close(part) for code, part in bars.groupby("code")}


def run(jquants_db: Path, market_db: Path, output: Path, config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    with read_only(jquants_db) as jq, read_only(market_db) as market:
        panel, closes = build_panel(jq, market)
    for name, starts in BASKETS.items():
        panel[f"z_{name}"] = basket_signal(panel, closes, starts, config)
    primary = primary_family([evaluate(panel, f"z_{b}", f"{a}_next", config) for b in BASKETS for a in ASSETS], config)
    secondary = primary_family([evaluate(panel, f"z_{b}", f"{a}_overnight", config) for b in BASKETS for a in ASSETS], config)
    linked = {}
    for name, starts in BASKETS.items():
        for code, start in starts.items():  # do these stocks move with BTC at all? The premise of the test.
            r = closes[code].reindex(panel.date).diff().to_numpy(float)
            ok = np.isfinite(r) & np.isfinite(panel.btc_window.to_numpy(float)) & (panel.date >= start).to_numpy()
            ok &= (panel.next_close_ms < CUTOFF_MS).to_numpy()
            linked[code] = {"basket": name, "days": int(ok.sum()), "corr_with_btc_same_window": float(np.corrcoef(r[ok], panel.btc_window.to_numpy(float)[ok])[0, 1])}
    output.mkdir(parents=True, exist_ok=True)
    panel.to_csv(output / "panel.csv", index=False)
    primary.to_csv(output / "tests_primary.csv", index=False)
    secondary.to_csv(output / "tests_secondary.csv", index=False)
    verdict = "supported" if primary.supports.any() else "not_supported"
    summary = {"question": "Do TSE crypto-linked stocks lead BTC and ETH over the next 24 hours?", "verdict": verdict,
               "config": asdict(config), "baskets": BASKETS, "primary": primary.to_dict("records"),
               "secondary_next_open": secondary.to_dict("records"), "premise_corr_with_btc": linked}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float) + "\n")
    (output / "manifest.json").write_text(json.dumps({"inputs": {str(p): sha256_file(p) for p in (jquants_db,)}}, indent=1) + "\n")
    write_summary(summary, "crypto_equities")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--market-db", type=Path, default=MARKET_DB)
    parser.add_argument("--output", type=Path, default=JQ_DIR / "results/japan_crypto_equities")
    args = parser.parse_args()
    out = run(args.jquants_db, args.market_db, args.output, Config())
    print(json.dumps({"verdict": out["verdict"]}))
    print(pd.DataFrame(out["primary"]).drop(columns=["first", "last", "split"]).round(5).to_string())


if __name__ == "__main__":
    main()
