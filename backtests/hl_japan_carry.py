#!/usr/bin/env python3
"""Short Hyperliquid xyz perps on Japanese prices, long the TSE reference: carry and hedge noise.

Descriptive feasibility of the project's funding-capture profit source (leveraged longs pay
funding) on xyz:KIOXIA (TSE 285A), xyz:SOFTBANK (TSE 9984) and xyz:JP225 (Nikkei 225, quanto).
Not a validated strategy: these perps listed in April-June 2026, so the sample is a few months
and runs past the 2026-08-01 research boundary (they are outside the sealed BTC/ETH registry;
the boundary split is reported). The fixed design is in docs/jquants-standard.md.

The hedge is valued once per TSE day at the 15:30 JST close (06:30 UTC). The perp and USD/JPY
(xyz:JPY) prices there come from 15-minute candles when stored, else from the close of the
06:00 UTC hourly candle (30 minutes late; the gap is measured where both exist).
USD-based accounting: a stock perp tracks TSE price / USDJPY, so long stock + short perp is FX
neutral; the quanto JP225 leaves the JPY value of the spot leg open, hedged with long xyz:JPY.

Outputs contain J-Quants-derived daily values (private use) and are refused inside the repo.
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

HOUR = 3_600_000
DAY = 24 * HOUR
CLOSE_OFFSET = 6 * HOUR + 30 * 60_000  # 15:30 JST
ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
HL_DIR = Path("/mnt/e/Datas/market/hyperliquid/xyz")
BOUNDARY = "2026-08-01"
INSTRUMENTS = {"xyz:KIOXIA": "285A0", "xyz:SOFTBANK": "99840", "xyz:JP225": None}  # None: Nikkei 225
FX = "xyz:JPY"
TSE_DAYS_PER_YEAR = 245
MAX_ABS_BASIS = 0.2  # beyond this the two legs are on different share bases (e.g. a split day)


@dataclass(frozen=True)
class Config:
    launch_days: int = 28
    perp_fee_bp: float = 0.9  # growth-mode taker, docs.trade.xyz as of 2026-10-20
    perp_slippage_bp: float = 1.0
    spot_cost_bp: float = 5.0  # TSE half-spread and impact at a commission-free broker
    fx_cost_bp: float = 5.0  # USDC <-> JPY conversion for the spot leg
    leverages: tuple[float, ...] = (2.0, 3.0, 4.0)

    def validate(self) -> None:
        if self.launch_days < 1:
            raise ValueError("launch window must be at least one day")
        if min(self.perp_fee_bp, self.perp_slippage_bp, self.spot_cost_bp, self.fx_cost_bp) < 0:
            raise ValueError("costs must be non-negative")
        if not self.leverages or any(not 1 <= x <= 4 for x in self.leverages):
            raise ValueError("leverage is capped at 4x")

    @property
    def round_trip(self) -> float:
        return 2 * (self.perp_fee_bp + self.perp_slippage_bp + self.spot_cost_bp + self.fx_cost_bp) / 10_000

    @property
    def perp_round_trip(self) -> float:
        return 2 * (self.perp_fee_bp + self.perp_slippage_bp) / 10_000


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("J-Quants-derived outputs must stay outside the public repository")
    return resolved


def stem(coin: str) -> str:
    return coin.replace(":", "_")


def load_funding(path: Path) -> pd.DataFrame:
    raw = pd.DataFrame(json.loads(Path(path).read_text()))
    t = raw.time.astype(np.int64)
    if (t % HOUR >= 60_000).any():  # settlements land a few milliseconds after the hour
        raise ValueError(f"{path}: funding times must be within a minute after the hour")
    frame = pd.DataFrame({"time": t // HOUR * HOUR, "rate": pd.to_numeric(raw.fundingRate),
                          "premium": pd.to_numeric(raw.premium)}).sort_values("time")
    if frame.time.duplicated().any():
        raise ValueError(f"{path}: two funding settlements in one hour")
    return frame.reset_index(drop=True)


def load_candles(path: Path) -> pd.DataFrame:
    raw = pd.DataFrame(json.loads(Path(path).read_text()))
    frame = pd.DataFrame({"t": raw.t.astype(np.int64), **{c: pd.to_numeric(raw[c]) for c in "ohlcv"}})
    frame = frame.sort_values("t").reset_index(drop=True)
    if frame.t.duplicated().any():
        raise ValueError(f"{path}: duplicate candle open times")
    return frame


def price_at_close(day: np.ndarray, m15: pd.DataFrame, h1: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Price at d 06:30 UTC: the 15m candle opening then, else the 06:00 hourly close (~07:00).

    Returns (price, source, hourly_proxy). The hourly proxy is filled wherever the hourly candle
    exists, so its error can be measured on days that also have the exact price.
    """
    ts = day * DAY + CLOSE_OFFSET
    exact = pd.Series(m15.o.to_numpy(), index=m15.t.to_numpy()).reindex(ts).to_numpy()
    proxy = pd.Series(h1.c.to_numpy(), index=h1.t.to_numpy()).reindex(ts - 30 * 60_000).to_numpy()
    price = np.where(np.isfinite(exact), exact, proxy)
    source = np.where(np.isfinite(exact), "15m_0630", np.where(np.isfinite(proxy), "1h_close_0700", "missing"))
    return price, source, proxy


def window_sum(times: np.ndarray, values: np.ndarray, start: np.ndarray, stop: np.ndarray) -> np.ndarray:
    """Sum of values whose time lies in (start, stop]."""
    cum = np.r_[0.0, np.cumsum(values)]
    return cum[np.searchsorted(times, stop, side="right")] - cum[np.searchsorted(times, start, side="right")]


def window_count(times: np.ndarray, start: np.ndarray, stop: np.ndarray) -> np.ndarray:
    return np.searchsorted(times, stop, side="right") - np.searchsorted(times, start, side="right")


def daily_panel(coin: str, days: np.ndarray, fair_jpy: np.ndarray, funding: pd.DataFrame, fx_funding: pd.DataFrame,
                candles: dict[str, pd.DataFrame], fx_candles: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per TSE day d; P&L columns describe the hold from close d-1 to close d."""
    price, source, proxy = price_at_close(days, candles["15m"], candles["1h"])
    fx, fx_source, fx_proxy = price_at_close(days, fx_candles["15m"], fx_candles["1h"])
    quanto = INSTRUMENTS[coin] is None
    fair = fair_jpy if quanto else fair_jpy / fx  # stock perps track TSE price / USDJPY
    with np.errstate(invalid="ignore", divide="ignore"):
        basis = np.log(price / fair)
        basis_proxy = np.log(proxy / (fair_jpy if quanto else fair_jpy / fx_proxy))
    frame = pd.DataFrame({"date": pd.to_datetime(days, unit="D").strftime("%Y-%m-%d"), "perp": price,
                          "perp_source": source, "fx": fx, "fx_source": fx_source, "fair_jpy": fair_jpy,
                          "basis": basis, "basis_hourly_proxy": basis_proxy})
    frame["scale_mismatch"] = np.abs(frame.basis) > MAX_ABS_BASIS
    ts = days * DAY + CLOSE_OFFSET
    prev = np.r_[ts[0] - DAY, ts[:-1]]
    t, rate = funding.time.to_numpy(), funding.rate.to_numpy()
    frame["funding_short"] = window_sum(t, rate, prev, ts)  # a short receives positive funding
    frame["funding_hours"] = window_count(t, prev, ts)
    frame["expected_hours"] = (ts - prev) // HOUR
    ft, frate = fx_funding.time.to_numpy(), fx_funding.rate.to_numpy()
    frame["fx_funding_long"] = -window_sum(ft, frate, prev, ts)  # a long pays positive funding
    db = frame.basis.diff()
    valid = (~frame.scale_mismatch & ~frame.scale_mismatch.shift(fill_value=True)
             & (frame.funding_hours == frame.expected_hours))
    frame["valid"] = valid & db.notna()
    frame["basis_pnl"] = -db
    dlogfx = np.log(frame.fx / frame.fx.shift())
    # USD accounting. Stock: spot USD return minus perp return = -d(basis). Quanto: the spot leg
    # also loses d(log USDJPY); a long xyz:JPY of the same notional offsets it and earns its funding.
    frame["dn_unhedged"] = frame.basis_pnl + frame.funding_short - (dlogfx if quanto else 0.0)
    frame["dn"] = frame.basis_pnl + frame.funding_short + (frame.fx_funding_long if quanto else 0.0)
    return frame


def funding_phases(funding: pd.DataFrame, launch_days: int) -> list[dict]:
    first = int(funding.time.iloc[0])
    phases = {"all": funding, "launch": funding.loc[funding.time < first + launch_days * DAY],
              "post_launch": funding.loc[funding.time >= first + launch_days * DAY],
              "before_boundary": funding.loc[funding.time < pd.Timestamp(BOUNDARY, tz="UTC").value // 10**6],
              "after_boundary": funding.loc[funding.time >= pd.Timestamp(BOUNDARY, tz="UTC").value // 10**6]}
    rows = []
    for name, part in phases.items():
        if part.empty:
            rows.append({"phase": name, "hours": 0})
            continue
        daily = part.groupby(part.time // DAY).rate.sum()
        weekly = part.groupby((part.time - first) // (7 * DAY)).rate.sum()
        means = weekly.to_numpy()
        rows.append({
            "phase": name, "hours": len(part),
            "first": pd.to_datetime(part.time.iloc[0], unit="ms").isoformat(),
            "annualised_mean": float(part.rate.mean() * 24 * 365),
            "median_hourly_bp": float(part.rate.median() * 1e4),
            "share_hours_positive": float((part.rate > 0).mean()),
            "share_from_hours_above_baseline": float(part.rate[part.rate > 0.00000625].sum() / part.rate.sum())
            if part.rate.sum() > 0 else None,
            "mean_premium_bp": float(part.premium.mean() * 1e4),
            "weekly_sum_bp_p10_p50_p90": [float(x) for x in np.percentile(means * 1e4, [10, 50, 90])],
            "share_weeks_negative": float((means < 0).mean()),
            "daily_autocorr_lag1": float(daily.autocorr(1)) if len(daily) > 3 else None,
            "weekly_autocorr_lag1": float(pd.Series(means).autocorr(1)) if len(means) > 3 else None,
        })
    return rows


def dn_summary(frame: pd.DataFrame, config: Config, column: str = "dn") -> dict:
    x = frame.loc[frame.valid, column].to_numpy(float)
    if len(x) < 2:
        return {"days": int(len(x))}
    cum = np.cumsum(x)
    return {
        "days": int(len(x)), "first": frame.loc[frame.valid, "date"].iloc[0], "last": frame.loc[frame.valid, "date"].iloc[-1],
        "annualised_return_before_costs": float(x.mean() * TSE_DAYS_PER_YEAR),
        "annualised_funding": float(frame.loc[frame.valid, "funding_short"].mean() * TSE_DAYS_PER_YEAR),
        "annualised_vol": float(x.std(ddof=1) * np.sqrt(TSE_DAYS_PER_YEAR)),
        "sharpe_before_costs": float(x.mean() / x.std(ddof=1) * np.sqrt(TSE_DAYS_PER_YEAR)) if x.std(ddof=1) > 0 else None,
        "round_trip_cost": config.round_trip,
        "annualised_return_one_round_trip": float((x.sum() - config.round_trip) / len(x) * TSE_DAYS_PER_YEAR),
        "max_drawdown": float(np.min(cum - np.maximum.accumulate(np.r_[0.0, cum])[1:])),
        "worst_day": float(x.min()), "best_day": float(x.max()),
    }


def adverse_moves(frame: pd.DataFrame, h1: pd.DataFrame) -> dict:
    """Largest perp rise between consecutive closes, from hourly highs (a short's liquidation risk)."""
    days = pd.to_datetime(frame.date).to_numpy("datetime64[D]").astype(np.int64)
    ts = days * DAY + CLOSE_OFFSET
    t, high = h1.t.to_numpy(), h1.h.to_numpy()
    worst = []
    for i in range(1, len(frame)):
        if not frame.valid.iloc[i]:
            continue
        inside = (t >= ts[i - 1]) & (t < ts[i])
        if inside.any() and np.isfinite(frame.perp.iloc[i - 1]):
            worst.append(high[inside].max() / frame.perp.iloc[i - 1] - 1)
    worst = np.asarray(worst)
    if not len(worst):
        return {"intervals": 0}
    return {"intervals": int(len(worst)), "max_rise_between_closes": float(worst.max()),
            "p99_rise_between_closes": float(np.percentile(worst, 99))}


def liquidation_room(leverage: float, max_leverage: float) -> float:
    """Approximate adverse move that exhausts isolated margin: 1/L minus maintenance (half of 1/max)."""
    return 1 / leverage - 0.5 / max_leverage


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_only(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True)


def load_reference(conn: sqlite3.Connection, coin: str, first_day: str) -> pd.DataFrame:
    """TSE business days from first_day with the JPY reference close (raw, not split-adjusted)."""
    calendar = pd.read_sql_query("SELECT date FROM trading_calendar WHERE hol_div IN ('1','2') AND date>=? ORDER BY date",
                                 conn, params=(first_day,))
    code = INSTRUMENTS[coin]
    if code is None:
        ref = pd.read_sql_query("SELECT date, MIN(underlying) AS fair_jpy, MAX(underlying) AS hi FROM n225_option_atm "
                                "WHERE date>=? GROUP BY date", conn, params=(first_day,))
        if (ref.hi != ref.fair_jpy).any():
            raise ValueError("Nikkei 225 underlying differs across expiries on the same day")
        ref = ref.drop(columns="hi")
    else:
        ref = pd.read_sql_query("SELECT date, close AS fair_jpy FROM equities_daily WHERE code=? AND date>=?",
                                conn, params=(code, first_day))
    return calendar.merge(ref, on="date", how="left")


def run(jquants_db: Path, hl_dir: Path, output: Path, config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    fx_funding = load_funding(hl_dir / f"{stem(FX)}-funding.json")
    fx_candles = {i: load_candles(hl_dir / f"{stem(FX)}-candles-{i}.json") for i in ("15m", "1h")}
    panels, summary = {}, {"instruments": {}}
    with read_only(jquants_db) as conn:
        for coin in INSTRUMENTS:
            funding = load_funding(hl_dir / f"{stem(coin)}-funding.json")
            candles = {i: load_candles(hl_dir / f"{stem(coin)}-candles-{i}.json") for i in ("15m", "1h")}
            first_day = pd.to_datetime(funding.time.iloc[0], unit="ms").strftime("%Y-%m-%d")
            ref = load_reference(conn, coin, first_day)
            last_day = pd.to_datetime(min(funding.time.iloc[-1], candles["1h"].t.iloc[-1]), unit="ms")
            ref = ref.loc[pd.to_datetime(ref.date) + pd.Timedelta(hours=7) <= last_day]
            days = pd.to_datetime(ref.date).to_numpy("datetime64[D]").astype(np.int64)
            panel = daily_panel(coin, days, ref.fair_jpy.to_numpy(float), funding, fx_funding, candles, fx_candles)
            panels[coin] = panel
            launch_end = (pd.to_datetime(first_day) + pd.Timedelta(days=config.launch_days)).strftime("%Y-%m-%d")
            both = panel.basis.notna() & panel.basis_hourly_proxy.notna() & (panel.perp_source == "15m_0630")
            windows = {"all": panel, "post_launch": panel.loc[panel.date >= launch_end],
                       "before_boundary": panel.loc[panel.date < BOUNDARY], "after_boundary": panel.loc[panel.date >= BOUNDARY]}
            adverse = adverse_moves(panel, candles["1h"])
            summary["instruments"][coin] = {
                "tse_code": INSTRUMENTS[coin] or "Nikkei 225 (options underlying)",
                "funding_phases": funding_phases(funding, config.launch_days),
                "dn": {name: dn_summary(part, config) for name, part in windows.items()},
                "dn_unhedged_fx": dn_summary(panel, config, "dn_unhedged") if INSTRUMENTS[coin] is None else None,
                "basis": {
                    "mean": float(panel.loc[~panel.scale_mismatch, "basis"].mean()),
                    "sd": float(panel.loc[~panel.scale_mismatch, "basis"].std()),
                    "sd_daily_change": float(panel.loc[panel.valid, "basis_pnl"].std()),
                    "days_scale_mismatch": int(panel.scale_mismatch.sum()),
                    "days_missing_reference": int(panel.fair_jpy.isna().sum()),
                    "days_missing_perp": int((panel.perp_source == "missing").sum()),
                    "share_days_exact_15m": float((panel.perp_source == "15m_0630").mean()),
                    "hourly_proxy_error_sd": float((panel.basis - panel.basis_hourly_proxy)[both].std()) if both.sum() > 2 else None,
                    "hourly_proxy_days_compared": int(both.sum()),
                },
                "adverse": adverse,
                "liquidation_room": {f"{x:g}x": liquidation_room(x, 20 if INSTRUMENTS[coin] is None else 10)
                                     for x in config.leverages},
            }
    output.mkdir(parents=True, exist_ok=True)
    pd.concat([p.assign(coin=c) for c, p in panels.items()], ignore_index=True).to_csv(
        output / "daily.csv", index=False, float_format="%.10g")
    summary["notes"] = [
        "Descriptive feasibility on a few months of data; not a validated or sealed test.",
        "Costs: one round trip per continuous hold; KIOXIA's 2026-09-28 split forced a perp re-entry "
        f"({config.perp_round_trip:.4%}) that the summary does not subtract.",
        "Capital cost of the spot leg and margin, borrow/financing, taxes and HIP-3 deployer risk are excluded.",
        "Outputs include J-Quants-derived daily values: private use only, do not publish or commit.",
    ]
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=True) + "\n")
    manifest = {"config": asdict(config), "boundary": BOUNDARY, "script_sha256": sha256_file(Path(__file__)),
                "inputs_sha256": {"jquants_db": sha256_file(jquants_db),
                                  **{p.name: sha256_file(p) for p in sorted(hl_dir.glob("*.json"))}},
                "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--hl-dir", type=Path, default=HL_DIR)
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/hl_japan_carry")
    args = parser.parse_args()
    summary = run(args.jquants_db, args.hl_dir, args.output_dir, Config())
    for coin, s in summary["instruments"].items():
        print(coin, json.dumps({"post_launch": s["dn"]["post_launch"], "basis": s["basis"]}, indent=1))


if __name__ == "__main__":
    main()
