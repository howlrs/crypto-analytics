#!/usr/bin/env python3
"""Load fetched CSV/JSON market data into /mnt/e/Datas/market/market.db (SQLite).

market.db already exists with a pre-existing `index_daily` table (untouched by
this script — CREATE TABLE IF NOT EXISTS only, no DROP/ALTER on it).

Tables created (schema fixed by spec, do not alter):
  klines(venue, market, symbol, ts, open, high, low, close, volume,
         quote_volume, trades, taker_buy_base, taker_buy_quote)
  funding(venue, symbol, ts, rate, interval_hours)
  oi_metrics(venue, symbol, ts, open_interest, oi_value, long_short_ratio, top_trader_ls_ratio)
  dataset_meta(dataset, period_start, period_end, rows, gaps, source, fetched_at)

ts is always UNIX ms (UTC). Binance 2025+ klines CSVs sometimes report open_time
in microseconds -- normalized to ms (values > 1e14 are divided by 1000).

Idempotent: INSERT OR REPLACE on natural keys; safe to re-run.
"""
import csv
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import now_iso, log

BASE = "/mnt/e/Datas/market"
DB_PATH = os.path.join(BASE, "market.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
  venue TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL,
  quote_volume REAL, trades INTEGER,
  taker_buy_base REAL, taker_buy_quote REAL,
  PRIMARY KEY (venue, market, symbol, ts)
);
CREATE TABLE IF NOT EXISTS funding (
  venue TEXT NOT NULL, symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  rate REAL NOT NULL,
  interval_hours REAL,
  PRIMARY KEY (venue, symbol, ts)
);
CREATE TABLE IF NOT EXISTS oi_metrics (
  venue TEXT NOT NULL, symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  open_interest REAL, oi_value REAL,
  long_short_ratio REAL, top_trader_ls_ratio REAL,
  PRIMARY KEY (venue, symbol, ts)
);
CREATE TABLE IF NOT EXISTS dataset_meta (
  dataset TEXT PRIMARY KEY,
  period_start TEXT, period_end TEXT, rows INTEGER,
  gaps INTEGER, source TEXT, fetched_at TEXT
);
"""

BATCH_SIZE = 50_000


def normalize_ms(v):
    v = int(float(v))
    if v > 10**14:
        v = v // 1000
    return v


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def load_binance_klines(conn, venue, market, symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-1m-*.csv")))
    total = 0
    min_ts = None
    max_ts = None
    for fp in files:
        rows = []
        with open(fp, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                try:
                    ts = normalize_ms(row[0])
                    o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
                    vol = float(row[5])
                    qvol = float(row[7])
                    trades = int(row[8])
                    taker_base = float(row[9])
                    taker_quote = float(row[10])
                except (ValueError, IndexError):
                    continue
                rows.append((venue, market, symbol, ts, o, h, l, c, vol, qvol, trades, taker_base, taker_quote))
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if max_ts is None or ts > max_ts:
                    max_ts = ts
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO klines "
                "(venue, market, symbol, ts, open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            total += len(rows)
    return total, min_ts, max_ts


def load_binance_funding(conn, venue, symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-fundingRate-*.csv")))
    total = 0
    min_ts = None
    max_ts = None
    rows = []
    for fp in files:
        with open(fp, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = normalize_ms(row["calc_time"])
                    rate = float(row["last_funding_rate"])
                    interval_h = float(row["funding_interval_hours"])
                except (ValueError, KeyError):
                    continue
                rows.append((venue, symbol, ts, rate, interval_h))
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if max_ts is None or ts > max_ts:
                    max_ts = ts
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO funding (venue, symbol, ts, rate, interval_hours) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
        total = len(rows)
    return total, min_ts, max_ts


def load_binance_metrics(conn, venue, symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-metrics-*.csv")))
    total = 0
    min_ts = None
    max_ts = None
    for fp in files:
        rows = []
        with open(fp, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.strptime(row["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp() * 1000)
                    oi = float(row["sum_open_interest"]) if row.get("sum_open_interest") else None
                    oi_val = float(row["sum_open_interest_value"]) if row.get("sum_open_interest_value") else None
                    ls_ratio = float(row["sum_taker_long_short_vol_ratio"]) if row.get("sum_taker_long_short_vol_ratio") else None
                    top_ls = float(row["sum_toptrader_long_short_ratio"]) if row.get("sum_toptrader_long_short_ratio") else None
                except (ValueError, KeyError):
                    continue
                rows.append((venue, symbol, ts, oi, oi_val, ls_ratio, top_ls))
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if max_ts is None or ts > max_ts:
                    max_ts = ts
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO oi_metrics (venue, symbol, ts, open_interest, oi_value, long_short_ratio, top_trader_ls_ratio) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            total += len(rows)
    return total, min_ts, max_ts


def load_bybit_funding(conn, venue, symbol, dir_path):
    fp = os.path.join(dir_path, f"{symbol}-funding-history.json")
    if not os.path.exists(fp):
        return 0, None, None
    with open(fp) as f:
        data = json.load(f)
    rows = []
    min_ts = None
    max_ts = None
    for rec in data:
        ts = normalize_ms(rec["fundingRateTimestamp"])
        rate = float(rec["fundingRate"])
        rows.append((venue, symbol, ts, rate, 8.0))
        if min_ts is None or ts < min_ts:
            min_ts = ts
        if max_ts is None or ts > max_ts:
            max_ts = ts
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO funding (venue, symbol, ts, rate, interval_hours) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows), min_ts, max_ts


def load_bybit_klines(conn, venue, market, symbol, dir_path):
    files = sorted(glob.glob(os.path.join(dir_path, f"{symbol}-1m-*.csv")))
    total = 0
    min_ts = None
    max_ts = None
    for fp in files:
        rows = []
        with open(fp, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ts = normalize_ms(row["open_time"])
                    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
                    vol = float(row["volume"])
                    turnover = float(row["turnover"])
                except (ValueError, KeyError):
                    continue
                rows.append((venue, market, symbol, ts, o, h, l, c, vol, turnover, None, None, None))
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if max_ts is None or ts > max_ts:
                    max_ts = ts
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO klines "
                "(venue, market, symbol, ts, open, high, low, close, volume, quote_volume, trades, taker_buy_base, taker_buy_quote) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            total += len(rows)
    return total, min_ts, max_ts


def load_hyperliquid_funding(conn, venue, coin, dir_path):
    fp = os.path.join(dir_path, f"{coin}-funding-history.json")
    if not os.path.exists(fp):
        return 0, None, None
    with open(fp) as f:
        data = json.load(f)
    rows = []
    min_ts = None
    max_ts = None
    for rec in data:
        ts = normalize_ms(rec["time"])
        rate = float(rec["fundingRate"])
        rows.append((venue, coin, ts, rate, 1.0))
        if min_ts is None or ts < min_ts:
            min_ts = ts
        if max_ts is None or ts > max_ts:
            max_ts = ts
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO funding (venue, symbol, ts, rate, interval_hours) VALUES (?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows), min_ts, max_ts


def record_meta(conn, dataset, rows, min_ts, max_ts, gaps, source):
    ps = datetime.fromtimestamp(min_ts / 1000, tz=timezone.utc).isoformat() if min_ts else None
    pe = datetime.fromtimestamp(max_ts / 1000, tz=timezone.utc).isoformat() if max_ts else None
    conn.execute(
        "INSERT OR REPLACE INTO dataset_meta (dataset, period_start, period_end, rows, gaps, source, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (dataset, ps, pe, rows, gaps, source, now_iso()),
    )
    conn.commit()


def expected_bar_gap(min_ts, max_ts, rows, bar_ms=60_000):
    if min_ts is None or max_ts is None or rows == 0:
        return None
    expected = (max_ts - min_ts) // bar_ms + 1
    return int(expected - rows)


def main():
    conn = connect()
    log(f"Connected to {DB_PATH}, schema ensured (index_daily left untouched)")

    # Binance spot/perp klines: BTCUSDT, ETHUSDT
    for market in ["spot", "perp"]:
        for symbol in ["BTCUSDT", "ETHUSDT"]:
            dir_path = os.path.join(BASE, f"binance/{market}/klines_1m/{symbol}")
            rows, min_ts, max_ts = load_binance_klines(conn, "binance", market, symbol, dir_path)
            dataset = f"binance/{market}/klines_1m/{symbol}"
            gaps = expected_bar_gap(min_ts, max_ts, rows)
            record_meta(conn, dataset, rows, min_ts, max_ts, gaps, "binance.vision monthly zip")
            log(f"klines {dataset}: rows={rows} gaps={gaps}")

    # Binance perp funding
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        dir_path = os.path.join(BASE, f"binance/perp/funding/{symbol}")
        rows, min_ts, max_ts = load_binance_funding(conn, "binance", symbol, dir_path)
        dataset = f"binance/perp/funding/{symbol}"
        record_meta(conn, dataset, rows, min_ts, max_ts, None, "binance.vision monthly zip")
        log(f"funding {dataset}: rows={rows}")

    # Binance perp metrics
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        dir_path = os.path.join(BASE, f"binance/perp/metrics/{symbol}")
        rows, min_ts, max_ts = load_binance_metrics(conn, "binance", symbol, dir_path)
        dataset = f"binance/perp/metrics/{symbol}"
        record_meta(conn, dataset, rows, min_ts, max_ts, None, "binance.vision daily zip")
        log(f"metrics {dataset}: rows={rows}")

    # Bybit funding: BTCUSDT, ETHUSDT, HYPEUSDT
    for symbol in ["BTCUSDT", "ETHUSDT", "HYPEUSDT"]:
        dir_path = os.path.join(BASE, f"bybit/perp/funding/{symbol}")
        rows, min_ts, max_ts = load_bybit_funding(conn, "bybit", symbol, dir_path)
        dataset = f"bybit/perp/funding/{symbol}"
        record_meta(conn, dataset, rows, min_ts, max_ts, None, "bybit v5 REST")
        log(f"bybit funding {dataset}: rows={rows}")

    # Bybit perp klines: BTCUSDT, ETHUSDT (2024-09 only), HYPEUSDT (2024-12..)
    for symbol in ["BTCUSDT", "ETHUSDT", "HYPEUSDT"]:
        dir_path = os.path.join(BASE, f"bybit/perp/klines_1m/{symbol}")
        rows, min_ts, max_ts = load_bybit_klines(conn, "bybit", "perp", symbol, dir_path)
        dataset = f"bybit/perp/klines_1m/{symbol}"
        gaps = expected_bar_gap(min_ts, max_ts, rows)
        record_meta(conn, dataset, rows, min_ts, max_ts, gaps, "bybit v5 REST")
        log(f"bybit klines {dataset}: rows={rows} gaps={gaps}")

    # Bybit spot klines: HYPEUSDT only
    dir_path = os.path.join(BASE, "bybit/spot/klines_1m/HYPEUSDT")
    rows, min_ts, max_ts = load_bybit_klines(conn, "bybit", "spot", "HYPEUSDT", dir_path)
    dataset = "bybit/spot/klines_1m/HYPEUSDT"
    gaps = expected_bar_gap(min_ts, max_ts, rows)
    record_meta(conn, dataset, rows, min_ts, max_ts, gaps, "bybit v5 REST")
    log(f"bybit klines {dataset}: rows={rows} gaps={gaps}")

    # Hyperliquid funding: BTC, ETH, HYPE
    for coin in ["BTC", "ETH", "HYPE"]:
        dir_path = os.path.join(BASE, f"hyperliquid/funding/{coin}")
        rows, min_ts, max_ts = load_hyperliquid_funding(conn, "hyperliquid", coin, dir_path)
        dataset = f"hyperliquid/funding/{coin}"
        record_meta(conn, dataset, rows, min_ts, max_ts, None, "hyperliquid REST fundingHistory")
        log(f"hl funding {dataset}: rows={rows}")

    log("Creating index and running ANALYZE...")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_klines_ts ON klines(ts);")
    conn.execute("ANALYZE;")
    conn.commit()

    # sanity: confirm index_daily untouched (still exists, not dropped)
    cur = conn.execute("SELECT COUNT(*) FROM index_daily;")
    idx_count = cur.fetchone()[0]
    log(f"index_daily rows (untouched, pre-existing table): {idx_count}")

    conn.close()
    log("DONE load_to_sqlite")


if __name__ == "__main__":
    main()
