#!/usr/bin/env python3
"""How far do SoftBank Group (TSE 9984) and KIOXIA (TSE 285A) jump around earnings, and which
perp-short leverage survives the jumps?

Design input for the pre-registered xyz carry tracking (docs/japan-perp-forward.md). A short
perp leg is margined on Hyperliquid while the hedging spot sits at a Japanese broker, so a jump
between daily margin checks can liquidate the short even though the pair is delta neutral.

Earnings dates come from J-Quants fins/summary (disclosure date and time). A disclosure reacts in
the same session when it lands before that day's TSE close (15:00 until 2024-11-01, 15:30 from
2024-11-05), otherwise in the next session. Moves are measured against the previous close,
split-adjusted with the J-Quants AdjFactor of the reaction day. On Hyperliquid the window is the
perp's hourly highs between the two 15:30 JST closes around the reaction session, which also
captures the evening (PTS-priced) reaction that TSE daily bars miss.

Outputs contain J-Quants-derived values (private use) and are refused inside the repository.
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

ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
HL_DIR = Path("/mnt/e/Datas/market/hyperliquid/xyz")
DAY = 86_400_000
CLOSE_OFFSET = 6 * 3_600_000 + 30 * 60_000  # 15:30 JST, the daily margin check of the carry
STOCKS = {"xyz:SOFTBANK": "99840", "xyz:KIOXIA": "285A0"}
MAX_LEVERAGE = 10  # both stock perps on xyz; maintenance margin is approximated as half of 1/max
SCHEDULED = "FinancialStatements"  # DocType of quarterly and annual statements, dated in advance
LONGER_SESSION = "2024-11-05"  # TSE close moved from 15:00 to 15:30


@dataclass(frozen=True)
class Config:
    leverages: tuple[float, ...] = (1.5, 2.0, 3.0, 4.0)
    buffer: float = 1.25  # liquidation room must exceed this multiple of the worst observed rise

    def validate(self) -> None:
        if not self.leverages or any(not 1 <= x <= 4 for x in self.leverages):
            raise ValueError("leverage is capped at 4x")
        if self.buffer < 1:
            raise ValueError("the buffer cannot shrink the worst observed rise")


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("J-Quants-derived outputs must stay outside the public repository")
    return resolved


def tse_close_time(day: str) -> str:
    return "15:30:00" if day >= LONGER_SESSION else "15:00:00"


def reaction_session(disc_date: str, disc_time: str | None, business: np.ndarray) -> str | None:
    """First TSE session whose close comes after the disclosure (business: sorted ISO dates)."""
    k = np.searchsorted(business, disc_date, side="left")
    if k < len(business) and business[k] == disc_date and disc_time and disc_time < tse_close_time(disc_date):
        return disc_date
    k = np.searchsorted(business, disc_date, side="right")
    return business[k] if k < len(business) else None


def daily_moves(bars: pd.DataFrame) -> pd.DataFrame:
    """Close, high and low against the previous close, adjusted by the day's split factor."""
    b = bars.sort_values("date").reset_index(drop=True)
    prev = b.close.shift() * b.adj_factor.fillna(1.0)
    return pd.DataFrame({"date": b.date, "ret_close": b.close / prev - 1, "rise_high": b.high / prev - 1,
                         "fall_low": b.low / prev - 1}).dropna().reset_index(drop=True)


def earnings_days(disclosures: pd.DataFrame, business: np.ndarray) -> pd.DataFrame:
    sched = disclosures.loc[disclosures.doc_type.fillna("").str.contains(SCHEDULED)].copy()
    sched["reaction"] = [reaction_session(d, t, business) for d, t in zip(sched.disc_date, sched.disc_time)]
    sched = sched.dropna(subset=["reaction"]).sort_values(["reaction", "disc_time"])
    return sched.drop_duplicates("reaction")[["disc_date", "disc_time", "doc_type", "reaction"]].reset_index(drop=True)


def side_summary(moves: pd.DataFrame) -> dict:
    if moves.empty:
        return {"days": 0}
    return {"days": int(len(moves)), "mean_abs_close": float(moves.ret_close.abs().mean()),
            "max_rise_high": float(moves.rise_high.max()), "p99_rise_high": float(moves.rise_high.quantile(0.99)),
            "max_fall_low": float(moves.fall_low.min()),
            "worst_rise_date": moves.loc[moves.rise_high.idxmax(), "date"]}


def hl_rise(day: str, prev_day: str, h1: pd.DataFrame) -> dict:
    """Largest perp rise from the 15:30 JST close of prev_day to that of day, from hourly highs."""
    start = np.datetime64(prev_day, "D").astype(np.int64) * DAY + CLOSE_OFFSET
    stop = np.datetime64(day, "D").astype(np.int64) * DAY + CLOSE_OFFSET
    at_start = h1.loc[h1.t == start - 30 * 60_000, "c"]  # the 06:00 UTC hourly close, as in hl_japan_carry
    inside = h1.loc[(h1.t >= start) & (h1.t < stop)]
    if at_start.empty or inside.empty:
        return {"hours": int(len(inside))}
    base = float(at_start.iloc[0])
    return {"hours": int(len(inside)), "complete": bool(inside.t.max() >= stop - 3_600_000),
            "rise_high": float(inside.h.max() / base - 1), "change_last": float(inside.c.iloc[-1] / base - 1)}


def liquidation_room(leverage: float) -> float:
    return 1 / leverage - 0.5 / MAX_LEVERAGE


def choose_leverage(worst_rise: float, config: Config) -> float | None:
    fits = [x for x in config.leverages if liquidation_room(x) >= config.buffer * worst_rise]
    return max(fits) if fits else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(jquants_db: Path, hl_dir: Path, hl_carry_daily: Path | None, output: Path, config: Config) -> dict:
    config.validate()
    output = outside_repo(output)
    with sqlite3.connect(f"{Path(jquants_db).resolve().as_uri()}?mode=ro", uri=True) as conn:
        business = pd.read_sql_query("SELECT date FROM trading_calendar WHERE hol_div IN ('1','2') ORDER BY date",
                                     conn).date.to_numpy(str)
        disclosures = pd.read_sql_query("SELECT disc_date, disc_time, code, doc_type FROM fins_summary WHERE code IN "
                                        f"({','.join('?' * len(STOCKS))})", conn, params=list(STOCKS.values()))
        bars = pd.read_sql_query("SELECT date, code, high, close, low, adj_factor FROM equities_daily WHERE code IN "
                                 f"({','.join('?' * len(STOCKS))}) AND close > 0", conn, params=list(STOCKS.values()))
    carry = pd.read_csv(hl_carry_daily) if hl_carry_daily and Path(hl_carry_daily).exists() else None
    summary, events = {"stocks": {}}, []
    for coin, code in STOCKS.items():
        moves = daily_moves(bars.loc[bars.code == code])
        earn = earnings_days(disclosures.loc[disclosures.code == code], business)
        is_earn = moves.date.isin(earn.reaction)
        h1 = pd.DataFrame(json.loads((hl_dir / f"{coin.replace(':', '_')}-candles-1h.json").read_text()))
        h1 = h1.assign(t=h1.t.astype(np.int64), **{c: pd.to_numeric(h1[c]) for c in "hc"})
        first_hl = pd.to_datetime(h1.t.min(), unit="ms").strftime("%Y-%m-%d")
        rows = []
        for _, e in earn.iterrows():
            k = np.searchsorted(business, e.reaction)
            prev_day = business[k - 1]
            m = moves.loc[moves.date == e.reaction]
            row = {"coin": coin, "disc_date": e.disc_date, "disc_time": e.disc_time, "doc_type": e.doc_type,
                   "reaction": e.reaction, **({c: float(m[c].iloc[0]) for c in ("ret_close", "rise_high", "fall_low")}
                                              if len(m) else {})}
            if prev_day >= first_hl:
                row.update({f"hl_{k2}": v for k2, v in hl_rise(e.reaction, prev_day, h1).items()})
            rows.append(row)
        events.extend(rows)
        hl_worst = None
        if carry is not None:
            part = carry.loc[(carry.coin == coin) & carry.valid.astype(bool)]
            rises = [hl_rise(d, p, h1).get("rise_high") for p, d in zip(part.date.shift(), part.date) if isinstance(p, str)]
            rises = [x for x in rises if x is not None]
            hl_worst = float(max(rises)) if rises else None
        hl_earn = [r["hl_rise_high"] for r in rows if r.get("hl_rise_high") is not None]
        worst = max([side_summary(moves).get("max_rise_high", 0.0), hl_worst or 0.0] + hl_earn)
        summary["stocks"][coin] = {
            "tse_code": code, "tse_days_from": moves.date.iloc[0], "scheduled_disclosures": int(len(earn)),
            "tse_earnings": side_summary(moves.loc[is_earn]), "tse_other_days": side_summary(moves.loc[~is_earn]),
            "earnings_share_of_top10_rises": float(is_earn[moves.rise_high.nlargest(10).index].mean()),
            "hl_max_rise_between_valid_closes": hl_worst,
            "hl_max_rise_on_earnings": float(max(hl_earn)) if hl_earn else None,
            "worst_rise_used": float(worst),
            "liquidation_room": {f"{x:g}x": liquidation_room(x) for x in config.leverages},
            "leverage_rule": f"largest grid leverage with room >= {config.buffer} x worst rise",
            "chosen_leverage": choose_leverage(worst, config),
        }
    output.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(events).to_csv(output / "earnings_events.csv", index=False, float_format="%.8g")
    summary["notes"] = ["TSE daily price limits cap the TSE history; the perp has none, so the HL rises are kept.",
                        "Outputs include J-Quants-derived values: private use only, do not publish or commit."]
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    inputs = {"jquants_db": sha256_file(jquants_db),
              **{p.name: sha256_file(p) for p in sorted(hl_dir.glob("*-candles-1h.json"))}}
    if carry is not None:
        inputs["hl_carry_daily"] = sha256_file(hl_carry_daily)
    manifest = {"config": asdict(config), "script_sha256": sha256_file(Path(__file__)), "inputs_sha256": inputs,
                "outputs_sha256": {p.name: sha256_file(p) for p in sorted(output.glob("*")) if p.name != "manifest.json"}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jquants-db", type=Path, default=JQ_DIR / "jquants.db")
    parser.add_argument("--hl-dir", type=Path, default=HL_DIR)
    parser.add_argument("--hl-carry-daily", type=Path, default=JQ_DIR / "results/hl_japan_carry/daily.csv",
                        help="daily.csv from hl_japan_carry.py; its valid close pairs give the HL worst rise")
    parser.add_argument("--output-dir", type=Path, default=JQ_DIR / "results/japan_earnings_risk")
    args = parser.parse_args()
    summary = run(args.jquants_db, args.hl_dir, args.hl_carry_daily, args.output_dir, Config())
    print(json.dumps(summary["stocks"], indent=1))


if __name__ == "__main__":
    main()
