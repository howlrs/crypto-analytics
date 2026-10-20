#!/usr/bin/env python3
"""Do Japanese positioning data (J-Quants Standard) predict next-week Nikkei 225 returns?

Pre-registered in docs/jquants-standard.md (section 4). Three weekly signals, each known before
the decision close of the last TSE business day of an ISO week:
  S1 foreign investors' net buying share (investor types, TokyoNagoya), expected sign +
  S2 log ratio of long to short margin balances valued at the reference-date close, sign -
  S3 short-selling share of selling value over 33 sectors, five-day mean, sign +
The target is the Nikkei 225 log return to the next decision close (tradable as Hyperliquid
xyz:JP225); BTC over the same weeks is a descriptive secondary family. Retrospective, not a
sealed prospective test. Inputs are opened read-only; outputs contain J-Quants-derived values
and are refused inside this public repository.
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
DAY = 86_400_000
CLOSE_OFFSET = 6 * 3_600_000 + 30 * MINUTE  # 15:30 JST
CUTOFF_DATE = "2026-08-01"
ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
SIGNALS = {"s1_foreign_flow": 1, "s2_margin_ratio": -1, "s3_short_ratio": 1}  # pre-registered signs
SHOCK_MONTHS = ("2020-03", "2024-08")
TOTAL_SECTOR = "9999"  # ETFs and others, outside the 33 stock sectors


@dataclass(frozen=True)
class Config:
    z_window: int = 52
    z_min_finite: int = 45
    max_signal_age_days: int = 14
    short_ratio_days: int = 5
    margin_publication_lag: int = 2  # TSE business days after the reference date
    hac_lags: int = 4
    alpha: float = 0.05
    split_date: str = "2022-01-01"
    cost_bp: float = 3.0  # per unit of position turnover on xyz:JP225
    max_target_gap_days: int = 10

    def validate(self) -> None:
        if not 0 < self.z_min_finite <= self.z_window:
            raise ValueError("z window settings are inconsistent")
        if min(self.max_signal_age_days, self.short_ratio_days, self.hac_lags, self.max_target_gap_days) < 1:
            raise ValueError("window settings must be positive")
        if self.margin_publication_lag < 0 or not 0 < self.alpha < 1 or self.cost_bp < 0:
            raise ValueError("lag, alpha or cost out of range")


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("J-Quants-derived outputs must stay outside the public repository")
    return resolved


def decision_days(business_days: pd.Series) -> pd.DataFrame:
    """The last TSE business day of each ISO week, with the next one as the target end."""
    days = pd.to_datetime(pd.Series(sorted(set(business_days))))
    iso = days.dt.isocalendar()
    last = days.groupby([iso.year, iso.week]).max().sort_values().reset_index(drop=True)
    frame = pd.DataFrame({"date": last.dt.strftime("%Y-%m-%d")})
    frame["next_date"] = frame.date.shift(-1)
    return frame


def latest_known(decisions: pd.Series, available: pd.Series, reference: pd.Series, values: pd.Series,
                 max_age_days: int) -> np.ndarray:
    """For each decision date, the value with the latest reference date among rows available
    strictly before the decision (ties: the later-available row); NaN when that reference is
    more than max_age_days old."""
    table = pd.DataFrame({"available": available.values, "reference": reference.values, "value": values.values})
    table = table.dropna(subset=["available", "reference"]).sort_values(["available", "reference"]).reset_index(drop=True)
    out = np.full(len(decisions), np.nan)
    if table.empty:
        return out
    ref = pd.to_datetime(table.reference).to_numpy("datetime64[D]").astype(np.int64)
    best = np.zeros(len(ref), np.int64)  # running argmax of the reference date, later rows win ties
    for k in range(1, len(ref)):
        best[k] = k if ref[k] >= ref[best[k - 1]] else best[k - 1]
    avail = table.available.to_numpy(str)
    val = table.value.to_numpy(float)
    for i, d in enumerate(decisions):
        k = np.searchsorted(avail, d, side="left") - 1
        if k >= 0 and np.datetime64(d, "D").astype(np.int64) - ref[best[k]] <= max_age_days:
            out[i] = val[best[k]]
    return out


def foreign_flow(investor: pd.DataFrame) -> pd.DataFrame:
    rows = investor.loc[investor.section == "TokyoNagoya"].copy()
    rows["value"] = rows.frgn_bal / rows.tot_tot
    return rows.rename(columns={"pub_date": "available", "en_date": "reference"})[["available", "reference", "value"]]


def nth_business_day_after(dates: pd.Series, business: np.ndarray, n: int) -> pd.Series:
    """The n-th TSE business day strictly after each date (business is sorted ISO strings)."""
    idx = np.searchsorted(business, dates.to_numpy(str), side="right") + n - 1
    out = np.where(idx < len(business), business[np.minimum(idx, len(business) - 1)], None)
    return pd.Series(out, index=dates.index)


def last_business_on_or_before(dates: pd.Series, business: np.ndarray) -> pd.Series:
    idx = np.searchsorted(business, dates.to_numpy(str), side="right") - 1
    return pd.Series(np.where(idx >= 0, business[np.maximum(idx, 0)], None), index=dates.index)


def margin_ratio(margin: pd.DataFrame, closes: pd.DataFrame, business: np.ndarray, lag: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ln(sum long value / sum short value) per reference date, valued at that date's raw close."""
    m = margin.copy()
    m["price_date"] = last_business_on_or_before(m.date, business)
    m = m.merge(closes.rename(columns={"date": "price_date"}), on=["price_date", "code"], how="left")
    priced = m.close.notna() & (m.close > 0)
    m["long_value"] = np.where(priced, m.long_shares * m.close, np.nan)
    m["short_value"] = np.where(priced, m.short_shares * m.close, np.nan)
    g = m.groupby("date")
    weekly = pd.DataFrame({"long_value": g.long_value.sum(), "short_value": g.short_value.sum(),
                           "codes": g.code.size(), "codes_priced": g.close.count(),
                           "long_shares_unpriced_share": g.apply(
                               lambda x: x.long_shares[x.close.isna()].sum() / x.long_shares.sum()
                               if x.long_shares.sum() > 0 else np.nan, include_groups=False)}).reset_index()
    weekly["value"] = np.log(weekly.long_value / weekly.short_value)
    # Publication day: J-Quants PubDate when filled (empty in early years), never earlier than the
    # JPX schedule of the lag-th business day after the reference date. Usable after that day.
    inferred = nth_business_day_after(weekly.date, business, lag)
    published = weekly.date.map(margin.dropna(subset=["pub_date"]).groupby("date").pub_date.max())
    weekly["available"] = [(max(a, b) if isinstance(b, str) else a) if isinstance(a, str) else None
                           for a, b in zip(inferred, published)]
    weekly = weekly.rename(columns={"date": "reference"})
    return weekly[["available", "reference", "value"]], weekly


def short_ratio_daily(short: pd.DataFrame) -> pd.DataFrame:
    stocks = short.loc[short.s33 != TOTAL_SECTOR]
    g = stocks.groupby("date")
    shorts = g.short_with_res_va.sum() + g.short_no_res_va.sum()
    total = shorts + g.sell_ex_short_va.sum()
    return pd.DataFrame({"date": shorts.index, "ratio": (shorts / total).to_numpy(), "sectors": g.s33.nunique().to_numpy()})


def short_ratio_signal(decisions: pd.Series, daily: pd.DataFrame, business: np.ndarray, days: int) -> np.ndarray:
    """Mean of the short ratio over the `days` TSE business days before each decision; all must exist."""
    series = daily.set_index("date").ratio
    out = np.full(len(decisions), np.nan)
    for i, d in enumerate(decisions):
        k = np.searchsorted(business, d, side="left")
        window = business[max(k - days, 0):k]
        if len(window) == days and all(w in series.index for w in window):
            out[i] = float(series.loc[list(window)].mean())
    return out


def trailing_z(x: np.ndarray, window: int, min_finite: int) -> np.ndarray:
    x = np.asarray(x, float)
    z = np.full(len(x), np.nan)
    for i in range(len(x)):
        past = x[max(i - window, 0):i]
        past = past[np.isfinite(past)]
        if len(past) >= min_finite and np.isfinite(x[i]):
            sd = past.std(ddof=1)
            if sd > 0:
                z[i] = (x[i] - past.mean()) / sd
    return z


def newey_west_slope(y: np.ndarray, x: np.ndarray, lags: int) -> tuple[float, float]:
    """OLS slope of y on [1, x] with a Newey-West (Bartlett) standard error."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    X = np.column_stack([np.ones(len(x)), x])
    xtx_inv = np.linalg.inv(X.T @ X)
    beta = xtx_inv @ X.T @ y
    u = y - X @ beta
    xu = X * u[:, None]
    s = xu.T @ xu
    for j in range(1, min(lags, len(y) - 1) + 1):
        g = xu[j:].T @ xu[:-j]
        s += (1 - j / (lags + 1)) * (g + g.T)
    cov = xtx_inv @ s @ xtx_inv
    return float(beta[1]), float(np.sqrt(max(cov[1, 1], 0.0)))


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


def rule_pnl(z: np.ndarray, simple_ret: np.ndarray, sign: int, cost_bp: float) -> np.ndarray:
    """Weekly P&L of position sign * clip(z, -1, 1); a missing z means flat. Turnover pays cost."""
    w = np.where(np.isfinite(z), sign * np.clip(z, -1, 1), 0.0)
    turnover = np.abs(np.diff(np.r_[0.0, w]))
    return w * np.nan_to_num(simple_ret) - turnover * cost_bp / 10_000


def touches(frame: pd.DataFrame, months: tuple[str, ...]) -> np.ndarray:
    return (frame.date.str[:7].isin(months) | frame.next_date.fillna("").str[:7].isin(months)).to_numpy()


def test_signals(panel: pd.DataFrame, target: str, config: Config) -> pd.DataFrame:
    rows = []
    for name, sign in SIGNALS.items():
        z = panel[f"z_{name}"].to_numpy(float)
        y = panel[target].to_numpy(float)
        ok = np.isfinite(z) & np.isfinite(y)
        row = {"target": target, "signal": name, "expected_sign": sign, "weeks": int(ok.sum())}
        if ok.sum() < 30:
            rows.append(row)
            continue
        beta, se = newey_west_slope(y[ok], z[ok], config.hac_lags)
        t = beta / se if se > 0 else np.nan
        early = ok & (panel.date < config.split_date).to_numpy()
        late = ok & (panel.date >= config.split_date).to_numpy()
        calm = ok & ~touches(panel, SHOCK_MONTHS)
        pnl = rule_pnl(np.where(ok, z, np.nan), np.expm1(y), sign, config.cost_bp)
        row.update({
            "slope": beta, "se_hac": se, "t_hac": t, "p_one_sided": float(stats.norm.sf(sign * t)),
            "corr": float(np.corrcoef(z[ok], y[ok])[0, 1]),
            "slope_before_split": newey_west_slope(y[early], z[early], config.hac_lags)[0] if early.sum() >= 30 else np.nan,
            "slope_after_split": newey_west_slope(y[late], z[late], config.hac_lags)[0] if late.sum() >= 30 else np.nan,
            "slope_without_shock_months": newey_west_slope(y[calm], z[calm], config.hac_lags)[0],
            "rule_mean_weekly": float(pnl[ok].mean()),
            "rule_sharpe_annual": float(pnl[ok].mean() / pnl[ok].std(ddof=1) * np.sqrt(52)) if pnl[ok].std(ddof=1) > 0 else np.nan,
        })
        rows.append(row)
    out = pd.DataFrame(rows)
    if "p_one_sided" in out:
        out["q_bh"] = benjamini_hochberg(out.p_one_sided)
        same = lambda col: np.sign(out[col]) == out.expected_sign  # noqa: E731
        out["supports"] = ((out.q_bh < config.alpha) & same("slope_before_split") & same("slope_after_split")
                           & same("slope_without_shock_months") & (out.rule_mean_weekly > 0))
    return out


def rule_by_year(panel: pd.DataFrame, config: Config) -> pd.DataFrame:
    rows = []
    y = panel.nikkei_ret.to_numpy(float)
    ok_y = np.isfinite(y)
    for name, sign in SIGNALS.items():
        z = panel[f"z_{name}"].to_numpy(float)
        ok = ok_y & np.isfinite(z)
        pnl = rule_pnl(np.where(ok, z, np.nan), np.expm1(y), sign, config.cost_bp)
        frame = pd.DataFrame({"year": panel.date.str[:4], "pnl": pnl, "ok": ok, "hold": np.expm1(y)})
        for year, part in frame.loc[frame.ok].groupby("year"):
            rows.append({"signal": name, "year": year, "weeks": len(part), "rule_sum": part.pnl.sum(),
                         "long_only_sum": part.hold.sum()})
    return pd.DataFrame(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_only(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)


def btc_returns(conn: sqlite3.Connection, decisions: pd.DataFrame) -> np.ndarray:
    """Log return from the 1m open at decision 06:30 UTC to the next decision 06:30 UTC (Binance perp)."""
    ts = pd.to_datetime(decisions.date).to_numpy("datetime64[D]").astype(np.int64) * DAY + CLOSE_OFFSET
    wanted = sorted(set(ts.tolist()))
    conn.execute("CREATE TEMP TABLE want (ts INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO want VALUES (?)", [(int(t),) for t in wanted])
    opens = dict(conn.execute("SELECT k.ts, k.open FROM klines k JOIN want w ON k.ts = w.ts WHERE k.venue='binance' "
                              "AND k.market='perp' AND k.symbol='BTCUSDT'"))
    px = np.array([opens.get(int(t), np.nan) for t in ts], float)
    nxt = np.r_[px[1:], np.nan]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.log(nxt / px)


def run(jquants_db: Path, market_db: Path, fred_nikkei: Path, output: Path, config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    with read_only(jquants_db) as conn:
        business = pd.read_sql_query("SELECT date FROM trading_calendar WHERE hol_div IN ('1','2') ORDER BY date",
                                     conn).date.to_numpy(str)
        nikkei = pd.read_sql_query("SELECT date, MIN(underlying) AS close, MAX(underlying) AS hi FROM n225_option_atm "
                                   "GROUP BY date", conn)
        investor = pd.read_sql_query("SELECT pub_date, en_date, section, frgn_bal, tot_tot FROM investor_types", conn)
        short = pd.read_sql_query("SELECT date, s33, sell_ex_short_va, short_with_res_va, short_no_res_va FROM short_ratio",
                                  conn)
        margin = pd.read_sql_query("SELECT date, code, pub_date, long_shares, short_shares FROM margin_weekly", conn)
        conn.execute("CREATE TEMP TABLE price_dates (date TEXT PRIMARY KEY)")
        price_dates = sorted(set(last_business_on_or_before(margin.date.drop_duplicates(), business).dropna()))
        conn.executemany("INSERT INTO price_dates VALUES (?)", [(d,) for d in price_dates])
        closes = pd.read_sql_query("SELECT e.date, e.code, e.close FROM equities_daily e JOIN price_dates p "
                                   "ON e.date = p.date", conn)
    if (nikkei.hi != nikkei.close).any():
        raise ValueError("Nikkei 225 underlying differs across expiries on the same day")
    business = business[business < CUTOFF_DATE]
    panel = decision_days(pd.Series(business))
    panel = panel.loc[panel.next_date.notna()].reset_index(drop=True)  # the target must end before the cutoff
    n225 = nikkei.set_index("date").close
    panel["nikkei_close"] = panel.date.map(n225)
    panel["nikkei_next"] = panel.next_date.map(n225)
    gap = (pd.to_datetime(panel.next_date) - pd.to_datetime(panel.date)).dt.days
    panel["nikkei_ret"] = np.where(gap <= config.max_target_gap_days, np.log(panel.nikkei_next / panel.nikkei_close), np.nan)
    investor = investor.loc[investor.pub_date < CUTOFF_DATE]
    short = short.loc[short.date < CUTOFF_DATE]
    margin = margin.loc[margin.date < CUTOFF_DATE]
    s1 = foreign_flow(investor)
    s2, margin_weeks = margin_ratio(margin, closes, business, config.margin_publication_lag)
    daily_short = short_ratio_daily(short)
    panel["s1_foreign_flow"] = latest_known(panel.date, s1.available, s1.reference, s1.value, config.max_signal_age_days)
    panel["s2_margin_ratio"] = latest_known(panel.date, s2.available, s2.reference, s2.value, config.max_signal_age_days)
    panel["s3_short_ratio"] = short_ratio_signal(panel.date, daily_short, business, config.short_ratio_days)
    for name in SIGNALS:
        panel[f"z_{name}"] = trailing_z(panel[name].to_numpy(), config.z_window, config.z_min_finite)
    with read_only(market_db) as conn:
        panel["btc_ret"] = btc_returns(conn, panel)
    gap_ok = gap <= config.max_target_gap_days
    panel.loc[~gap_ok, "btc_ret"] = np.nan
    first = panel[[f"z_{n}" for n in SIGNALS]].dropna().index.min()
    panel = panel.loc[first:].reset_index(drop=True)
    primary = test_signals(panel, "nikkei_ret", config).assign(family="primary")
    secondary = test_signals(panel, "btc_ret", config).assign(family="secondary")
    tests = pd.concat([primary, secondary], ignore_index=True)
    by_year = rule_by_year(panel, config)
    fred = pd.read_csv(fred_nikkei)
    fred = fred.set_index("observation_date").iloc[:, 0]
    both = pd.concat([n225, pd.to_numeric(fred, errors="coerce")], axis=1, join="inner").dropna()
    both = both.loc[(both.index >= panel.date.iloc[0]) & (both.index < CUTOFF_DATE)]
    hold = np.expm1(panel.nikkei_ret.dropna())
    checks = {
        "weeks": len(panel), "first_decision": panel.date.iloc[0], "last_decision": panel.date.iloc[-1],
        "missing_target": int(panel.nikkei_ret.isna().sum()), "missing_btc": int(panel.btc_ret.isna().sum()),
        "missing_z": {n: int(panel[f"z_{n}"].isna().sum()) for n in SIGNALS},
        "z_correlations": panel[[f"z_{n}" for n in SIGNALS]].corr().round(3).to_dict(),
        "nikkei_vs_fred_days": len(both),
        "nikkei_vs_fred_max_abs_rel_diff": float((both.iloc[:, 0] / both.iloc[:, 1] - 1).abs().max()) if len(both) else None,
        "margin_codes_priced_share_min": float((margin_weeks.codes_priced / margin_weeks.codes).min()),
        "margin_unpriced_long_share_max": float(margin_weeks.long_shares_unpriced_share.max()),
        "short_ratio_sector_count_min": int(daily_short.sectors.min()),
        "long_only_sharpe_annual": float(hold.mean() / hold.std(ddof=1) * np.sqrt(52)),
    }
    output.mkdir(parents=True, exist_ok=True)
    panel.to_csv(output / "weekly.csv", index=False, float_format="%.10g")
    tests.to_csv(output / "tests.csv", index=False, float_format="%.10g")
    by_year.to_csv(output / "rule_by_year.csv", index=False, float_format="%.10g")
    supported = primary.loc[primary.get("supports", pd.Series(False, index=primary.index)).fillna(False), "signal"].tolist()
    summary = {
        "question": "Do J-Quants Standard positioning signals add robust weekly information for Nikkei 225 (xyz:JP225)?",
        "verdict": (f"robust weekly information: {', '.join(supported)}" if supported else
                    "no robust weekly information at the index level from the Standard positioning data"),
        "decision_rule": f"BH q<{config.alpha} in the pre-registered direction, same sign before/after {config.split_date} "
                         f"and without {', '.join(SHOCK_MONTHS)}, rule mean after {config.cost_bp}bp turnover cost > 0",
        "primary": json.loads(primary.to_json(orient="records")),
        "secondary": json.loads(secondary.to_json(orient="records")),
        "data_checks": checks,
        "notes": ["Retrospective; not a sealed prospective test.",
                  "Outputs include J-Quants-derived values: private use only, do not publish or commit."],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=True) + "\n")
    stat = Path(market_db).stat()
    manifest = {"config": asdict(config), "signals": SIGNALS, "cutoff_date": CUTOFF_DATE,
                "inputs": {"jquants_db_sha256": sha256_file(jquants_db), "fred_nikkei_sha256": sha256_file(fred_nikkei),
                           "market_db": {"path": str(market_db), "byte_size": stat.st_size, "mtime_ns": stat.st_mtime_ns}},
                "script_sha256": sha256_file(Path(__file__)),
                "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--market-db", type=Path, default=Path("/mnt/e/Datas/market/market.db"))
    parser.add_argument("--fred-nikkei", type=Path, default=Path("/mnt/e/Datas/market/indices/fred_NIKKEI225.csv"))
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/japan_positioning")
    args = parser.parse_args()
    summary = run(args.jquants_db, args.market_db, args.fred_nikkei, args.output_dir, Config())
    print(json.dumps({k: summary[k] for k in ("verdict", "decision_rule", "data_checks")}, indent=2))


if __name__ == "__main__":
    main()
