#!/usr/bin/env python3
"""Pre-registered forward tracking of the J-Quants-referenced Japan trades (docs/japan-perp-forward.md).

H1  carry: long the TSE stock, short its Hyperliquid xyz perp (SOFTBANK = TSE 9984, KIOXIA = 285A)
    at the registered perp leverage, held from the 15:30 JST close of 2026-11-02 to that of
    2027-10-29. The profit source is the funding paid by leveraged longs on Hyperliquid, the same
    as the adopted BTC/ETH funding capture.
H2  short ratio: the J-Quants Standard short-selling share (S3 of japan_positioning.py) against
    the next-week Nikkei 225, weekly decisions 2026-11-06 .. 2028-10-27.

  register  write results/japan_forward/registry.json and its .sha256 sidecar (once, before the start)
  status    recompute both ledgers from stored data (offline) and write them outside the repository

The registry holds rules only. Ledgers contain J-Quants-derived values and are refused inside the
public repository. Numbers that decide a hypothesis (H1 p-value and verdict, all H2 performance)
stay empty until its window is complete. H1 running P&L and liquidation checks are shown because a
paper position needs them; they do not change the rule.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests import hl_japan_carry as carry  # noqa: E402
from backtests import japan_positioning as positioning  # noqa: E402

JQ_DIR = Path("/mnt/e/Datas/market/jquants")
HL_DIR = Path("/mnt/e/Datas/market/hyperliquid/xyz")
REGISTRY = ROOT / "results/japan_forward/registry.json"
DAY, HOUR, CLOSE_OFFSET = carry.DAY, carry.HOUR, carry.CLOSE_OFFSET
TRACKED_CODE = ("backtests/hl_japan_carry.py", "backtests/japan_positioning.py", "backtests/japan_forward.py",
                "backtests/japan_earnings_risk.py")

RULES = {
    "name": "japan-forward-v1",
    "anchor_tag": "japan-forward-2026-11-02-v1",
    "h1_carry": {
        "question": "Does the funding received by shorting xyz Japanese stock perps against TSE spot beat the "
                    "BTC/ETH funding capture after costs, without liquidation, over one year?",
        "instruments": {"xyz:SOFTBANK": {"tse_code": "99840", "leverage": 3.0},
                        "xyz:KIOXIA": {"tse_code": "285A0", "leverage": 2.0}},
        "open_close": "2026-11-02",
        "final_close": "2027-10-29",
        "valuation": "15:30 JST (06:30 UTC) each TSE day: perp = 15m open at 06:30 UTC (else 06:00 hourly close), "
                     "fair = TSE raw close / xyz:JPY; daily P&L per notional = -d(basis) + funding received",
        "capital_per_notional": "1 (TSE spot bought with cash) + 1/leverage (isolated perp margin)",
        "weights": "equal capital across instruments; the day's portfolio return averages the instruments measured",
        "costs_bp_one_way": {"perp_fee": 0.9, "perp_slippage": 1.0, "spot": 5.0, "fx": 5.0},
        "cost_events": "one full round trip at open+close; a perp round trip per TSE split (AdjFactor != 1) "
                       "and per liquidation",
        "liquidation": "the perp's highest 15m high (else 1h) between two daily closes rises >= 1/L - 0.05 over "
                       "the earlier close; that day the margin is lost: P&L = spot USD return - 1/L - perp round "
                       "trip, and the short is reopened at the close with margin reset to 1/L",
        "criteria": {"min_annual_return_on_capital_after_costs": 0.08, "alpha_one_sided": 0.05,
                     "hac_lags_days": 5, "max_liquidations": 0, "min_valid_share": 0.9},
        "verdicts": {"confirmed": "all criteria hold",
                     "not_confirmed": "no liquidation and coverage ok, but return or significance fails",
                     "rejected_for_risk": "a liquidation at the registered leverage",
                     "insufficient_data": "valid days below the minimum share for an instrument"},
        "secondary_descriptive": {"funding_exit_days": 10,
                                  "funding_exit_rule": "flat after a close where the trailing 10 TSE days' funding "
                                                       "sum is negative, back in when it is >= 0; each switch pays "
                                                       "half a round trip",
                                  "per_instrument": True},
        "leverage_source": "backtests/japan_earnings_risk.py: largest of 1.5/2/3/4x whose liquidation room is "
                           ">= 1.25 x the worst rise between closes (TSE since 2016-10 or listing, and xyz)",
    },
    "h2_short_ratio": {
        "question": "Does the J-Quants short-selling share (S3) predict the next-week Nikkei 225 out of sample?",
        "signal": "s3_short_ratio", "expected_sign": 1,
        "first_decision": "2026-11-06", "last_decision": "2028-10-27",
        "positioning_config": asdict(positioning.Config()),
        "criteria": {"alpha_one_sided": 0.05, "hac_lags": 4, "rule_mean_weekly_positive": True, "min_weeks": 90},
        "gap_window": {"first_decision": "2026-08-07", "last_decision": "2026-10-30",
                       "use": "descriptive only: decided after the 2026-08-01 research boundary, computed only "
                              "after registration, never pooled"},
        "power_note": "about 104 weeks: roughly 60% power for a weekly correlation of 0.2 and 25% for 0.1",
    },
}


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("J-Quants-derived outputs must stay outside the public repository")
    return resolved


def sha256_file(path: Path) -> str:
    return carry.sha256_file(path)


def canonical(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def payload_sha256(registry: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in registry.items() if k != "integrity"})).hexdigest()


def sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def open_ms(day: str) -> int:
    return int(np.datetime64(day, "D").astype(np.int64)) * DAY + CLOSE_OFFSET


def register(path: Path, design_summary: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if path.exists() or sidecar(path).exists():
        raise FileExistsError(f"registry already exists: {path}")
    if now.timestamp() * 1000 >= open_ms(RULES["h1_carry"]["open_close"]):
        raise ValueError("registration must precede the first evaluated close")
    registry = {**RULES, "registered_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "code_sha256": {p: sha256_file(ROOT / p) for p in TRACKED_CODE},
                "design_input_sha256": {"japan_earnings_risk/summary.json": sha256_file(design_summary)}}
    registry["integrity"] = {"registry_sha256": payload_sha256(registry)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(registry))
    sidecar(path).write_text(registry["integrity"]["registry_sha256"] + "\n", encoding="ascii")
    return registry


def load_registry(path: Path) -> dict:
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    digest = payload_sha256(registry)
    if registry.get("integrity", {}).get("registry_sha256") != digest or Path(path).read_bytes() != canonical(registry):
        raise ValueError("registry content does not match its integrity hash")
    if sidecar(path).read_text(encoding="ascii").strip() != digest:
        raise ValueError("registry sidecar does not match")
    rules = {k: v for k, v in registry.items() if k not in ("registered_at", "code_sha256", "design_input_sha256",
                                                             "integrity")}
    if rules != json.loads(json.dumps(RULES)):
        raise ValueError("registered rules differ from the rules in this script")
    return registry


def hac_mean_test(x: np.ndarray, lags: int) -> tuple[float, float]:
    """One-sided test of mean > 0 with a Newey-West (Bartlett) long-run variance."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 2 or np.ptp(x) == 0:
        return np.nan, np.nan
    u = x - x.mean()
    lrv = u @ u / n
    for j in range(1, min(lags, n - 1) + 1):
        lrv += 2 * (1 - j / (lags + 1)) * (u[j:] @ u[:-j]) / n
    se = np.sqrt(max(lrv, 0.0) / n)
    t = x.mean() / se if se > 0 else np.nan
    return float(t), float(stats.norm.sf(t)) if np.isfinite(t) else np.nan


def max_rise(prev_ts: np.ndarray, ts: np.ndarray, base: np.ndarray, m15: pd.DataFrame, h1: pd.DataFrame) -> np.ndarray:
    """Highest high in [prev_ts, ts) over base; 15m candles when they cover 90% of the window, else hourly
    candles from 30 minutes before prev_ts (conservative: the 06:00 bar partly precedes the close)."""
    out = np.full(len(ts), np.nan)
    t15, h15, t1, hh = m15.t.to_numpy(), m15.h.to_numpy(), h1.t.to_numpy(), h1.h.to_numpy()
    for i, (a, b) in enumerate(zip(prev_ts, ts)):
        lo, hi = np.searchsorted(t15, a), np.searchsorted(t15, b)
        if hi - lo >= 0.9 * (b - a) / 900_000:
            out[i] = h15[lo:hi].max() / base[i] - 1
            continue
        lo, hi = np.searchsorted(t1, a - 30 * 60_000), np.searchsorted(t1, b)
        if hi > lo:
            out[i] = hh[lo:hi].max() / base[i] - 1
    return out


def instrument_ledger(window: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, leverage: float,
                      split_days: set[str], perp_round_trip: float) -> pd.DataFrame:
    """Forward-window rows of a daily_panel with prev_date, perp_prev and fair_usd(_prev) columns
    (row d holds the hold from close d-1 to close d). Costs are kept apart from the P&L so they are
    charged even on a day whose P&L cannot be measured."""
    p = window.reset_index(drop=True).copy()
    to_ts = lambda col: pd.to_datetime(p[col]).to_numpy("datetime64[D]").astype(np.int64) * DAY + CLOSE_OFFSET  # noqa: E731
    p["rise"] = max_rise(to_ts("prev_date"), to_ts("date"), p.perp_prev.to_numpy(float), m15, h1) if len(p) else []
    p["room"] = carry.liquidation_room(leverage, 10)
    p["liquidated"] = p.rise >= p.room
    spot_usd = np.log(p.fair_usd / p.fair_usd_prev)
    p["pnl_notional"] = np.where(p.liquidated, spot_usd - 1 / leverage, np.where(p.valid, p.dn, np.nan))
    p["cost_notional"] = perp_round_trip * (p.liquidated.astype(int) + p.date.isin(split_days).astype(int))
    p["roc"] = p.pnl_notional / (1 + 1 / leverage)
    p["roc_net"] = (p.pnl_notional - p.cost_notional) / (1 + 1 / leverage)
    p["measured"] = p.pnl_notional.notna()
    return p


def funding_exit(history: pd.Series, window: pd.DataFrame, days: int, half_trip_roc: float) -> np.ndarray:
    """Daily return on capital of the descriptive funding-exit variant.

    history: funding_short of the TSE days up to and including the opening close. The signal at a
    close is the trailing `days` sum including that close; it sets the holding over the next day.
    Entering and exiting each pay half a round trip; a held position is marked with its exit cost."""
    series = pd.concat([history, window.funding_short], ignore_index=True)
    trailing = series.rolling(days, min_periods=days).sum().to_numpy()
    held = trailing[len(history) - 1:len(history) - 1 + len(window)] >= 0  # unknown -> flat
    out = np.where(held, np.nan_to_num(window.roc_net.to_numpy(float)), 0.0)
    out -= np.abs(np.diff(np.r_[0, held.astype(int)])) * half_trip_roc
    if len(out) and held[-1]:
        out[-1] -= half_trip_roc
    return out


def h1_status(rules: dict, jquants_db: Path, hl_dir: Path, now_day: str) -> tuple[dict, pd.DataFrame]:
    r = rules["h1_carry"]
    cost = carry.Config(perp_fee_bp=r["costs_bp_one_way"]["perp_fee"], perp_slippage_bp=r["costs_bp_one_way"][
        "perp_slippage"], spot_cost_bp=r["costs_bp_one_way"]["spot"], fx_cost_bp=r["costs_bp_one_way"]["fx"])
    fx_funding = carry.load_funding(hl_dir / f"{carry.stem(carry.FX)}-funding.json")
    fx_candles = {i: carry.load_candles(hl_dir / f"{carry.stem(carry.FX)}-candles-{i}.json") for i in ("15m", "1h")}
    ledgers, out = [], {"instruments": {}}
    with carry.read_only(jquants_db) as conn:
        for coin, spec in r["instruments"].items():
            funding = carry.load_funding(hl_dir / f"{carry.stem(coin)}-funding.json")
            candles = {i: carry.load_candles(hl_dir / f"{carry.stem(coin)}-candles-{i}.json") for i in ("15m", "1h")}
            first_day = pd.to_datetime(funding.time.iloc[0], unit="ms").strftime("%Y-%m-%d")
            ref = carry.load_reference(conn, coin, first_day)
            last = min(funding.time.iloc[-1], candles["1h"].t.iloc[-1], fx_candles["1h"].t.iloc[-1])
            ref = ref.loc[pd.to_datetime(ref.date) + pd.Timedelta(hours=7) <= pd.to_datetime(last, unit="ms")]
            ref = ref.loc[ref.date <= r["final_close"]]
            days = pd.to_datetime(ref.date).to_numpy("datetime64[D]").astype(np.int64)
            panel = carry.daily_panel(coin, days, ref.fair_jpy.to_numpy(float), funding, fx_funding, candles, fx_candles)
            panel["fair_usd"] = panel.fair_jpy / panel.fx
            panel["fair_usd_prev"] = panel.fair_usd.shift()
            panel["perp_prev"] = panel.perp.shift()
            panel["prev_date"] = panel.date.shift()
            splits = set(pd.read_sql_query("SELECT date FROM equities_daily WHERE code=? AND date>? AND date<=? AND "
                                           "adj_factor IS NOT NULL AND adj_factor != 1", conn,
                                           params=(spec["tse_code"], r["open_close"], r["final_close"])).date)
            window = panel.loc[(panel.date > r["open_close"]) & (panel.date <= r["final_close"])]
            exit_days = r["secondary_descriptive"]["funding_exit_days"]
            history = panel.loc[panel.date <= r["open_close"], "funding_short"].tail(exit_days)
            led = instrument_ledger(window, candles["15m"], candles["1h"], spec["leverage"], splits,
                                    cost.perp_round_trip).assign(coin=coin)
            led["roc_funding_exit"] = funding_exit(history, led, exit_days,
                                                   cost.round_trip / 2 / (1 + 1 / spec["leverage"]))
            ledgers.append(led)
            m = led.loc[led.measured]
            entry_exit = cost.round_trip / (1 + 1 / spec["leverage"])
            out["instruments"][coin] = {
                "leverage": spec["leverage"], "liquidation_room": carry.liquidation_room(spec["leverage"], 10),
                "tse_days_elapsed": int(len(led)), "measured_days": int(len(m)),
                "valid_share": float(len(m) / len(led)) if len(led) else None,
                "funding_received_sum": float(led.loc[led.valid, "funding_short"].sum()),
                "return_on_capital_sum_before_costs": float(m.roc.sum()),
                "return_on_capital_sum_after_costs": float(m.roc.sum() - led.cost_notional.sum() / (1 + 1 / spec[
                    "leverage"]) - entry_exit) if len(led) else None,
                "worst_rise": float(led.rise.max()) if led.rise.notna().any() else None,
                "liquidations": int(led.liquidated.sum()), "split_days": sorted(splits),
                "funding_exit_variant_sum": float(led.roc_funding_exit.sum()),
                "last_basis": float(panel.basis.iloc[-1]) if len(panel) else None,
            }
    ledger = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
    crit = r["criteria"]
    final_complete = bool(len(ledger) and (ledger.groupby("coin").date.max() == r["final_close"]).all())
    if len(ledger):
        daily = ledger.loc[ledger.measured].groupby("date").roc_net.mean().sort_index()
        costs = np.mean([cost.round_trip / (1 + 1 / s["leverage"]) for s in r["instruments"].values()])
        cum = daily.cumsum()
        out["portfolio"] = {"days": int(len(daily)), "first": daily.index.min() if len(daily) else None,
                            "last": daily.index.max() if len(daily) else None,
                            "return_on_capital_sum_after_costs": float(daily.sum() - costs),
                            "max_drawdown": float((cum - cum.cummax().clip(lower=0)).min()) if len(cum) else None}
    else:
        out["portfolio"] = {"days": 0}
    out["state"] = "complete" if final_complete else ("not_started" if not len(ledger) else "running")
    out["as_of_data_day"] = now_day
    out["verdict"], out["final"] = None, None
    if final_complete:
        t, p = hac_mean_test(daily.to_numpy(), crit["hac_lags_days"])
        annual = (daily.sum() - costs) / len(daily) * carry.TSE_DAYS_PER_YEAR
        liq = int(ledger.liquidated.sum())
        coverage_ok = all(v["valid_share"] >= crit["min_valid_share"] for v in out["instruments"].values())
        out["final"] = {"annual_return_on_capital_after_costs": float(annual), "hac_t": t, "p_one_sided": p,
                        "liquidations": liq, "coverage_ok": coverage_ok}
        if not coverage_ok:
            out["verdict"] = "insufficient_data"
        elif liq > crit["max_liquidations"]:
            out["verdict"] = "rejected_for_risk"
        elif annual >= crit["min_annual_return_on_capital_after_costs"] and p < crit["alpha_one_sided"]:
            out["verdict"] = "confirmed"
        else:
            out["verdict"] = "not_confirmed"
    return out, ledger


def h2_status(rules: dict, jquants_db: Path) -> tuple[dict, pd.DataFrame]:
    r = rules["h2_short_ratio"]
    cfg = positioning.Config(**r["positioning_config"])
    with positioning.read_only(jquants_db) as conn:
        business = pd.read_sql_query("SELECT date FROM trading_calendar WHERE hol_div IN ('1','2') ORDER BY date",
                                     conn).date.to_numpy(str)
        nikkei = pd.read_sql_query("SELECT date, MIN(underlying) AS close, MAX(underlying) AS hi FROM n225_option_atm "
                                   "GROUP BY date", conn)
        short = pd.read_sql_query("SELECT date, s33, sell_ex_short_va, short_with_res_va, short_no_res_va FROM short_ratio",
                                  conn)
    if (nikkei.hi != nikkei.close).any():
        raise ValueError("Nikkei 225 underlying differs across expiries on the same day")
    last_data = min(nikkei.date.max(), short.date.max())
    panel = positioning.decision_days(pd.Series(business[business <= max(last_data, r["last_decision"])]))
    panel = panel.loc[panel.date <= r["last_decision"]].reset_index(drop=True)
    n225 = nikkei.set_index("date").close
    gap = (pd.to_datetime(panel.next_date) - pd.to_datetime(panel.date)).dt.days
    ret = np.log(panel.next_date.map(n225) / panel.date.map(n225))
    panel["nikkei_ret"] = np.where(gap <= cfg.max_target_gap_days, ret, np.nan)
    daily_short = positioning.short_ratio_daily(short)
    signal = positioning.short_ratio_signal(panel.date, daily_short, business, cfg.short_ratio_days)
    panel["s3_short_ratio"] = np.where(panel.date <= last_data, signal, np.nan)
    panel["z"] = positioning.trailing_z(panel.s3_short_ratio.to_numpy(), cfg.z_window, cfg.z_min_finite)
    panel["position"] = np.where(np.isfinite(panel.z), r["expected_sign"] * np.clip(panel.z, -1, 1), 0.0)
    evaluated = panel.loc[panel.date >= r["first_decision"]].reset_index(drop=True)
    gap_rows = panel.loc[(panel.date >= r["gap_window"]["first_decision"])
                         & (panel.date <= r["gap_window"]["last_decision"])]
    ok = np.isfinite(evaluated.z) & np.isfinite(evaluated.nikkei_ret)
    expected = int(len(evaluated))
    complete = bool(expected and evaluated.date.iloc[-1] == r["last_decision"] and np.isfinite(evaluated.nikkei_ret.iloc[-1]))
    out = {"state": "complete" if complete else ("not_started" if not ok.any() else "running"),
           "decisions_in_calendar": expected, "decisions_with_target": int(ok.sum()),
           "latest_signal": None, "verdict": None, "final": None}
    known = panel.loc[np.isfinite(panel.z)]
    if len(known):
        out["latest_signal"] = {"decision": known.date.iloc[-1], "z": float(known.z.iloc[-1]),
                                "position": float(known.position.iloc[-1])}
    g_ok = np.isfinite(gap_rows.z) & np.isfinite(gap_rows.nikkei_ret)
    if g_ok.sum():
        g = gap_rows.loc[g_ok]
        pnl = positioning.rule_pnl(g.z.to_numpy(), np.expm1(g.nikkei_ret.to_numpy()), r["expected_sign"], cfg.cost_bp)
        out["gap_window_descriptive"] = {"weeks": int(g_ok.sum()), "corr": float(np.corrcoef(g.z, g.nikkei_ret)[0, 1])
                                         if g_ok.sum() > 2 else None, "rule_sum": float(pnl.sum()),
                                         "long_only_sum": float(np.expm1(g.nikkei_ret).sum())}
    if complete:
        e = evaluated.loc[ok]
        beta, se = positioning.newey_west_slope(e.nikkei_ret.to_numpy(), e.z.to_numpy(), r["criteria"]["hac_lags"])
        t = beta / se if se > 0 else np.nan
        p = float(stats.norm.sf(r["expected_sign"] * t))
        pnl = positioning.rule_pnl(evaluated.z.to_numpy(), np.expm1(evaluated.nikkei_ret.fillna(0).to_numpy()),
                                   r["expected_sign"], cfg.cost_bp)
        out["final"] = {"weeks": int(ok.sum()), "slope": beta, "hac_t": float(t), "p_one_sided": p,
                        "rule_mean_weekly": float(pnl[ok.to_numpy()].mean())}
        if ok.sum() < r["criteria"]["min_weeks"]:
            out["verdict"] = "insufficient_data"
        else:
            supported = p < r["criteria"]["alpha_one_sided"] and out["final"]["rule_mean_weekly"] > 0
            out["verdict"] = "confirmed" if supported else "not_confirmed"
    return out, evaluated


def status(registry_path: Path, jquants_db: Path, hl_dir: Path, output: Path) -> dict:
    output = outside_repo(output)
    registry = load_registry(registry_path)
    changed = [p for p, h in registry["code_sha256"].items() if sha256_file(ROOT / p) != h]
    with carry.read_only(jquants_db) as conn:
        data_day = conn.execute("SELECT MAX(date) FROM equities_daily").fetchone()[0]
    h1, h1_ledger = h1_status(registry, jquants_db, hl_dir, data_day)
    h2, h2_ledger = h2_status(registry, jquants_db)
    summary = {"registry": registry["name"], "registered_at": registry["registered_at"],
               "registry_sha256": registry["integrity"]["registry_sha256"], "code_changed_since_registration": changed,
               "h1_carry": h1, "h2_short_ratio": h2,
               "notes": ["A changed tracked file makes the outcome non-confirmatory unless the change is logged in "
                         "docs/japan-perp-forward.md before the affected data is seen.",
                         "Outputs include J-Quants-derived values: private use only, do not publish or commit."]}
    output.mkdir(parents=True, exist_ok=True)
    h1_ledger.to_csv(output / "h1_carry_daily.csv", index=False, float_format="%.10g")
    h2_ledger.drop(columns=[c for c in ("nikkei_ret",) if h2["state"] != "complete"]).to_csv(
        output / "h2_short_ratio_weekly.csv", index=False, float_format="%.10g")
    (output / "status.json").write_text(json.dumps(summary, indent=2, allow_nan=True) + "\n")
    manifest = {"registry_sha256": summary["registry_sha256"], "script_sha256": sha256_file(Path(__file__)),
                "inputs_sha256": {"jquants_db": sha256_file(jquants_db),
                                  **{p.name: sha256_file(p) for p in sorted(hl_dir.glob("*.json"))}},
                "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("register", "status"), nargs="?", default="status")
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--hl-dir", type=Path, default=HL_DIR)
    parser.add_argument("--design-summary", type=Path, default=JQ_DIR / "results/japan_earnings_risk/summary.json")
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/japan_forward")
    args = parser.parse_args()
    if args.command == "register":
        registry = register(args.registry, args.design_summary)
        print(json.dumps({k: registry[k] for k in ("name", "registered_at", "integrity")}, indent=1))
        return
    summary = status(args.registry, args.jquants_db, args.hl_dir, args.output_dir)
    print(json.dumps({k: summary[k] for k in ("registered_at", "code_changed_since_registration", "h1_carry",
                                              "h2_short_ratio")}, indent=1, allow_nan=True))


if __name__ == "__main__":
    main()
