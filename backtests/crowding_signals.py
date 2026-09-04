#!/usr/bin/env python3
"""
Crowding-signal validation: does funding/OI overheating (crowded leveraged-long
positioning) have predictive power as a contrarian (or momentum) signal?

This is a pure VALIDATION script. Negative / weak / statistically insignificant
results are expected possible outcomes and MUST be reported as-is -- no
cherry-picking of favorable variants.

Cost convention (matches backtests/liq_reversion.py exactly):
    TAKER_FEE_BP = 5.0
    SLIPPAGE_BP  = 2.0
    ONE_WAY_COST_BP = 7.0   -- charged once at entry AND once at exit
                               (i.e. round-trip cost = 2 * 7bp = 14bp total)

Lookahead-bias policy (critical, see inline comments at each signal):
    Every rolling/expanding statistic used as of decision-time t is computed
    using only observations with timestamp <= the *previous* settlement/bar,
    via an explicit `.shift(1)` (or equivalent: window strictly excludes the
    current row) before it is used to form deciles / thresholds / entries.
    Forward returns are computed strictly forward from the decision timestamp.

DB is read-only (file:...?mode=ro). No write queries are issued anywhere.

Run:
    python3 backtests/crowding_signals.py
"""
import argparse
import glob
import hashlib
import json
import math
import os
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path
import unicodedata

import numpy as np
import pandas as pd
from scipy import stats

DB_PATH = "/mnt/e/Datas/market/market.db"
DB_URI = f"file:{DB_PATH}?mode=ro"
OUT_DIR = "/home/o9oem/workspace/crypto/analytics/results/crowding"
REGISTRY_PATH = Path(__file__).resolve().parents[1] / "results/prospective_validation/registry.json"

# ---------------------------------------------------------------------------
# Cost convention -- IDENTICAL to backtests/liq_reversion.py
# ---------------------------------------------------------------------------
TAKER_FEE_BP = 5.0
SLIPPAGE_BP = 2.0
ONE_WAY_COST_BP = TAKER_FEE_BP + SLIPPAGE_BP  # 7bp, charged at entry AND exit
ROUND_TRIP_COST_BP = 2 * ONE_WAY_COST_BP      # 14bp total

VENUE = "binance"
MARKET = "perp"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]

# klines full coverage (per project convention, same as funding_capture.py / liq_reversion.py)
TS_FULL_START = 1609459200000
TS_FULL_END = 1785542340000
# OI data available only 2023-01-01 onward
TS_OI_START = 1672531200000
TS_OI_END = 1785542100000

N_BOOTSTRAP = 200
RNG_SEED = 20260811
INFERENCE_SCOPE = "exploratory_uncorrected"

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS


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
    """Refuse prospective source refreshes until the sealed follow-up completes."""
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


def mark_exploratory_inference(frame):
    """Label every persisted inferential table as exploratory and uncorrected."""
    frame = frame.copy()
    frame["inference_scope"] = INFERENCE_SCOPE
    return frame


def parse_end_ts(value):
    """Parse the CLI end bound: an integer millisecond timestamp or ``latest``."""
    if value == "latest":
        return "latest"
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--end-ts must be an integer millisecond timestamp or 'latest'") from exc
    if parsed < TS_FULL_START:
        raise argparse.ArgumentTypeError(f"--end-ts must be >= {TS_FULL_START}")
    return parsed


def last_complete_minute_open(now_ms=None):
    """Open timestamp of the latest fully closed UTC minute."""
    current_ms = int(_time.time() * 1000) if now_ms is None else int(now_ms)
    return current_ms // 60_000 * 60_000 - 60_000


def resolve_latest_end_ts(conn, now_ms=None):
    """Return the common latest Binance-perp 1m bar open across required symbols."""
    q = """
        SELECT symbol, MAX(ts) AS max_ts
        FROM klines
        WHERE venue=? AND market=? AND symbol IN (?, ?) AND ts<=?
        GROUP BY symbol
    """
    rows = conn.execute(
        q, (VENUE, MARKET, *SYMBOLS, last_complete_minute_open(now_ms))
    ).fetchall()
    maxima = {symbol: max_ts for symbol, max_ts in rows}
    missing = [symbol for symbol in SYMBOLS if maxima.get(symbol) is None]
    if missing:
        raise ValueError(f"cannot resolve latest: missing Binance perp klines for {', '.join(missing)}")
    return int(min(maxima[symbol] for symbol in SYMBOLS))


def resolve_analysis_end_ts(conn, requested_end):
    """Resolve a parsed end request without mutating the read-only source DB."""
    return resolve_latest_end_ts(conn) if requested_end == "latest" else int(requested_end)


def oi_end_ts_for_run(analysis_end_ts):
    """Keep the legacy default OI bound byte-for-byte compatible; future runs extend it."""
    if analysis_end_ts <= TS_FULL_END:
        return min(analysis_end_ts, TS_OI_END)
    return analysis_end_ts


def bootstrap_sample_end(analysis_end_ts, coverage_end_exclusive_ts, horizon_ms):
    """Exclusive random-decision bound with enough loaded bars for the holding period."""
    return min(analysis_end_ts - horizon_ms, coverage_end_exclusive_ts - horizon_ms)


def frame_coverage(frame, coverage_interval_ms, *, maximum_expected_interval_ms=None,
                   gap_tolerance_ms=0):
    """JSON-safe min/max/count coverage for a loaded source frame."""
    if frame.empty:
        return {"min_ts": None, "max_ts": None, "count": 0,
                "coverage_end_exclusive_ts": None, "unexpected_gap_count": 0,
                "tail_contiguous_start_ts": None}
    timestamps = np.sort(frame["ts"].astype(np.int64).unique())
    max_ts = int(timestamps[-1])
    expected = int(maximum_expected_interval_ms or coverage_interval_ms)
    gaps = np.flatnonzero(np.diff(timestamps) > expected + int(gap_tolerance_ms))
    tail_start = int(timestamps[gaps[-1] + 1]) if len(gaps) else int(timestamps[0])
    return {
        "min_ts": int(timestamps[0]),
        "max_ts": max_ts,
        "count": int(len(frame)),
        "coverage_end_exclusive_ts": max_ts + int(coverage_interval_ms),
        "unexpected_gap_count": int(len(gaps)),
        "tail_contiguous_start_ts": tail_start,
    }


def script_sha256():
    with open(__file__, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_run_manifest(*, requested_end, analysis_end_ts, kline_data, source_coverage,
                       source_event_file_count, event_file_sha256=None,
                       generated_at_utc=None):
    """Build the provenance record separately so its invariants are unit-testable."""
    kline_maxima = [data["coverage"]["max_ts"] for data in kline_data.values()]
    if any(value is None for value in kline_maxima):
        coverage_end = None
    else:
        coverage_end = int(min(kline_maxima) + 60_000)
    return {
        "schema_version": 1,
        "script": "backtests/crowding_signals.py",
        "statistical_warning": (
            "Raw t-statistics, p-values, and null percentiles are non-confirmatory; "
            "strategy_robustness.py global-BY output is authoritative for inference."
        ),
        "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_end": requested_end,
        "analysis_end_ts": int(analysis_end_ts),
        "coverage_end_exclusive_ts": coverage_end,
        "sources": source_coverage,
        "event_schema": {
            "event_columns": ["ts", "entry_ts", "exit_ts", "net_bp", "year"],
            "timing": "entry is the first 1m bar open strictly after decision ts; exit is at/after actual entry plus horizon",
            "oi_funding_asof": "OI snapshot timestamp is backward-as-of joined (oi_signal_ts <= funding ts)",
            "c_trial_strategy": "disabled: current-settlement funding percentile is unavailable before settlement",
        },
        "source_event_file_count": int(source_event_file_count),
        "event_file_sha256": dict(sorted((event_file_sha256 or {}).items())),
        "script_sha256": script_sha256(),
    }


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_funding(conn, venue, symbol, ts_start, ts_end):
    q = """
        SELECT ts, rate, interval_hours FROM funding
        WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?
        ORDER BY ts
    """
    df = pd.read_sql_query(q, conn, params=(venue, symbol, ts_start, ts_end))
    df["ts"] = df["ts"].astype(np.int64)
    df["rate"] = df["rate"].astype(np.float64)
    return df


def load_klines_1m(conn, venue, market, symbol, ts_start, ts_end):
    q = """
        SELECT ts, open, close FROM klines
        WHERE venue=? AND market=? AND symbol=? AND ts BETWEEN ? AND ?
        ORDER BY ts
    """
    df = pd.read_sql_query(q, conn, params=(venue, market, symbol, ts_start, ts_end))
    df["ts"] = df["ts"].astype(np.int64)
    df["open"] = df["open"].astype(np.float64)
    df["close"] = df["close"].astype(np.float64)
    return df


def load_oi(conn, venue, symbol, ts_start, ts_end):
    q = """
        SELECT ts, open_interest FROM oi_metrics
        WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?
        ORDER BY ts
    """
    df = pd.read_sql_query(q, conn, params=(venue, symbol, ts_start, ts_end))
    df["ts"] = df["ts"].astype(np.int64)
    df["open_interest"] = df["open_interest"].astype(np.float64)
    return df


# ---------------------------------------------------------------------------
# Price lookup helpers
# ---------------------------------------------------------------------------
def asof_price_forward(kline_ts, kline_open, target_ts, *, strictly_after=False):
    """For each target timestamp, find an eligible 1m-bar OPEN price.

    ``strictly_after=True`` selects the first bar whose open timestamp is
    strictly after the target (for decisions made at a timestamp).  The
    default permits an exact timestamp match (for the holding-period exit).
    Returns (price array, actual bar ts array, valid mask)."""
    n = len(kline_ts)
    pos = np.searchsorted(kline_ts, target_ts, side="right" if strictly_after else "left")
    valid = pos < n
    px = np.full(len(target_ts), np.nan)
    actual_ts = np.full(len(target_ts), -1, dtype=np.int64)
    px[valid] = kline_open[pos[valid]]
    actual_ts[valid] = kline_ts[pos[valid]]
    return px, actual_ts, valid


def forward_return(kline_ts, kline_open, base_ts, horizon_ms):
    """Return from the first eligible OPEN after a decision to an exit OPEN.

    A decision at ``base_ts`` cannot trade the bar that opened at that same
    timestamp, so entry is the first 1m bar OPEN strictly after it.  The exit
    is the first bar OPEN at or after the *actual* entry timestamp plus the
    holding horizon.  This preserves the requested holding period when bars
    are missing.
    """
    base_ts = np.asarray(base_ts, dtype=np.int64)
    entry_px, entry_ts, v1 = asof_price_forward(
        kline_ts, kline_open, base_ts, strictly_after=True
    )
    exit_target = entry_ts + horizon_ms
    exit_px, exit_ts, v2 = asof_price_forward(kline_ts, kline_open, exit_target)
    valid = v1 & v2
    ret = np.full(len(base_ts), np.nan)
    ret[valid] = exit_px[valid] / entry_px[valid] - 1.0
    return ret, entry_px, entry_ts, exit_px, exit_ts, valid


def asof_backward_index(source_ts, target_ts):
    """Index of the latest source timestamp <= each target, or -1 if absent."""
    source_ts = np.asarray(source_ts, dtype=np.int64)
    target_ts = np.asarray(target_ts, dtype=np.int64)
    return np.searchsorted(source_ts, target_ts, side="right") - 1


# ---------------------------------------------------------------------------
# Rolling percentile, LOOKAHEAD-SAFE
# ---------------------------------------------------------------------------
def rolling_percentile_safe(series, window):
    """
    For each index i, compute the percentile RANK of series[i] within the
    trailing `window` observations ENDING AT i-1 (i.e. series[i] itself is
    EXCLUDED from its own reference window, and no future data is used).

    Implementation: shift(1) first (so the rolling window at row i covers
    rows i-window .. i-1), then for the value at row i we need its rank
    relative to that already-shifted window. We do this via rolling.apply
    with a rank-of-last-element trick: we build an array where each row's
    percentile is rank(x_i among x_{i-window}..x_{i-1}) / window.
    Equivalent formulation used here (efficient, vectorized-ish):
        shifted = series.shift(1)
        pct[i] = rolling_rank(shifted, window)[i] computed as the fraction of
                 values in shifted[i-window+1 : i+1] <= shifted[i]... but that
                 would rank shifted[i] within a window ending at i, not what
                 we want.
    Simpler and unambiguous approach actually used: for row i, reference
    window = series[i-window : i] (i.e. the `window` observations strictly
    before i, NOT including i). We compute percentile of series[i] within
    that reference window.
    """
    n = len(series)
    vals = series.to_numpy(dtype=np.float64)
    out = np.full(n, np.nan)
    for i in range(n):
        lo = i - window
        if lo < 0:
            continue
        ref = vals[lo:i]  # strictly before i, length == window
        ref = ref[~np.isnan(ref)]
        if len(ref) < max(10, window // 4):
            continue
        out[i] = (ref < vals[i]).sum() / len(ref) * 100.0
    return out


def rolling_percentile_safe_fast(series, window, min_ref=None):
    """Vectorized-ish faster version using searchsorted on sorted rolling
    windows via pandas rolling().apply is O(n*window*log window); for our
    sizes (funding ~6000 rows, OI daily-resampled ~1000s of rows) the pure
    python loop above is fine performance-wise but slow in the worst case.
    This variant uses argsort-based approach per window through pandas but
    kept identical semantics to rolling_percentile_safe. Provided for the
    higher-frequency OI 7d-change series if needed; falls back to the loop.
    """
    return rolling_percentile_safe(series, window)


# ---------------------------------------------------------------------------
# t-test helper
# ---------------------------------------------------------------------------
def ttest_top_vs_bottom(top_vals, bottom_vals):
    top_vals = top_vals[~np.isnan(top_vals)]
    bottom_vals = bottom_vals[~np.isnan(bottom_vals)]
    if len(top_vals) < 2 or len(bottom_vals) < 2:
        return dict(t_stat=np.nan, p_value=np.nan, n_top=len(top_vals), n_bottom=len(bottom_vals))
    t, p = stats.ttest_ind(top_vals, bottom_vals, equal_var=False)
    return dict(t_stat=float(t), p_value=float(p), n_top=len(top_vals), n_bottom=len(bottom_vals))


def net_return_bp_directional(gross_ret, direction):
    """direction: 'long' -> keep sign; 'short' -> negate. gross_ret is simple
    fractional forward return of the underlying (entry->exit). Cost is
    ROUND_TRIP_COST_BP (2 * one-way 7bp = 14bp), matching liq_reversion.py's
    'entry AND exit each cost one-way' convention applied as one full round
    trip per trade here (single entry + single exit per trade, so total
    cost = 2 * ONE_WAY_COST_BP)."""
    sign = 1.0 if direction == "long" else -1.0
    gross_bp = sign * gross_ret * 10000.0
    net_bp = gross_bp - ROUND_TRIP_COST_BP
    return net_bp


def bootstrap_null_mean(all_ts, all_fwd_ret_lookup_fn, n_events, direction, sample_start, sample_end,
                         horizon_ms, kline_ts, kline_open, rng):
    """Draw N_BOOTSTRAP samples of n_events random decision timestamps
    uniformly in [sample_start, sample_end), compute the same net_bp metric
    the real strategy would get, return array of length N_BOOTSTRAP of the
    mean net_bp per bootstrap draw."""
    means = np.empty(N_BOOTSTRAP)
    for b in range(N_BOOTSTRAP):
        rand_ts = rng.integers(sample_start, sample_end, size=n_events, dtype=np.int64)
        rand_ts.sort()
        ret, _, _, _, _, valid = forward_return(kline_ts, kline_open, rand_ts, horizon_ms)
        if valid.sum() == 0:
            means[b] = np.nan
            continue
        net_bp = net_return_bp_directional(ret[valid], direction)
        means[b] = net_bp.mean()
    return means


def percentile_of_score(dist, value):
    dist = dist[~np.isnan(dist)]
    if len(dist) == 0:
        return np.nan
    return float((dist < value).sum()) / len(dist) * 100.0


def year_of(ts_ms):
    return pd.to_datetime(ts_ms, unit="ms", utc=True).year


# ===========================================================================
# VALIDATION A: funding extreme decile forward returns + strategy
# ===========================================================================
def run_validation_a(conn, kline_data, symbol, rng, analysis_end_ts):
    log(f"=== Validation A ({symbol}): funding percentile decile analysis ===")
    fund = load_funding(conn, VENUE, symbol, TS_FULL_START, analysis_end_ts)
    kt, ko = kline_data["ts"], kline_data["open"]

    # LOOKAHEAD-SAFE signal: percentile of funding rate at settlement i computed
    # against the trailing 90-observation (settlement-count) window STRICTLY
    # BEFORE i (rolling_percentile_safe excludes the current row by construction:
    # ref window = vals[i-window:i], i.e. it uses only rows observed up to and
    # including settlement i-1). No future funding values are used to build the
    # percentile assigned to settlement i, and forward returns are computed
    # strictly forward from settlement i's own timestamp.
    window = 90 * 3  # ~90 days of 8h settlements (3/day) for binance funding
    fund["pctile"] = rolling_percentile_safe(fund["rate"], window)
    fund = fund.dropna(subset=["pctile"]).reset_index(drop=True)
    log(f"  {symbol}: {len(fund)} settlements with valid (lookahead-safe) percentile signal")

    fund["decile"] = pd.cut(fund["pctile"], bins=np.linspace(0, 100, 11),
                             labels=range(10), include_lowest=True).astype(int)

    horizons = {"8h": 8 * HOUR_MS, "24h": 24 * HOUR_MS, "72h": 72 * HOUR_MS}
    base_ts = fund["ts"].to_numpy()

    decile_rows = []
    fwd_ret_by_h = {}
    for hlabel, hms in horizons.items():
        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, base_ts, hms)
        # Entry is the next tradable bar OPEN; exit is after actual entry + hold.
        assert np.all(entry_ts[valid] > base_ts[valid]), "LOOKAHEAD: entry not after decision"
        assert np.all(exit_ts[valid] >= entry_ts[valid] + hms), "EXIT: holding horizon not met"
        fund[f"fwd_ret_{hlabel}"] = ret
        fwd_ret_by_h[hlabel] = ret

    for dec, g in fund.groupby("decile"):
        row = dict(decile=int(dec), n=len(g))
        for hlabel in horizons:
            vals = g[f"fwd_ret_{hlabel}"].to_numpy()
            vals = vals[~np.isnan(vals)]
            row[f"mean_{hlabel}"] = float(vals.mean()) if len(vals) else np.nan
            row[f"median_{hlabel}"] = float(np.median(vals)) if len(vals) else np.nan
        decile_rows.append(row)
    decile_df = pd.DataFrame(decile_rows).sort_values("decile")

    # t-test top decile (9) vs bottom decile (0), per horizon
    ttest_rows = []
    for hlabel in horizons:
        top = fund.loc[fund["decile"] == 9, f"fwd_ret_{hlabel}"].to_numpy()
        bot = fund.loc[fund["decile"] == 0, f"fwd_ret_{hlabel}"].to_numpy()
        tt = ttest_top_vs_bottom(top, bot)
        tt["horizon"] = hlabel
        tt["mean_diff_top_minus_bottom"] = float(np.nanmean(top) - np.nanmean(bot))
        ttest_rows.append(tt)
    ttest_df = mark_exploratory_inference(pd.DataFrame(ttest_rows))

    decile_df.to_csv(os.path.join(OUT_DIR, f"A1_decile_table_{symbol}.csv"), index=False)
    ttest_df.to_csv(os.path.join(OUT_DIR, f"A1_ttest_top_vs_bottom_{symbol}.csv"), index=False)
    fund.to_csv(os.path.join(OUT_DIR, f"A_funding_signal_raw_{symbol}.csv"), index=False)

    # -----------------------------------------------------------------
    # Analysis 2: strategy - short top decile, long bottom decile
    # Analysis 3: reverse (momentum) - long top decile, short bottom decile
    # -----------------------------------------------------------------
    strategy_rows = []
    for strat_name, top_dir, bot_dir in [
        ("A2_contrarian", "short", "long"),   # top decile (crowded long) -> short; bottom -> long
        ("A3_momentum", "long", "short"),     # reverse
    ]:
        for hold_label, hold_ms in [("24h", 24 * HOUR_MS), ("72h", 72 * HOUR_MS)]:
            ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, base_ts, hold_ms)
            assert np.all(entry_ts[valid] > base_ts[valid])
            assert np.all(exit_ts[valid] >= entry_ts[valid] + hold_ms)

            top_mask = (fund["decile"].to_numpy() == 9) & valid
            bot_mask = (fund["decile"].to_numpy() == 0) & valid

            top_net = net_return_bp_directional(ret[top_mask], top_dir)
            bot_net = net_return_bp_directional(ret[bot_mask], bot_dir)
            all_net = np.concatenate([top_net, bot_net])
            all_ts_combo = np.concatenate([base_ts[top_mask], base_ts[bot_mask]])
            all_entry_ts = np.concatenate([entry_ts[top_mask], entry_ts[bot_mask]])
            all_exit_ts = np.concatenate([exit_ts[top_mask], exit_ts[bot_mask]])
            all_dir = np.array(["top"] * len(top_net) + ["bottom"] * len(bot_net))

            events_df = pd.DataFrame({
                "ts": all_ts_combo, "entry_ts": all_entry_ts, "exit_ts": all_exit_ts,
                "leg": all_dir, "net_bp": all_net,
                "year": year_of(all_ts_combo),
            }).sort_values("ts")
            events_df.to_csv(
                os.path.join(OUT_DIR, f"{strat_name}_events_{symbol}_{hold_label}.csv"), index=False
            )

            n_ev = len(all_net)
            mean_bp = float(np.mean(all_net)) if n_ev else np.nan
            std_bp = float(np.std(all_net, ddof=1)) if n_ev > 1 else np.nan
            se = std_bp / math.sqrt(n_ev) if n_ev > 1 and std_bp > 0 else np.nan
            t_stat = mean_bp / se if se and not np.isnan(se) and se > 0 else np.nan

            # yearly breakdown (sign stability check)
            yearly = events_df.groupby("year")["net_bp"].agg(["mean", "count"]).reset_index()
            yearly = yearly.rename(columns={"mean": "mean_net_bp", "count": "n"})

            # null model: 200 random-entry draws matched count/horizon, same direction mix
            # (approximate: run null separately for top-leg count using top_dir, then bottom
            # leg count using bot_dir, combine means weighted -- but simplest faithful approach:
            # bootstrap total n_ev random entries using long/short 50/50 split matching realized
            # proportion is overkill; instead we bootstrap each leg separately to preserve the
            # direction mix exactly.)
            null_means = np.empty(N_BOOTSTRAP)
            for b in range(N_BOOTSTRAP):
                sample_end = bootstrap_sample_end(
                    analysis_end_ts, kline_data["coverage_end_exclusive_ts"], hold_ms
                )
                rand_ts_top = rng.integers(TS_FULL_START, sample_end, size=top_mask.sum(), dtype=np.int64)
                rand_ts_bot = rng.integers(TS_FULL_START, sample_end, size=bot_mask.sum(), dtype=np.int64)
                rand_ts_top.sort(); rand_ts_bot.sort()
                rt, _, _, _, _, vt = forward_return(kt, ko, rand_ts_top, hold_ms)
                rb, _, _, _, _, vb = forward_return(kt, ko, rand_ts_bot, hold_ms)
                nt = net_return_bp_directional(rt[vt], top_dir)
                nb = net_return_bp_directional(rb[vb], bot_dir)
                combo = np.concatenate([nt, nb])
                null_means[b] = combo.mean() if len(combo) else np.nan
            null_mean = float(np.nanmean(null_means))
            null_pctile = percentile_of_score(null_means, mean_bp)

            strategy_rows.append(dict(
                strategy=strat_name, symbol=symbol, hold=hold_label,
                n_events=n_ev, mean_net_bp=mean_bp, std_net_bp=std_bp, t_stat=t_stat,
                null_mean_bp=null_mean, null_percentile=null_pctile,
                edge_vs_null_bp=mean_bp - null_mean if not np.isnan(mean_bp) else np.nan,
            ))

            yearly.to_csv(
                os.path.join(OUT_DIR, f"{strat_name}_yearly_{symbol}_{hold_label}.csv"), index=False
            )

    strategy_df = mark_exploratory_inference(pd.DataFrame(strategy_rows))
    strategy_df.to_csv(os.path.join(OUT_DIR, f"A_strategy_summary_{symbol}.csv"), index=False)

    return dict(decile=decile_df, ttest=ttest_df, strategy=strategy_df)


# ===========================================================================
# VALIDATION B: OI crowding (2023-01+ only)
# ===========================================================================
def run_validation_b(conn, kline_data, symbol, rng, analysis_end_ts, oi_end_ts):
    log(f"=== Validation B ({symbol}): OI crowding analysis (2023-01+) ===")
    kt, ko = kline_data["ts"], kline_data["open"]
    oi = load_oi(conn, VENUE, symbol, TS_OI_START, oi_end_ts)
    fund = load_funding(conn, VENUE, symbol, TS_OI_START, oi_end_ts)
    log(f"  {symbol}: {len(oi)} OI rows, {len(fund)} funding rows in OI-coverage period")

    # Collapse to one end-of-UTC-day OI snapshot while retaining the actual
    # timestamp of that day's last native observation (normally 23:55 UTC).
    # The actual timestamp is required for causal backward-as-of joins below.
    oi = oi.sort_values("ts").reset_index(drop=True)
    oi["day"] = oi["ts"] // DAY_MS
    daily_oi = oi.groupby("day").agg(ts=("ts", "last"), open_interest=("open_interest", "last")).reset_index()
    daily_oi = daily_oi.sort_values("day").reset_index(drop=True)

    # 7-day OI change rate: pct change vs value 7 days prior on the daily grid.
    # This value at day d uses only OI observations up to day d (its own last
    # obs of the day) vs day d-7 -- both are "known" as of day d's close, so
    # no future leakage in the change-rate itself. The PERCENTILE of that
    # change-rate, however, must still be lookahead-safe (see below).
    daily_oi["oi_chg_7d"] = daily_oi["open_interest"] / daily_oi["open_interest"].shift(7) - 1.0

    # LOOKAHEAD-SAFE percentile: trailing 90-day window strictly BEFORE day d
    daily_oi["oi_chg_7d_pctile"] = rolling_percentile_safe(daily_oi["oi_chg_7d"], 90)
    daily_oi = daily_oi.dropna(subset=["oi_chg_7d_pctile"]).reset_index(drop=True)
    log(f"  {symbol}: {len(daily_oi)} days with valid OI 7d-change percentile signal")

    daily_oi["decile"] = pd.cut(daily_oi["oi_chg_7d_pctile"], bins=np.linspace(0, 100, 11),
                                 labels=range(10), include_lowest=True).astype(int)

    base_ts = daily_oi["ts"].to_numpy()
    horizons_b1 = {"24h": 24 * HOUR_MS, "72h": 72 * HOUR_MS, "168h": 168 * HOUR_MS}

    # --- Signal 1: decile table ---
    decile_rows = []
    for hlabel, hms in horizons_b1.items():
        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, base_ts, hms)
        assert np.all(entry_ts[valid] > base_ts[valid])
        assert np.all(exit_ts[valid] >= entry_ts[valid] + hms)
        daily_oi[f"fwd_ret_{hlabel}"] = ret
    for dec, g in daily_oi.groupby("decile"):
        row = dict(decile=int(dec), n=len(g))
        for hlabel in horizons_b1:
            vals = g[f"fwd_ret_{hlabel}"].dropna().to_numpy()
            row[f"mean_{hlabel}"] = float(vals.mean()) if len(vals) else np.nan
            row[f"median_{hlabel}"] = float(np.median(vals)) if len(vals) else np.nan
        decile_rows.append(row)
    b1_decile_df = pd.DataFrame(decile_rows).sort_values("decile")
    b1_decile_df.to_csv(os.path.join(OUT_DIR, f"B1_oi_decile_table_{symbol}.csv"), index=False)
    daily_oi.to_csv(os.path.join(OUT_DIR, f"B_oi_signal_raw_{symbol}.csv"), index=False)

    # t-test top vs bottom decile, per horizon (statistical rigor, not
    # explicitly required by spec for B1 but consistent w/ A1 and useful)
    b1_ttest_rows = []
    for hlabel in horizons_b1:
        top = daily_oi.loc[daily_oi["decile"] == 9, f"fwd_ret_{hlabel}"].to_numpy()
        bot = daily_oi.loc[daily_oi["decile"] == 0, f"fwd_ret_{hlabel}"].to_numpy()
        tt = ttest_top_vs_bottom(top, bot)
        tt["horizon"] = hlabel
        b1_ttest_rows.append(tt)
    b1_ttest_df = mark_exploratory_inference(pd.DataFrame(b1_ttest_rows))
    b1_ttest_df.to_csv(os.path.join(OUT_DIR, f"B1_ttest_top_vs_bottom_{symbol}.csv"), index=False)

    # --- Signal 2: deleverage continuation events ---
    # OI 24h change compares the end-of-day snapshot with the prior day's
    # snapshot. Event entry uses the first bar open strictly after the actual
    # current snapshot timestamp, so both inputs are already observable.
    daily_oi["oi_chg_24h"] = daily_oi["open_interest"] / daily_oi["open_interest"].shift(1) - 1.0

    delevent_rows = []
    for thresh_label, thresh in [("neg3pct", -0.03), ("neg5pct", -0.05)]:
        mask = daily_oi["oi_chg_24h"] < thresh
        ev = daily_oi.loc[mask].copy()
        n_events = len(ev)
        log(f"  {symbol} deleverage event ({thresh_label}): {n_events} events")
        if n_events == 0:
            for hlabel, hms in [("24h", 24 * HOUR_MS), ("72h", 72 * HOUR_MS)]:
                delevent_rows.append(dict(
                    symbol=symbol, threshold=thresh_label, horizon=hlabel, n_events=0,
                    mean_gross_bp=np.nan, mean_net_bp=np.nan, t_stat=np.nan,
                    null_mean_bp=np.nan, null_percentile=np.nan,
                ))
            pd.DataFrame(columns=["ts", "entry_ts", "exit_ts", "oi_chg_24h", "horizon", "gross_ret", "net_bp", "year"]).to_csv(
                os.path.join(OUT_DIR, f"B2_deleverage_events_{symbol}_{thresh_label}.csv"), index=False)
            continue

        ev_ts = ev["ts"].to_numpy()
        all_events_out = []
        for hlabel, hms in [("24h", 24 * HOUR_MS), ("72h", 72 * HOUR_MS)]:
            ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, ev_ts, hms)
            assert np.all(entry_ts[valid] > ev_ts[valid])
            assert np.all(exit_ts[valid] >= entry_ts[valid] + hms)
            gross = ret[valid]
            # long-only interpretation (buy the dip / follow-through direction
            # unspecified by the spec; we report raw long-side forward return
            # since this signal is descriptive: "is it cascade-mid or
            # cascade-end"), net cost applied assuming a long entry
            net_bp = net_return_bp_directional(gross, "long")
            ts_v = ev_ts[valid]
            for t_, e_, x_, g_, n_ in zip(ts_v, entry_ts[valid], exit_ts[valid], gross, net_bp):
                all_events_out.append(dict(ts=int(t_), entry_ts=int(e_), exit_ts=int(x_),
                                            oi_chg_24h=thresh_label, horizon=hlabel,
                                            gross_ret=float(g_), net_bp=float(n_), year=int(year_of(t_))))

            mean_gross_bp = float(gross.mean() * 10000.0) if len(gross) else np.nan
            mean_net_bp = float(net_bp.mean()) if len(net_bp) else np.nan
            std_bp = float(np.std(net_bp, ddof=1)) if len(net_bp) > 1 else np.nan
            se = std_bp / math.sqrt(len(net_bp)) if len(net_bp) > 1 and std_bp > 0 else np.nan
            t_stat = mean_net_bp / se if se and not np.isnan(se) and se > 0 else np.nan

            null_dist = bootstrap_null_mean(None, None, len(ts_v), "long", TS_OI_START,
                                             bootstrap_sample_end(oi_end_ts, kline_data["coverage_end_exclusive_ts"], hms),
                                             hms, kt, ko, rng)
            null_mean = float(np.nanmean(null_dist))
            null_pctile = percentile_of_score(null_dist, mean_net_bp)

            delevent_rows.append(dict(
                symbol=symbol, threshold=thresh_label, horizon=hlabel, n_events=len(ts_v),
                mean_gross_bp=mean_gross_bp, mean_net_bp=mean_net_bp, t_stat=t_stat,
                null_mean_bp=null_mean, null_percentile=null_pctile,
            ))

        pd.DataFrame(all_events_out).to_csv(
            os.path.join(OUT_DIR, f"B2_deleverage_events_{symbol}_{thresh_label}.csv"), index=False)

        # yearly breakdown for this threshold (24h horizon events, by year)
        yb = pd.DataFrame(all_events_out)
        if len(yb):
            yb_summary = yb[yb.horizon == "24h"].groupby("year")["net_bp"].agg(["mean", "count"]).reset_index()
            yb_summary.to_csv(
                os.path.join(OUT_DIR, f"B2_deleverage_yearly_{symbol}_{thresh_label}.csv"), index=False)

    b2_df = mark_exploratory_inference(pd.DataFrame(delevent_rows))
    b2_df.to_csv(os.path.join(OUT_DIR, f"B2_deleverage_summary_{symbol}.csv"), index=False)

    # --- Signal 3: composite (funding pctile top quartile AND OI 7d-chg top quartile) ---
    # Build funding percentile on the SAME lookahead-safe basis as validation A,
    # restricted to the OI coverage window, then align funding settlements to
    # the daily OI-decile grid by actual snapshot timestamp (not calendar day).
    window = 90 * 3
    fund_full = load_funding(conn, VENUE, symbol, TS_FULL_START, analysis_end_ts)
    fund_full["pctile"] = rolling_percentile_safe(fund_full["rate"], window)
    fund_b = fund_full[(fund_full["ts"] >= TS_OI_START) & (fund_full["ts"] <= oi_end_ts)].dropna(
        subset=["pctile"]).reset_index(drop=True)

    oi_ts = daily_oi["ts"].to_numpy()
    oi_pctile = daily_oi["oi_chg_7d_pctile"].to_numpy()
    # Backward as-of join: an OI signal is eligible only when its actual
    # snapshot timestamp is <= the funding decision timestamp.
    idx = asof_backward_index(oi_ts, fund_b["ts"].to_numpy())
    valid_join = idx >= 0
    fund_b = fund_b.loc[valid_join].reset_index(drop=True)
    idx = idx[valid_join]
    fund_b["oi_chg_7d_pctile"] = oi_pctile[idx]
    fund_b["oi_signal_ts"] = oi_ts[idx]
    assert np.all(fund_b["oi_signal_ts"].to_numpy() <= fund_b["ts"].to_numpy())

    fund_ts = fund_b["ts"].to_numpy()
    f_pct = fund_b["pctile"].to_numpy()
    o_pct = fund_b["oi_chg_7d_pctile"].to_numpy()

    groups = {
        "double_overheat": (f_pct >= 75) & (o_pct >= 75),
        "funding_only_top_quartile": (f_pct >= 75) & (o_pct < 75),
        "oi_only_top_quartile": (f_pct < 75) & (o_pct >= 75),
        "neither": (f_pct < 75) & (o_pct < 75),
    }

    composite_rows = []
    for hlabel, hms in horizons_b1.items():
        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, fund_ts, hms)
        assert np.all(entry_ts[valid] > fund_ts[valid])
        assert np.all(exit_ts[valid] >= entry_ts[valid] + hms)
        for gname, gmask in groups.items():
            m = gmask & valid
            vals = ret[m]
            n_ev = len(vals)
            mean_bp = float(vals.mean() * 10000.0) if n_ev else np.nan
            std_bp = float(np.std(vals, ddof=1) * 10000.0) if n_ev > 1 else np.nan
            se = std_bp / math.sqrt(n_ev) if n_ev > 1 and std_bp > 0 else np.nan
            t_stat = mean_bp / se if se and not np.isnan(se) and se > 0 else np.nan
            composite_rows.append(dict(
                group=gname, horizon=hlabel, n_events=n_ev, mean_gross_bp=mean_bp,
                t_stat=t_stat,
            ))
    composite_df = mark_exploratory_inference(pd.DataFrame(composite_rows))
    composite_df.to_csv(os.path.join(OUT_DIR, f"B3_composite_signal_{symbol}.csv"), index=False)

    # net/cost + year + null for the double_overheat group specifically (the
    # signal of interest), short side (crowded-long contrarian hypothesis)
    b3_strategy_rows = []
    dbl_mask = groups["double_overheat"]
    for hlabel, hms in horizons_b1.items():
        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(kt, ko, fund_ts, hms)
        assert np.all(entry_ts[valid] > fund_ts[valid])
        assert np.all(exit_ts[valid] >= entry_ts[valid] + hms)
        m = dbl_mask & valid
        ts_v = fund_ts[m]
        gross_v = ret[m]
        net_bp = net_return_bp_directional(gross_v, "short")  # contrarian: short the double-overheat
        n_ev = len(net_bp)
        events_out = pd.DataFrame({
            "ts": ts_v,
            "oi_signal_ts": fund_b["oi_signal_ts"].to_numpy()[m],
            "funding_pctile": f_pct[m],
            "oi_chg_7d_pctile": o_pct[m],
            "entry_ts": entry_ts[m], "exit_ts": exit_ts[m],
            "net_bp": net_bp, "year": year_of(ts_v),
        })
        events_out.to_csv(os.path.join(OUT_DIR, f"B3_double_overheat_events_{symbol}_{hlabel}.csv"), index=False)

        if n_ev == 0:
            b3_strategy_rows.append(dict(symbol=symbol, horizon=hlabel, n_events=0,
                                          mean_net_bp=np.nan, t_stat=np.nan,
                                          null_mean_bp=np.nan, null_percentile=np.nan))
            continue

        mean_bp = float(net_bp.mean())
        std_bp = float(np.std(net_bp, ddof=1)) if n_ev > 1 else np.nan
        se = std_bp / math.sqrt(n_ev) if n_ev > 1 and std_bp > 0 else np.nan
        t_stat = mean_bp / se if se and not np.isnan(se) and se > 0 else np.nan

        null_dist = bootstrap_null_mean(None, None, n_ev, "short", TS_OI_START,
                                        bootstrap_sample_end(oi_end_ts, kline_data["coverage_end_exclusive_ts"], hms),
                                        hms, kt, ko, rng)
        null_mean = float(np.nanmean(null_dist))
        null_pctile = percentile_of_score(null_dist, mean_bp)

        b3_strategy_rows.append(dict(symbol=symbol, horizon=hlabel, n_events=n_ev,
                                      mean_net_bp=mean_bp, t_stat=t_stat,
                                      null_mean_bp=null_mean, null_percentile=null_pctile))

        yearly = events_out.groupby("year")["net_bp"].agg(["mean", "count"]).reset_index()
        yearly.to_csv(os.path.join(OUT_DIR, f"B3_double_overheat_yearly_{symbol}_{hlabel}.csv"), index=False)

    b3_strategy_df = mark_exploratory_inference(pd.DataFrame(b3_strategy_rows))
    b3_strategy_df.to_csv(os.path.join(OUT_DIR, f"B3_double_overheat_strategy_{symbol}.csv"), index=False)

    return dict(b1_decile=b1_decile_df, b2=b2_df, b3=composite_df)


# ===========================================================================
# VALIDATION C: settlement time-of-day effect
# ===========================================================================
def run_validation_c(conn, kline_data, symbol, analysis_end_ts):
    log(f"=== Validation C ({symbol}): settlement time-of-day event profile ===")
    kt, kc = kline_data["ts"], kline_data["close"]

    fund = load_funding(conn, VENUE, symbol, TS_FULL_START, analysis_end_ts)
    # settlement hours are 00/08/16 UTC by construction of binance funding;
    # verify empirically rather than assume
    fund["hour"] = pd.to_datetime(fund["ts"], unit="ms", utc=True).dt.hour
    hour_counts = fund["hour"].value_counts()
    log(f"  {symbol} funding settlement hour distribution: {hour_counts.to_dict()}")

    # per-minute simple return series (close-to-close) for event-time averaging
    ret_1m = np.full(len(kc), np.nan)
    ret_1m[1:] = kc[1:] / kc[:-1] - 1.0

    # For each settlement event, gather returns at relative minute offsets
    # -60..+60 around the settlement ts (settlement ts is exact per DB row).
    offsets = np.arange(-60, 61)
    kt_arr = kt
    n = len(kt_arr)

    # This is a purely retrospective profile, not a tradable decision rule. It
    # uses both sides of a scheduled settlement and, below, stratifies them by
    # the current settlement's realized funding-rate percentile. In particular,
    # the pre-settlement leg cannot use that classification prospectively; the
    # former trial strategy based on it is explicitly disabled below.

    def event_profile(event_ts_arr):
        # accumulate sum and count of ret_1m at each relative-minute offset
        sums = np.zeros(len(offsets))
        counts = np.zeros(len(offsets))
        for ts_e in event_ts_arr:
            base_pos = np.searchsorted(kt_arr, ts_e, side="left")
            if base_pos <= 0 or base_pos >= n:
                continue
            lo = base_pos - 60
            hi = base_pos + 60
            if lo < 0 or hi >= n:
                continue
            window_ret = ret_1m[lo:hi + 1]  # length 121, aligned to offsets -60..60
            valid = ~np.isnan(window_ret)
            sums[valid] += window_ret[valid]
            counts[valid] += 1
        mean_ret = np.divide(sums, counts, out=np.full(len(offsets), np.nan), where=counts > 0)
        return mean_ret, counts

    all_event_ts = fund["ts"].to_numpy()
    mean_all, cnt_all = event_profile(all_event_ts)

    # The percentile reference window excludes the current observation, as in
    # A. The current rate still only becomes an actionable classifier at the
    # settlement itself, so pre-settlement results remain descriptive.
    window = 90 * 3
    fund["pctile"] = rolling_percentile_safe(fund["rate"], window)
    fund_valid = fund.dropna(subset=["pctile"]).reset_index(drop=True)
    top_q = fund_valid.loc[fund_valid["pctile"] >= 75, "ts"].to_numpy()
    bot_q = fund_valid.loc[fund_valid["pctile"] <= 25, "ts"].to_numpy()
    log(f"  {symbol}: top-quartile funding settlements n={len(top_q)}, bottom-quartile n={len(bot_q)}")

    mean_top, cnt_top = event_profile(top_q)
    mean_bot, cnt_bot = event_profile(bot_q)

    profile_df = pd.DataFrame({
        "offset_min": offsets,
        "mean_ret_all": mean_all, "n_all": cnt_all,
        "mean_ret_top_quartile_funding": mean_top, "n_top_quartile": cnt_top,
        "mean_ret_bottom_quartile_funding": mean_bot, "n_bottom_quartile": cnt_bot,
    })
    profile_df.to_csv(os.path.join(OUT_DIR, f"C_settlement_profile_{symbol}.csv"), index=False)

    # cumulative return -60..+60 for pattern-spotting convenience
    profile_df["cum_ret_all"] = profile_df["mean_ret_all"].cumsum()
    profile_df["cum_ret_top_quartile"] = profile_df["mean_ret_top_quartile_funding"].cumsum()
    profile_df["cum_ret_bottom_quartile"] = profile_df["mean_ret_bottom_quartile_funding"].cumsum()
    profile_df.to_csv(os.path.join(OUT_DIR, f"C_settlement_profile_{symbol}.csv"), index=False)

    # simple diagnostic: pre-settlement drift (-30..0) vs post-settlement
    # drift (0..+30), separately for top/bottom quartile groups
    pre_all = profile_df.loc[(profile_df.offset_min >= -30) & (profile_df.offset_min < 0), "mean_ret_all"].sum()
    post_all = profile_df.loc[(profile_df.offset_min > 0) & (profile_df.offset_min <= 30), "mean_ret_all"].sum()
    pre_top = profile_df.loc[(profile_df.offset_min >= -30) & (profile_df.offset_min < 0), "mean_ret_top_quartile_funding"].sum()
    post_top = profile_df.loc[(profile_df.offset_min > 0) & (profile_df.offset_min <= 30), "mean_ret_top_quartile_funding"].sum()
    pre_bot = profile_df.loc[(profile_df.offset_min >= -30) & (profile_df.offset_min < 0), "mean_ret_bottom_quartile_funding"].sum()
    post_bot = profile_df.loc[(profile_df.offset_min > 0) & (profile_df.offset_min <= 30), "mean_ret_bottom_quartile_funding"].sum()

    diag = dict(
        symbol=symbol,
        pre30_all_bp=pre_all * 10000, post30_all_bp=post_all * 10000,
        pre30_top_bp=pre_top * 10000, post30_top_bp=post_top * 10000,
        pre30_bottom_bp=pre_bot * 10000, post30_bottom_bp=post_bot * 10000,
    )
    log(f"  {symbol} C diagnostic (sum of avg 1m rets, bp): {diag}")

    max_abs_signal_bp = max(abs(diag["pre30_top_bp"]), abs(diag["post30_top_bp"]),
                             abs(diag["pre30_bottom_bp"]), abs(diag["post30_bottom_bp"]))
    # The descriptive profile remains useful, but a current-settlement funding
    # percentile is not available before that settlement.  Any pre-settlement
    # entry selected from it is therefore invalid and deliberately disabled.
    trial_strategy = dict(
        enabled=False,
        reason=("disabled: a current-settlement funding percentile cannot be "
                "known before settlement; pre-settlement entries would leak it"),
        max_abs_descriptive_signal_bp=float(max_abs_signal_bp),
    )
    log(f"  {symbol}: C trial strategy disabled: {trial_strategy['reason']}")

    with open(os.path.join(OUT_DIR, f"C_diagnostic_{symbol}.json"), "w") as f:
        json.dump(dict(diagnostic=diag, trial_strategy=trial_strategy,
                        hour_distribution=hour_counts.to_dict()), f, indent=2, default=str)

    return dict(profile=profile_df, diagnostic=diag, trial_strategy=trial_strategy)


# ===========================================================================
# Main
# ===========================================================================
def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate crowding signals with causal execution timing.")
    parser.add_argument(
        "--end-ts", default=str(TS_FULL_END), type=parse_end_ts,
        help="inclusive analysis end as epoch milliseconds, or 'latest' for the BTC/ETH common latest kline",
    )
    args = parser.parse_args(argv)
    t0 = _time.time()
    log("=== crowding_signals.py starting ===")
    conn = sqlite3.connect(DB_URI, uri=True)

    requested_end = args.end_ts
    analysis_end_ts = resolve_analysis_end_ts(conn, requested_end)
    enforce_prospective_seal(analysis_end_ts)
    os.makedirs(OUT_DIR, exist_ok=True)
    latest_common_ts = resolve_latest_end_ts(conn)
    if analysis_end_ts > latest_common_ts:
        conn.close()
        raise ValueError(
            f"--end-ts {analysis_end_ts} exceeds common loaded BTC/ETH kline coverage "
            f"({latest_common_ts}); use --end-ts latest or ingest both symbols first"
        )
    oi_end_ts = oi_end_ts_for_run(analysis_end_ts)
    log(f"analysis end: requested={requested_end}, resolved={analysis_end_ts}, oi_end={oi_end_ts}")

    rng = np.random.default_rng(RNG_SEED)

    kline_data = {}
    source_coverage = {}
    for sym in SYMBOLS:
        log(f"loading 1m klines for {sym} ...")
        kl = load_klines_1m(conn, VENUE, MARKET, sym, TS_FULL_START, analysis_end_ts)
        coverage = frame_coverage(kl, 60_000)
        kline_data[sym] = dict(
            ts=kl["ts"].to_numpy(), close=kl["close"].to_numpy(), open=kl["open"].to_numpy(),
            coverage=coverage, coverage_end_exclusive_ts=coverage["coverage_end_exclusive_ts"],
        )
        funding_for_manifest = load_funding(conn, VENUE, sym, TS_FULL_START, analysis_end_ts)
        funding_interval_ms = (
            int(funding_for_manifest["interval_hours"].iloc[-1]) * HOUR_MS
            if not funding_for_manifest.empty else 0
        )
        maximum_funding_interval_ms = (
            int(funding_for_manifest["interval_hours"].max()) * HOUR_MS
            if not funding_for_manifest.empty else 0
        )
        oi_for_manifest = load_oi(conn, VENUE, sym, TS_OI_START, oi_end_ts)
        source_coverage[sym] = {
            "kline": coverage,
            "funding": frame_coverage(
                funding_for_manifest, funding_interval_ms,
                maximum_expected_interval_ms=maximum_funding_interval_ms,
                gap_tolerance_ms=60_000,
            ),
            "oi": frame_coverage(oi_for_manifest, 5 * 60_000),
        }
        log(f"  {sym}: {len(kl)} klines, range {kl.ts.min()}..{kl.ts.max()}")

    common_coverage_end = min(data["coverage_end_exclusive_ts"] for data in kline_data.values())
    if analysis_end_ts >= common_coverage_end:
        conn.close()
        raise ValueError(f"analysis end {analysis_end_ts} has no loaded common kline coverage")

    results_a = {}
    results_b = {}
    results_c = {}

    for sym in SYMBOLS:
        results_a[sym] = run_validation_a(conn, kline_data[sym], sym, rng, analysis_end_ts)

    for sym in SYMBOLS:
        results_b[sym] = run_validation_b(conn, kline_data[sym], sym, rng, analysis_end_ts, oi_end_ts)

    for sym in SYMBOLS:
        results_c[sym] = run_validation_c(conn, kline_data[sym], sym, analysis_end_ts)

    conn.close()

    event_paths = sorted(glob.glob(os.path.join(OUT_DIR, "*_events_*.csv")))
    event_hashes = {}
    for path in event_paths:
        with open(path, "rb") as f:
            event_hashes[os.path.basename(path)] = hashlib.sha256(f.read()).hexdigest()
    event_file_count = len(event_paths)
    manifest = build_run_manifest(
        requested_end=requested_end,
        analysis_end_ts=analysis_end_ts,
        kline_data=kline_data,
        source_coverage=source_coverage,
        source_event_file_count=event_file_count,
        event_file_sha256=event_hashes,
    )
    with open(os.path.join(OUT_DIR, "run_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)

    # ---- console summary ----
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", lambda x: f"{x:,.4f}")

    print("\n\n================= VALIDATION A: decile tables =================")
    for sym in SYMBOLS:
        print(f"\n-- {sym} A1 decile table --")
        print(results_a[sym]["decile"].to_string(index=False))
        print(f"\n-- {sym} A1 top-vs-bottom t-test --")
        print(results_a[sym]["ttest"].to_string(index=False))
        print(f"\n-- {sym} A2/A3 strategy summary --")
        print(results_a[sym]["strategy"].to_string(index=False))

    print("\n\n================= VALIDATION B: OI crowding =================")
    for sym in SYMBOLS:
        print(f"\n-- {sym} B1 OI decile table --")
        print(results_b[sym]["b1_decile"].to_string(index=False))
        print(f"\n-- {sym} B2 deleverage event summary --")
        print(results_b[sym]["b2"].to_string(index=False))
        print(f"\n-- {sym} B3 composite signal --")
        print(results_b[sym]["b3"].to_string(index=False))

    print("\n\n================= VALIDATION C: settlement time-of-day =================")
    for sym in SYMBOLS:
        print(f"\n-- {sym} C diagnostic --")
        print(results_c[sym]["diagnostic"])
        print(f"-- {sym} C trial strategy --")
        print(results_c[sym]["trial_strategy"])

    total_elapsed = _time.time() - t0
    log(f"=== DONE in {total_elapsed:.1f}s ===")
    log(f"All outputs written to {OUT_DIR}")


if __name__ == "__main__":
    main()
