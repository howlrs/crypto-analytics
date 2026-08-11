#!/usr/bin/env python3
"""
Delta-neutral funding-rate-capture backtest.

Strategy: spot long q units x perp short q units, q fixed at initial price
(no rebalancing). Capital C = 1.5 * N (N = initial spot notional).

Daily return = [q*(S_t - S_{t-1}) - q*(F_t - F_{t-1}) + funding_pnl_t] / C
  basis_pnl_t  = q*(S_t - S_{t-1}) - q*(F_t - F_{t-1})
  funding_pnl_t = sum over funding events that day of: q * F_at_event_ts * rate
                  (short perp receives when rate > 0)

NAV convention (explicit choice, used consistently throughout):
  nav_t = nav_{t-1} + (basis_pnl_t + funding_pnl_t - cost_t)   [additive on $ terms]
  ret_t = (basis_pnl_t + funding_pnl_t - cost_t) / C
This is the "nav_t = C + cumulative(...)" form from the spec, applied
incrementally day by day. ret_t is always relative to fixed C (not
compounding on drifting nav), consistent with the delta-neutral fixed-q
design (q is NOT rebalanced, so there is no compounding notion of "nav").

Costs: taker/maker bps per venue leg, + flat 2bp slippage per fill.
A round trip (entry+exit) = 4 fills (spot buy, perp sell-short at entry;
spot sell, perp buy-to-cover at exit), each leg costed at its own venue's bp.

Variants:
  always_on : one round trip for the whole period (entry costs + exit costs).
  cond_5_0  : trailing 7d sum of daily funding_pnl / C, annualized by
              *(365/7). Enter when > 5%, exit when < 0%, hysteresis (no
              re-entry while already in / no re-exit while already out).
              Each toggle costs a full round trip's worth of fills
              (entry OR exit = 2 fills for spot+perp on that side... see
              note below). We treat each transition (enter or exit) as
              costing entry-fills (2 fills: 1 spot + 1 perp) since a
              transition is either an entry (open both legs) or an exit
              (close both legs) -- i.e. 2 fills per transition, and a
              full in->out->in round trip = 4 fills total, matching the
              spec's "4 fills per round trip" definition.
  cond_10_3 : same logic, thresholds 10% / 3%.

Margin maintenance check (always_on only, per pair):
  perp margin = 0.5N initially (mark-to-market daily as
  0.5N + cumulative(-q*dF)). Flag days where margin < 0.25N (50% of
  initial margin). Report count and max additional margin needed.
"""

import sqlite3
import math
import os
import sys
import numpy as np
import pandas as pd

DB_PATH = "/mnt/e/Datas/market/market.db"
DB_URI = f"file:{DB_PATH}?mode=ro"
OUT_DIR = "/home/o9oem/workspace/crypto/analytics/results/funding_capture"
os.makedirs(OUT_DIR, exist_ok=True)

DAY_MS = 86400000

# ---------------------------------------------------------------------------
# Cost tables (bps), spec-confirmed
# ---------------------------------------------------------------------------
SLIPPAGE_BP = 2.0

TAKER_BP = {
    ("binance", "spot"): 10.0,
    ("binance", "perp"): 5.0,
    ("bybit", "spot"): 10.0,
    ("bybit", "perp"): 5.5,
    ("hyperliquid", "perp"): 4.5,
}
MAKER_BP = {
    ("binance", "spot"): 10.0,   # no maker rate given -> reuse taker
    ("binance", "perp"): 2.0,
    ("bybit", "spot"): 10.0,     # reuse taker
    ("bybit", "perp"): 2.0,
    ("hyperliquid", "perp"): 1.5,
}


def eff_cost_bp(venue, market, fee_table):
    return fee_table[(venue, market)] + SLIPPAGE_BP


# ---------------------------------------------------------------------------
# Pair definitions
# ---------------------------------------------------------------------------
# Each pair specifies which klines rows to use for spot (S) and perp (F)
# price legs, and which funding table rows to use for the funding leg,
# plus the venue/market used for fee lookups on each leg.
PAIRS = {
    "A_BTC": dict(
        label="A: Binance BTC spot/perp",
        spot_venue="binance", spot_market="spot", spot_symbol="BTCUSDT",
        perp_venue="binance", perp_market="perp", perp_symbol="BTCUSDT",
        fund_venue="binance", fund_symbol="BTCUSDT",
        period_start=1609459200000, period_end=1785542340000,
    ),
    "B_ETH": dict(
        label="B: Binance ETH spot/perp",
        spot_venue="binance", spot_market="spot", spot_symbol="ETHUSDT",
        perp_venue="binance", perp_market="perp", perp_symbol="ETHUSDT",
        fund_venue="binance", fund_symbol="ETHUSDT",
        period_start=1609459200000, period_end=1785542340000,
    ),
    "C_HYPE": dict(
        label="C: Bybit spot HYPE / HL perp funding (price proxy: bybit perp HYPEUSDT)",
        spot_venue="bybit", spot_market="spot", spot_symbol="HYPEUSDT",
        perp_venue="bybit", perp_market="perp", perp_symbol="HYPEUSDT",
        fund_venue="hyperliquid", fund_symbol="HYPE",
        # funding leg fee-venue treated as hyperliquid perp (matches actual
        # short-funding counterparty), price/klines fee-venue = bybit
        fund_fee_venue="hyperliquid", fund_fee_market="perp",
        period_start=1752224400000, period_end=1786406340000,
    ),
    "D_HL_BTC": dict(
        label="D: HL BTC funding, Binance BTC spot/perp price proxy",
        spot_venue="binance", spot_market="spot", spot_symbol="BTCUSDT",
        perp_venue="binance", perp_market="perp", perp_symbol="BTCUSDT",
        fund_venue="hyperliquid", fund_symbol="BTC",
        fund_fee_venue="hyperliquid", fund_fee_market="perp",
        period_start=1683849600048, period_end=1786417200120,
    ),
}

VARIANTS = ["always_on", "cond_5_0", "cond_10_3"]


# ---------------------------------------------------------------------------
# Data loading (SQL pre-aggregation, no full 1m load into pandas)
# ---------------------------------------------------------------------------
def load_daily_prices(conn, venue, market, symbol, ts_start, ts_end):
    """Daily snapshot: for each UTC day, the close of the 1m bar with
    MIN(ts) within that day (i.e. bar at/just after 00:00:00 UTC)."""
    q = """
    WITH days AS (
        SELECT ts/86400000 AS day, MIN(ts) AS first_ts
        FROM klines
        WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
        GROUP BY ts/86400000
    )
    SELECT d.day, k.ts, k.open
    FROM days d
    JOIN klines k
      ON k.venue=? AND k.market=? AND k.symbol=? AND k.ts = d.first_ts
    ORDER BY d.day
    """
    params = (venue, market, symbol, ts_start, ts_end, venue, market, symbol)
    df = pd.read_sql_query(q, conn, params=params)
    df["date"] = pd.to_datetime(df["day"] * DAY_MS, unit="ms")
    return df[["date", "ts", "open"]].rename(columns={"open": "price"})


def load_funding(conn, venue, symbol, ts_start, ts_end):
    q = """
    SELECT ts, rate, interval_hours
    FROM funding
    WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?
    ORDER BY ts
    """
    df = pd.read_sql_query(q, conn, params=(venue, symbol, ts_start, ts_end))
    df["day"] = df["ts"] // DAY_MS
    df["date"] = pd.to_datetime(df["day"] * DAY_MS, unit="ms")
    return df


# ---------------------------------------------------------------------------
# Core per-pair daily series builder
# ---------------------------------------------------------------------------
def build_pair_series(conn, pair_key, cfg):
    ts_start, ts_end = cfg["period_start"], cfg["period_end"]

    spot = load_daily_prices(conn, cfg["spot_venue"], cfg["spot_market"],
                              cfg["spot_symbol"], ts_start, ts_end)
    perp = load_daily_prices(conn, cfg["perp_venue"], cfg["perp_market"],
                              cfg["perp_symbol"], ts_start, ts_end)
    fund = load_funding(conn, cfg["fund_venue"], cfg["fund_symbol"], ts_start, ts_end)

    spot = spot.rename(columns={"price": "S"})
    perp = perp.rename(columns={"price": "F"})

    px = pd.merge(spot[["date", "S"]], perp[["date", "F"]], on="date", how="inner")
    px = px.sort_values("date").reset_index(drop=True)

    # per-event funding needs F at the event's own timestamp for dollarizing.
    # We use the perp daily-snapshot F price interpolated as "price prevailing
    # on that day" (nearest available daily perp close at/prior to event day),
    # since we do not load full 1m history. This is the daily-granularity
    # price proxy consistent with the rest of the daily-snapshot backtest.
    fund_by_day = fund.copy()
    fund_by_day = fund_by_day.merge(
        px[["date", "F"]], on="date", how="left"
    )
    # forward-fill any day gaps in F (shouldn't normally occur inside px range)
    fund_by_day["F"] = fund_by_day["F"].ffill().bfill()
    daily_funding_pnl_unit = (
        fund_by_day.groupby("date")
        .apply(lambda g: (g["F"] * g["rate"]).sum())
        .rename("funding_pnl_per_q")
    )

    df = px.merge(daily_funding_pnl_unit.reset_index(), on="date", how="left")
    df["funding_pnl_per_q"] = df["funding_pnl_per_q"].fillna(0.0)

    df["dS"] = df["S"].diff()
    df["dF"] = df["F"].diff()
    df = df.iloc[1:].reset_index(drop=True)  # first day has no return (t-1 unknown)

    return df, fund  # fund kept raw (event-level) for reconciliation/margin/trades


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
def run_backtest(df, cfg, variant, fee_table, N=1_000_000.0):
    """df has columns date,S,F,dS,dF,funding_pnl_per_q (per-unit-q, $ per q).
    N = initial notional (spot leg $ value at entry). C = 1.5N.
    q = N / S0 (fixed at first day's S in df, i.e. day after period start
    snapshot -- we use the first row's S as entry price)."""
    C = 1.5 * N
    S0 = df["S"].iloc[0]
    q = N / S0

    spot_venue, spot_market = cfg["spot_venue"], cfg["spot_market"]
    perp_venue, perp_market = cfg["perp_venue"], cfg["perp_market"]

    def fill_cost_bp(venue, market):
        return eff_cost_bp(venue, market, fee_table)

    # cost of one full "leg pair" transition (spot fill + perp fill), in $ of C
    def transition_cost_dollars(price_notional):
        spot_bp = fill_cost_bp(spot_venue, spot_market)
        perp_bp = fill_cost_bp(perp_venue, perp_market)
        return price_notional * (spot_bp + perp_bp) / 10000.0

    n = len(df)
    basis_pnl = (q * df["dS"] - q * df["dF"]).values
    funding_pnl = (q * df["funding_pnl_per_q"]).values
    dates = df["date"].values

    cost = np.zeros(n)
    in_position = np.zeros(n, dtype=bool)
    trades = 0

    if variant == "always_on":
        in_position[:] = True
        # entry cost on day 0, exit cost on last day
        cost[0] += transition_cost_dollars(N)
        cost[-1] += transition_cost_dollars(N)
        trades = 1  # one round trip

    else:
        if variant == "cond_5_0":
            enter_thr, exit_thr = 0.05, 0.0
        elif variant == "cond_10_3":
            enter_thr, exit_thr = 0.10, 0.03
        else:
            raise ValueError(variant)

        # trailing 7-day sum of daily funding_pnl / C, annualized *365/7
        fpnl_series = pd.Series(funding_pnl)
        trail7 = fpnl_series.rolling(window=7, min_periods=7).sum()
        ann = (trail7 / C) * (365.0 / 7.0)
        ann = ann.fillna(-np.inf)  # can't evaluate signal -> stay flat until enough data

        holding = False
        for i in range(n):
            a = ann.iloc[i]
            if not holding:
                if a > enter_thr:
                    holding = True
                    cost[i] += transition_cost_dollars(N)
                    trades += 1
            else:
                if a < exit_thr:
                    holding = False
                    cost[i] += transition_cost_dollars(N)
                    trades += 1
            in_position[i] = holding
        # if still holding at the end, close out (exit cost) to realize final state
        if holding:
            cost[-1] += transition_cost_dollars(N)
            trades += 1

        # zero out basis/funding pnl on days not in position (flat = no exposure)
        basis_pnl = np.where(in_position, basis_pnl, 0.0)
        funding_pnl = np.where(in_position, funding_pnl, 0.0)

    ret = (basis_pnl + funding_pnl - cost) / C
    nav = C + np.cumsum(basis_pnl + funding_pnl - cost)

    out = pd.DataFrame({
        "date": dates,
        "nav": nav,
        "ret": ret,
        "funding_pnl": funding_pnl,
        "basis_pnl": basis_pnl,
        "cost": cost,
    })

    total_cost_bp = (cost.sum() / C) * 10000.0

    return out, dict(C=C, q=q, N=N, trades=trades, total_cost_bp=total_cost_bp)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def compute_metrics(out, meta):
    ret = out["ret"].values
    n_days = len(ret)
    if n_days == 0:
        return {}

    mean_d = ret.mean()
    std_d = ret.std(ddof=1) if n_days > 1 else 0.0
    ann_return = mean_d * 365.0
    ann_vol = std_d * math.sqrt(365.0)
    sharpe = (mean_d * 365.0) / (std_d * math.sqrt(365.0)) if std_d > 0 else float("nan")

    nav = out["nav"].values
    running_max = np.maximum.accumulate(nav)
    C = meta["C"]
    dd = (nav - running_max) / C
    max_dd = dd.min()

    pct_pos = (ret > 0).mean() * 100.0

    funding_pnl = out["funding_pnl"].values
    fund_received = funding_pnl[funding_pnl > 0].sum()
    fund_paid = funding_pnl[funding_pnl < 0].sum()
    pct_neg_funding_days = (funding_pnl < 0).mean() * 100.0

    return dict(
        ann_return=ann_return,
        ann_vol=ann_vol,
        sharpe=sharpe,
        max_dd=max_dd,
        pct_pos_days=pct_pos,
        n_trades=meta["trades"],
        total_cost_bp=meta["total_cost_bp"],
        fund_received=fund_received,
        fund_paid=fund_paid,
        pct_neg_funding_days=pct_neg_funding_days,
        n_days=n_days,
    )


def yearly_breakdown(out):
    d = out.copy()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    rows = []
    for yr, g in d.groupby("year"):
        ret = g["ret"].values
        if len(ret) < 2:
            continue
        mean_d = ret.mean()
        std_d = ret.std(ddof=1)
        yr_return = mean_d * 365.0
        yr_sharpe = (mean_d * 365.0) / (std_d * math.sqrt(365.0)) if std_d > 0 else float("nan")
        rows.append(dict(year=int(yr), ann_return=yr_return, sharpe=yr_sharpe, n_days=len(ret)))
    return rows


# ---------------------------------------------------------------------------
# Margin maintenance check
# ---------------------------------------------------------------------------
def margin_check(df, N=1_000_000.0):
    """always_on: perp margin = 0.5N initially, marked-to-market daily as
    0.5N + cumulative(-q*dF). Flag days margin < 0.25N."""
    S0 = df["S"].iloc[0]
    q = N / S0
    dF = df["dF"].values
    margin = 0.5 * N + np.cumsum(-q * dF)
    threshold = 0.25 * N
    below = margin < threshold
    n_below = int(below.sum())
    additional_needed = np.where(below, threshold - margin, 0.0)
    max_additional = float(additional_needed.max()) if n_below > 0 else 0.0
    worst_idx = int(np.argmin(margin))
    worst_margin = float(margin[worst_idx])
    worst_date = df["date"].iloc[worst_idx]
    return dict(
        n_days_below_50pct=n_below,
        max_additional_margin=max_additional,
        worst_margin_value=worst_margin,
        worst_margin_pct_of_initial=worst_margin / (0.5 * N) * 100.0,
        worst_date=str(worst_date),
    )


# ---------------------------------------------------------------------------
# Verification steps
# ---------------------------------------------------------------------------
def verify_funding_reconciliation(conn, pair_key, cfg, fund_used):
    venue, symbol = cfg["fund_venue"], cfg["fund_symbol"]
    ts_start, ts_end = cfg["period_start"], cfg["period_end"]
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(rate) FROM funding WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?",
        (venue, symbol, ts_start, ts_end),
    )
    db_sum = cur.fetchone()[0] or 0.0
    used_sum = fund_used["rate"].sum()
    match = math.isclose(db_sum, used_sum, rel_tol=1e-9, abs_tol=1e-12)
    return dict(pair=pair_key, db_sum=db_sum, used_sum=used_sum, match=match,
                n_rows_db=None, n_rows_used=len(fund_used))


def verify_2021_btc_sanity(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(rate), COUNT(*) FROM funding WHERE venue='binance' AND symbol='BTCUSDT' "
        "AND ts >= 1609459200000 AND ts < 1640995200000"
    )
    sum_rate, n = cur.fetchone()
    # actual annualization per spec: (2021 rate values) x 365x3 -- interpreted as
    # mean per-event rate x 3 events/day x 365 days/year
    mean_rate = sum_rate / n if n else 0.0
    ann = mean_rate * 365 * 3
    return dict(sum_rate=sum_rate, n_events=n, mean_rate=mean_rate, annualized=ann,
                exceeds_10pct=ann > 0.10)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    conn = sqlite3.connect(DB_URI, uri=True)

    all_metrics = []
    yearly_rows_always_on = {}
    margin_rows = {}
    recon_rows = []

    for pair_key, cfg in PAIRS.items():
        print(f"\n=== Loading pair {pair_key}: {cfg['label']} ===")
        df, fund_raw = build_pair_series(conn, pair_key, cfg)
        print(f"  {len(df)} daily rows, date range {df['date'].min()} .. {df['date'].max()}")
        print(f"  {len(fund_raw)} funding events loaded")

        # funding reconciliation
        recon = verify_funding_reconciliation(conn, pair_key, cfg, fund_raw)
        recon_rows.append(recon)

        # margin check (always_on q, based on df)
        margin_rows[pair_key] = margin_check(df)

        for variant in VARIANTS:
            fee_table = TAKER_BP  # report taker as primary; maker computed separately below
            out, meta = run_backtest(df, cfg, variant, fee_table)
            metrics = compute_metrics(out, meta)
            metrics["pair"] = pair_key
            metrics["variant"] = variant
            metrics["fee_mode"] = "taker"
            all_metrics.append(metrics)

            csv_path = os.path.join(OUT_DIR, f"{pair_key}_{variant}_nav.csv")
            out.to_csv(csv_path, index=False)

            if variant == "always_on":
                yearly_rows_always_on[pair_key] = yearly_breakdown(out)

            # maker variant too (separate metrics row, not separate NAV csv
            # per spec's CSV naming which doesn't mention fee-mode suffix;
            # we still want the numbers for the report, so also write a
            # maker-suffixed CSV to keep everything reproducible/inspectable)
            out_m, meta_m = run_backtest(df, cfg, variant, MAKER_BP)
            metrics_m = compute_metrics(out_m, meta_m)
            metrics_m["pair"] = pair_key
            metrics_m["variant"] = variant
            metrics_m["fee_mode"] = "maker"
            all_metrics.append(metrics_m)
            csv_path_m = os.path.join(OUT_DIR, f"{pair_key}_{variant}_maker_nav.csv")
            out_m.to_csv(csv_path_m, index=False)

    metrics_df = pd.DataFrame(all_metrics)
    metrics_csv = os.path.join(OUT_DIR, "summary_metrics.csv")
    metrics_df.to_csv(metrics_csv, index=False)

    print("\n\n================= RESULTS MATRIX (taker fees) =================")
    disp = metrics_df[metrics_df["fee_mode"] == "taker"][
        ["pair", "variant", "ann_return", "ann_vol", "sharpe", "max_dd",
         "pct_pos_days", "n_trades", "total_cost_bp"]
    ]
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:,.4f}")
    print(disp.to_string(index=False))

    print("\n================= RESULTS MATRIX (maker fees) =================")
    disp_m = metrics_df[metrics_df["fee_mode"] == "maker"][
        ["pair", "variant", "ann_return", "ann_vol", "sharpe", "max_dd",
         "pct_pos_days", "n_trades", "total_cost_bp"]
    ]
    print(disp_m.to_string(index=False))

    print("\n================= FUNDING BREAKDOWN (taker fees) =================")
    fdisp = metrics_df[metrics_df["fee_mode"] == "taker"][
        ["pair", "variant", "fund_received", "fund_paid", "pct_neg_funding_days"]
    ]
    print(fdisp.to_string(index=False))

    print("\n================= YEARLY BREAKDOWN (always_on) =================")
    for pair_key, rows in yearly_rows_always_on.items():
        print(f"\n-- {pair_key} --")
        ydf = pd.DataFrame(rows)
        print(ydf.to_string(index=False))

    print("\n================= MARGIN MAINTENANCE CHECK (always_on) =================")
    mdf = pd.DataFrame.from_dict(margin_rows, orient="index")
    print(mdf.to_string())

    print("\n================= FUNDING RECONCILIATION =================")
    rdf = pd.DataFrame(recon_rows)
    print(rdf.to_string(index=False))

    print("\n================= 2021 BTC SANITY CHECK =================")
    sanity = verify_2021_btc_sanity(conn)
    print(sanity)

    print("\n================= SHARPE >= 2 CHECK =================")
    hi = metrics_df[(metrics_df["fee_mode"] == "taker") & (metrics_df["sharpe"] >= 2.0)]
    if len(hi) > 0:
        print(hi[["pair", "variant", "sharpe"]].to_string(index=False))
    else:
        print("None found with taker fees.")
    hi_m = metrics_df[(metrics_df["fee_mode"] == "maker") & (metrics_df["sharpe"] >= 2.0)]
    if len(hi_m) > 0:
        print("(maker fees):")
        print(hi_m[["pair", "variant", "sharpe"]].to_string(index=False))

    conn.close()

    print(f"\nAll outputs written to {OUT_DIR}")
    print(f"Summary metrics CSV: {metrics_csv}")


if __name__ == "__main__":
    main()
