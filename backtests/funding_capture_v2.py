#!/usr/bin/env python3
"""
Delta-neutral funding-rate-capture backtest v2 (rebalanced, unified-account model).

Differences from v1 (`funding_capture.py`, untouched):
  - v1: fixed q at entry, no rebalancing, C = 1.5N fixed forever. Breaks down
    (short margin becomes insufficient) once price moves a large multiple away
    from entry.
  - v2 (this file): NAV = spot mark value + perp margin account (equity),
    updated daily via mark-to-market. Target notional N_t = k * NAV_t is
    re-targeted, and rebalanced either monthly (UTC month start) or whenever
    actual notional drifts >= 10% from target (whichever fires first). Each
    rebalance re-quotes q (spot units = perp short units, kept equal for
    delta-neutrality) and re-bases the liquidation-price calculation off the
    new entry price/quantity/margin. This directly answers the "what if price
    goes 3x" fragility of v1 by keeping margin proportional to a fresh mark,
    and lets us measure the liquidation safety margin explicitly (see
    Part 1 "Liquidation distance verification" below).

Three parts:
  Part 1: rebalanced funding capture w/ liquidation-distance verification,
          on pairs A_BTC / B_ETH / C_HYPE, k in {0.66, 0.75}.
  Part 2: 3-venue funding rotation (binance/bybit/hyperliquid) for BTC/ETH,
          spot leg fixed on binance spot, short leg on whichever venue has
          the highest trailing-7d average funding (hysteresis 0.3bp/8h).
  Part 3: HYPE cross-venue funding spread (HL vs Bybit) and, if the spread
          is economically significant, a perp-perp DN backtest comparing
          capital efficiency against the Part 1 C_HYPE spot-type strategy.

NAV convention: additive-dollar NAV, updated day by day as
  nav_t = nav_{t-1} + basis_pnl_t + funding_pnl_t - rebalance_cost_t
where basis_pnl_t uses the *previous day's* held quantity q_{t-1} marked
against the day's price change (standard daily mark-to-market with quantity
frozen between rebalances), consistent with v1's daily-snapshot design
(each UTC day's price = first available 1m bar's `open` that day).

All DB access is read-only:  sqlite3.connect("file:...?mode=ro", uri=True)
"""

import sqlite3
import math
import os
import sys
import datetime
import numpy as np
import pandas as pd

DB_PATH = "/mnt/e/Datas/market/market.db"
DB_URI = f"file:{DB_PATH}?mode=ro"
OUT_DIR = "/home/o9oem/workspace/crypto/analytics/results/funding_capture_v2"
os.makedirs(OUT_DIR, exist_ok=True)
V1_SUMMARY_CSV = "/home/o9oem/workspace/crypto/analytics/results/funding_capture/summary_metrics.csv"

DAY_MS = 86400000

pd.set_option("display.width", 220)
pd.set_option("display.float_format", lambda x: f"{x:,.5f}")


def log(msg):
    print(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Cost tables (bps, taker), spec-confirmed. Used for Part 2/3 venue-specific
# costing. Part 1 uses the simplified flat 17bp (spot10 + perp5 + slip2) per
# the task spec, regardless of venue.
# ---------------------------------------------------------------------------
TAKER_BP = {
    ("binance", "spot"): 10.0,
    ("binance", "perp"): 5.0,
    ("bybit", "spot"): 10.0,
    ("bybit", "perp"): 5.5,
    ("hyperliquid", "perp"): 4.5,
}
SLIPPAGE_BP = 2.0
PART1_REBALANCE_COST_BP = 10.0 + 5.0 + 2.0  # = 17.0, flat simplification per spec


def venue_leg_cost_bp(venue, market):
    return TAKER_BP[(venue, market)] + SLIPPAGE_BP


# ---------------------------------------------------------------------------
# Pair definitions (Part 1)
# ---------------------------------------------------------------------------
# C_HYPE period_start: spec calls for 2025-07-14 00:00:00 UTC because Bybit
# spot HYPEUSDT listed 2025-07-11 and the first ~3 days are excluded from
# basis calc (thin/unstable listing-day liquidity). Verified independently:
#   datetime.datetime(2025,7,14,0,0,0,tzinfo=timezone.utc).timestamp()*1000
#   == 1752451200000
# (Note: the value 1752541200000 mentioned in the original task prompt
# decodes to 2025-07-15 01:00:00 UTC, not 2025-07-14 00:00:00 UTC -- it does
# not match the stated intent. Recomputed independently here and used as
# 1752451200000, with this discrepancy flagged explicitly in the report.)
C_HYPE_PERIOD_START = 1752451200000
_chk = datetime.datetime.fromtimestamp(C_HYPE_PERIOD_START / 1000, tz=datetime.timezone.utc)
assert _chk == datetime.datetime(2025, 7, 14, 0, 0, 0, tzinfo=datetime.timezone.utc), _chk

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
        label="C: Bybit spot HYPE / HL perp funding",
        spot_venue="bybit", spot_market="spot", spot_symbol="HYPEUSDT",
        perp_venue="bybit", perp_market="perp", perp_symbol="HYPEUSDT",
        fund_venue="hyperliquid", fund_symbol="HYPE",
        period_start=C_HYPE_PERIOD_START, period_end=1786406340000,
    ),
}

K_VARIANTS = [0.66, 0.75]
C0 = 1_000_000.0  # initial NAV; N_target0 = k * C0

MARGIN_RATIO = 0.5      # isolated margin allocated: 0.5 * notional at entry
MAINT_MARGIN_RATE = 0.005  # 0.5% maintenance margin rate (of notional at F_t)

MONTHLY = "monthly"
DRIFT10 = "drift_10pct"


# ---------------------------------------------------------------------------
# Data loading (SQL pre-aggregation, daily-snapshot convention from v1)
# ---------------------------------------------------------------------------
def load_daily_prices(conn, venue, market, symbol, ts_start, ts_end):
    """Daily snapshot: for each UTC day, the `open` of the 1m bar with
    MIN(ts) within that day (bar at/just after 00:00:00 UTC)."""
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


def load_daily_high(conn, venue, market, symbol, ts_start, ts_end):
    """For liquidation-distance scanning: per-UTC-day MAX(high) over all 1m
    bars that day (used as a coarse pre-filter before per-minute scan is
    unnecessary -- we scan every 1m high directly in the liquidation check,
    this helper is kept for a fast daily overview only, not used for the
    actual liq-distance computation)."""
    q = """
    SELECT ts/86400000 AS day, MAX(high) AS day_high
    FROM klines
    WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
    GROUP BY ts/86400000
    ORDER BY day
    """
    df = pd.read_sql_query(q, conn, params=(venue, market, symbol, ts_start, ts_end))
    return df


def load_minute_highs(conn, venue, market, symbol, ts_start, ts_end):
    """Full 1m ts,high series for a period -- used for liquidation-distance
    scanning per rebalance interval. Loaded once per pair (not per interval)
    to avoid O(n_intervals) separate SQL round-trips."""
    q = """
    SELECT ts, high
    FROM klines
    WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
    ORDER BY ts
    """
    df = pd.read_sql_query(q, conn, params=(venue, market, symbol, ts_start, ts_end))
    return df


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

    fund_by_day = fund.merge(px[["date", "F"]], on="date", how="left")
    fund_by_day["F"] = fund_by_day["F"].ffill().bfill()
    daily_funding_pnl_unit = (
        fund_by_day.groupby("date")
        .apply(lambda g: (g["F"] * g["rate"]).sum())
        .rename("funding_pnl_per_q")
    )

    df = px.merge(daily_funding_pnl_unit.reset_index(), on="date", how="left")
    df["funding_pnl_per_q"] = df["funding_pnl_per_q"].fillna(0.0)
    df = df.sort_values("date").reset_index(drop=True)

    return df, fund


# ---------------------------------------------------------------------------
# Liquidation price derivation (short perp position, isolated margin)
# ---------------------------------------------------------------------------
# At entry (most recent rebalance): entry price F_entry, quantity q (short),
# allocated isolated margin M0 = MARGIN_RATIO * notional_at_entry.
# As price rises to F_t, unrealized loss on the short = q * (F_t - F_entry).
# Remaining margin equity at F_t = M0 - q * (F_t - F_entry).
# Liquidation fires when remaining equity equals the maintenance margin,
# itself computed on the *current* mark F_t:
#     M0 - q*(F_t - F_entry) = MAINT_MARGIN_RATE * q * F_t
# Expand and solve for F_t (call it F_liq):
#     M0 - q*F_t + q*F_entry = MAINT_MARGIN_RATE * q * F_t
#     M0 + q*F_entry = q*F_t + MAINT_MARGIN_RATE*q*F_t
#     M0 + q*F_entry = q*F_t*(1 + MAINT_MARGIN_RATE)
#     F_t = (M0 + q*F_entry) / (q * (1 + MAINT_MARGIN_RATE))
# => F_liq = (M0 + q*F_entry) / (q * (1 + MAINT_MARGIN_RATE))
def liq_price_short(M0, q, F_entry):
    return (M0 + q * F_entry) / (q * (1.0 + MAINT_MARGIN_RATE))


# ---------------------------------------------------------------------------
# Part 1 backtest engine (rebalanced, unified-account NAV model)
# ---------------------------------------------------------------------------
def is_month_start(prev_date, cur_date):
    return (cur_date.year, cur_date.month) != (prev_date.year, prev_date.month)


def run_part1_backtest(df, k, C0=C0):
    """df: date,S,F,funding_pnl_per_q (daily snapshot, per pair).
    Unified-account model:
      NAV_t = NAV_{t-1} + basis_pnl_t + funding_pnl_t - rebal_cost_t
      basis_pnl_t = q_{t-1} * (dS_t - dF_t)   [q held over the day, frozen
                    until the next rebalance event]
      funding_pnl_t = q_{t-1} * funding_pnl_per_q_t
    Rebalance evaluated *after* the day's mark-to-market NAV update, at
    which point q (and F_entry/margin for liq-distance calc) are reset
    based on N_target = k * NAV_t.
    Returns: out DataFrame (date,nav,q,F_entry,margin,ret,funding_pnl,
    basis_pnl,cost,rebalanced,N_target,N_actual) and meta dict.
    """
    n = len(df)
    dates = df["date"].tolist()
    S = df["S"].values
    F = df["F"].values
    fpnl_unit = df["funding_pnl_per_q"].values

    nav = np.zeros(n)
    q_arr = np.zeros(n)          # q HELD during day t (i.e. q_{t-1} used for pnl on day t)
    F_entry_arr = np.zeros(n)    # entry price backing the currently held q
    margin_arr = np.zeros(n)     # isolated margin backing currently held q
    cost = np.zeros(n)
    rebalanced = np.zeros(n, dtype=bool)
    N_target_arr = np.zeros(n)
    N_actual_arr = np.zeros(n)
    basis_pnl = np.zeros(n)
    funding_pnl = np.zeros(n)

    # Day 0: initialize NAV=C0, then immediately size q to N_target0=k*C0 at S[0].
    nav_prev = C0
    q_prev = 0.0
    F_entry_prev = S[0]
    margin_prev = 0.0
    n_rebalances = 0
    total_cost = 0.0

    prev_date = dates[0]
    for t in range(n):
        # --- mark-to-market using yesterday's held q (0 on day 0) ---
        if t == 0:
            dS = 0.0
            dF = 0.0
        else:
            dS = S[t] - S[t - 1]
            dF = F[t] - F[t - 1]
        bpnl = q_prev * (dS - dF)
        fpnl = q_prev * fpnl_unit[t]
        nav_t = nav_prev + bpnl + fpnl

        # --- rebalance decision ---
        cur_date = dates[t]
        month_flag = (t == 0) or is_month_start(prev_date, cur_date)
        N_target = k * nav_t
        N_actual = q_prev * F[t]
        if N_target > 0:
            drift = abs(N_actual - N_target) / N_target
        else:
            drift = 0.0
        drift_flag = (t > 0) and (drift >= 0.10)
        do_rebalance = (t == 0) or month_flag or drift_flag

        rcost = 0.0
        if do_rebalance:
            dN = N_target - N_actual
            rcost = abs(dN) * (PART1_REBALANCE_COST_BP / 10000.0)
            nav_t -= rcost
            total_cost += rcost
            n_rebalances += 1
            # re-quote: new q at today's price F[t], notional = N_target (post-cost NAV re-targeted)
            N_target_post = k * nav_t
            q_new = N_target_post / F[t] if F[t] > 0 else 0.0
            F_entry_new = F[t]
            margin_new = MARGIN_RATIO * (q_new * F[t])
        else:
            q_new = q_prev
            F_entry_new = F_entry_prev
            margin_new = margin_prev

        nav[t] = nav_t
        basis_pnl[t] = bpnl
        funding_pnl[t] = fpnl
        cost[t] = rcost
        rebalanced[t] = do_rebalance
        N_target_arr[t] = N_target
        N_actual_arr[t] = N_actual
        q_arr[t] = q_new
        F_entry_arr[t] = F_entry_new
        margin_arr[t] = margin_new

        nav_prev = nav_t
        q_prev = q_new
        F_entry_prev = F_entry_new
        margin_prev = margin_new
        prev_date = cur_date

    ret = np.zeros(n)
    ret[1:] = (nav[1:] - nav[:-1]) / C0  # relative to fixed C0, matching v1's convention

    out = pd.DataFrame({
        "date": dates, "nav": nav, "ret": ret,
        "basis_pnl": basis_pnl, "funding_pnl": funding_pnl, "cost": cost,
        "q": q_arr, "F_entry": F_entry_arr, "margin": margin_arr,
        "rebalanced": rebalanced, "N_target": N_target_arr, "N_actual": N_actual_arr,
    })
    meta = dict(C0=C0, k=k, n_rebalances=n_rebalances, total_cost=total_cost,
                total_cost_bp_of_C0=total_cost / C0 * 10000.0)
    return out, meta


def compute_metrics(out, C0=C0):
    ret = out["ret"].values[1:]  # day0 ret is 0 by construction, exclude
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
    dd = (nav - running_max) / C0
    max_dd = dd.min()

    return dict(ann_return=ann_return, ann_vol=ann_vol, sharpe=sharpe, max_dd=max_dd, n_days=n_days)


def yearly_breakdown(out):
    d = out.copy()
    d["year"] = pd.to_datetime(d["date"]).dt.year
    rows = []
    for yr, g in d.groupby("year"):
        ret = g["ret"].values[1:] if g.index[0] == 0 else g["ret"].values
        if len(ret) < 2:
            continue
        mean_d = ret.mean()
        std_d = ret.std(ddof=1)
        yr_return = mean_d * 365.0
        yr_sharpe = (mean_d * 365.0) / (std_d * math.sqrt(365.0)) if std_d > 0 else float("nan")
        rows.append(dict(year=int(yr), ann_return=yr_return, sharpe=yr_sharpe, n_days=len(ret)))
    return rows


# ---------------------------------------------------------------------------
# Liquidation distance verification
# ---------------------------------------------------------------------------
def liquidation_distance_scan(out, minute_highs):
    """For each rebalance interval [rebalance_date, next_rebalance_date),
    compute F_liq from that interval's (margin, q, F_entry), then scan all
    1m `high` values (perp market) within the interval and compute
    approach ratio = (F_liq - high) / F_liq for each bar (positive = safety
    margin remaining). Returns a DataFrame of per-bar closest approaches
    aggregated to the minimum-per-interval, plus the full worst-N list.
    Counting convention: bars (1-minute) are the unit of counting for the
    ">=10%/20% approach" thresholds below (explicitly noted per spec).
    """
    rebal_idx = out.index[out["rebalanced"]].tolist()
    mh = minute_highs.sort_values("ts").reset_index(drop=True)
    ts_arr = mh["ts"].values
    high_arr = mh["high"].values

    rows = []
    n_within_10pct = 0
    n_within_20pct = 0

    dates = out["date"].tolist()
    for i, ridx in enumerate(rebal_idx):
        interval_start_date = dates[ridx]
        interval_end_date = dates[rebal_idx[i + 1]] if i + 1 < len(rebal_idx) else dates[-1] + pd.Timedelta(days=1)
        ts_start = int(interval_start_date.value // 1_000_000)  # ns -> ms
        ts_end = int(interval_end_date.value // 1_000_000)

        margin = out["margin"].iloc[ridx]
        q = out["q"].iloc[ridx]
        F_entry = out["F_entry"].iloc[ridx]
        if q <= 0 or margin <= 0:
            continue
        F_liq = liq_price_short(margin, q, F_entry)

        mask = (ts_arr >= ts_start) & (ts_arr < ts_end)
        if not mask.any():
            continue
        seg_high = high_arr[mask]
        seg_ts = ts_arr[mask]
        approach = (F_liq - seg_high) / F_liq  # smaller = closer to liquidation

        n_within_10pct += int((approach <= 0.10).sum())
        n_within_20pct += int((approach <= 0.20).sum())

        min_i = np.argmin(approach)
        rows.append(dict(
            interval_start=interval_start_date, F_liq=F_liq, F_entry=F_entry,
            q=q, margin=margin,
            worst_ts=int(seg_ts[min_i]), worst_high=float(seg_high[min_i]),
            worst_approach=float(approach[min_i]),
        ))

    interval_df = pd.DataFrame(rows)
    return interval_df, n_within_10pct, n_within_20pct


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def verify_funding_reconciliation(conn, pair_key, venue, symbol, ts_start, ts_end, fund_used):
    cur = conn.cursor()
    cur.execute(
        "SELECT SUM(rate) FROM funding WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?",
        (venue, symbol, ts_start, ts_end),
    )
    db_sum = cur.fetchone()[0] or 0.0
    used_sum = fund_used["rate"].sum()
    match = math.isclose(db_sum, used_sum, rel_tol=1e-9, abs_tol=1e-12)
    return dict(pair=pair_key, venue=venue, symbol=symbol, db_sum=db_sum, used_sum=used_sum,
                match=match, n_rows_used=len(fund_used))


# ---------------------------------------------------------------------------
# Part 1 driver
# ---------------------------------------------------------------------------
def run_part1(conn):
    log("=== PART 1: rebalanced funding capture ===")
    all_metrics = []
    all_liq_rows = []
    recon_rows = []
    yearly_rows = {}

    for pair_key, cfg in PAIRS.items():
        log(f"  loading pair {pair_key}: {cfg['label']}")
        df, fund_raw = build_pair_series(conn, pair_key, cfg)
        log(f"    {len(df)} daily rows, {df['date'].min()}..{df['date'].max()}, {len(fund_raw)} funding events")

        recon = verify_funding_reconciliation(
            conn, pair_key, cfg["fund_venue"], cfg["fund_symbol"],
            cfg["period_start"], cfg["period_end"], fund_raw)
        recon_rows.append(recon)

        minute_highs = load_minute_highs(
            conn, cfg["perp_venue"], cfg["perp_market"], cfg["perp_symbol"],
            cfg["period_start"], cfg["period_end"])
        log(f"    loaded {len(minute_highs)} 1m bars for liq-distance scan")

        for k in K_VARIANTS:
            out, meta = run_part1_backtest(df, k)
            metrics = compute_metrics(out)
            metrics.update(pair=pair_key, k=k, n_rebalances=meta["n_rebalances"],
                            total_cost_bp_of_C0=meta["total_cost_bp_of_C0"])
            all_metrics.append(metrics)

            csv_path = os.path.join(OUT_DIR, f"{pair_key}_k{k}_nav.csv")
            out.to_csv(csv_path, index=False)

            if k == 0.66:
                yearly_rows[pair_key] = yearly_breakdown(out)

            interval_df, n10, n20 = liquidation_distance_scan(out, minute_highs)
            interval_df["pair"] = pair_key
            interval_df["k"] = k
            all_liq_rows.append(interval_df)
            metrics["n_within_10pct_liq"] = n10
            metrics["n_within_20pct_liq"] = n20

            log(f"    k={k}: ann_ret={metrics['ann_return']:.4f} sharpe={metrics['sharpe']:.3f} "
                f"rebalances={meta['n_rebalances']} cost_bp={meta['total_cost_bp_of_C0']:.2f} "
                f"n_within_10pct_liq={n10} n_within_20pct_liq={n20}")

    metrics_df = pd.DataFrame(all_metrics)
    metrics_csv = os.path.join(OUT_DIR, "part1_summary_metrics.csv")
    metrics_df.to_csv(metrics_csv, index=False)

    liq_all = pd.concat(all_liq_rows, ignore_index=True) if all_liq_rows else pd.DataFrame()
    liq_csv = os.path.join(OUT_DIR, "part1_liquidation_intervals.csv")
    liq_all.to_csv(liq_csv, index=False)

    top10_closest = liq_all.sort_values("worst_approach").head(10) if len(liq_all) else liq_all
    top10_csv = os.path.join(OUT_DIR, "part1_liquidation_top10_closest.csv")
    top10_closest.to_csv(top10_csv, index=False)

    recon_df = pd.DataFrame(recon_rows)
    recon_csv = os.path.join(OUT_DIR, "part1_funding_reconciliation.csv")
    recon_df.to_csv(recon_csv, index=False)

    # Comparison vs v1 always_on/taker
    compare_rows = []
    if os.path.exists(V1_SUMMARY_CSV):
        v1 = pd.read_csv(V1_SUMMARY_CSV)
        v1_ref = v1[(v1["variant"] == "always_on") & (v1["fee_mode"] == "taker")]
        for pair_key in PAIRS:
            v1row = v1_ref[v1_ref["pair"] == pair_key]
            if len(v1row) == 0:
                continue
            v1_ret = float(v1row["ann_return"].iloc[0])
            v1_sharpe = float(v1row["sharpe"].iloc[0])
            for k in K_VARIANTS:
                v2row = metrics_df[(metrics_df["pair"] == pair_key) & (metrics_df["k"] == k)]
                if len(v2row) == 0:
                    continue
                v2_ret = float(v2row["ann_return"].iloc[0])
                v2_sharpe = float(v2row["sharpe"].iloc[0])
                compare_rows.append(dict(
                    pair=pair_key, k=k,
                    v1_ann_return=v1_ret, v2_ann_return=v2_ret,
                    ann_return_degradation_pct=(v2_ret - v1_ret) / abs(v1_ret) * 100.0 if v1_ret != 0 else float("nan"),
                    v1_sharpe=v1_sharpe, v2_sharpe=v2_sharpe,
                    sharpe_degradation_pct=(v2_sharpe - v1_sharpe) / abs(v1_sharpe) * 100.0 if v1_sharpe != 0 else float("nan"),
                ))
    compare_df = pd.DataFrame(compare_rows)
    compare_csv = os.path.join(OUT_DIR, "part1_v1_vs_v2_comparison.csv")
    compare_df.to_csv(compare_csv, index=False)

    print("\n================= PART 1: RESULTS MATRIX =================")
    print(metrics_df.to_string(index=False))
    print("\n================= PART 1: v1(always_on/taker) vs v2 comparison =================")
    print(compare_df.to_string(index=False))
    print("\n================= PART 1: LIQUIDATION TOP-10 CLOSEST APPROACHES =================")
    print(top10_closest.to_string(index=False))
    print("\n================= PART 1: FUNDING RECONCILIATION =================")
    print(recon_df.to_string(index=False))
    print("\n================= PART 1: YEARLY BREAKDOWN (k=0.66) =================")
    for pair_key, rows in yearly_rows.items():
        print(f"\n-- {pair_key} --")
        print(pd.DataFrame(rows).to_string(index=False))

    return dict(metrics_df=metrics_df, compare_df=compare_df, top10=top10_closest,
                recon_df=recon_df)


# ---------------------------------------------------------------------------
# Part 2: 3-venue funding rotation (BTC, ETH)
# ---------------------------------------------------------------------------
PART2_PAIRS = {
    "BTC": dict(
        spot_venue="binance", spot_market="spot", spot_symbol="BTCUSDT",
        venues=dict(
            binance=dict(fund_symbol="BTCUSDT", perp_symbol="BTCUSDT"),
            bybit=dict(fund_symbol="BTCUSDT", perp_symbol="BTCUSDT"),
            hyperliquid=dict(fund_symbol="BTC", perp_symbol="BTCUSDT"),  # HL has no local price feed loaded; use binance perp price proxy for basis
        ),
        period_start=1683849600048,  # earliest common: HL funding start
        period_end=1785542340000,
    ),
    "ETH": dict(
        spot_venue="binance", spot_market="spot", spot_symbol="ETHUSDT",
        venues=dict(
            binance=dict(fund_symbol="ETHUSDT", perp_symbol="ETHUSDT"),
            bybit=dict(fund_symbol="ETHUSDT", perp_symbol="ETHUSDT"),
            hyperliquid=dict(fund_symbol="ETH", perp_symbol="ETHUSDT"),
        ),
        period_start=1683849600048,
        period_end=1785542340000,
    ),
}
HYSTERESIS_BP_PER_8H = 0.3
PART2_K = 0.66


def load_funding_8h_grid(conn, venue, symbol, ts_start, ts_end):
    """Return funding rate resampled/aggregated onto the 8h UTC grid
    (00/08/16). For native 8h-interval venues, this is a passthrough
    (grouping by 8h bucket collapses to the single matching row). For
    finer-grained data (e.g. would-be 1h), rows within each 8h bucket are
    summed so the result is comparable across venues on an 8h-equivalent
    basis, per the task's explicit instruction."""
    df = load_funding(conn, venue, symbol, ts_start, ts_end)
    if len(df) == 0:
        return df
    EIGHT_H_MS = 8 * 3600 * 1000
    df["bucket"] = (df["ts"] // EIGHT_H_MS) * EIGHT_H_MS
    agg = df.groupby("bucket").agg(rate=("rate", "sum"), n_rows=("rate", "size")).reset_index()
    agg["date"] = pd.to_datetime(agg["bucket"], unit="ms")
    return agg


def run_part2_symbol(conn, sym_key, cfg):
    log(f"  Part2 {sym_key}: loading funding grids for binance/bybit/hyperliquid")
    ts_start, ts_end = cfg["period_start"], cfg["period_end"]

    grids = {}
    intervals_native = {}
    for venue, vc in cfg["venues"].items():
        raw = load_funding(conn, venue, vc["fund_symbol"], ts_start, ts_end)
        if len(raw):
            intervals_native[venue] = sorted(raw["interval_hours"].unique().tolist())
        else:
            intervals_native[venue] = []
        g = load_funding_8h_grid(conn, venue, vc["fund_symbol"], ts_start, ts_end)
        g = g.rename(columns={"rate": f"rate_{venue}"})[["bucket", "date", f"rate_{venue}"]]
        grids[venue] = g
        log(f"    {venue}: native interval_hours={intervals_native[venue]}, "
            f"{len(raw)} raw rows -> {len(g)} 8h-bucket rows")

    merged = None
    for venue, g in grids.items():
        merged = g if merged is None else pd.merge(merged, g, on=["bucket", "date"], how="outer")
    merged = merged.sort_values("bucket").reset_index(drop=True)
    for venue in cfg["venues"]:
        merged[f"rate_{venue}"] = merged[f"rate_{venue}"].fillna(0.0)

    # trailing 7-day average per venue (7d = 21 buckets of 8h), used to pick venue
    for venue in cfg["venues"]:
        merged[f"trail7_{venue}"] = merged[f"rate_{venue}"].rolling(window=21, min_periods=21).mean()

    # spot/perp daily price (binance spot fixed; use binance perp price proxy for all venues' PnL basis,
    # since HL/bybit local price series would introduce cross-venue basis noise not requested by spec)
    spot_daily = load_daily_prices(conn, cfg["spot_venue"], cfg["spot_market"], cfg["spot_symbol"], ts_start, ts_end)
    perp_daily = load_daily_prices(conn, "binance", "perp", cfg["venues"]["binance"]["perp_symbol"], ts_start, ts_end)
    spot_daily = spot_daily.rename(columns={"price": "S"})[["date", "S"]]
    perp_daily = perp_daily.rename(columns={"price": "F"})[["date", "F"]]

    # merge 8h bucket funding onto its calendar day for pnl accounting (day-level backtest,
    # consistent w/ Part1's daily-snapshot convention)
    merged["day_date"] = merged["date"].dt.floor("D")

    px = pd.merge(spot_daily, perp_daily, on="date", how="inner").sort_values("date").reset_index(drop=True)

    venues = list(cfg["venues"].keys())

    results = {}
    for venue in venues:
        results[venue] = _simulate_part2(px, merged, venues, mode=venue)
    results["rotation"] = _simulate_part2(px, merged, venues, mode="rotation")

    return results, merged, intervals_native


def _simulate_part2(px, funding_grid, venues, mode):
    """Daily loop. N (notional) = PART2_K * C0, FIXED (Part2 spec doesn't ask
    for dynamic re-targeting like Part1; we keep notional constant at
    PART2_K*C0 throughout, consistent w/ v1's fixed-N style, since Part2's
    focus is venue-selection alpha not capital-efficiency rebalancing).
    Spot leg is binance spot (buy&hold q = N/S0 units). Perp short leg
    rotates across venue (or is pinned for single-venue baselines).
    NAV_t = NAV_{t-1} + q*(dS - dF_perp_price) + funding_pnl - switch_cost.
    We use binance perp price for the perp mark on all venues, per spec's
    focus on funding-rate difference not cross-venue basis risk.
    """
    N = PART2_K * C0
    S0 = px["S"].iloc[0]
    q = N / S0

    fmap = funding_grid.set_index("day_date")
    trail_cols = {v: f"trail7_{v}" for v in venues}
    rate_cols = {v: f"rate_{v}" for v in venues}

    cur_venue = mode if mode in venues else None
    nav = C0
    n_switches = 0
    total_switch_cost = 0.0
    rows = []

    for i in range(len(px)):
        date = px["date"].iloc[i]
        S = px["S"].iloc[i]
        F = px["F"].iloc[i]
        if i == 0:
            dS = dF = 0.0
        else:
            dS = S - px["S"].iloc[i - 1]
            dF = F - px["F"].iloc[i - 1]
        basis_pnl = q * (dS - dF)

        day_key = date.floor("D")
        funding_pnl = 0.0
        switch_cost = 0.0

        if day_key in fmap.index:
            day_rows = fmap.loc[[day_key]] if isinstance(fmap.loc[day_key], pd.DataFrame) else fmap.loc[day_key:day_key]
            for _, r in day_rows.iterrows():
                if mode == "rotation":
                    trail_vals = {v: r[trail_cols[v]] for v in venues}
                    if all(pd.notna(x) for x in trail_vals.values()):
                        best_venue = max(trail_vals, key=trail_vals.get)
                        if cur_venue is None:
                            cur_venue = best_venue
                        elif best_venue != cur_venue:
                            diff = trail_vals[best_venue] - trail_vals[cur_venue]
                            if diff > (HYSTERESIS_BP_PER_8H / 10000.0):
                                old_bp = venue_leg_cost_bp(cur_venue, "perp")
                                new_bp = venue_leg_cost_bp(best_venue, "perp")
                                cost = N * (old_bp + new_bp) / 10000.0
                                switch_cost += cost
                                n_switches += 1
                                cur_venue = best_venue
                    if cur_venue is None:
                        continue  # not enough trailing history yet, no funding accrued
                use_venue = cur_venue if mode == "rotation" else mode
                rate = r[rate_cols[use_venue]]
                if pd.notna(rate):
                    funding_pnl += q * F * rate

        nav = nav + basis_pnl + funding_pnl - switch_cost
        total_switch_cost += switch_cost
        rows.append(dict(date=date, nav=nav, basis_pnl=basis_pnl, funding_pnl=funding_pnl,
                          switch_cost=switch_cost, venue=cur_venue))

    out = pd.DataFrame(rows)
    out["ret"] = out["nav"].diff().fillna(0.0) / C0
    return dict(out=out, n_switches=n_switches, total_switch_cost=total_switch_cost, N=N)


def run_part2(conn):
    log("=== PART 2: 3-venue funding rotation ===")
    summary_rows = []
    yearly_rows = {}
    verify_rows = []

    for sym_key, cfg in PART2_PAIRS.items():
        results, merged, intervals_native = run_part2_symbol(conn, sym_key, cfg)

        for mode, res in results.items():
            out = res["out"]
            m = compute_metrics_generic(out)
            m.update(symbol=sym_key, mode=mode, n_switches=res["n_switches"],
                      total_switch_cost=res["total_switch_cost"],
                      switch_cost_bp_of_C0=res["total_switch_cost"] / C0 * 10000.0)
            summary_rows.append(m)
            csv_path = os.path.join(OUT_DIR, f"part2_{sym_key}_{mode}_nav.csv")
            out.to_csv(csv_path, index=False)
            if mode == "rotation":
                yearly_rows[sym_key] = yearly_breakdown_generic(out)

        rot_final_nav = results["rotation"]["out"]["nav"].iloc[-1]
        single_final = {v: results[v]["out"]["nav"].iloc[-1] for v in cfg["venues"]}
        best_single = max(single_final, key=single_final.get)
        beats_all = all(rot_final_nav >= single_final[v] - 1e-6 for v in cfg["venues"])
        verify_rows.append(dict(symbol=sym_key, rotation_final_nav=rot_final_nav,
                                 best_single_venue=best_single,
                                 best_single_final_nav=single_final[best_single],
                                 rotation_ge_all_singles=beats_all,
                                 **{f"final_nav_{v}": single_final[v] for v in cfg["venues"]}))

        merged_csv = os.path.join(OUT_DIR, f"part2_{sym_key}_funding_grid.csv")
        merged.to_csv(merged_csv, index=False)
        log(f"  {sym_key}: rotation_final_nav={rot_final_nav:.2f} best_single={best_single}"
            f"({single_final[best_single]:.2f}) rotation>=all_singles={beats_all}")

    summary_df = pd.DataFrame(summary_rows)
    summary_csv = os.path.join(OUT_DIR, "part2_summary_metrics.csv")
    summary_df.to_csv(summary_csv, index=False)

    verify_df = pd.DataFrame(verify_rows)
    verify_csv = os.path.join(OUT_DIR, "part2_rotation_vs_single_verification.csv")
    verify_df.to_csv(verify_csv, index=False)

    print("\n================= PART 2: RESULTS MATRIX =================")
    print(summary_df.to_string(index=False))
    print("\n================= PART 2: ROTATION vs SINGLE-VENUE VERIFICATION =================")
    print(verify_df.to_string(index=False))
    print("\n================= PART 2: YEARLY BREAKDOWN (rotation) =================")
    for sym_key, rows in yearly_rows.items():
        print(f"\n-- {sym_key} --")
        print(pd.DataFrame(rows).to_string(index=False))

    return dict(summary_df=summary_df, verify_df=verify_df)


def compute_metrics_generic(out, C0=C0):
    ret = out["ret"].values[1:]
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
    dd = (nav - running_max) / C0
    max_dd = dd.min()
    return dict(ann_return=ann_return, ann_vol=ann_vol, sharpe=sharpe, max_dd=max_dd, n_days=n_days)


def yearly_breakdown_generic(out):
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
# Part 3: HYPE cross-venue funding spread
# ---------------------------------------------------------------------------
def run_part3(conn):
    log("=== PART 3: HYPE cross-venue funding spread ===")

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM funding WHERE venue='bybit' AND symbol='HYPEUSDT'")
    n_bybit_hype = cur.fetchone()[0]
    log(f"  bybit HYPEUSDT funding rows in DB: {n_bybit_hype}")
    if n_bybit_hype == 0:
        print("WARNING: bybit HYPEUSDT funding still 0 rows after fetch+load step; "
              "Part 3 cannot proceed with spread computation.")
        return dict(skipped=True, reason="no_bybit_hype_funding_data")

    ts_start = C_HYPE_PERIOD_START
    ts_end = 1786406340000

    hl = load_funding(conn, "hyperliquid", "HYPE", ts_start, ts_end)
    by = load_funding(conn, "bybit", "HYPEUSDT", ts_start, ts_end)
    log(f"  HL HYPE funding: {len(hl)} rows, interval_hours={sorted(hl['interval_hours'].unique().tolist())}")
    log(f"  Bybit HYPEUSDT funding: {len(by)} rows, interval_hours={sorted(by['interval_hours'].unique().tolist())}")

    EIGHT_H_MS = 8 * 3600 * 1000
    hl2 = hl.copy()
    hl2["bucket"] = (hl2["ts"] // EIGHT_H_MS) * EIGHT_H_MS
    hl_8h = hl2.groupby("bucket").agg(rate_hl=("rate", "sum")).reset_index()

    by2 = by.copy()
    by2["bucket"] = (by2["ts"] // EIGHT_H_MS) * EIGHT_H_MS
    by_8h = by2.groupby("bucket").agg(rate_bybit=("rate", "sum")).reset_index()

    spread = pd.merge(hl_8h, by_8h, on="bucket", how="inner")
    spread["date"] = pd.to_datetime(spread["bucket"], unit="ms")
    spread["spread"] = spread["rate_hl"] - spread["rate_bybit"]  # HL short - Bybit short equivalent

    spread_csv = os.path.join(OUT_DIR, "part3_hype_funding_spread.csv")
    spread.to_csv(spread_csv, index=False)

    stats = dict(
        n_buckets=len(spread),
        mean=spread["spread"].mean(),
        p25=spread["spread"].quantile(0.25),
        p50=spread["spread"].quantile(0.50),
        p75=spread["spread"].quantile(0.75),
        p90=spread["spread"].quantile(0.90),
        sign_agreement_rate=(np.sign(spread["rate_hl"]) == np.sign(spread["rate_bybit"])).mean(),
    )
    # annualize mean 8h spread: 3 events/day * 365 days
    ann_spread = stats["mean"] * 3 * 365
    stats["annualized_mean_spread_pct"] = ann_spread * 100.0

    print("\n================= PART 3: HYPE FUNDING SPREAD STATS =================")
    for kk, vv in stats.items():
        print(f"  {kk}: {vv}")

    decision_threshold = 0.05  # 5% annualized, per task's explicitly-authorized decision rule
    run_backtest = ann_spread >= decision_threshold
    print(f"\nDecision rule: annualized mean spread ({ann_spread*100:.3f}%) "
          f"{'>=' if run_backtest else '<'} {decision_threshold*100:.1f}% threshold "
          f"=> {'RUN' if run_backtest else 'SKIP'} perp-perp DN backtest")

    result = dict(spread_stats=stats, ann_spread=ann_spread, ran_backtest=run_backtest)

    if not run_backtest:
        print("Spread too small; perp-perp DN backtest SKIPPED per decision rule.")
        return result

    # --- perp-perp DN backtest ---
    # Short the higher-funding venue's perp, long the lower-funding venue's
    # perp, 0.33N margin each leg, C = 0.66N notional gross <= 2x (well within
    # the 3-4x gross cap noted in spec). Rebalanced monthly (same cadence as
    # Part1), using bybit perp HYPEUSDT price for both legs' price marks
    # (HL doesn't have a local price series loaded; bybit perp price used as
    # a shared reference so basis nets to ~0 between the two legs' price
    # exposure, isolating the funding-spread capture).
    price = load_daily_prices(conn, "bybit", "perp", "HYPEUSDT", ts_start, ts_end)
    price = price.rename(columns={"price": "P"})[["date", "P"]]
    spread["day_date"] = spread["date"].dt.floor("D")

    k_pp = 0.66
    C0_pp = C0
    N = k_pp * C0_pp  # gross notional per leg target (both legs same |N|, net delta ~0)

    dates = price["date"].tolist()
    P = price["P"].values
    n = len(dates)

    nav = C0_pp
    q = 0.0  # units per leg (long low-funding venue, short high-funding venue)
    short_venue = None  # 'hl' or 'bybit'
    n_rebalances = 0
    total_cost = 0.0
    rows = []
    prev_date = dates[0] if n else None

    smap = spread.set_index("day_date")

    for i in range(n):
        date = dates[i]
        p = P[i]
        dP = 0.0 if i == 0 else (p - P[i - 1])
        # both legs same |q|, opposite price sign, but we mark both against
        # the same shared price P -> basis pnl nets to 0 by construction
        basis_pnl = 0.0

        funding_pnl = 0.0
        day_key = date.floor("D")
        if day_key in smap.index and q > 0 and short_venue is not None:
            day_rows = smap.loc[[day_key]] if isinstance(smap.loc[day_key], pd.DataFrame) else smap.loc[day_key:day_key]
            for _, r in day_rows.iterrows():
                if short_venue == "hl":
                    # short HL (receive HL rate if positive), long bybit (pay bybit rate if positive)
                    funding_pnl += q * p * (r["rate_hl"] - r["rate_bybit"])
                else:
                    funding_pnl += q * p * (r["rate_bybit"] - r["rate_hl"])

        nav_t = nav + basis_pnl + funding_pnl

        month_flag = (i == 0) or is_month_start(prev_date, date)
        rcost = 0.0
        if month_flag:
            # determine which venue currently has higher trailing info: use
            # the bucket at/just before this day if available, else same-day
            avg_row = smap.loc[day_key] if day_key in smap.index else None
            if avg_row is not None:
                if isinstance(avg_row, pd.DataFrame):
                    avg_row = avg_row.iloc[-1]
                new_short_venue = "hl" if avg_row["rate_hl"] >= avg_row["rate_bybit"] else "bybit"
            else:
                new_short_venue = short_venue if short_venue else "hl"
            N_target = k_pp * nav_t
            q_new = N_target / p if p > 0 else 0.0
            # cost: close old (if any) + open new, both legs, using PART1's flat 17bp simplification
            # extended to 2 legs (perp+perp instead of spot+perp) -> reuse same flat bp rate per leg
            dN = abs(q_new * p - q * p) if short_venue == new_short_venue else (q_new * p + q * p)
            rcost = dN * (PART1_REBALANCE_COST_BP / 10000.0)
            nav_t -= rcost
            total_cost += rcost
            n_rebalances += 1
            q = q_new
            short_venue = new_short_venue

        nav = nav_t
        rows.append(dict(date=date, nav=nav, basis_pnl=basis_pnl, funding_pnl=funding_pnl,
                          cost=rcost, q=q, short_venue=short_venue))
        prev_date = date

    pp_out = pd.DataFrame(rows)
    pp_out["ret"] = pp_out["nav"].diff().fillna(0.0) / C0_pp
    pp_csv = os.path.join(OUT_DIR, "part3_hype_perpperp_dn_nav.csv")
    pp_out.to_csv(pp_csv, index=False)

    pp_metrics = compute_metrics_generic(pp_out)
    pp_metrics.update(n_rebalances=n_rebalances, total_cost_bp_of_C0=total_cost / C0_pp * 10000.0)

    print("\n================= PART 3: PERP-PERP DN BACKTEST METRICS =================")
    for kk, vv in pp_metrics.items():
        print(f"  {kk}: {vv}")

    # capital efficiency comparison vs Part1 C_HYPE k=0.66 (return per unit C0, both use C0)
    part1_chype_csv = os.path.join(OUT_DIR, "C_HYPE_k0.66_nav.csv")
    if os.path.exists(part1_chype_csv):
        p1 = pd.read_csv(part1_chype_csv)
        p1_ret = compute_metrics_generic(p1)
        print("\n-- Capital efficiency comparison (return per unit C0) --")
        print(f"  Part1 C_HYPE k=0.66: ann_return={p1_ret.get('ann_return', float('nan')):.4f}")
        print(f"  Part3 perp-perp DN:  ann_return={pp_metrics.get('ann_return', float('nan')):.4f}")
        result["capital_efficiency_comparison"] = dict(
            part1_c_hype_ann_return=p1_ret.get("ann_return"),
            part3_perpperp_ann_return=pp_metrics.get("ann_return"),
        )

    result["perpperp_metrics"] = pp_metrics
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    conn = sqlite3.connect(DB_URI, uri=True)
    try:
        part1 = run_part1(conn)
        part2 = run_part2(conn)
        part3 = run_part3(conn)
    finally:
        conn.close()

    print(f"\nAll outputs written to {OUT_DIR}")


if __name__ == "__main__":
    main()
