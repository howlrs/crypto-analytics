#!/usr/bin/env python3
"""Follow the H1 paper carry (docs/japan-perp-forward.md) while J-Quants is not available.

The registered ledger needs the TSE close, so it stops at the cancellation and the verdict waits for a renewal
after 2027-10-29. Most of the ledger does not need that close: funding and the liquidation check come
from Hyperliquid, and the basis term telescopes to its two end points. This script reruns the same ledger
code with the perp's own price as the fair value, so the basis term is zero.

  status     (no J-Quants) what the paper position has earned so far and how close it came to liquidation
  calibrate  (needs J-Quants, before the purge) how far that proxy is from the registered ledger

Neither touches the registered rules, and neither can replace the verdict: the proxy says nothing about
the basis at the two ends and H1 is judged only on the registered ledger. Output goes to RETAINED_DIR.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests import hl_japan_carry as carry  # noqa: E402
from backtests import japan_forward as forward  # noqa: E402
from backtests.japan_retained import RETAINED_DIR, retained_path, write_summary  # noqa: E402

HL_DIR = Path("/mnt/e/Datas/market/hyperliquid/xyz")
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
REGISTRY = forward.REGISTRY
# TSE weekday closures in the H1 window (JPX calendar; compared with J-Quants' trading_calendar on 2026-11-07).
TSE_CLOSED = frozenset((
    "2026-11-03", "2026-11-23", "2026-12-31", "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-22",
    "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05", "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
    "2027-10-11"))


def tse_days(first: str, last: str) -> list[str]:
    return [d for d in pd.bdate_range(first, last).strftime("%Y-%m-%d") if d not in TSE_CLOSED]


def load_market(coin: str, hl_dir: Path) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    funding = carry.load_funding(hl_dir / f"{carry.stem(coin)}-funding.json")
    return funding, {i: carry.load_candles(hl_dir / f"{carry.stem(coin)}-candles-{i}.json") for i in ("15m", "1h")}


def proxy_panel(coin: str, days: list[str], fair_jpy: np.ndarray | None, hl_dir: Path) -> pd.DataFrame:
    """A daily_panel with the columns instrument_ledger reads. fair_jpy None uses the perp itself as the fair value."""
    funding, candles = load_market(coin, hl_dir)
    fx_funding, fx_candles = load_market(carry.FX, hl_dir)
    number = pd.to_datetime(pd.Series(days)).to_numpy("datetime64[D]").astype(np.int64)
    if fair_jpy is None:
        perp = carry.price_at_close(number, candles["15m"], candles["1h"])[0]
        fx = carry.price_at_close(number, fx_candles["15m"], fx_candles["1h"])[0]
        fair_jpy = perp * fx
    panel = carry.daily_panel(coin, number, fair_jpy, funding, fx_funding, candles, fx_candles)
    panel["fair_usd"] = panel.fair_jpy / panel.fx
    panel["fair_usd_prev"] = panel.fair_usd.shift()
    panel["perp_prev"] = panel.perp.shift()
    panel["prev_date"] = panel.date.shift()
    return panel


def last_complete_day(hl_dir: Path, coin: str) -> str:
    funding, candles = load_market(coin, hl_dir)
    last = min(funding.time.iloc[-1], candles["1h"].t.iloc[-1])
    moment = pd.to_datetime(last, unit="ms") - pd.Timedelta(hours=7)
    return moment.strftime("%Y-%m-%d")


def ledger(coin: str, spec: dict, days: list[str], fair_jpy: np.ndarray | None, start: str, hl_dir: Path) -> pd.DataFrame:
    panel = proxy_panel(coin, days, fair_jpy, hl_dir)
    _, candles = load_market(coin, hl_dir)
    window = panel.loc[panel.date > start]
    return forward.instrument_ledger(window, candles["15m"], candles["1h"], spec["leverage"], set(), 0.0)


def status(registry: Path, hl_dir: Path, directory: Path) -> dict:
    rules = forward.load_registry(registry)["h1_carry"]
    start = rules["open_close"]
    out = {"note": "Proxy without the TSE close: funding and liquidation checks only. It is not the registered ledger.",
           "start": start, "instruments": {}}
    for coin, spec in rules["instruments"].items():
        last = min(last_complete_day(hl_dir, coin), rules["final_close"])
        days = tse_days(start, last)
        led = ledger(coin, spec, days, None, start, hl_dir)
        capital = 1 + 1 / spec["leverage"]
        out["instruments"][coin] = {
            "leverage": spec["leverage"], "tse_days": int(len(led)), "last_day": days[-1] if days else None,
            "funding_received_sum": float(led.loc[led.valid, "funding_short"].sum()),
            "funding_on_capital_sum": float(led.loc[led.valid, "funding_short"].sum() / capital),
            "funding_on_capital_annual": float(led.loc[led.valid, "funding_short"].sum() / capital / max(len(led), 1) * carry.TSE_DAYS_PER_YEAR),
            "worst_rise": float(led.rise.max()) if led.rise.notna().any() else None,
            "liquidation_room": float(carry.liquidation_room(spec["leverage"], 10)),
            "room_used": float(led.rise.max() / carry.liquidation_room(spec["leverage"], 10)) if led.rise.notna().any() else None,
            "liquidations_on_proxy": int(led.liquidated.sum()), "days_with_gaps": int((~led.valid).sum())}
    directory = retained_path(directory) / "h1_monitor"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "status.json").write_text(json.dumps(out, indent=1) + "\n")
    return out


def calibrate(registry: Path, jquants_db: Path, hl_dir: Path, directory: Path, first: str = "2026-08-31") -> dict:
    """From the first day with 15-minute candles: the exact closes only, on the same ledger code."""
    rules = forward.load_registry(registry)["h1_carry"]
    out = {"first": first, "instruments": {}}
    with carry.read_only(jquants_db) as conn:
        for coin, spec in rules["instruments"].items():
            ref = carry.load_reference(conn, coin, first)
            ref = ref.loc[ref.date <= last_complete_day(hl_dir, coin)]
            days = ref.date.tolist()
            official = ledger(coin, spec, days, ref.fair_jpy.to_numpy(float), first, hl_dir)
            proxy = ledger(coin, spec, days, None, first, hl_dir)
            good = (official.valid & proxy.valid & (official.perp_source == "15m")).to_numpy()
            error = (official.roc - proxy.roc).to_numpy(float)[good]
            basis = official.basis.to_numpy(float)[good]
            rho = float(np.corrcoef(basis[:-1], basis[1:])[0, 1]) if len(basis) > 3 else np.nan
            out["instruments"][coin] = {
                "days": int(good.sum()), "daily_error_sd_on_capital": float(error.std(ddof=1)),
                "cumulative_error_on_capital": float(error.sum()), "basis_mean": float(basis.mean()), "basis_sd": float(basis.std(ddof=1)),
                "basis_day_to_day_correlation": rho,
                "basis_half_life_days": float(np.log(0.5) / np.log(rho)) if 0 < rho < 1 else None,
                "year_end_error_sd_on_capital": float(basis.std(ddof=1) * np.sqrt(2) / (1 + 1 / spec["leverage"])),
                "same_liquidations": bool((official.liquidated == proxy.liquidated).all()),
                "funding_share_of_official_income": float(official.funding_short.to_numpy(float)[good].sum() / official.roc.to_numpy(float)[good].sum() / (1 + 1 / spec["leverage"])) if abs(official.roc.to_numpy(float)[good].sum()) > 1e-9 else None}
    write_summary(out, "h1_proxy_calibration", directory)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("status", "calibrate"))
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--hl-dir", type=Path, default=HL_DIR)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--directory", type=Path, default=RETAINED_DIR)
    args = parser.parse_args()
    out = (status(args.registry, args.hl_dir, args.directory) if args.command == "status"
           else calibrate(args.registry, args.jquants_db, args.hl_dir, args.directory))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
