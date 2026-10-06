#!/usr/bin/env python3
"""Long-running supervisor for the order-book collector (standard library only).

Runs bounded ``collect`` chunks back to back into one database per UTC month
(``<data-dir>/orderbook-YYYY-MM.db``) or, with ``--db-period day``, per UTC day
(``orderbook-YYYY-MM-DD.db``, convenient for incremental uploads).  A chunk
never writes past a period boundary.  After a chunk stops it waits according to the stop reason: briefly
for a lost VPN path, longer after a rate limit.  A storage limit or the
``--until`` time ends the supervisor; a transport that cannot be set up (for
example the VPN interface is down) is retried later and never replaced by the
default route.  SIGTERM ends the current chunk as ``interrupted``.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from orderbook.collect import CaptureConfig, Stream, Transport, TransportError, collect

DB_FORMATS = {"month": "orderbook-%Y-%m.db", "day": "orderbook-%Y-%m-%d.db"}
TERMINAL_REASONS = ("db_budget_reached", "free_space_floor_reached")
BACKOFF_SEC = {"transport_unavailable": 120, "rate_limited": 1_800, "all_streams_failing": 600, "error": 300}


def period_db(data_dir: Path, now: float, period: str = "month") -> Path:
    return data_dir / datetime.fromtimestamp(now, timezone.utc).strftime(DB_FORMATS[period])


def next_period_start(now: float, period: str = "month") -> float:
    current = datetime.fromtimestamp(now, timezone.utc)
    if period == "day":
        start = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
        return start.timestamp() + 86_400
    year, month = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def chunk_minutes(now: float, chunk_min: float, until: float | None, period: str = "month") -> float:
    """Minutes for the next chunk: capped by the chunk size, the period boundary and ``until``."""
    end = min(now + chunk_min * 60, next_period_start(now, period), until if until is not None else float("inf"))
    return max(0.0, (end - now) / 60)


def backoff_for(stop_reason: str) -> float:
    kind = stop_reason.split(":", 1)[0]
    if kind in ("completed", "interrupted"):
        return 0.0
    return float(BACKOFF_SEC.get(kind, BACKOFF_SEC["error"]))


class Stop(Exception):
    pass


def supervise(*, data_dir: Path, streams: Sequence[Stream], cfg: CaptureConfig, interval_sec: float,
              chunk_min: float, until: float | None, max_db_mb: float, min_free_gb: float,
              make_transport: Callable[[], Transport], run_chunk: Callable = collect, period: str = "month",
              clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
              log: Callable[[dict], None] = lambda event: print(json.dumps(event), flush=True)) -> str:
    """Loop until ``until``, a storage limit or an interrupt; return the final reason."""
    while True:
        now = clock()
        minutes = chunk_minutes(now, chunk_min, until, period)
        if minutes * 60 < interval_sec:
            if until is not None and now + interval_sec >= until:
                log({"event": "finished", "reason": "until_reached"})
                return "until_reached"
            # Too close to a period boundary for one tick: wait for the next database.
            sleep(max(1.0, next_period_start(now, period) - now))
            continue
        db = period_db(data_dir, now, period)
        try:
            transport = make_transport()
        except TransportError as exc:
            log({"event": "transport_unavailable", "error": str(exc), "retry_in_sec": BACKOFF_SEC["transport_unavailable"]})
            sleep(BACKOFF_SEC["transport_unavailable"])
            continue
        try:
            result = run_chunk(db, streams, cfg, transport.request, interval_sec=interval_sec, count=None,
                               duration_min=minutes, max_db_mb=max_db_mb, min_free_gb=min_free_gb,
                               transport_info=transport.describe(), check_transport=transport.check_path)
            reason = result["stop_reason"]
        except Exception as exc:  # recorded by collect(); keep supervising
            result, reason = {}, f"error: {type(exc).__name__}: {exc}"
        log({"event": "chunk_end", "db": str(db), "stop_reason": reason,
             **{k: result.get(k) for k in ("run_id", "ticks", "snapshots", "errors", "skipped_ticks")}})
        if reason == "interrupted":
            log({"event": "finished", "reason": "interrupted"})
            return "interrupted"
        if reason.split(":", 1)[0] in TERMINAL_REASONS:
            log({"event": "finished", "reason": reason})
            return reason
        wait = backoff_for(reason)
        if wait:
            sleep(wait)


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt  # collect() records the chunk as interrupted


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the order-book collector continuously in monthly databases.")
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--streams", required=True)
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--chunk-min", type=float, default=360.0, help="length of each bounded collection run")
    p.add_argument("--until", help="UTC time at which to stop, e.g. 2026-11-06T00:00:00Z")
    p.add_argument("--band-bp", type=float, default=100.0)
    p.add_argument("--max-levels", type=int, default=0)
    p.add_argument("--binance-limit", type=int, default=1000)
    p.add_argument("--bybit-limit", type=int, default=500)
    p.add_argument("--max-db-mb", type=float, default=5_000.0)
    p.add_argument("--min-free-gb", type=float, default=50.0)
    p.add_argument("--db-period", choices=sorted(DB_FORMATS), default="month")
    route = p.add_mutually_exclusive_group(required=True)
    route.add_argument("--source-interface", help="bind to this interface (e.g. a VPN); requires --dns-server")
    route.add_argument("--allow-default-route", action="store_true",
                       help="explicitly use the default route, e.g. on a cloud VM whose address is not the user's")
    p.add_argument("--dns-server")
    p.add_argument("--timeout-sec", type=float, default=10.0)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        streams = [Stream.parse(x) for x in args.streams.split(",") if x.strip()]
        if not streams or len({s.key for s in streams}) != len(streams):
            raise ValueError("streams must be non-empty and unique")
        cfg = CaptureConfig(args.binance_limit, args.bybit_limit, args.band_bp, args.max_levels)
        cfg.validate()
        if not 5 <= args.interval_sec <= 86_400 or args.chunk_min * 60 < args.interval_sec:
            raise ValueError("--interval-sec must be in [5, 86400] and fit within --chunk-min")
        until = None
        if args.until:
            stamp = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
            until = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
        if args.source_interface and not args.dns_server:
            raise ValueError("--source-interface requires --dns-server")
        if args.allow_default_route and args.dns_server:
            raise ValueError("--dns-server applies only with --source-interface")
        if not args.data_dir.is_dir():
            raise ValueError(f"--data-dir does not exist: {args.data_dir}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _sigterm)

    def make_transport() -> Transport:
        return Transport(source_interface=args.source_interface, dns_server=args.dns_server,
                         allow_default_route=args.allow_default_route, timeout=args.timeout_sec)
    try:
        reason = supervise(data_dir=args.data_dir, streams=streams, cfg=cfg, interval_sec=args.interval_sec,
                           chunk_min=args.chunk_min, until=until, max_db_mb=args.max_db_mb,
                           min_free_gb=args.min_free_gb, make_transport=make_transport, period=args.db_period)
    except KeyboardInterrupt:  # stopped while waiting between chunks
        return 0
    # Storage limits need a decision; exit non-zero so the service shows as failed instead of silently idle.
    return 3 if reason in TERMINAL_REASONS else 0


if __name__ == "__main__":
    raise SystemExit(main())
