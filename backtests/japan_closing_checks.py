#!/usr/bin/env python3
"""The post-hoc checks quoted in docs/jquants-closing-research.md section 7.

None of these changes a registered verdict. They exist so the figures can be reproduced after a renewal.
Run them after japan_crypto_equities.py and japan_margin_crowding.py; they read those two outputs.

Crypto-linked stocks: the same signal against the BTC window it was fitted on and against the window before
it, a permutation of the signal, and how the rule's P&L falls across three periods.
Margin crowding, two kinds of dependence:
  across stocks: the signal is shuffled inside each week and sector, which keeps the cross-section of returns
  over time: weekly slopes are resampled in moving blocks, and the Newey-West lag is varied. Shuffling inside
  a week cannot test this: it destroys any persistence between weeks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests import japan_margin_crowding as crowding  # noqa: E402
from backtests.japan_positioning import newey_west_slope, outside_repo, read_only  # noqa: E402
from backtests.japan_retained import write_summary  # noqa: E402

JQ_DIR = Path("/mnt/e/Datas/market/jquants")
RESULTS = JQ_DIR / "results"
CUTOFF_MS = 1785542400000
PERIODS = (("2024-08", "2025-03"), ("2025-04", "2025-10"), ("2025-11", "2026-07"))
BLOCKS = (4, 8, 13, 26)
LAGS = (0, 2, 4, 8, 12)
SEED = 20261107


def hac_t(y: np.ndarray, x: np.ndarray, lags: int = 5) -> float:
    beta, se = newey_west_slope(y, x, lags)
    return float(beta / se)


def permutation_p(y: np.ndarray, z: np.ndarray, draws: int, seed: int) -> float:
    """One-sided share of shuffled signals with a t at least the observed one."""
    rng = np.random.default_rng(seed)
    observed = hac_t(y, z)
    return float(np.mean([hac_t(y, rng.permutation(z)) >= observed for _ in range(draws)]))


def crypto_checks(panel: pd.DataFrame, draws: int) -> dict:
    p = panel.loc[panel.next_close_ms < CUTOFF_MS].reset_index(drop=True)
    out: dict = {"same_window": {}, "previous_window": {}, "treasury": {}}
    previous = p.btc_next.shift(2)  # btc_next of d-2 runs from close d-2 to close d-1: it ended before the signal's window
    for basket in ("z_treasury", "z_exchange"):
        ok = p[basket].notna() & p.btc_window.notna()
        out["same_window"][basket] = hac_t(p.loc[ok, "btc_window"].to_numpy(), p.loc[ok, basket].to_numpy())
        ok = p[basket].notna() & previous.notna()
        out["previous_window"][basket] = hac_t(previous[ok].to_numpy(), p.loc[ok, basket].to_numpy())
    for target in ("btc_next", "eth_next"):
        ok = p.z_treasury.notna() & p[target].notna()
        z, y, dates = p.loc[ok, "z_treasury"].to_numpy(), p.loc[ok, target].to_numpy(), p.loc[ok, "date"]
        gross = np.clip(z, -1, 1) * np.expm1(y)
        parts = [float(gross[((dates.str[:7] >= a) & (dates.str[:7] <= b)).to_numpy()].sum()) for a, b in PERIODS]
        out["treasury"][target] = {"permutation_p_one_sided": permutation_p(y, z, draws, SEED), "gross_rule_by_period": parts,
                                   "first_period_share": parts[0] / sum(parts)}
    return out


def block_bootstrap(x: np.ndarray, block: int, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Means of series rebuilt from overlapping blocks of `block` weeks, which keeps the dependence inside a block."""
    n = len(x)
    wrapped = np.r_[x, x[:block - 1]]
    totals = np.r_[0.0, np.cumsum(wrapped)]
    blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(draws, blocks))
    return (totals[starts + block] - totals[starts]).sum(axis=1) / (blocks * block)


def serial_checks(x: np.ndarray, draws: int) -> dict:
    x = x[np.isfinite(x)]
    rng = np.random.default_rng(SEED)
    centred = x - x.mean()
    out = {"mean": float(x.mean()), "autocorrelation": {str(k): float(centred[k:] @ centred[:-k] / (centred @ centred)) for k in (1, 2, 3, 4)},
           "hac_t_by_lag": {str(k): crowding.mean_test(x, crowding.Config(hac_lags=k))[2] for k in LAGS}, "blocks": {}}
    for block in BLOCKS:
        means = block_bootstrap(x, block, draws, rng)
        low, high = np.quantile(means, [0.025, 0.975])
        out["blocks"][str(block)] = {"ci95": [float(low), float(high)], "t_equivalent": float(x.mean() / means.std(ddof=1))}
    return out


def stress_episodes(weeks: pd.DataFrame, threshold: float, gap_days: int = 21) -> dict:
    """Weeks after a TOPIX fall, merged into one episode when they are close, so one crash counts once."""
    s = weeks.loc[weeks.topix_5d <= threshold].copy()
    s["episode"] = (pd.to_datetime(s.decision).diff().dt.days.fillna(gap_days + 1) > gap_days).cumsum()
    means = s.groupby("episode").slope_level.mean()
    calm = s.loc[~s.decision.str.startswith("2020-03"), "slope_level"]
    return {"weeks": int(len(s)), "episodes": int(len(means)), "positive_episodes": int((means > 0).sum()), "episode_mean": float(means.mean()),
            "episode_t": float(means.mean() / means.std(ddof=1) * np.sqrt(len(means))), "weekly_mean_without_2020_03": float(calm.mean())}


_CASES: list = []


def _shuffled_t(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    out = []
    for store in _CASES:
        slopes = []
        for X, Y, groups in store:
            shuffled = X.copy()
            base = np.argsort(groups, kind="stable")
            shuffled[base, 0] = X[np.argsort(groups + rng.random(len(groups)), kind="stable"), 0]
            slopes.append(np.linalg.lstsq(shuffled, Y, rcond=None)[0][0])
        out.append(crowding.mean_test(np.asarray(slopes), crowding.Config())[2])
    return out


def week_case(y, signal, controls, sectors, config) -> tuple | None:
    """The sector-demeaned regression of one week, ready to be repeated with a shuffled signal."""
    columns = [signal, *controls]
    ok = np.isfinite(y) & np.logical_and.reduce([np.isfinite(c) for c in columns]) & pd.notna(sectors)
    if ok.sum() < config.min_stocks:
        return None
    X = np.column_stack([crowding.standardize(c[ok], config.tail) for c in columns])
    Y = crowding.winsorize(y[ok], config.tail_return)
    groups = pd.factorize(sectors[ok])[0]
    size = np.bincount(groups)
    demean = lambda a: a - (np.bincount(groups, a) / size)[groups]  # noqa: E731
    return np.column_stack([demean(X[:, j]) for j in range(X.shape[1])]), demean(Y), groups


def within_week_permutation(jquants_db: Path, draws: int) -> dict:
    config = crowding.Config()
    with read_only(jquants_db) as conn:
        wide = crowding.load_wide(conn)
        margin = pd.read_sql_query("SELECT date, pub_date, code, long_shares, short_shares FROM margin_weekly WHERE date < ?", conn, params=(crowding.CUTOFF_DATE,))
        master = pd.read_sql_query("SELECT code, valid_from, valid_to, s33, mkt_nm, mrgn_nm, prod_cat FROM equities_master_history ORDER BY code, valid_from", conn)
        topix = pd.read_sql_query("SELECT date, close FROM topix_daily WHERE close IS NOT NULL", conn).set_index("date").close
    business = wide["close"].index[wide["close"].notna().any(axis=1)].to_numpy()
    wide = {k: v.loc[business] for k, v in wide.items()}
    schedule = crowding.weekly_schedule(business, margin[["date", "pub_date"]].drop_duplicates("date"), config.publication_lag)
    keep = set(schedule.loc[schedule.next_decision < crowding.CUTOFF_DATE, "decision"])
    calls: list = []

    def record(y, signal, controls, sectors, cfg):
        calls.append((y, signal, controls, sectors))
        return np.nan
    real, crowding.cross_section_slope = crowding.cross_section_slope, record
    try:
        weeks = crowding.weekly_cross_sections(wide, margin, master, schedule, topix, config)
    finally:
        crowding.cross_section_slope = real
    per_week = len(calls) // len(weeks)
    level, change = [], []
    for i, day in enumerate(weeks.decision):
        if day in keep:
            for offset, store in ((0, level), (1, change)):  # the level and change regressions come first in each week
                case = week_case(*calls[i * per_week + offset], config)
                if case is not None:
                    store.append(case)
    del calls, wide, margin
    _CASES[:] = [level, change]
    observed = [crowding.mean_test(np.asarray([np.linalg.lstsq(X, Y, rcond=None)[0][0] for X, Y, _ in store]), config)[2] for store in _CASES]
    with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 2)) as pool:
        null = np.array(list(pool.map(_shuffled_t, range(draws))))
    return {name: {"observed_t": observed[j], "null_mean": float(null[:, j].mean()), "null_sd": float(null[:, j].std(ddof=1)),
                   "p_two_sided": float((np.abs(null[:, j]) >= abs(observed[j])).mean()), "draws": draws}
            for j, name in enumerate(("slope_level", "slope_change"))}


def run(jquants_db: Path, results: Path, draws: int, permutations: int) -> dict:
    warnings.filterwarnings("ignore", message="(All-NaN slice|Degrees of freedom|Mean of empty)", category=RuntimeWarning)
    outside_repo(results)
    panel = pd.read_csv(results / "japan_crypto_equities/panel.csv")
    weeks = pd.read_csv(results / "japan_margin_crowding/weekly.csv")
    config = crowding.Config()
    summary = {"crypto_equities": crypto_checks(panel, 2000),
               "margin_crowding": {"serial": {c: serial_checks(weeks[c].to_numpy(float), draws) for c in ("slope_level", "slope_change")},
                                   "stress": stress_episodes(weeks, config.stress_return),
                                   "shuffle_inside_week": within_week_permutation(jquants_db, permutations)}}
    output = results / "japan_closing_checks"
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    write_summary(summary, "closing_checks")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--bootstrap-draws", type=int, default=20_000)
    parser.add_argument("--permutations", type=int, default=500)
    args = parser.parse_args()
    print(json.dumps(run(args.jquants_db, args.results, args.bootstrap_draws, args.permutations), indent=1))


if __name__ == "__main__":
    main()
