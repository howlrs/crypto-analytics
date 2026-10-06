#!/usr/bin/env python3
"""Order-book snapshot analysis (databases are opened read-only).

Outputs, all from stored snapshots:

* ``book_metrics.csv``      spread, depth by band, imbalance and market-order impact per snapshot
* ``book_summary.csv``      per-stream distributions and how often depth/impact were knowable
* ``book_by_hour.csv``      per-stream medians by UTC hour (when liquidity is thin)
* ``book_cross_venue.csv``  pairwise same-tick comparison of impact and mid across streams of one asset
* ``book_walls.csv``        large resting levels tracked across snapshots, and how each episode ended
* ``book_imbalance.csv``    forward mid change by fixed imbalance bins (descriptive, not a strategy)
* ``book_manifest.json``    inputs, parameters and output hashes

Depth or impact beyond a side's stored coverage is unknown, never extrapolated.
Snapshots cannot tell a cancellation from a fill between observations; wall
endings are classified only by what the observations show.
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from orderbook import book as bk
from orderbook.store import connect_reader, decode_book

PACKAGE_DIR = Path(__file__).resolve().parent
IMBALANCE_EDGES = (-1.0, -0.6, -0.2, 0.2, 0.6, 1.0)
MIN_DAYS = 5
MIN_BIN_OBS = 30


@dataclass(frozen=True)
class AnalysisConfig:
    bands_bp: tuple = (5.0, 10.0, 25.0, 50.0, 100.0)
    notionals_usd: tuple = (10_000.0, 100_000.0, 1_000_000.0)
    wall_band_bp: float = 100.0
    wall_multiple: float = 5.0
    wall_min_usd: float = 100_000.0
    wall_min_share: float = 0.05
    wall_persist_ratio: float = 0.5
    wall_approach_bp: float = 5.0
    max_gap_multiple: float = 2.5
    imbalance_band_bp: float = 10.0
    horizons_sec: tuple = (60.0, 300.0, 900.0)
    bootstrap_samples: int = 2_000
    seed: int = 20261006

    def validate(self) -> None:
        positives = (*self.bands_bp, *self.notionals_usd, self.wall_band_bp, self.wall_multiple,
                     self.wall_persist_ratio, self.max_gap_multiple, self.imbalance_band_bp, *self.horizons_sec)
        if not all(math.isfinite(x) and x > 0 for x in positives):
            raise ValueError("bands, notionals, wall and horizon parameters must be positive")
        if (self.wall_min_usd < 0 or self.wall_approach_bp < 0 or self.bootstrap_samples < 1
                or not 0 <= self.wall_min_share < 1):
            raise ValueError("invalid wall or bootstrap parameter")
        if self.imbalance_band_bp not in self.bands_bp:
            raise ValueError("imbalance band must be one of the depth bands")


def notional_label(value: float) -> str:
    if value >= 1e6 and value % 1e6 == 0:
        return f"{int(value // 1e6)}m"
    if value >= 1e3 and value % 1e3 == 0:
        return f"{int(value // 1e3)}k"
    return f"{value:g}"


def band_label(value: float) -> str:
    return f"{value:g}bp"


def stream_key(venue: str, market: str, symbol: str, aggregation: str) -> str:
    return ":".join(x for x in (venue, market, symbol, aggregation) if x)


def asset_of(venue: str, symbol: str) -> str:
    base = symbol.upper()
    for suffix in ("USDT", "USDC", "USD"):
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def snapshot_metrics(book: bk.Book, cfg: AnalysisConfig) -> dict:
    row = {"mid": book.mid, "spread_bp": book.spread_bp,
           "bid_coverage_bp": book.bid_coverage_bp, "ask_coverage_bp": book.ask_coverage_bp}
    for band in cfg.bands_bp:
        label = band_label(band)
        bid, ask = bk.depth_usd(book, "bid", band), bk.depth_usd(book, "ask", band)
        row[f"bid_usd_{label}"], row[f"ask_usd_{label}"] = bid, ask
        row[f"imbalance_{label}"] = bk.imbalance(bid, ask)
    for notional in cfg.notionals_usd:
        label = notional_label(notional)
        row[f"buy_impact_bp_{label}"] = bk.impact_bp(book, "ask", notional)
        row[f"sell_impact_bp_{label}"] = bk.impact_bp(book, "bid", notional)
    return row


# --------------------------------------------------------------------- walls


class WallTracker:
    """Follow large levels of one stream across consecutive snapshots.

    An episode continues while the level keeps at least ``wall_persist_ratio`` of
    its peak notional.  ``price_crossed`` requires the opposite best quote to
    reach the wall price, so cancelling a wall that was the best level is not
    mistaken for price moving through it.
    """

    def __init__(self, stream: str, cfg: AnalysisConfig):
        self.stream, self.cfg = stream, cfg
        self.active: dict[tuple[str, float], dict] = {}
        self.episodes: list[dict] = []
        self.prev_ts: int | None = None

    def _close(self, key: tuple[str, float], reason: str, end_ms: int) -> None:
        episode = self.active.pop(key)
        episode.update(end_reason=reason, end_ms=end_ms,
                       duration_sec=(episode["last_seen_ms"] - episode["start_ms"]) / 1000)
        self.episodes.append(episode)

    def update(self, ts: int, book: bk.Book, found: list[dict], max_gap_ms: float) -> None:
        if self.prev_ts is not None and ts - self.prev_ts > max_gap_ms:
            for key in list(self.active):
                self._close(key, "observation_gap", self.prev_ts)
        mid = book.mid
        current = {(w["side"], w["px"]): w for w in found}
        for key in list(self.active):
            side, px = key
            episode = self.active[key]
            levels, coverage = book.side(side)
            usd = px * next((lvl[1] for lvl in levels if lvl[0] == px), 0.0)
            distance = bk.distance_bp(px, mid)
            if key in current or usd >= self.cfg.wall_persist_ratio * episode["max_usd"]:
                episode.update(last_seen_ms=ts, snapshots=episode["snapshots"] + 1, last_usd=usd,
                               max_usd=max(episode["max_usd"], usd),
                               min_distance_bp=min(episode["min_distance_bp"], distance))
                continue
            crossed = book.best_ask <= px if side == "bid" else book.best_bid >= px
            if crossed:
                reason = "price_crossed"
            elif distance > coverage:
                reason = "out_of_view"
            elif min(episode["min_distance_bp"], distance) <= self.cfg.wall_approach_bp:
                reason = "removed_after_approach"
            else:
                reason = "removed_while_mid_away"
            self._close(key, reason, ts)
        for key, wall in current.items():
            if key not in self.active:
                self.active[key] = {"stream": self.stream, "side": wall["side"], "px": wall["px"], "start_ms": ts,
                                    "last_seen_ms": ts, "snapshots": 1, "first_usd": wall["usd"],
                                    "last_usd": wall["usd"], "max_usd": wall["usd"],
                                    "first_distance_bp": wall["distance_bp"], "min_distance_bp": wall["distance_bp"],
                                    "share_at_start": wall["share"], "orders_at_start": wall["orders"],
                                    "median_level_usd": wall["median_level_usd"]}
        self.prev_ts = ts

    def finish(self) -> list[dict]:
        if self.prev_ts is not None:
            for key in list(self.active):
                self._close(key, "censored_at_end", self.prev_ts)
        return self.episodes


WALL_COLUMNS = ["stream", "side", "px", "start_ms", "end_ms", "duration_sec", "snapshots", "first_usd", "max_usd",
                "last_usd", "first_distance_bp", "min_distance_bp", "share_at_start", "orders_at_start", "median_level_usd",
                "end_reason"]


# ---------------------------------------------------------------- loading


def _db_rows(index: int, db: Path, where: str, params: list, typical_gap: dict, provenance: dict):
    """Yield one DB's snapshots ordered by stream and time, inside a single read transaction."""
    with closing(connect_reader(db)) as conn:
        conn.execute("BEGIN")  # all queries see the same snapshot even while a collector writes
        configured = {}
        for run_id, config in conn.execute("SELECT run_id, config_json FROM runs"):
            try:
                configured[run_id] = float(json.loads(config)["interval_sec"]) * 1000
            except (ValueError, KeyError, TypeError):
                pass
        stamps = pd.read_sql_query(
            f"SELECT venue, market, symbol, aggregation, run_id, received_ms FROM snapshots WHERE {where} "
            "ORDER BY venue, market, symbol, aggregation, run_id, received_ms", conn, params=params)
        for (venue, market, symbol, agg, run_id), part in stamps.groupby(
                ["venue", "market", "symbol", "aggregation", "run_id"], sort=False):
            # The run's configured interval; observed gaps only when the config lacks it.
            gaps = np.diff(part.received_ms.to_numpy(np.int64))
            typical_gap[(index, run_id, stream_key(venue, market, symbol, agg))] = configured.get(
                run_id, float(np.median(gaps)) if len(gaps) else math.inf)
        cursor = conn.execute(
            "SELECT venue, market, symbol, aggregation, received_ms, snapshot_id, run_id, tick_ms, requested_ms, "
            "exchange_ms, bid_coverage_bp, ask_coverage_bp, payload_format, payload FROM snapshots "
            f"WHERE {where} ORDER BY venue, market, symbol, aggregation, received_ms, snapshot_id", params)
        for row in cursor:
            provenance["snapshots_seen"] += 1
            provenance["max_snapshot_id"] = max(provenance["max_snapshot_id"], row[5])
            yield (*row[:5], index, *row[5:])
        conn.rollback()


def load_metrics(dbs: Sequence[Path], cfg: AnalysisConfig, *, streams: set[str] | None, start_ms: int | None,
                 end_ms: int | None) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Decode every selected snapshot once; return metrics, wall episodes and input provenance.

    Several databases (e.g. monthly files) are merged in time order per stream,
    so a wall episode can continue across a file boundary.
    """
    where, params = ["1=1"], []
    if start_ms is not None:
        where.append("received_ms >= ?"); params.append(start_ms)
    if end_ms is not None:
        where.append("received_ms < ?"); params.append(end_ms)
    typical_gap: dict = {}
    provenance = [{"db": str(db), "snapshots_seen": 0, "snapshots_used": 0, "max_snapshot_id": 0} for db in dbs]
    sources = [_db_rows(i, db, " AND ".join(where), params, typical_gap, provenance[i]) for i, db in enumerate(dbs)]
    rows, trackers = [], {}
    for (venue, market, symbol, agg, received, index, sid, run_id, tick, requested, exchange, bid_cov, ask_cov,
         fmt, payload) in heapq.merge(*sources, key=lambda r: (r[0], r[1], r[2], r[3], r[4], r[5], r[6])):
        key = stream_key(venue, market, symbol, agg)
        if streams and key not in streams:
            continue
        provenance[index]["snapshots_used"] += 1
        book = decode_book(payload, bid_cov, ask_cov, fmt)
        row = {"db": str(dbs[index]), "snapshot_id": sid, "run_id": run_id, "stream": key,
               "asset": asset_of(venue, symbol), "venue": venue, "market": market, "aggregated": bool(agg),
               "tick_ms": tick, "received_ms": received, "latency_ms": received - requested,
               "exchange_lag_ms": received - exchange if exchange is not None else np.nan}
        row.update(snapshot_metrics(book, cfg))
        rows.append(row)
        tracker = trackers.setdefault(key, WallTracker(key, cfg))
        tracker.update(received, book, bk.walls(book, band_bp=cfg.wall_band_bp, multiple=cfg.wall_multiple,
                                                min_usd=cfg.wall_min_usd, min_share=cfg.wall_min_share),
                       cfg.max_gap_multiple * typical_gap[(index, run_id, key)])
    episodes = [episode for tracker in trackers.values() for episode in tracker.finish()]
    for item, db in zip(provenance, dbs):
        stat = db.stat()
        item.update(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    metrics = pd.DataFrame(rows)
    walls = pd.DataFrame(episodes).reindex(columns=WALL_COLUMNS)
    return metrics, walls, provenance


# ------------------------------------------------------------- summaries


def impact_quantile(series: pd.Series, q: float) -> float:
    """Quantile of impact where unknown (book too thin to fill) counts as more expensive than any
    known value; nan when the quantile falls among the unknown."""
    if not len(series):
        return np.nan
    value = float(series.fillna(np.inf).quantile(q, interpolation="higher"))
    return value if math.isfinite(value) else np.nan


def complete_quantile(series: pd.Series, q: float) -> float:
    """Quantile only when every value is known (unknown depth is not ordered against known depth)."""
    if not len(series) or series.isna().any():
        return np.nan
    return float(series.quantile(q))


def summarize(metrics: pd.DataFrame, cfg: AnalysisConfig) -> pd.DataFrame:
    rows = []
    for key, part in metrics.groupby("stream", sort=True):
        row = {"stream": key, "aggregated": bool(part.aggregated.iloc[0]), "snapshots": len(part),
               "first_ms": int(part.received_ms.min()), "last_ms": int(part.received_ms.max()),
               "spread_bp_p50": complete_quantile(part.spread_bp, .5),
               "spread_bp_p90": complete_quantile(part.spread_bp, .9),
               "latency_ms_p50": complete_quantile(part.latency_ms, .5),
               "exchange_lag_ms_p50": float(part.exchange_lag_ms.median())}
        for band in cfg.bands_bp:
            label = band_label(band)
            for side in ("bid", "ask"):
                col = part[f"{side}_usd_{label}"]
                row[f"{side}_usd_{label}_p50"] = complete_quantile(col, .5)
                row[f"{side}_usd_{label}_p10"] = complete_quantile(col, .1)
            row[f"depth_known_share_{label}"] = float((part[f"bid_usd_{label}"].notna()
                                                       & part[f"ask_usd_{label}"].notna()).mean())
        for notional in cfg.notionals_usd:
            label = notional_label(notional)
            for side in ("buy", "sell"):
                col = part[f"{side}_impact_bp_{label}"]
                row[f"{side}_impact_bp_{label}_p50"] = impact_quantile(col, .5)
                row[f"{side}_impact_bp_{label}_p90"] = impact_quantile(col, .9)
                row[f"{side}_impact_known_share_{label}"] = float(col.notna().mean())
        rows.append(row)
    return pd.DataFrame(rows)


def by_hour(metrics: pd.DataFrame, cfg: AnalysisConfig) -> pd.DataFrame:
    frame = metrics.assign(hour_utc=pd.to_datetime(metrics.received_ms, unit="ms", utc=True).dt.hour)
    depth = [f"{s}_usd_{band_label(b)}" for b in cfg.bands_bp for s in ("bid", "ask")]
    impact = [f"{s}_impact_bp_{notional_label(n)}" for n in cfg.notionals_usd for s in ("buy", "sell")]
    rows = []
    for (key, hour), part in frame.groupby(["stream", "hour_utc"], sort=True):
        row = {"stream": key, "hour_utc": hour, "snapshots": len(part),
               "spread_bp": complete_quantile(part.spread_bp, .5)}
        row.update({col: complete_quantile(part[col], .5) for col in depth})
        row.update({col: impact_quantile(part[col], .5) for col in impact})
        rows.append(row)
    return pd.DataFrame(rows)


CROSS_COLUMNS = ["asset", "metric", "notional", "side", "stream_a", "stream_b", "common_ticks", "a_known_ticks",
                 "b_known_ticks", "a_p50", "b_p50", "diff_p50", "a_better_share"]


def cross_venue(metrics: pd.DataFrame, cfg: AnalysisConfig) -> pd.DataFrame:
    """Pairwise comparison of streams of one asset on ticks where both values are known.

    Pairs keep a sparse stream from shrinking every other comparison.  Mid
    premia exclude aggregated (price-bucketed) streams, whose mid is shifted by
    up to half a bucket.
    """
    rows = []
    for asset, part in metrics.groupby("asset", sort=True):
        keys = sorted(part.stream.unique())
        if len(keys) < 2:
            continue
        exact = set(part.loc[~part.aggregated.astype(bool), "stream"])
        specs = [("mid_premium_bp", "mid", None, None)] + [
            ("impact_bp", f"{side}_impact_bp_{notional_label(n)}", n, side)
            for n in cfg.notionals_usd for side in ("buy", "sell")]
        for metric, column, notional, side in specs:
            table = part.pivot_table(index="tick_ms", columns="stream", values=column, aggfunc="first", dropna=False)
            table = table.reindex(columns=keys)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    if metric == "mid_premium_bp" and not {a, b} <= exact:
                        continue
                    both = table[[a, b]].dropna()
                    row = {"asset": asset, "metric": metric, "notional": notional, "side": side, "stream_a": a,
                           "stream_b": b, "common_ticks": len(both), "a_known_ticks": int(table[a].notna().sum()),
                           "b_known_ticks": int(table[b].notna().sum())}
                    if len(both):
                        diff = ((both[a] / both[b] - 1) * 10_000 if metric == "mid_premium_bp"
                                else both[a] - both[b])
                        row.update(a_p50=float(both[a].median()), b_p50=float(both[b].median()),
                                   diff_p50=float(diff.median()),
                                   a_better_share=np.nan if metric == "mid_premium_bp" else float((diff < 0).mean()))
                    rows.append(row)
    return pd.DataFrame(rows).reindex(columns=CROSS_COLUMNS)


def _day_bootstrap(top: pd.DataFrame, bottom: pd.DataFrame, samples: int, seed: int) -> tuple[float, float]:
    days = np.union1d(top.day.unique(), bottom.day.unique())
    if len(days) < 2:
        return np.nan, np.nan
    index = {d: i for i, d in enumerate(days)}
    def sums(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        idx = frame.day.map(index).to_numpy()
        return (np.bincount(idx, weights=frame.fwd_bp.to_numpy(), minlength=len(days)),
                np.bincount(idx, minlength=len(days)).astype(float))
    ts, tc = sums(top)
    bs, bc = sums(bottom)
    pick = np.random.default_rng(seed).integers(0, len(days), size=(samples, len(days)))
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = ts[pick].sum(1) / tc[pick].sum(1) - bs[pick].sum(1) / bc[pick].sum(1)
    draws = draws[np.isfinite(draws)]
    if not len(draws):
        return np.nan, np.nan
    return float(np.quantile(draws, .025)), float(np.quantile(draws, .975))


def imbalance_forward(metrics: pd.DataFrame, cfg: AnalysisConfig) -> pd.DataFrame:
    """Forward mid change after each snapshot, grouped by fixed (not sample-fitted) imbalance bins."""
    column = f"imbalance_{band_label(cfg.imbalance_band_bp)}"
    if metrics.empty or column not in metrics:
        return pd.DataFrame()
    labels = [f"[{a:g},{b:g})" if b < 1 else f"[{a:g},{b:g}]" for a, b in zip(IMBALANCE_EDGES, IMBALANCE_EDGES[1:])]
    rows = []
    for key, part in metrics.groupby("stream", sort=True):
        # Aligned collection ticks, not receive times, so latency jitter cannot drop matches.
        part = part.sort_values(["tick_ms", "received_ms"]).drop_duplicates("tick_ms")
        ts = part.tick_ms.to_numpy(np.int64)
        mids = part.mid.to_numpy(float)
        gaps = np.diff(ts)
        tolerance = 0.5 * float(np.median(gaps)) if len(gaps) else 0.0
        for horizon in cfg.horizons_sec:
            target = ts + int(horizon * 1000)
            match = np.full(len(ts), -1)
            right = np.searchsorted(ts, target, side="left")
            for candidate in (right, right - 1):  # nearest later snapshot on either side of the target
                idx = np.flatnonzero((candidate > np.arange(len(ts))) & (candidate < len(ts)))
                error = np.abs(ts[candidate[idx]] - target[idx])
                current = np.where(match[idx] >= 0, np.abs(ts[np.maximum(match[idx], 0)] - target[idx]), np.inf)
                better = (error <= tolerance) & (error < current)
                match[idx[better]] = candidate[idx[better]]
            ok = match >= 0
            fwd = np.full(len(ts), np.nan)
            fwd[ok] = (mids[match[ok]] / mids[ok] - 1) * 10_000
            frame = pd.DataFrame({"imbalance": part[column].to_numpy(float), "fwd_bp": fwd,
                                  "day": pd.to_datetime(ts, unit="ms", utc=True).strftime("%Y-%m-%d")}).dropna()
            frame["bin"] = pd.cut(frame.imbalance, IMBALANCE_EDGES, labels=labels, right=False, include_lowest=True)
            frame.loc[frame.imbalance >= 1.0, "bin"] = labels[-1]
            for label in labels:
                cell = frame[frame.bin == label]
                rows.append({"stream": key, "horizon_sec": horizon, "row": "bin", "bin": label, "obs": len(cell),
                             "days": cell.day.nunique(), "mean_fwd_bp": float(cell.fwd_bp.mean()) if len(cell) else np.nan,
                             "median_fwd_bp": float(cell.fwd_bp.median()) if len(cell) else np.nan,
                             "share_up": float((cell.fwd_bp > 0).mean()) if len(cell) else np.nan})
            top, bottom = frame[frame.bin == labels[-1]], frame[frame.bin == labels[0]]
            lo, hi = _day_bootstrap(top, bottom, cfg.bootstrap_samples,
                                    int.from_bytes(hashlib.sha256(f"{cfg.seed}|{key}|{horizon}".encode()).digest()[:4],
                                                   "little"))
            days = frame.day.nunique()
            enough = days >= MIN_DAYS and len(top) >= MIN_BIN_OBS and len(bottom) >= MIN_BIN_OBS
            diff = float(top.fwd_bp.mean() - bottom.fwd_bp.mean()) if len(top) and len(bottom) else np.nan
            status = ("insufficient_sample" if not enough or not np.isfinite(lo) else
                      "ci_above_zero" if lo > 0 else "ci_below_zero" if hi < 0 else "ci_includes_zero")
            rank_corr = float(frame.imbalance.rank().corr(frame.fwd_bp.rank())) if len(frame) > 2 else np.nan
            rows.append({"stream": key, "horizon_sec": horizon, "row": "top_minus_bottom", "bin": None,
                         "obs": len(frame), "days": days, "mean_fwd_bp": diff, "median_fwd_bp": np.nan,
                         "share_up": np.nan, "ci_low_bp": lo, "ci_high_bp": hi, "rank_corr": rank_corr,
                         "status": status})
    return pd.DataFrame(rows, columns=["stream", "horizon_sec", "row", "bin", "obs", "days", "mean_fwd_bp",
                                       "median_fwd_bp", "share_up", "ci_low_bp", "ci_high_bp", "rank_corr", "status"])


def wall_summary(walls: pd.DataFrame) -> pd.DataFrame:
    columns = ["stream", "side", "end_reason", "episodes", "multi_snapshot_episodes", "duration_sec_p50",
               "max_usd_p50", "min_distance_bp_p50"]
    if walls.empty:
        return pd.DataFrame(columns=columns)
    frame = walls.assign(multi=walls.snapshots.ge(2))
    grouped = frame.groupby(["stream", "side", "end_reason"], sort=True)
    return grouped.agg(episodes=("px", "size"), multi_snapshot_episodes=("multi", "sum"),
                       duration_sec_p50=("duration_sec", "median"), max_usd_p50=("max_usd", "median"),
                       min_distance_bp_p50=("min_distance_bp", "median")).reset_index().reindex(columns=columns)


# ------------------------------------------------------------------ run


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(dbs: Sequence[Path], output_dir: Path, cfg: AnalysisConfig, *, streams: set[str] | None = None,
        start_ms: int | None = None, end_ms: int | None = None) -> dict:
    cfg.validate()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("--output-dir must be new or empty")
    if len({db.expanduser().resolve() for db in dbs}) != len(dbs):
        raise ValueError("each --db may be given once")
    metrics, walls, inputs = load_metrics(dbs, cfg, streams=streams, start_ms=start_ms, end_ms=end_ms)
    if metrics.empty:
        raise ValueError("no snapshots matched the selection")
    outputs = {"book_metrics.csv": metrics, "book_summary.csv": summarize(metrics, cfg),
               "book_by_hour.csv": by_hour(metrics, cfg), "book_cross_venue.csv": cross_venue(metrics, cfg),
               "book_walls.csv": walls, "book_wall_summary.csv": wall_summary(walls),
               "book_imbalance.csv": imbalance_forward(metrics, cfg)}
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in outputs.items():
        frame.to_csv(output_dir / name, index=False)
    manifest = {"format": 1, "tool": "orderbook/analyze.py",
                "code_sha256": {name: _sha256(PACKAGE_DIR / name) for name in ("book.py", "store.py", "analyze.py")},
                "config": asdict(cfg), "selection": {"streams": sorted(streams) if streams else None,
                                                     "start_ms": start_ms, "end_ms": end_ms},
                "inputs": inputs, "snapshots": len(metrics), "wall_episodes": len(walls),
                "quantile_rules": {"impact": "unknown (insufficient visible depth) ranks above every known value",
                                   "depth_spread": "reported only when every snapshot in the group is known"},
                "interpretation": ("Descriptive statistics of public REST snapshots. Depth beyond coverage is unknown; "
                                   "snapshot spacing hides fills and cancels between observations; imbalance bins "
                                   "are fixed in advance but any edge is not a tested strategy."),
                "output_sha256": {name: _sha256(output_dir / name) for name in outputs}}
    (output_dir / "book_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    return {"snapshots": len(metrics), "streams": int(metrics.stream.nunique()), "wall_episodes": len(walls),
            "output_dir": str(output_dir)}


def _floats(text: str) -> tuple:
    return tuple(float(x) for x in text.split(",") if x.strip())


def _time(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if text.isdigit() and len(text) >= 12:
        return int(text)
    stamp = pd.Timestamp(text)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return int(stamp.value // 1_000_000)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyse stored order-book snapshots.")
    p.add_argument("--db", type=Path, action="append", required=True, help="snapshot database (repeatable)")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--streams", help="comma list of stream keys, e.g. hyperliquid:perp:BTC,binance:perp:BTCUSDT")
    p.add_argument("--start")
    p.add_argument("--end")
    defaults = AnalysisConfig()
    p.add_argument("--bands-bp", default=",".join(f"{x:g}" for x in defaults.bands_bp))
    p.add_argument("--notionals-usd", default=",".join(f"{x:g}" for x in defaults.notionals_usd))
    p.add_argument("--wall-band-bp", type=float, default=defaults.wall_band_bp)
    p.add_argument("--wall-multiple", type=float, default=defaults.wall_multiple)
    p.add_argument("--wall-min-usd", type=float, default=defaults.wall_min_usd)
    p.add_argument("--wall-min-share", type=float, default=defaults.wall_min_share)
    p.add_argument("--wall-approach-bp", type=float, default=defaults.wall_approach_bp)
    p.add_argument("--imbalance-band-bp", type=float, default=defaults.imbalance_band_bp)
    p.add_argument("--horizons-sec", default=",".join(f"{x:g}" for x in defaults.horizons_sec))
    p.add_argument("--bootstrap-samples", type=int, default=defaults.bootstrap_samples)
    p.add_argument("--seed", type=int, default=defaults.seed)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = AnalysisConfig(bands_bp=_floats(args.bands_bp), notionals_usd=_floats(args.notionals_usd),
                             wall_band_bp=args.wall_band_bp, wall_multiple=args.wall_multiple,
                             wall_min_usd=args.wall_min_usd, wall_min_share=args.wall_min_share,
                             wall_approach_bp=args.wall_approach_bp,
                             imbalance_band_bp=args.imbalance_band_bp, horizons_sec=_floats(args.horizons_sec),
                             bootstrap_samples=args.bootstrap_samples, seed=args.seed)
        streams = {x.strip() for x in args.streams.split(",") if x.strip()} if args.streams else None
        result = run(args.db, args.output_dir, cfg, streams=streams, start_ms=_time(args.start), end_ms=_time(args.end))
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        raise SystemExit(f"error: {exc}")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
