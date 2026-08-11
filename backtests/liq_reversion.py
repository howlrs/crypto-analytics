#!/usr/bin/env python3
"""
Event study: "mean reversion after large liquidation cascades" strategy validation.

Hypothesis: forced liquidations (leveraged longs being sold, or shorts being
squeezed) push price away from fair value on a 1h-1d scale, and price
subsequently mean-reverts.

This script is purely a VALIDATION analysis. Negative / non-significant
results are expected outcomes and must be reported as-is (no cherry-picking).

Run with no arguments:
    python3 liq_reversion.py

Outputs:
    results/liq_reversion/events_<variant>.csv   (per-event detail, ~324 files)
    results/liq_reversion/summary.csv            (one row per variant, all 324)
    results/liq_reversion/summary.json           (same, JSON)
    results/liq_reversion/top5_btc_long_ret-5_oi-2.log  (lookahead + eyeball check)

DB is read-only. No write queries are issued anywhere in this script.
"""
import itertools
import json
import os
import sqlite3
import sys
import time as _time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB_PATH = "/mnt/e/Datas/market/market.db"
OUT_DIR = "/home/o9oem/workspace/crypto/analytics/results/liq_reversion"
RNG_SEED = 20260811  # fixed seed for reproducibility of the null model

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
VENUE = "binance"
MARKET = "perp"

# Full period (klines): 2021-01-01 .. 2026-07-31
TS_FULL_START = 1609459200000
TS_FULL_END = 1785542340000
# OI data available only 2023-01-01 onward
TS_OI_START = 1672531200000
TS_OI_END = 1785542100000

RET_THRESHOLDS_LONG = [-0.02, -0.03, -0.05]   # price condition, long-side (crash) events
RET_THRESHOLDS_SHORT = [0.02, 0.03, 0.05]     # price condition, short-side (squeeze) events
OI_THRESHOLDS = [-0.01, -0.02, None]          # OI change over same 60min window; None = no OI condition
COOLDOWN_MS = 12 * 3600 * 1000                # 12h cooldown between events

ENTRY_DELAYS_MIN = [0, 30, 60]                # minutes after detection
EXIT_HOLDS_H = [4, 24, 72]                    # hours after entry

TAKER_FEE_BP = 5.0
SLIPPAGE_BP = 2.0
ONE_WAY_COST_BP = TAKER_FEE_BP + SLIPPAGE_BP  # 7bp, charged at entry AND exit (not round trip)

N_BOOTSTRAP = 200

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_klines(symbol):
    """Load 1m klines (ts, close) for symbol as a sorted numpy-backed DataFrame."""
    log(f"loading klines for {symbol} ...")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        q = """
            SELECT ts, close FROM klines
            WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
            ORDER BY ts
        """
        df = pd.read_sql_query(q, con, params=(VENUE, MARKET, symbol, TS_FULL_START, TS_FULL_END))
    finally:
        con.close()
    df["ts"] = df["ts"].astype(np.int64)
    df["close"] = df["close"].astype(np.float64)
    log(f"  {symbol}: {len(df)} klines loaded, ts range {df.ts.min()}..{df.ts.max()}")
    return df


def load_oi(symbol):
    """Load 5m OI (ts, open_interest) for symbol."""
    log(f"loading oi_metrics for {symbol} ...")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        q = """
            SELECT ts, open_interest FROM oi_metrics
            WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?
            ORDER BY ts
        """
        df = pd.read_sql_query(q, con, params=(VENUE, symbol, TS_OI_START, TS_OI_END))
    finally:
        con.close()
    df["ts"] = df["ts"].astype(np.int64)
    df["open_interest"] = df["open_interest"].astype(np.float64)
    log(f"  {symbol}: {len(df)} oi rows loaded, ts range {df.ts.min()}..{df.ts.max()}")
    return df


def compute_60min_return(close):
    """close: np.ndarray of 1m closes, contiguous 1-min bars assumed (validated by caller
    via ts diffs where gaps are tolerated -- return is still 'close[t]/close[t-60]-1', i.e.
    a 60-bar lookback, not a strict wall-clock 60 minutes, when bars are missing gaps are rare
    (<0.1%) in this dataset."""
    n = len(close)
    ret = np.full(n, np.nan)
    ret[60:] = close[60:] / close[:-60] - 1.0
    return ret


def compute_oi_change_aligned_to_1m(oi_ts, oi_val, kline_ts):
    """
    Map 5-min OI series onto the 1-min kline timeline via as-of (backward) fill,
    then compute the OI value 60 minutes ago vs now, aligned per kline bar.
    Returns oi_now_arr, oi_chg_60m_arr (both length == len(kline_ts), NaN outside OI coverage).
    """
    # as-of merge: for each kline ts, find latest oi ts <= kline ts
    oi_idx = np.searchsorted(oi_ts, kline_ts, side="right") - 1
    valid = oi_idx >= 0
    oi_now = np.full(len(kline_ts), np.nan)
    oi_now[valid] = oi_val[oi_idx[valid]]

    # oi 60 minutes ago: find as-of oi value at kline_ts - 60min
    ts_60_ago = kline_ts - 60 * 60 * 1000
    oi_idx_60 = np.searchsorted(oi_ts, ts_60_ago, side="right") - 1
    valid_60 = oi_idx_60 >= 0
    oi_60_ago = np.full(len(kline_ts), np.nan)
    oi_60_ago[valid_60] = oi_val[oi_idx_60[valid_60]]

    with np.errstate(divide="ignore", invalid="ignore"):
        oi_chg = oi_now / oi_60_ago - 1.0
    return oi_now, oi_chg


def detect_events(ts, close, ret60, oi_chg60, ret_thresh, oi_thresh, direction, oi_start_ts=None):
    """
    Vectorized-then-sequential event detection with 12h cooldown.
    direction: 'long' (ret60 <= ret_thresh) or 'short' (ret60 >= ret_thresh)
    oi_thresh: None (no OI condition) or a negative float; condition is oi_chg60 <= oi_thresh.
    If oi_thresh is not None, restrict candidate universe to ts >= oi_start_ts (OI data availability).
    Returns np.ndarray of detect_ts (int64), and their integer positions in ts (int array).
    """
    n = len(ts)
    if direction == "long":
        price_cond = ret60 <= ret_thresh
    else:
        price_cond = ret60 >= ret_thresh

    if oi_thresh is not None:
        oi_cond = oi_chg60 <= oi_thresh
        cond = price_cond & oi_cond & ~np.isnan(oi_chg60)
        if oi_start_ts is not None:
            cond = cond & (ts >= oi_start_ts)
    else:
        cond = price_cond

    cond = cond & ~np.isnan(ret60)
    cand_idx = np.flatnonzero(cond)
    if len(cand_idx) == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.int64)

    # sequential cooldown filter (candidates are already time-ordered since ts is sorted)
    selected_idx = []
    last_ts = -np.inf
    cand_ts = ts[cand_idx]
    for i, t in zip(cand_idx, cand_ts):
        if t - last_ts >= COOLDOWN_MS:
            selected_idx.append(i)
            last_ts = t
    selected_idx = np.array(selected_idx, dtype=np.int64)
    return ts[selected_idx], selected_idx


def entry_exit_prices(ts, close, event_idx, entry_delay_min, exit_hold_h):
    """
    For each event (by index into ts/close arrays), find the entry bar at
    entry_delay_min minutes after detection, and exit bar at exit_hold_h hours
    after the ENTRY bar. Uses as-of forward lookup (first bar with ts >= target).
    Returns arrays: entry_ts, entry_px, exit_ts, exit_px, valid_mask.
    Also returns detect_ts for lookahead assertion.
    """
    n = len(ts)
    detect_ts = ts[event_idx]

    entry_target_ts = detect_ts + entry_delay_min * 60 * 1000
    # searchsorted for first ts >= target
    entry_pos = np.searchsorted(ts, entry_target_ts, side="left")
    valid_entry = entry_pos < n

    exit_target_ts = np.where(valid_entry, ts[np.clip(entry_pos, 0, n - 1)] + exit_hold_h * 3600 * 1000, np.iinfo(np.int64).max)
    exit_pos = np.searchsorted(ts, exit_target_ts, side="left")
    valid_exit = exit_pos < n

    valid = valid_entry & valid_exit

    entry_ts = np.full(len(event_idx), -1, dtype=np.int64)
    entry_px = np.full(len(event_idx), np.nan)
    exit_ts = np.full(len(event_idx), -1, dtype=np.int64)
    exit_px = np.full(len(event_idx), np.nan)

    ep = entry_pos[valid]
    xp = exit_pos[valid]
    entry_ts[valid] = ts[ep]
    entry_px[valid] = close[ep]
    exit_ts[valid] = ts[xp]
    exit_px[valid] = close[xp]

    return detect_ts, entry_ts, entry_px, exit_ts, exit_px, valid


def net_return_bp(entry_px, exit_px, direction):
    """
    direction 'long' => buy at entry, sell at exit.
    direction 'short' => sell at entry, buy at exit.
    Cost: 7bp charged at entry AND 7bp at exit (2 x 7bp total, not a single round-trip 7bp).
    """
    if direction == "long":
        gross = exit_px / entry_px - 1.0
    else:
        gross = entry_px / exit_px - 1.0
    gross_bp = gross * 10000.0
    net_bp = gross_bp - 2 * ONE_WAY_COST_BP
    return net_bp


def bootstrap_null(ts, close, n_events, entry_delay_min, exit_hold_h, direction,
                    sample_start_ts, sample_end_ts, rng):
    """
    Draw N_BOOTSTRAP samples of n_events random 'detection' timestamps uniformly
    from [sample_start_ts, sample_end_ts], compute entry/exit net returns the same
    way as the real strategy, and return array of length N_BOOTSTRAP of mean net_bp.
    """
    n = len(ts)
    means = np.empty(N_BOOTSTRAP)
    for b in range(N_BOOTSTRAP):
        rand_ts = rng.integers(sample_start_ts, sample_end_ts, size=n_events, dtype=np.int64)
        rand_ts.sort()
        # map random ts to nearest existing bar index (as-of backward, i.e. nearest prior bar
        # acting as a synthetic "detection" bar close)
        idx = np.searchsorted(ts, rand_ts, side="right") - 1
        idx = np.clip(idx, 0, n - 1)
        _, entry_ts, entry_px, exit_ts, exit_px, valid = entry_exit_prices(
            ts, close, idx, entry_delay_min, exit_hold_h
        )
        if valid.sum() == 0:
            means[b] = np.nan
            continue
        net_bp = net_return_bp(entry_px[valid], exit_px[valid], direction)
        means[b] = net_bp.mean()
    return means


def percentile_of_score(dist, value):
    dist = dist[~np.isnan(dist)]
    if len(dist) == 0:
        return np.nan
    return float((dist < value).sum()) / len(dist) * 100.0


def year_breakdown(detect_ts, net_bp):
    years = pd.to_datetime(detect_ts, unit="ms", utc=True).year
    df = pd.DataFrame({"year": years, "net_bp": net_bp})
    out = {}
    for y in [2021, 2022, 2023, 2024, 2025, 2026]:
        sub = df[df.year == y]
        out[y] = {"n": int(len(sub)), "mean_bp": float(sub.net_bp.mean()) if len(sub) else None}
    return out


def main():
    t0 = _time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    log("=== liq_reversion event study starting ===")

    data = {}
    for sym in SYMBOLS:
        kl = load_klines(sym)
        ts = kl["ts"].to_numpy()
        close = kl["close"].to_numpy()

        # gap check (informational only)
        diffs = np.diff(ts)
        gap_frac = float((diffs != 60000).sum()) / len(diffs) if len(diffs) else 0.0
        log(f"  {sym}: non-60000ms gaps = {gap_frac*100:.3f}% of bars (informational)")

        ret60 = compute_60min_return(close)

        oi = load_oi(sym)
        oi_ts = oi["ts"].to_numpy()
        oi_val = oi["open_interest"].to_numpy()
        oi_now, oi_chg60 = compute_oi_change_aligned_to_1m(oi_ts, oi_val, ts)

        data[sym] = dict(ts=ts, close=close, ret60=ret60, oi_chg60=oi_chg60)
        log(f"  {sym}: ready (n={len(ts)})")

    log(f"data loaded in {_time.time()-t0:.1f}s")

    rng_master = np.random.default_rng(RNG_SEED)

    summary_rows = []
    n_variants_total = 0
    n_files_written = 0
    lookahead_failures = []

    oi_thresh_labels = {-0.01: "oi-1", -0.02: "oi-2", None: "oinone"}

    directions = [
        ("long", RET_THRESHOLDS_LONG),
        ("short", RET_THRESHOLDS_SHORT),
    ]

    detect_variant_count = 0
    for sym in SYMBOLS:
        d = data[sym]
        ts, close, ret60, oi_chg60 = d["ts"], d["close"], d["ret60"], d["oi_chg60"]

        for direction, ret_thresholds in directions:
            for ret_thresh in ret_thresholds:
                for oi_thresh in OI_THRESHOLDS:
                    detect_variant_count += 1
                    ret_label = f"ret{ret_thresh*100:+.0f}".replace("+", "")
                    oi_label = oi_thresh_labels[oi_thresh]

                    if oi_thresh is None:
                        oi_start_ts = None
                    else:
                        oi_start_ts = TS_OI_START

                    detect_ts_arr, event_idx = detect_events(
                        ts, close, ret60, oi_chg60, ret_thresh, oi_thresh, direction,
                        oi_start_ts=oi_start_ts,
                    )
                    n_events = len(detect_ts_arr)
                    log(f"[detect {detect_variant_count}/36] {sym} {direction} ret{ret_thresh} oi{oi_thresh}: {n_events} events")

                    sample_start = oi_start_ts if oi_thresh is not None else TS_FULL_START
                    sample_end = TS_FULL_END

                    for entry_delay in ENTRY_DELAYS_MIN:
                        for exit_hold in EXIT_HOLDS_H:
                            n_variants_total += 1
                            variant = f"{sym.replace('USDT','').lower()}_{direction}_{ret_label}_{oi_label}_entry{entry_delay}_exit{exit_hold}h"

                            if n_events == 0:
                                row = dict(
                                    variant=variant, symbol=sym, direction=direction,
                                    ret_threshold=ret_thresh, oi_threshold=oi_thresh,
                                    entry_delay_min=entry_delay, exit_hold_h=exit_hold,
                                    n_events=0, mean_net_bp=None, median_net_bp=None,
                                    win_rate=None, std_bp=None, t_stat=None,
                                    null_percentile=None, edge_vs_null_bp=None, significant=False,
                                    year_breakdown=json.dumps({}),
                                )
                                summary_rows.append(row)
                                # still write an empty csv for consistency
                                pd.DataFrame(columns=["detect_ts", "entry_px", "exit_px", "net_ret_bp"]).to_csv(
                                    os.path.join(OUT_DIR, f"events_{variant}.csv"), index=False
                                )
                                n_files_written += 1
                                continue

                            detect_ts_v, entry_ts_v, entry_px_v, exit_ts_v, exit_px_v, valid = entry_exit_prices(
                                ts, close, event_idx, entry_delay, exit_hold
                            )

                            # lookahead assertion: entry ts must be strictly after detect ts
                            # (guaranteed unless entry_delay==0, in which case entry bar could
                            # equal detect bar if it is itself >= detect_ts; searchsorted 'left'
                            # with target==detect_ts finds detect bar itself when entry_delay==0,
                            # so we require entry_ts >= detect_ts always, and > when entry_delay>0)
                            if valid.sum() > 0:
                                if entry_delay == 0:
                                    ok = np.all(entry_ts_v[valid] >= detect_ts_v[valid])
                                else:
                                    ok = np.all(entry_ts_v[valid] > detect_ts_v[valid])
                                if not ok:
                                    lookahead_failures.append(variant)
                                assert ok, f"LOOKAHEAD BIAS: entry_ts not >= detect_ts for {variant}"
                                assert np.all(exit_ts_v[valid] > entry_ts_v[valid]), \
                                    f"LOOKAHEAD BIAS: exit_ts not > entry_ts for {variant}"

                            dts = detect_ts_v[valid]
                            ept = entry_px_v[valid]
                            xpt = exit_px_v[valid]
                            net_bp = net_return_bp(ept, xpt, direction)

                            # write per-event csv
                            out_df = pd.DataFrame({
                                "detect_ts": dts,
                                "entry_px": ept,
                                "exit_px": xpt,
                                "net_ret_bp": net_bp,
                            })
                            out_df.to_csv(os.path.join(OUT_DIR, f"events_{variant}.csv"), index=False)
                            n_files_written += 1

                            n_ev = len(net_bp)
                            if n_ev == 0:
                                row = dict(
                                    variant=variant, symbol=sym, direction=direction,
                                    ret_threshold=ret_thresh, oi_threshold=oi_thresh,
                                    entry_delay_min=entry_delay, exit_hold_h=exit_hold,
                                    n_events=0, mean_net_bp=None, median_net_bp=None,
                                    win_rate=None, std_bp=None, t_stat=None,
                                    null_percentile=None, edge_vs_null_bp=None, significant=False,
                                    year_breakdown=json.dumps({}),
                                )
                                summary_rows.append(row)
                                continue

                            mean_bp = float(np.mean(net_bp))
                            median_bp = float(np.median(net_bp))
                            win_rate = float((net_bp > 0).sum()) / n_ev
                            std_bp = float(np.std(net_bp, ddof=1)) if n_ev > 1 else float("nan")
                            se = std_bp / np.sqrt(n_ev) if n_ev > 1 and std_bp > 0 else float("nan")
                            t_stat = mean_bp / se if se and not np.isnan(se) and se > 0 else float("nan")

                            null_dist = bootstrap_null(
                                ts, close, n_ev, entry_delay, exit_hold, direction,
                                sample_start, sample_end, rng_master,
                            )
                            null_mean = float(np.nanmean(null_dist))
                            pctile = percentile_of_score(null_dist, mean_bp)
                            edge = mean_bp - null_mean
                            significant = (pctile >= 95.0) or (pctile <= 5.0)

                            yb = year_breakdown(dts, net_bp)

                            row = dict(
                                variant=variant, symbol=sym, direction=direction,
                                ret_threshold=ret_thresh, oi_threshold=oi_thresh,
                                entry_delay_min=entry_delay, exit_hold_h=exit_hold,
                                n_events=n_ev, mean_net_bp=mean_bp, median_net_bp=median_bp,
                                win_rate=win_rate, std_bp=std_bp, t_stat=t_stat,
                                null_percentile=pctile, edge_vs_null_bp=edge, significant=bool(significant),
                                year_breakdown=json.dumps(yb),
                            )
                            summary_rows.append(row)

                    if detect_variant_count % 6 == 0:
                        elapsed = _time.time() - t0
                        log(f"progress: {detect_variant_count}/36 detect-variants, "
                            f"{n_variants_total}/324 entry/exit-variants done, elapsed={elapsed:.1f}s")

    log(f"all variants computed: {n_variants_total} rows, {n_files_written} csv files, elapsed={_time.time()-t0:.1f}s")

    if lookahead_failures:
        log(f"LOOKAHEAD BIAS DETECTED in variants: {lookahead_failures}")
    else:
        log("LOOKAHEAD CHECK: PASS - entry_ts strictly after detect_ts (or >= for entry_delay=0) "
            "and exit_ts strictly after entry_ts, for all variants with events. Verified via assert.")

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(os.path.join(OUT_DIR, "summary.csv"), index=False)
    summary_df.to_json(os.path.join(OUT_DIR, "summary.json"), orient="records", indent=2)
    log(f"summary written: {os.path.join(OUT_DIR, 'summary.csv')} ({len(summary_df)} rows)")

    # --- Required check #2: top 5 largest-drawdown BTC events, ret<=-5% & oi<=-2% variant ---
    log("=== top-5 eyeball check: BTC long, ret<=-5%, oi<=-2% ===")
    d = data["BTCUSDT"]
    ts, close, ret60, oi_chg60 = d["ts"], d["close"], d["ret60"], d["oi_chg60"]
    detect_ts5, event_idx5 = detect_events(ts, close, ret60, oi_chg60, -0.05, -0.02, "long", oi_start_ts=TS_OI_START)
    if len(detect_ts5) == 0:
        log("no events found for BTC ret<=-5% oi<=-2% variant -- cannot run top-5 check")
    else:
        ev_ret = ret60[event_idx5]
        order = np.argsort(ev_ret)  # most negative first
        top5_idx = event_idx5[order[:5]]
        top5_ts = ts[top5_idx]
        top5_ret = ret60[top5_idx]

        check_lines = []
        for i, (idx, t, r) in enumerate(zip(top5_idx, top5_ts, top5_ret)):
            dt_str = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            # price window: 6h before to 24h after, sampled hourly
            window_before_ts = t - 6 * 3600 * 1000
            window_after_ts = t + 24 * 3600 * 1000
            pos_before = np.searchsorted(ts, window_before_ts, side="left")
            pos_at = idx
            pos_after = min(np.searchsorted(ts, window_after_ts, side="left"), len(ts) - 1)
            px_before = close[pos_before]
            px_at = close[pos_at]
            px_after = close[pos_after]
            line = (f"#{i+1} {dt_str} | 60m_ret={r*100:.2f}% | "
                    f"px(-6h)={px_before:.1f} px(detect)={px_at:.1f} px(+24h)={px_after:.1f} | "
                    f"chg_-6h_to_detect={((px_at/px_before-1)*100):.2f}% "
                    f"chg_detect_to_+24h={((px_after/px_at-1)*100):.2f}%")
            check_lines.append(line)
            log(line)

        with open(os.path.join(OUT_DIR, "top5_btc_long_ret-5_oi-2.log"), "w") as f:
            f.write("Top-5 largest-drawdown BTC events, ret60<=-5% AND oi_chg60<=-2%\n")
            f.write("Eyeball check against known real events (2024-08-05 carry-trade unwind/global\n")
            f.write("equity selloff, 2025 crash events, etc.) -- approximate timing match only,\n")
            f.write("no strict verification performed.\n\n")
            f.write("\n".join(check_lines) + "\n")

    total_elapsed = _time.time() - t0
    log(f"=== DONE in {total_elapsed:.1f}s ===")
    log(f"summary rows: {len(summary_df)} (expect 324)")
    log(f"csv files written: {n_files_written} (expect 324)")


if __name__ == "__main__":
    main()
