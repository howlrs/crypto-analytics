"""SQLite storage for order-book snapshots (standard library only).

Snapshots live in their own database, never in ``market.db`` or ``hl_watch.db``.
Levels are stored as zlib-compressed JSON so storage can be budgeted; the
header columns keep top of book, coverage and provenance queryable without
decompressing payloads.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

from orderbook.book import Book

PAYLOAD_FORMAT = "zlib-json-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY,
  started_ms INTEGER NOT NULL,
  ended_ms INTEGER,
  stop_reason TEXT,
  config_json TEXT NOT NULL,
  collector_sha256 TEXT NOT NULL,
  snapshots INTEGER NOT NULL DEFAULT 0,
  errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(run_id),
  tick_ms INTEGER NOT NULL,
  venue TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  aggregation TEXT NOT NULL DEFAULT '',
  requested_ms INTEGER NOT NULL,
  received_ms INTEGER NOT NULL,
  exchange_ms INTEGER,
  request_limit INTEGER NOT NULL,
  raw_bid_levels INTEGER NOT NULL,
  raw_ask_levels INTEGER NOT NULL,
  best_bid REAL NOT NULL,
  best_ask REAL NOT NULL,
  bid_coverage_bp REAL NOT NULL,
  ask_coverage_bp REAL NOT NULL,
  payload_format TEXT NOT NULL,
  payload BLOB NOT NULL,
  payload_bytes INTEGER NOT NULL,
  UNIQUE (venue, market, symbol, aggregation, tick_ms)
);
CREATE INDEX IF NOT EXISTS ix_snapshots_stream_time
  ON snapshots (venue, market, symbol, aggregation, received_ms);
CREATE TABLE IF NOT EXISTS fetch_errors (
  run_id INTEGER NOT NULL,
  tick_ms INTEGER NOT NULL,
  venue TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  aggregation TEXT NOT NULL DEFAULT '',
  at_ms INTEGER NOT NULL,
  error TEXT NOT NULL
);
"""


def encode_levels(book: Book) -> bytes:
    payload = {"b": [list(level) for level in book.bids], "a": [list(level) for level in book.asks]}
    return zlib.compress(json.dumps(payload, separators=(",", ":")).encode(), 6)


def decode_book(payload: bytes, bid_coverage_bp: float, ask_coverage_bp: float, fmt: str = PAYLOAD_FORMAT) -> Book:
    if fmt != PAYLOAD_FORMAT:
        raise ValueError(f"unsupported payload format {fmt!r}")
    data = json.loads(zlib.decompress(payload))
    return Book([tuple(x) for x in data["b"]], [tuple(x) for x in data["a"]],
                float(bid_coverage_bp), float(ask_coverage_bp))


def connect_writer(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def connect_reader(path: Path) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"order-book database not found: {path}")
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def database_bytes(path: Path) -> int:
    """Main file plus WAL/SHM, so a budget check sees uncheckpointed writes."""
    return sum(p.stat().st_size for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists())
