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
    results/liq_reversion/events_<variant>.csv   (per-event detail, ~324 files;
        detect_ts is the close-based decision timestamp, execution uses bar opens)
    results/liq_reversion/summary.csv            (one row per variant, all 324)
    results/liq_reversion/summary.json           (same, JSON)
    results/liq_reversion/top5_btc_long_ret-5_oi-2.log  (lookahead + eyeball check)

DB is read-only. No write queries are issued anywhere in this script.
"""
import argparse
import hashlib
import itertools
import json
import math
import os
import sqlite3
import sys
import time as _time
from datetime import datetime, timezone
from pathlib import Path
import unicodedata

import numpy as np
import pandas as pd

DB_PATH = "/mnt/e/Datas/market/market.db"
OUT_DIR = "/home/o9oem/workspace/crypto/analytics/results/liq_reversion"
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "results/prospective_validation/registry.json"
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


def _canonical_registry_value(value):
    """Match the registry's canonical JSON domain without importing its evaluator."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, float):
        return None if not math.isfinite(value) else value.hex()
    if isinstance(value, dict):
        return {str(_canonical_registry_value(k)): _canonical_registry_value(v)
                for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_canonical_registry_value(item) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    raise ValueError("registry contains an unsupported canonical JSON value")


def _canonical_registry_bytes(registry, *, include_integrity=False):
    payload = dict(registry)
    if not include_integrity:
        payload.pop("integrity", None)
    return (json.dumps(_canonical_registry_value(payload), ensure_ascii=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _registry_timestamp_ms(value, name):
    if not isinstance(value, str):
        raise ValueError(f"registry {name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"registry {name} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"registry {name} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def enforce_prospective_seal(analysis_end_ts, *, registry_path=REGISTRY_PATH, now=None):
    """Refuse prospective source refreshes until the sealed follow-up completes.

    A present registry is verified before deciding whether the requested range is
    historical.  Consequently a malformed/tampered registry fails closed rather
    than silently allowing outputs to be rewritten.
    """
    registry_path = Path(registry_path)
    sidecar_path = registry_path.with_name(registry_path.name + ".sha256")
    if not registry_path.is_file() or not sidecar_path.is_file():
        raise RuntimeError("sealed prospective registry or its SHA-256 sidecar is missing")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        if registry_path.read_bytes() != _canonical_registry_bytes(registry, include_integrity=True):
            raise ValueError("registry is not canonical JSON")
        expected = registry.get("integrity", {}).get("registry_sha256")
        actual = hashlib.sha256(_canonical_registry_bytes(registry)).hexdigest()
        sidecar = sidecar_path.read_text(encoding="ascii").strip()
        if not isinstance(expected, str) or expected != actual or sidecar != actual:
            raise ValueError("registry integrity hash mismatch")
        evaluation_start_ms = _registry_timestamp_ms(registry["evaluation_start"], "evaluation_start")
        followup_end_ms = _registry_timestamp_ms(registry["followup_end"], "followup_end")
    except (KeyError, OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed prospective registry is malformed or fails integrity verification") from exc
    if now is None:
        now_ms = int(_time.time() * 1000)
    elif isinstance(now, (int, float)):
        now_ms = int(now)
    elif isinstance(now, datetime):
        now_ms = int(now.astimezone(timezone.utc).timestamp() * 1000) if now.tzinfo else int(now.replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        raise TypeError("now must be epoch milliseconds or a datetime")
    if now_ms < followup_end_ms and int(analysis_end_ts) >= evaluation_start_ms:
        raise RuntimeError(
            "sealed prospective validation is active: source refresh at/after "
            "evaluation_start is prohibited until followup_end"
        )


def last_complete_minute_open(now_ms=None):
    """Open timestamp of the latest fully closed UTC minute."""
    current_ms = int(_time.time() * 1000) if now_ms is None else int(now_ms)
    return current_ms // 60_000 * 60_000 - 60_000


def resolve_latest_kline_end(db_path=DB_PATH, symbols=SYMBOLS, now_ms=None):
    """Return the last complete 1m bar shared by every required symbol.

    The query is deliberately read-only.  A shared end prevents a ``latest``
    run from silently giving one symbol a longer sample than another.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        q = """
            SELECT symbol, MAX(ts) AS max_ts
            FROM klines
            WHERE venue=? AND market=? AND symbol IN ({}) AND ts<=?
            GROUP BY symbol
        """.format(",".join("?" for _ in symbols))
        rows = con.execute(
            q, (VENUE, MARKET, *symbols, last_complete_minute_open(now_ms))
        ).fetchall()
    finally:
        con.close()
    maxima = {symbol: max_ts for symbol, max_ts in rows}
    missing = [symbol for symbol in symbols if maxima.get(symbol) is None]
    if missing:
        raise ValueError(f"cannot resolve latest end: no Binance perp klines for {', '.join(missing)}")
    return int(min(maxima.values()))


def parse_end_ts(value, db_path=DB_PATH, symbols=SYMBOLS):
    """Parse ``--end-ts`` as an integer millisecond timestamp or ``latest``."""
    if isinstance(value, (int, np.integer)):
        end_ts = int(value)
    elif isinstance(value, str) and value.strip().lower() == "latest":
        end_ts = resolve_latest_kline_end(db_path=db_path, symbols=symbols)
    else:
        try:
            end_ts = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("--end-ts must be an integer millisecond timestamp or 'latest'") from exc
    if end_ts <= TS_FULL_START:
        raise ValueError(f"--end-ts must be after TS_FULL_START ({TS_FULL_START})")
    return end_ts


def load_klines(symbol, end_ts=TS_FULL_END, db_path=DB_PATH):
    """Load 1m klines (open-time ts, open, close) in chronological order."""
    log(f"loading klines for {symbol} ...")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        q = """
            SELECT ts, open, close FROM klines
            WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
            ORDER BY ts
        """
        df = pd.read_sql_query(q, con, params=(VENUE, MARKET, symbol, TS_FULL_START, end_ts))
    finally:
        con.close()
    if df.empty:
        raise ValueError(f"no klines for {symbol} in {TS_FULL_START}..{end_ts}")
    df["ts"] = df["ts"].astype(np.int64)
    df["open"] = df["open"].astype(np.float64)
    df["close"] = df["close"].astype(np.float64)
    log(f"  {symbol}: {len(df)} klines loaded, ts range {df.ts.min()}..{df.ts.max()}")
    return df


def oi_end_ts_for_run(end_ts):
    """Preserve the historical default while allowing prospective extension."""
    if end_ts <= TS_FULL_END:
        return min(end_ts, TS_OI_END)
    return end_ts


def load_oi(symbol, end_ts=TS_FULL_END, db_path=DB_PATH):
    """Load 5m OI (ts, open_interest) for symbol."""
    log(f"loading oi_metrics for {symbol} ...")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        q = """
            SELECT ts, open_interest FROM oi_metrics
            WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?
            ORDER BY ts
        """
        oi_end_ts = oi_end_ts_for_run(end_ts)
        df = pd.read_sql_query(q, con, params=(VENUE, symbol, TS_OI_START, oi_end_ts))
    finally:
        con.close()
    if df.empty:
        log(f"  {symbol}: 0 oi rows loaded")
        return pd.DataFrame({"ts": pd.Series(dtype="int64"), "open_interest": pd.Series(dtype="float64")})
    df["ts"] = df["ts"].astype(np.int64)
    df["open_interest"] = df["open_interest"].astype(np.float64)
    log(f"  {symbol}: {len(df)} oi rows loaded, ts range {df.ts.min()}..{df.ts.max()}")
    return df


def frame_coverage(df, interval_ms):
    """Return JSON-safe min/max/count metadata for a timestamped DataFrame."""
    if df.empty:
        return {"min_ts": None, "max_ts": None, "count": 0,
                "coverage_end_exclusive_ts": None, "unexpected_gap_count": 0,
                "tail_contiguous_start_ts": None}
    timestamps = np.sort(df["ts"].astype(np.int64).unique())
    max_ts = int(timestamps[-1])
    gaps = np.flatnonzero(np.diff(timestamps) > int(interval_ms))
    tail_start = int(timestamps[gaps[-1] + 1]) if len(gaps) else int(timestamps[0])
    return {
        "min_ts": int(timestamps[0]),
        "max_ts": max_ts,
        "count": int(len(df)),
        "coverage_end_exclusive_ts": max_ts + interval_ms,
        "unexpected_gap_count": int(len(gaps)),
        "tail_contiguous_start_ts": tail_start,
    }


def build_run_manifest(requested_end, analysis_end_ts, kline_frames, oi_frames,
                       summary_count, event_file_count, event_file_sha256=None,
                       script_path=__file__):
    """Build a JSON-safe manifest describing the actual data used by this run."""
    kline_coverage = {symbol: frame_coverage(frame, 60 * 1000) for symbol, frame in kline_frames.items()}
    if not kline_coverage or any(meta["count"] == 0 for meta in kline_coverage.values()):
        raise ValueError("cannot build manifest with empty kline input")
    common_max = min(meta["max_ts"] for meta in kline_coverage.values())
    with open(script_path, "rb") as handle:
        script_sha256 = hashlib.sha256(handle.read()).hexdigest()
    return {
        "schema_version": 1,
        "script": os.path.basename(script_path),
        "statistical_warning": (
            "Raw t-statistics, null percentiles, and exploratory flags are non-confirmatory; "
            "strategy_robustness.py global-BY output is authoritative for inference."
        ),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_end": requested_end,
        "analysis_end_ts": int(analysis_end_ts),
        "coverage_end_exclusive_ts": int(common_max + 60 * 1000),
        "kline_coverage": kline_coverage,
        "oi_coverage": {symbol: frame_coverage(frame, 5 * 60 * 1000) for symbol, frame in oi_frames.items()},
        "event_schema": ["detect_ts", "entry_ts", "exit_ts", "entry_px", "exit_px", "net_ret_bp"],
        "execution_rule": (
            "Signals are observed at the detection bar close (open_time + 60000 ms); "
            "entry and exit use the first available 1m bar open at or after their targets."
        ),
        "output_counts": {"summary_rows": int(summary_count), "event_csv_files": int(event_file_count)},
        "event_file_sha256": dict(sorted((event_file_sha256 or {}).items())),
        "script_sha256": script_sha256,
    }


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


def entry_exit_prices(ts, open_px, event_idx, entry_delay_min, exit_hold_h):
    """
    The kline timestamp is its open time, so an event observed at a bar's close
    becomes actionable at ts[event_idx] + 60 seconds.  For each event, enter at
    the OPEN of the first bar at or after that decision time plus entry delay;
    exit at the OPEN of the first bar at or after the actual entry time plus the
    holding period. Forward as-of lookups preserve this behavior across gaps.

    Returns decision_ts, entry_ts, entry_px, exit_ts, exit_px, valid_mask.
    """
    n = len(ts)
    decision_ts = ts[event_idx] + 60 * 1000

    entry_target_ts = decision_ts + entry_delay_min * 60 * 1000
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
    entry_px[valid] = open_px[ep]
    exit_ts[valid] = ts[xp]
    exit_px[valid] = open_px[xp]

    return decision_ts, entry_ts, entry_px, exit_ts, exit_px, valid


def net_return_bp(entry_px, exit_px, direction):
    """
    direction 'long' => buy at entry, sell at exit.
    direction 'short' => sell at entry, buy at exit.
    Cost: 7bp charged at entry AND 7bp at exit (2 x 7bp total, not a single round-trip 7bp).
    """
    if direction == "long":
        gross = exit_px / entry_px - 1.0
    elif direction == "short":
        # Linear USDT-margined perp PnL is measured against entry notional.
        # The reciprocal return (entry / exit - 1) would instead overstate
        # profitable shorts and understate losing shorts.
        gross = 1.0 - exit_px / entry_px
    else:
        raise ValueError("direction must be 'long' or 'short'")
    gross_bp = gross * 10000.0
    net_bp = gross_bp - 2 * ONE_WAY_COST_BP
    return net_bp


def bootstrap_null(ts, open_px, n_events, entry_delay_min, exit_hold_h, direction,
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
        # Map random timestamps to the prior bar. Its close is the synthetic
        # detection observation, so execution remains unavailable until its
        # decision timestamp one minute later.
        idx = np.searchsorted(ts, rand_ts, side="right") - 1
        idx = np.clip(idx, 0, n - 1)
        _, entry_ts, entry_px, exit_ts, exit_px, valid = entry_exit_prices(
            ts, open_px, idx, entry_delay_min, exit_hold_h
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end-ts",
        default=str(TS_FULL_END),
        help="inclusive kline open timestamp in milliseconds, or 'latest' (default: existing fixed end)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    t0 = _time.time()

    args = parse_args(argv)
    requested_end_ts = parse_end_ts(args.end_ts)
    requested_end = args.end_ts.strip().lower() if isinstance(args.end_ts, str) and args.end_ts.strip().lower() == "latest" else requested_end_ts
    enforce_prospective_seal(requested_end_ts)
    os.makedirs(OUT_DIR, exist_ok=True)

    log("=== liq_reversion event study starting ===")
    log(f"requested end: {requested_end}; initial end timestamp: {requested_end_ts}")

    kline_frames = {}
    for sym in SYMBOLS:
        kline_frames[sym] = load_klines(sym, requested_end_ts)

    # A requested endpoint must be a complete bar shared by BTC and ETH.  Do
    # not silently shorten an explicit prospective evaluation: its manifest
    # would otherwise look fresher than the requested evaluation boundary.
    analysis_end_ts = min(int(frame["ts"].max()) for frame in kline_frames.values())
    if analysis_end_ts <= TS_FULL_START:
        raise ValueError("shared kline coverage does not extend beyond analysis start")
    if analysis_end_ts != requested_end_ts:
        raise ValueError(
            f"requested end {requested_end_ts} is not a complete shared BTC/ETH kline bar "
            f"(shared loaded max is {analysis_end_ts})"
        )

    data = {}
    oi_frames = {}
    for sym in SYMBOLS:
        kl = kline_frames[sym]
        ts = kl["ts"].to_numpy()
        open_px = kl["open"].to_numpy()
        close = kl["close"].to_numpy()

        # gap check (informational only)
        diffs = np.diff(ts)
        gap_frac = float((diffs != 60000).sum()) / len(diffs) if len(diffs) else 0.0
        log(f"  {sym}: non-60000ms gaps = {gap_frac*100:.3f}% of bars (informational)")

        ret60 = compute_60min_return(close)

        oi = load_oi(sym, analysis_end_ts)
        oi_frames[sym] = oi
        oi_ts = oi["ts"].to_numpy()
        oi_val = oi["open_interest"].to_numpy()
        oi_now, oi_chg60 = compute_oi_change_aligned_to_1m(oi_ts, oi_val, ts)

        data[sym] = dict(ts=ts, open_px=open_px, close=close, ret60=ret60, oi_chg60=oi_chg60)
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
        ts, open_px, close, ret60, oi_chg60 = d["ts"], d["open_px"], d["close"], d["ret60"], d["oi_chg60"]

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
                    sample_end = analysis_end_ts

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
                                    null_percentile=None, edge_vs_null_bp=None, exploratory_uncorrected_flag=False,
                                    inference_scope="exploratory_uncorrected",
                                    year_breakdown=json.dumps({}),
                                )
                                summary_rows.append(row)
                                # still write an empty csv for consistency
                                pd.DataFrame(columns=["detect_ts", "entry_ts", "exit_ts", "entry_px", "exit_px", "net_ret_bp"]).to_csv(
                                    os.path.join(OUT_DIR, f"events_{variant}.csv"), index=False
                                )
                                n_files_written += 1
                                continue

                            detect_ts_v, entry_ts_v, entry_px_v, exit_ts_v, exit_px_v, valid = entry_exit_prices(
                                ts, open_px, event_idx, entry_delay, exit_hold
                            )

                            # Execution must happen no earlier than the close-based decision,
                            # and all execution prices are the chosen bars' OPEN prices.
                            if valid.sum() > 0:
                                ok = np.all(entry_ts_v[valid] >= detect_ts_v[valid])
                                if not ok:
                                    lookahead_failures.append(variant)
                                assert ok, f"LOOKAHEAD BIAS: entry_ts before decision_ts for {variant}"
                                assert np.all(exit_ts_v[valid] >= entry_ts_v[valid] + exit_hold * 3600 * 1000), \
                                    f"LOOKAHEAD BIAS: exit_ts before hold completion for {variant}"

                            dts = detect_ts_v[valid]
                            ept = entry_px_v[valid]
                            xpt = exit_px_v[valid]
                            net_bp = net_return_bp(ept, xpt, direction)

                            # write per-event csv
                            out_df = pd.DataFrame({
                                "detect_ts": dts,
                                "entry_ts": entry_ts_v[valid],
                                "exit_ts": exit_ts_v[valid],
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
                                    null_percentile=None, edge_vs_null_bp=None, exploratory_uncorrected_flag=False,
                                    inference_scope="exploratory_uncorrected",
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
                                ts, open_px, n_ev, entry_delay, exit_hold, direction,
                                sample_start, sample_end, rng_master,
                            )
                            null_mean = float(np.nanmean(null_dist))
                            pctile = percentile_of_score(null_dist, mean_bp)
                            edge = mean_bp - null_mean
                            exploratory_uncorrected_flag = (pctile >= 95.0) or (pctile <= 5.0)

                            yb = year_breakdown(dts, net_bp)

                            row = dict(
                                variant=variant, symbol=sym, direction=direction,
                                ret_threshold=ret_thresh, oi_threshold=oi_thresh,
                                entry_delay_min=entry_delay, exit_hold_h=exit_hold,
                                n_events=n_ev, mean_net_bp=mean_bp, median_net_bp=median_bp,
                                win_rate=win_rate, std_bp=std_bp, t_stat=t_stat,
                                null_percentile=pctile, edge_vs_null_bp=edge,
                                exploratory_uncorrected_flag=bool(exploratory_uncorrected_flag),
                                inference_scope="exploratory_uncorrected",
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
        log("LOOKAHEAD CHECK: PASS - entry_ts is at/after close-based decision_ts and "
            "exit_ts is at/after the actual-entry hold completion, for all variants with events.")

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
        top5_decision_ts = top5_ts + 60 * 1000
        top5_ret = ret60[top5_idx]

        check_lines = []
        for i, (idx, t, decision_t, r) in enumerate(zip(top5_idx, top5_ts, top5_decision_ts, top5_ret)):
            dt_str = datetime.fromtimestamp(decision_t / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            # price window: 6h before to 24h after, sampled hourly
            window_before_ts = t - 6 * 3600 * 1000
            window_after_ts = t + 24 * 3600 * 1000
            pos_before = np.searchsorted(ts, window_before_ts, side="left")
            pos_at = idx
            pos_after = min(np.searchsorted(ts, window_after_ts, side="left"), len(ts) - 1)
            px_before = close[pos_before]
            px_at = close[pos_at]
            px_after = close[pos_after]
            line = (f"#{i+1} decision={dt_str} | 60m_ret={r*100:.2f}% | "
                    f"px(-6h)={px_before:.1f} px(detect)={px_at:.1f} px(+24h)={px_after:.1f} | "
                    f"chg_-6h_to_detect={((px_at/px_before-1)*100):.2f}% "
                    f"chg_detect_to_+24h={((px_after/px_at-1)*100):.2f}%")
            check_lines.append(line)
            log(line)

        with open(os.path.join(OUT_DIR, "top5_btc_long_ret-5_oi-2.log"), "w") as f:
            f.write("Top-5 largest-drawdown BTC events, ret60<=-5% AND oi_chg60<=-2%\n")
            f.write("decision timestamps are each detection bar's close (open_time + 1 minute).\n")
            f.write("Eyeball check against known real events (2024-08-05 carry-trade unwind/global\n")
            f.write("equity selloff, 2025 crash events, etc.) -- approximate timing match only,\n")
            f.write("no strict verification performed.\n\n")
            f.write("\n".join(check_lines) + "\n")

    event_names = sorted(
        name for name in os.listdir(OUT_DIR)
        if name.startswith("events_") and name.endswith(".csv")
    )
    event_hashes = {}
    for name in event_names:
        with open(os.path.join(OUT_DIR, name), "rb") as handle:
            event_hashes[name] = hashlib.sha256(handle.read()).hexdigest()
    if len(event_names) != n_files_written:
        raise ValueError(
            f"event output inventory mismatch: wrote {n_files_written}, found {len(event_names)}"
        )

    manifest = build_run_manifest(
        requested_end=requested_end,
        analysis_end_ts=analysis_end_ts,
        kline_frames=kline_frames,
        oi_frames=oi_frames,
        summary_count=len(summary_df),
        event_file_count=n_files_written,
        event_file_sha256=event_hashes,
    )
    manifest_path = os.path.join(OUT_DIR, "run_manifest.json")
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    log(f"run manifest written: {manifest_path}")

    total_elapsed = _time.time() - t0
    log(f"=== DONE in {total_elapsed:.1f}s ===")
    log(f"summary rows: {len(summary_df)} (expect 324)")
    log(f"csv files written: {n_files_written} (expect 324)")


if __name__ == "__main__":
    main()
