#!/usr/bin/env python3
"""Bounded order-book snapshot collector (standard library only).

Public REST order books from Hyperliquid, Binance and Bybit are captured on an
aligned clock so venues can be compared tick by tick.  Every run is bounded
(``--count`` or ``--duration-min``), checks a storage budget before each tick,
and stops instead of retrying when a venue rate-limits it.

Network privacy: with ``--source-interface`` every HTTPS socket (and, with
``--dns-server``, every DNS query) is bound to that interface's IPv4 address
and each destination route is checked to leave through it.  If the interface
disappears or changes address the run stops; it never falls back to the
default route.  ``--allow-default-route`` must be passed explicitly to run
without this guard.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import math
import random
import shutil
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlencode

from orderbook.book import make_book, parse_levels, side_coverage_bp, truncate
from orderbook.store import PAYLOAD_FORMAT, connect_reader, connect_writer, database_bytes, encode_levels

PACKAGE_DIR = Path(__file__).resolve().parent
VENUES = {"hyperliquid": ("perp", "spot"), "binance": ("perp", "spot"), "bybit": ("perp", "spot")}
HL_LEVELS = 20
MAX_FAILED_TICKS = 3
BINANCE_PERP_LIMITS = (5, 10, 20, 50, 100, 500, 1000)
MAX_RESPONSE_BYTES = 8 << 20
USER_AGENT = "crypto-analytics-orderbook/1"


class TransportError(RuntimeError):
    """The protected network path is unavailable; the run must stop."""


class RateLimited(RuntimeError):
    """A venue asked us to back off; the run stops rather than risk a ban."""


@dataclass(frozen=True)
class Stream:
    venue: str
    market: str
    symbol: str
    aggregation: str = ""

    @classmethod
    def parse(cls, text: str) -> "Stream":
        parts = text.strip().split(":")
        if len(parts) not in (3, 4) or not all(parts):
            raise ValueError(f"stream {text!r} must be venue:market:symbol[:hl_sig_figs]")
        venue, market, symbol = parts[0].lower(), parts[1].lower(), parts[2]
        if venue not in VENUES or market not in VENUES[venue]:
            raise ValueError(f"unsupported stream {text!r}")
        aggregation = parts[3] if len(parts) == 4 else ""
        if aggregation and (venue != "hyperliquid" or aggregation not in {"2", "3", "4", "5"}):
            raise ValueError("aggregation is Hyperliquid nSigFigs 2-5 only")
        if venue != "hyperliquid":
            symbol = symbol.upper()
        return cls(venue, market, symbol, aggregation)

    @property
    def key(self) -> str:
        return ":".join(x for x in (self.venue, self.market, self.symbol, self.aggregation) if x)


@dataclass(frozen=True)
class CaptureConfig:
    binance_limit: int = 1000
    bybit_limit: int = 500
    band_bp: float = 100.0
    max_levels: int = 0

    def validate(self) -> None:
        if self.binance_limit not in BINANCE_PERP_LIMITS:
            raise ValueError(f"--binance-limit must be one of {BINANCE_PERP_LIMITS}")
        if not 1 <= self.bybit_limit <= 500:
            raise ValueError("--bybit-limit must be in [1, 500]")
        if not math.isfinite(self.band_bp) or self.band_bp < 0 or self.max_levels < 0:
            raise ValueError("band and max levels must be non-negative")


def request_for(stream: Stream, cfg: CaptureConfig) -> tuple[str, str, str, dict | None, int]:
    """Return method, host, path, JSON body and the requested levels per side."""
    if stream.venue == "hyperliquid":
        body: dict = {"type": "l2Book", "coin": stream.symbol}
        if stream.aggregation:
            body["nSigFigs"] = int(stream.aggregation)
        return "POST", "api.hyperliquid.xyz", "/info", body, HL_LEVELS
    if stream.venue == "binance":
        host, path = (("fapi.binance.com", "/fapi/v1/depth") if stream.market == "perp"
                      else ("api.binance.com", "/api/v3/depth"))
        limit = cfg.binance_limit
        return "GET", host, f"{path}?{urlencode({'symbol': stream.symbol, 'limit': limit})}", None, limit
    category = "linear" if stream.market == "perp" else "spot"
    limit = cfg.bybit_limit if category == "linear" else min(cfg.bybit_limit, 200)
    query = urlencode({"category": category, "symbol": stream.symbol, "limit": limit})
    return "GET", "api.bybit.com", f"/v5/market/orderbook?{query}", None, limit


def parse_response(stream: Stream, obj: object) -> tuple[list, list, int | None]:
    """Extract raw bid/ask levels and the venue's own timestamp, if it supplies one."""
    if stream.venue == "hyperliquid":
        if not isinstance(obj, dict) or "levels" not in obj:
            raise ValueError("unexpected Hyperliquid response")
        bids, asks = obj["levels"]
        return bids, asks, int(obj["time"]) if obj.get("time") is not None else None
    if stream.venue == "binance":
        if not isinstance(obj, dict) or "bids" not in obj:
            raise ValueError(f"unexpected Binance response: {str(obj)[:200]}")
        stamp = obj.get("T", obj.get("E"))
        return obj["bids"], obj["asks"], int(stamp) if stamp is not None else None
    if not isinstance(obj, dict) or obj.get("retCode") != 0:
        raise ValueError(f"Bybit error: {str(obj)[:200]}")
    result = obj["result"]
    stamp = result.get("cts") or result.get("ts")
    return result["b"], result["a"], int(stamp) if stamp is not None else None


# ------------------------------------------------------------------- transport


def interface_ipv4(name: str) -> str:
    """IPv4 address of a local interface (Linux SIOCGIFADDR)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
    except OSError as exc:
        raise TransportError(f"interface {name!r} has no IPv4 address") from exc
    finally:
        probe.close()
    return socket.inet_ntoa(packed[20:24])


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        length = data[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + length


def dns_query(name: str) -> tuple[int, bytes]:
    qid = random.randrange(65_536)
    question = b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"
    return qid, struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", 1, 1)


def parse_dns_a(data: bytes, qid: int) -> list[str]:
    """IPv4 answers of a DNS response; any malformed, mismatched or failed reply is a TransportError."""
    try:
        rid, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
        if rid != qid or not flags & 0x8000:
            raise TransportError("unexpected DNS response")
        if flags & 0x0200:
            raise TransportError("truncated DNS response")
        if flags & 0x000F:
            raise TransportError(f"DNS error code {flags & 0x000F}")
        pos = 12
        for _ in range(qdcount):
            pos = _skip_name(data, pos) + 4
        addresses = []
        for _ in range(ancount):
            pos = _skip_name(data, pos)
            rtype, _cls, _ttl, length = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            if pos + length > len(data):
                raise TransportError("DNS answer exceeds packet")
            if rtype == 1 and length == 4:
                addresses.append(socket.inet_ntoa(data[pos:pos + 4]))
            pos += length
    except (struct.error, IndexError, ValueError) as exc:
        raise TransportError(f"malformed DNS response: {exc}") from exc
    if not addresses:
        raise TransportError("no IPv4 address in DNS response")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-resolved IPv4 address over a socket prepared by ``Transport``;
    SNI and certificate checks still use the host name."""

    def __init__(self, host: str, ip: str, *, make_socket: Callable[[int], socket.socket], timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._pinned_ip = ip
        self._make_socket = make_socket
        self._pinned_context = context

    def connect(self) -> None:
        sock = self._make_socket(socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self._pinned_ip, self.port))
        except OSError:
            sock.close()
            raise
        self.sock = self._pinned_context.wrap_socket(sock, server_hostname=self.host)


class Transport:
    """IPv4 HTTPS client that, with ``source_interface``, cannot leave through another interface.

    Every socket is bound to the interface device (``SO_BINDTODEVICE``) when the
    kernel allows it, and to the interface's address in any case.  Routes to every
    destination and the DNS server are re-verified before each tick
    (``check_path``).  DNS uses ``dns_server`` from the same socket binding; the
    system resolver is only used when ``system_dns`` is passed explicitly.
    """

    def __init__(self, *, source_interface: str | None, dns_server: str | None,
                 allow_default_route: bool, system_dns: bool = False, timeout: float = 10.0):
        if not source_interface and not allow_default_route:
            raise ValueError("--source-interface is required (or pass --allow-default-route explicitly)")
        if dns_server and not source_interface:
            raise ValueError("--dns-server requires --source-interface")
        if source_interface and not dns_server and not system_dns:
            raise ValueError("--dns-server is required with --source-interface (or pass --system-dns explicitly)")
        self.source_interface = source_interface
        self.dns_server = dns_server
        self.allow_default_route = allow_default_route
        self.system_dns = system_dns
        self.timeout = timeout
        self.source_ip = interface_ipv4(source_interface) if source_interface else None
        self.device_bound = False
        if source_interface:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, source_interface.encode() + b"\0")
                self.device_bound = True
            except OSError:
                self.device_bound = False  # older kernels need CAP_NET_RAW; route checks still apply
            finally:
                probe.close()
        self.context = ssl.create_default_context()
        self._dns_cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        if self.dns_server:
            self._verify_route(self.dns_server)

    def describe(self) -> dict:
        return {"source_interface": self.source_interface, "source_ip": self.source_ip,
                "device_bound": self.device_bound,
                "dns": self.dns_server or "system_resolver",
                "allow_default_route": self.allow_default_route, "ip_version": 4}

    def make_socket(self, kind: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, kind)
        try:
            if self.device_bound:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.source_interface.encode() + b"\0")
            if self.source_ip:
                sock.bind((self.source_ip, 0))
        except OSError as exc:
            sock.close()
            raise TransportError(f"cannot bind to {self.source_interface}: {exc}") from exc
        return sock

    def _verify_route(self, ip: str) -> None:
        if not self.source_interface:
            return
        try:
            out = subprocess.run(["ip", "-4", "route", "get", ip, "from", self.source_ip],
                                 capture_output=True, text=True, timeout=5, check=True).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransportError(f"cannot verify route to {ip}") from exc
        if f" dev {self.source_interface} " not in f" {out.splitlines()[0] if out else ''} ":
            raise TransportError(f"route to {ip} does not use {self.source_interface}: {out.strip()}")

    def check_path(self) -> str:
        """Empty when the interface keeps its address and every known route still uses it."""
        if not self.source_interface:
            return ""
        try:
            current = interface_ipv4(self.source_interface)
        except TransportError as exc:
            return str(exc)
        if current != self.source_ip:
            return f"interface address changed to {current}"
        with self._lock:  # only addresses still in use, so the check stays flat as CDN addresses rotate
            ips = {ip for _, ip in self._dns_cache.values()} | ({self.dns_server} if self.dns_server else set())
            try:
                for ip in sorted(ips):
                    self._verify_route(ip)
            except TransportError as exc:
                return str(exc)
        return ""

    def _udp_lookup(self, host: str, attempts: int = 3) -> str:
        """A-record lookup sent from the bound socket; UDP loss is retried, never rerouted."""
        error: Exception | None = None
        for _ in range(attempts):
            qid, query = dns_query(host)
            sock = self.make_socket(socket.SOCK_DGRAM)
            try:
                sock.settimeout(2.0)
                sock.sendto(query, (self.dns_server, 53))
                while True:
                    data, sender = sock.recvfrom(4096)
                    if sender[0] == self.dns_server:
                        break
                return parse_dns_a(data, qid)[0]
            except (OSError, TransportError) as exc:
                error = exc
            finally:
                sock.close()
        raise TransportError(f"DNS lookup of {host} failed: {error}")

    def resolve(self, host: str) -> str:
        with self._lock:  # single flight per process: one lookup and route check per host
            cached = self._dns_cache.get(host)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            if self.dns_server:
                ip = self._udp_lookup(host)
            else:
                try:
                    ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
                except OSError as exc:
                    raise TransportError(f"DNS lookup of {host} failed: {exc}") from exc
            self._verify_route(ip)
            self._dns_cache[host] = (time.monotonic() + 600, ip)
            return ip

    def request(self, method: str, host: str, path: str, body: dict | None = None) -> object:
        ip = self.resolve(host)
        conn = _PinnedHTTPSConnection(host, ip, make_socket=self.make_socket, timeout=self.timeout,
                                      context=self.context)
        try:
            data = None if body is None else json.dumps(body).encode()
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            if self.check_path():
                raise TransportError(f"protected path lost during request to {host}") from exc
            raise
        finally:
            conn.close()
        # Bybit signals IP rate limits with 403 and Binance uses it for WAF blocks; never keep hammering.
        if response.status in (403, 418, 429):
            raise RateLimited(f"{host} returned HTTP {response.status}")
        if response.status != 200:
            raise RuntimeError(f"{host} returned HTTP {response.status}: {raw[:200]!r}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError(f"{host} response exceeds {MAX_RESPONSE_BYTES} bytes")
        return json.loads(raw)


# --------------------------------------------------------------------- capture


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def capture(stream: Stream, fetch: Callable[[str, str, str, dict | None], object], cfg: CaptureConfig,
            clock: Callable[[], int] = now_ms) -> dict:
    """Fetch, validate and truncate one book; return a snapshot row without ids."""
    method, host, path, body, limit = request_for(stream, cfg)
    requested = clock()
    obj = fetch(method, host, path, body)
    received = clock()
    raw_bids, raw_asks, exchange_ms = parse_response(stream, obj)
    bids = parse_levels(raw_bids, descending=True)
    asks = parse_levels(raw_asks, descending=False)
    if not bids or not asks:
        raise ValueError("empty book side")
    if bids[0][0] >= asks[0][0]:
        raise ValueError("crossed or locked book")
    mid = (bids[0][0] + asks[0][0]) / 2
    if cfg.band_bp > 0 and (asks[0][0] - bids[0][0]) / mid * 5_000 > cfg.band_bp:
        raise ValueError("half spread exceeds the storage band")
    kept_bids, bid_cov = truncate(bids, mid, side_coverage_bp(bids, mid, exhausted=len(bids) < limit),
                                  band_bp=cfg.band_bp, max_levels=cfg.max_levels)
    kept_asks, ask_cov = truncate(asks, mid, side_coverage_bp(asks, mid, exhausted=len(asks) < limit),
                                  band_bp=cfg.band_bp, max_levels=cfg.max_levels)
    book = make_book(kept_bids, kept_asks, bid_coverage_bp=bid_cov, ask_coverage_bp=ask_cov)
    payload = encode_levels(book)
    return {"venue": stream.venue, "market": stream.market, "symbol": stream.symbol,
            "aggregation": stream.aggregation, "requested_ms": requested, "received_ms": received,
            "exchange_ms": exchange_ms, "request_limit": limit, "raw_bid_levels": len(bids),
            "raw_ask_levels": len(asks), "best_bid": bids[0][0], "best_ask": asks[0][0],
            "bid_coverage_bp": bid_cov, "ask_coverage_bp": ask_cov, "payload_format": PAYLOAD_FORMAT,
            "payload": payload, "payload_bytes": len(payload)}


SNAPSHOT_COLUMNS = ("run_id", "tick_ms", "venue", "market", "symbol", "aggregation", "requested_ms", "received_ms",
                    "exchange_ms", "request_limit", "raw_bid_levels", "raw_ask_levels", "best_bid", "best_ask",
                    "bid_coverage_bp", "ask_coverage_bp", "payload_format", "payload", "payload_bytes")


def code_sha256() -> str:
    digest = hashlib.sha256()
    for name in ("book.py", "store.py", "collect.py"):
        digest.update((PACKAGE_DIR / name).read_bytes())
    return digest.hexdigest()


def storage_guard(db: Path, max_db_mb: float, min_free_gb: float) -> str:
    if database_bytes(db) >= max_db_mb * 1e6:
        return "db_budget_reached"
    if shutil.disk_usage(db.parent).free < min_free_gb * 1e9:
        return "free_space_floor_reached"
    return ""


def collect(db: Path, streams: Sequence[Stream], cfg: CaptureConfig, fetch, *, interval_sec: float,
            count: int | None, duration_min: float | None, max_db_mb: float, min_free_gb: float,
            transport_info: dict, check_transport: Callable[[], str] = lambda: "",
            clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Capture aligned ticks until the count/duration bound or a stop condition."""
    conn = connect_writer(db)
    config = {"streams": [s.key for s in streams], "capture": asdict(cfg), "interval_sec": interval_sec,
              "count": count, "duration_min": duration_min, "max_db_mb": max_db_mb, "min_free_gb": min_free_gb,
              "transport": transport_info}
    started = clock()
    run_id = conn.execute("INSERT INTO runs (started_ms, config_json, collector_sha256) VALUES (?, ?, ?)",
                          (int(started * 1000), json.dumps(config, sort_keys=True), code_sha256())).lastrowid
    conn.commit()
    deadline = None if duration_min is None else started + duration_min * 60
    next_tick = math.ceil(started / interval_sec) * interval_sec
    ticks = snapshots = errors = skipped = failed_ticks = 0
    stop_reason = "completed"
    try:
        with ThreadPoolExecutor(max_workers=len(streams)) as pool:
            while True:
                if count is not None and ticks >= count:
                    break
                if deadline is not None and next_tick > deadline:
                    break
                reason = storage_guard(db, max_db_mb, min_free_gb)
                if not reason:
                    path_problem = check_transport()
                    reason = f"transport_unavailable: {path_problem}" if path_problem else ""
                if reason:
                    stop_reason = reason
                    break
                delay = next_tick - clock()
                if delay > 0:
                    sleep(min(delay, interval_sec))  # a backward clock step cannot stall the run
                tick_ms = int(round(next_tick * 1000))

                def one(stream: Stream):
                    try:  # one clock for tick labels and request/receive times
                        return stream, capture(stream, fetch, cfg, clock=lambda: int(clock() * 1000)), None
                    except (TransportError, RateLimited) as exc:
                        return stream, None, exc
                    except Exception as exc:  # recorded per stream; other streams continue
                        return stream, None, exc

                results = list(pool.map(one, streams))
                fatal = next((exc for _, _, exc in results if isinstance(exc, (TransportError, RateLimited))), None)
                ok = sum(row is not None for _, row, _ in results)
                with conn:
                    for stream, row, exc in results:
                        if row is not None:
                            values = {"run_id": run_id, "tick_ms": tick_ms, **row}
                            conn.execute(f"INSERT INTO snapshots ({', '.join(SNAPSHOT_COLUMNS)}) VALUES "
                                         f"({', '.join('?' * len(SNAPSHOT_COLUMNS))})",
                                         [values[c] for c in SNAPSHOT_COLUMNS])
                        else:
                            conn.execute("INSERT INTO fetch_errors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         (run_id, tick_ms, stream.venue, stream.market, stream.symbol,
                                          stream.aggregation, int(clock() * 1000), f"{type(exc).__name__}: {exc}"))
                    conn.execute("UPDATE runs SET snapshots=?, errors=? WHERE run_id=?",
                                 (snapshots + ok, errors + len(results) - ok, run_id))
                # Counters advance only after the tick's transaction has committed.
                snapshots, errors = snapshots + ok, errors + len(results) - ok
                ticks += 1
                if fatal is not None:
                    kind = "transport_unavailable" if isinstance(fatal, TransportError) else "rate_limited"
                    stop_reason = f"{kind}: {fatal}"[:500]
                    break
                failed_ticks = failed_ticks + 1 if ok == 0 else 0
                if failed_ticks >= MAX_FAILED_TICKS:
                    stop_reason = "all_streams_failing"
                    break
                next_tick += interval_sec
                behind = clock() - next_tick
                if behind > 0:  # never burst to catch up; skip missed ticks
                    missed = math.floor(behind / interval_sec) + 1
                    skipped += missed
                    next_tick += missed * interval_sec
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    except Exception as exc:
        stop_reason = f"error: {type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        with conn:
            conn.execute("UPDATE runs SET ended_ms=?, stop_reason=?, snapshots=?, errors=? WHERE run_id=?",
                         (int(clock() * 1000), stop_reason, snapshots, errors, run_id))
        conn.close()
    return {"run_id": run_id, "ticks": ticks, "snapshots": snapshots, "errors": errors,
            "skipped_ticks": skipped, "stop_reason": stop_reason, "db": str(db), "db_mb": database_bytes(db) / 1e6}


def estimate(streams: Sequence[Stream], cfg: CaptureConfig, fetch, *, interval_sec: float,
             db: Path | None, max_db_mb: float, min_free_gb: float) -> dict:
    """Capture each stream once (nothing is written) and project storage use."""
    with ThreadPoolExecutor(max_workers=len(streams)) as pool:
        def one(stream: Stream):
            try:
                return stream, capture(stream, fetch, cfg), None
            except Exception as exc:
                return stream, None, exc
        results = list(pool.map(one, streams))
    rows, fatal = [], None
    per_tick = 0
    for stream, row, exc in results:
        if row is None:
            rows.append({"stream": stream.key, "error": f"{type(exc).__name__}: {exc}"})
            fatal = fatal or (exc if isinstance(exc, (TransportError, RateLimited)) else None)
            continue
        # ~180 bytes of header columns and index entries per row on top of the payload.
        size = row["payload_bytes"] + 180
        per_tick += size
        rows.append({"stream": stream.key, "bytes_per_snapshot": size, "payload_bytes": row["payload_bytes"],
                     "raw_levels": [row["raw_bid_levels"], row["raw_ask_levels"]],
                     "coverage_bp": [row["bid_coverage_bp"], row["ask_coverage_bp"]],
                     "spread_bp": (row["best_ask"] - row["best_bid"]) / ((row["best_ask"] + row["best_bid"]) / 2) * 1e4})
    per_day_mb = per_tick * 86_400 / interval_sec / 1e6
    target = db.parent if db is not None else Path.cwd()
    free_gb = shutil.disk_usage(target if target.exists() else Path.cwd()).free / 1e9
    used_mb = database_bytes(db) / 1e6 if db is not None and db.exists() else 0.0
    def days(limit_mb: float) -> float | None:
        return round(max(0.0, limit_mb) / per_day_mb, 1) if per_day_mb > 0 else None
    return {"interval_sec": interval_sec, "streams": rows, "projected_mb_per_day": round(per_day_mb, 2),
            "projected_gb_per_30d": round(per_day_mb * 30 / 1e3, 2), "disk": str(target), "free_gb": round(free_gb, 1),
            "db_mb": round(used_mb, 2), "days_until_db_budget": days(max_db_mb - used_mb),
            "days_until_free_floor": days((free_gb - min_free_gb) * 1e3),
            "transport_error": None if fatal is None else str(fatal)}


def backup(db: Path, output: Path) -> dict:
    """Consistent single-file copy of a (possibly live, WAL-mode) database via SQLite's backup API."""
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    partial.unlink(missing_ok=True)
    with closing(connect_reader(db)) as source, closing(sqlite3.connect(partial)) as target:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        rows = target.execute("SELECT COUNT(*), COALESCE(MAX(received_ms), 0) FROM snapshots").fetchone()
        check = target.execute("PRAGMA integrity_check").fetchone()[0]
    if check != "ok":
        partial.unlink(missing_ok=True)
        raise ValueError(f"integrity check failed: {check}")
    partial.replace(output)
    return {"db": str(db), "output": str(output), "snapshots": rows[0], "last_received_ms": rows[1],
            "bytes": output.stat().st_size}


def status(db: Path) -> dict:
    with closing(connect_reader(db)) as conn:
        streams = [dict(zip(("venue", "market", "symbol", "aggregation", "snapshots", "first_ms", "last_ms",
                             "payload_mb", "avg_payload_bytes"), row)) for row in conn.execute(
            "SELECT venue, market, symbol, aggregation, COUNT(*), MIN(received_ms), MAX(received_ms), "
            "SUM(payload_bytes)/1e6, AVG(payload_bytes) FROM snapshots GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4")]
        runs = [dict(zip(("run_id", "started_ms", "ended_ms", "stop_reason", "snapshots", "errors"), row))
                for row in conn.execute("SELECT run_id, started_ms, ended_ms, stop_reason, snapshots, errors "
                                        "FROM runs ORDER BY run_id DESC LIMIT 10")]
        # SQLite returns the bare ``error`` column from the row holding MAX(at_ms).
        errors = [dict(zip(("venue", "market", "symbol", "aggregation", "count", "last_error", "last_ms"), row))
                  for row in conn.execute("SELECT venue, market, symbol, aggregation, COUNT(*), error, MAX(at_ms) "
                                          "FROM fetch_errors GROUP BY 1, 2, 3, 4")]
    return {"db": str(db), "db_mb": round(database_bytes(db) / 1e6, 2),
            "free_gb": round(shutil.disk_usage(db.parent).free / 1e9, 1), "streams": streams,
            "recent_runs": runs, "errors": errors}


# ------------------------------------------------------------------------- CLI


def _add_capture_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--streams", required=True,
                   help="comma list of venue:market:symbol[:hl_sig_figs], e.g. hyperliquid:perp:BTC,binance:perp:BTCUSDT")
    p.add_argument("--interval-sec", type=float, default=60.0)
    p.add_argument("--band-bp", type=float, default=100.0, help="store levels within this distance of mid; 0 keeps all")
    p.add_argument("--max-levels", type=int, default=0, help="cap stored levels per side; 0 means no cap")
    p.add_argument("--binance-limit", type=int, default=1000)
    p.add_argument("--bybit-limit", type=int, default=500)
    p.add_argument("--max-db-mb", type=float, default=5_000.0)
    p.add_argument("--min-free-gb", type=float, default=50.0)
    p.add_argument("--source-interface", help="bind all traffic to this interface's IPv4 address (e.g. the VPN)")
    p.add_argument("--dns-server", help="resolve through this DNS server from the bound address")
    p.add_argument("--system-dns", action="store_true",
                   help="explicitly allow the system resolver together with --source-interface")
    p.add_argument("--allow-default-route", action="store_true",
                   help="explicitly allow running without an interface-bound transport")
    p.add_argument("--timeout-sec", type=float, default=10.0)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded order-book snapshot collector.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="capture snapshots into --db")
    _add_capture_args(run_p)
    run_p.add_argument("--db", type=Path, required=True)
    bound = run_p.add_mutually_exclusive_group(required=True)
    bound.add_argument("--count", type=int, help="number of ticks")
    bound.add_argument("--duration-min", type=float)
    est_p = sub.add_parser("estimate", help="fetch each stream once and project storage; writes nothing")
    _add_capture_args(est_p)
    est_p.add_argument("--db", type=Path, help="target database, for disk and budget figures")
    stat_p = sub.add_parser("status", help="summarise an existing database (read-only)")
    stat_p.add_argument("--db", type=Path, required=True)
    backup_p = sub.add_parser("backup", help="write a consistent single-file copy of a database (read-only source)")
    backup_p.add_argument("--db", type=Path, required=True)
    backup_p.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(args.db), indent=2, default=str))
            return 0
        if args.command == "backup":
            print(json.dumps(backup(args.db, args.output), indent=2))
            return 0
        streams = [Stream.parse(x) for x in args.streams.split(",") if x.strip()]
        if not streams or len({s.key for s in streams}) != len(streams):
            raise ValueError("streams must be non-empty and unique")
        cfg = CaptureConfig(args.binance_limit, args.bybit_limit, args.band_bp, args.max_levels)
        cfg.validate()
        if not 5 <= args.interval_sec <= 86_400:
            raise ValueError("--interval-sec must be in [5, 86400]")
        transport = Transport(source_interface=args.source_interface, dns_server=args.dns_server,
                              allow_default_route=args.allow_default_route, system_dns=args.system_dns,
                              timeout=args.timeout_sec)
        fetch = transport.request
        if args.command == "estimate":
            result = estimate(streams, cfg, fetch, interval_sec=args.interval_sec, db=args.db,
                              max_db_mb=args.max_db_mb, min_free_gb=args.min_free_gb)
            print(json.dumps(result, indent=2))
            return 1 if result["transport_error"] else 0
        if (args.count is not None and args.count < 1) or (args.duration_min is not None and args.duration_min <= 0):
            raise ValueError("--count/--duration-min must be positive")
        result = collect(args.db, streams, cfg, fetch, interval_sec=args.interval_sec, count=args.count,
                         duration_min=args.duration_min, max_db_mb=args.max_db_mb, min_free_gb=args.min_free_gb,
                         transport_info=transport.describe(), check_transport=transport.check_path)
        print(json.dumps(result, indent=2))
        return 0 if result["stop_reason"] in ("completed", "interrupted") else 2
    except (ValueError, TransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
