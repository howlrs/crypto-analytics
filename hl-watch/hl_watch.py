#!/usr/bin/env python3
"""hl-watch: Hyperliquid 大口 TWAP 実行者の常時観測システム (MVP).

単一プロセス + ThreadPoolExecutor によるポーリングループ。stdlib のみ (Python 3.12)。
DB: /mnt/e/Datas/market/hl_watch.db (SQLite, WAL) — 既存の market.db とは完全に別物。

サブコマンド:
  run     [--coins BTC,ETH,SOL,HYPE] [--duration-min N]  収集ループ
  status                                                  対象者リスト表示
  flow    [--coin BTC] [--limit 60]                       twap_flow 時系列表示
  analyze [--coins BTC,ETH,SOL,HYPE] [--fresh MIN] [--no-collect]  需給・清算・複合シグナル解析
  levels  [--coins BTC,ETH,SOL,HYPE] [--band-pct 1.0] [--range-pct 30] [--min-usd 500]
          [--fresh-min 60]  価格帯別 清算ウォール (OI様ラダー)

詳細は README.md を参照。
"""
import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import math
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
DB_PATH = "/mnt/e/Datas/market/hl_watch.db"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPURRSCAN_TWAP_URL = "https://api.hypurrscan.io/twap/{addr}"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) hl-watch/1.0"

DEFAULT_COINS = ["BTC", "ETH", "SOL", "HYPE"]

DISCOVERY_INTERVAL_S = 20          # recentTrades ポーリング間隔 (対象コイン毎)
UNCONFIRMED_CHECK_INTERVAL_S = 300  # 未確認アドレスの userTwapSliceFills 確認間隔 (5分)
ACTIVE_CHECK_INTERVAL_S = 60        # active TWAP 保有アドレスの確認間隔 (60秒)
POSITION_SNAPSHOT_INTERVAL_S = 60   # watch_positions スナップショット間隔 (active TWAP保有user)
CANDIDATE_SNAPSHOT_INTERVAL_S = 300  # watch_positions スナップショット間隔 (非active候補全体, 5分)
AGGREGATION_INTERVAL_S = 60         # twap_flow 集計間隔 (毎分)
MAIN_LOOP_TICK_S = 5                # メインループの粒度

CANDIDATE_TTL_S = 30 * 60           # 候補アドレス LRU TTL (30分)
CANDIDATE_MAX = 300                 # 候補アドレス LRU 上限

ENDED_SUSPECTED_GAP_S = 120         # 最終スライスからこの秒数 fill なしで ended_suspected (30s*4)
COMPLETED_RATIO = 0.98              # 宣言総量に対する充足率で completed 判定

HYPURRSCAN_MIN_INTERVAL_S = 2.0     # Hypurrscan 呼び出し間隔下限

MAX_REQ_PER_SEC = 4.0               # HL info API 全体のペーシング上限

# analyze サブコマンド用
ANALYZE_FRESHNESS_THRESHOLD_S = 10 * 60   # 鮮度チェック閾値 (固定10分)
ANALYZE_DEFAULT_BURST_MIN = 3             # --fresh 既定値 (バースト実行時間、分)
ANALYZE_NEW_TWAP_WINDOW_S = 5 * 60        # 「開始5分未満」の新規TWAP判定窓
ANALYZE_BIAS_STRONG_RATIO = 2.0           # 「優勢」判定の比率閾値
ANALYZE_BIAS_MILD_RATIO = 1.5             # 「拮抗」とみなす上限比率
ANALYZE_LIQ_PROXIMITY_RATIO = 0.25        # §3 清算近接: |mark-liq|/mark <= 0.25
ANALYZE_TOP_TWAP_LIMIT = 10               # §2 上位TWAP件数
ANALYZE_TOP_LIQ_LIMIT = 15                # §3 上位清算近接件数
ANALYZE_SQUEEZE_BAND_RATIO = 0.15         # §4 スクイーズ素地: mark の上下15%以内

# levels サブコマンド (清算ウォール) 用
LEVELS_DEFAULT_BAND_PCT = 1.0             # バケット刻み幅 (mark比 %)
LEVELS_DEFAULT_RANGE_PCT = 30.0           # 集計対象範囲 (mark比 ±%)
LEVELS_DEFAULT_MIN_USD = 500.0            # 対象化する position_value 下限 ($)
LEVELS_DEFAULT_FRESH_MIN = 60             # watch_positions 鮮度閾値 (分)
LEVELS_DANGER_BAND_PCT = 5.0              # mark直近の危険帯として強調する範囲 (mark比 ±%)
LEVELS_BAR_MAX_WIDTH = 40                 # ASCII バー最大文字数


# ---------------------------------------------------------------------------
# ロギング
# ---------------------------------------------------------------------------
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# グレースフルシャットダウン
# ---------------------------------------------------------------------------
class ShutdownFlag:
    def __init__(self):
        self._event = threading.Event()

    def set(self):
        self._event.set()

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout):
        return self._event.wait(timeout)


SHUTDOWN = ShutdownFlag()


def _signal_handler(signum, frame):
    log(f"received signal {signum}, shutting down gracefully...")
    SHUTDOWN.set()


# ---------------------------------------------------------------------------
# レートリミッタ (全リクエスト共通のペーシング)
# ---------------------------------------------------------------------------
class RateLimiter:
    """トークンバケット風の単純な最小間隔ペーシング。スレッドセーフ。"""

    def __init__(self, max_per_sec):
        self._min_interval = 1.0 / max_per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last = now


HL_RATE_LIMITER = RateLimiter(MAX_REQ_PER_SEC)


# ---------------------------------------------------------------------------
# HTTP ヘルパー (指数バックオフ + 429/5xx 対応)
# ---------------------------------------------------------------------------
def http_post_json(url, payload, timeout=15, max_retries=5, base_backoff=1.0, rate_limiter=None):
    """POST json body, return parsed JSON or None on persistent failure (never raises)."""
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(max_retries):
        if SHUTDOWN.is_set():
            return None
        if rate_limiter is not None:
            rate_limiter.acquire()
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                backoff = base_backoff * (2 ** attempt)
                log(f"  HTTP {e.code} on POST {url} (attempt {attempt+1}/{max_retries}), backing off {backoff:.1f}s")
                SHUTDOWN.wait(backoff)
                continue
            log(f"  HTTP {e.code} on POST {url}: non-retryable")
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
            last_err = e
            backoff = base_backoff * (2 ** attempt)
            log(f"  network error on POST {url}: {e} (attempt {attempt+1}/{max_retries}), backing off {backoff:.1f}s")
            SHUTDOWN.wait(backoff)
            continue
    log(f"  giving up on POST {url} after {max_retries} attempts: {last_err}")
    return None


def http_get_json(url, timeout=15, max_retries=4, base_backoff=1.0):
    """GET json (used for Hypurrscan). Returns parsed JSON or None on failure."""
    last_err = None
    for attempt in range(max_retries):
        if SHUTDOWN.is_set():
            return None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                backoff = base_backoff * (2 ** attempt)
                log(f"  hypurrscan HTTP {e.code} (attempt {attempt+1}/{max_retries}), backing off {backoff:.1f}s")
                SHUTDOWN.wait(backoff)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
            last_err = e
            backoff = base_backoff * (2 ** attempt)
            log(f"  hypurrscan network error: {e} (attempt {attempt+1}/{max_retries}), backing off {backoff:.1f}s")
            SHUTDOWN.wait(backoff)
            continue
    log(f"  hypurrscan giving up after {max_retries} attempts: {last_err}")
    return None


def hl_info(payload):
    return http_post_json(HL_INFO_URL, payload, rate_limiter=HL_RATE_LIMITER)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS twap_orders(
  twap_id INTEGER PRIMARY KEY,
  user TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
  declared_sz REAL, declared_minutes INTEGER, reduce_only INTEGER,
  start_ts INTEGER, last_fill_ts INTEGER,
  cum_sz REAL DEFAULT 0, cum_notional REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  updated_at INTEGER);
CREATE TABLE IF NOT EXISTS twap_fills(
  tid INTEGER PRIMARY KEY, twap_id INTEGER, ts INTEGER, coin TEXT, px REAL, sz REAL, side TEXT);
CREATE TABLE IF NOT EXISTS watch_positions(
  user TEXT, ts INTEGER, coin TEXT, szi REAL, entry_px REAL, position_value REAL,
  liq_px REAL,
  lev REAL, lev_type TEXT, acct_value REAL, PRIMARY KEY(user, ts, coin));
CREATE TABLE IF NOT EXISTS twap_flow(
  coin TEXT, ts_min INTEGER, active_buy INTEGER, active_sell INTEGER,
  buy_rate_usd_min REAL, sell_rate_usd_min REAL,
  buy_remaining_usd REAL, sell_remaining_usd REAL,
  PRIMARY KEY(coin, ts_min));
CREATE INDEX IF NOT EXISTS idx_twap_orders_user ON twap_orders(user);
CREATE INDEX IF NOT EXISTS idx_twap_orders_status ON twap_orders(status);
CREATE INDEX IF NOT EXISTS idx_twap_fills_twapid ON twap_fills(twap_id);
CREATE INDEX IF NOT EXISTS idx_watch_positions_user ON watch_positions(user);
-- Each clearinghouse request is retained, including successful empty results
-- and failures.  A position row is current only if it belongs to the latest
-- successful account attempt for that user.
CREATE TABLE IF NOT EXISTS position_attempts(
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user TEXT NOT NULL, observed_ts INTEGER NOT NULL, api_ts INTEGER,
  status TEXT NOT NULL CHECK(status IN ('success','empty','failure')),
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_position_attempts_user_ts ON position_attempts(user, observed_ts);
-- Frozen, append-only inputs and outputs of every aggregation pass.  JSON is
-- deliberate here: the frame can be replayed after live tables have changed.
CREATE TABLE IF NOT EXISTS observation_frames(
  frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_min INTEGER NOT NULL, created_ts INTEGER NOT NULL,
  coins_json TEXT NOT NULL, candidate_json TEXT NOT NULL, frame_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observation_frames_ts ON observation_frames(ts_min);
"""


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


DB_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# asset id -> coin name キャッシュ (meta / spotMeta, 起動時1回取得)
# ---------------------------------------------------------------------------
class AssetIdResolver:
    def __init__(self):
        self._perp = {}  # id -> coin
        self._spot = {}  # id (10000+) -> coin
        self._loaded = False

    def load(self):
        meta = hl_info({"type": "meta"})
        if meta and "universe" in meta:
            for i, asset in enumerate(meta["universe"]):
                self._perp[i] = asset.get("name")
        spot_meta = hl_info({"type": "spotMeta"})
        if spot_meta and "universe" in spot_meta:
            # spot universe entries have their own index; per HL convention spot asset id = 10000 + index
            tokens = {t["index"]: t["name"] for t in spot_meta.get("tokens", [])}
            for i, pair in enumerate(spot_meta["universe"]):
                name = pair.get("name")
                self._spot[10000 + i] = name
        self._loaded = True
        log(f"asset resolver loaded: {len(self._perp)} perp, {len(self._spot)} spot")

    def resolve(self, asset_id):
        if asset_id is None:
            return None
        if asset_id >= 10000:
            return self._spot.get(asset_id)
        return self._perp.get(asset_id)


ASSET_RESOLVER = AssetIdResolver()


# ---------------------------------------------------------------------------
# 候補アドレス LRU (TTLパージ付き)
# ---------------------------------------------------------------------------
class CandidateSet:
    """発見された候補アドレスの集合。最終観測時刻を保持し LRU + TTL でパージする。"""

    def __init__(self, max_size=CANDIDATE_MAX, ttl_s=CANDIDATE_TTL_S):
        self._max_size = max_size
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._data = OrderedDict()  # addr -> last_seen_ts (epoch seconds)
        self._recently_stale = set()

    def touch(self, addr, coin=None):
        now = time.time()
        with self._lock:
            self._recently_stale.discard(addr)
            if addr in self._data:
                self._data.move_to_end(addr)
            previous = self._data.get(addr)
            coins = set(previous["coins"]) if isinstance(previous, dict) else set()
            first_seen = previous["first_seen_ts"] if isinstance(previous, dict) else now
            if coin:
                coins.add(coin)
            self._data[addr] = {"ts": now, "first_seen_ts": first_seen, "coins": coins}
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def purge_stale(self):
        now = time.time()
        removed = 0
        with self._lock:
            stale = [a for a, value in self._data.items() if now - value["ts"] > self._ttl_s]
            for a in stale:
                del self._data[a]
                self._recently_stale.add(a)
                removed += 1
        return removed

    def snapshot(self):
        with self._lock:
            return list(self._data.keys())

    def snapshot_with_coins(self):
        with self._lock:
            return [{"user": addr, "coins": sorted(value["coins"]), "first_seen_ts": value["first_seen_ts"], "last_seen_ts": value["ts"]}
                    for addr, value in self._data.items()]

    def drain_stale(self):
        with self._lock:
            stale = sorted(self._recently_stale)
            self._recently_stale.clear()
            return stale


CANDIDATES = CandidateSet()

# アドレス -> 状態管理
CONFIRMED_TWAP_USERS = set()      # active TWAP を最低1件保有したことがあるユーザー
UNCONFIRMED_LAST_CHECK = {}       # addr -> last checked epoch (未確認アドレス、5分毎)
ACTIVE_LAST_CHECK = {}            # addr -> last checked epoch (active TWAP保有、60秒毎)
CANDIDATE_SNAPSHOT_LAST_CHECK = {}  # addr -> last checked epoch (非active候補全体、5分毎)
HYPURRSCAN_SEEN_USERS = set()     # Hypurrscan を既に叩いたユーザー (初回1回のみ)
STATE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# ステップ1: 発見 (recentTrades)
# ---------------------------------------------------------------------------
def discover_candidates(coin):
    resp = hl_info({"type": "recentTrades", "coin": coin})
    if not resp or not isinstance(resp, list):
        return 0
    n = 0
    for trade in resp:
        users = trade.get("users") or []
        for addr in users:
            if not addr:
                continue
            CANDIDATES.touch(addr, coin)
            n += 1
    return n


# ---------------------------------------------------------------------------
# ステップ4: Hypurrscan 宣言補完
# ---------------------------------------------------------------------------
_last_hypurrscan_call = [0.0]
_hypurrscan_call_lock = threading.Lock()


def hypurrscan_pace():
    with _hypurrscan_call_lock:
        now = time.monotonic()
        wait = _last_hypurrscan_call[0] + HYPURRSCAN_MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        _last_hypurrscan_call[0] = time.monotonic()


def fetch_hypurrscan_declared(user):
    """Hypurrscan から user の TWAP 宣言リストを取得。失敗時は None (呼び出し側でフォールバック)。"""
    hypurrscan_pace()
    url = HYPURRSCAN_TWAP_URL.format(addr=user)
    data = http_get_json(url, max_retries=2, base_backoff=1.0)
    if data is None or not isinstance(data, list):
        return None
    results = []
    for entry in data:
        action = entry.get("action") or {}
        twap = action.get("twap") or {}
        asset_id = twap.get("a")
        coin = ASSET_RESOLVER.resolve(asset_id)
        if coin is None:
            continue
        try:
            declared_sz = float(twap.get("s"))
        except (TypeError, ValueError):
            declared_sz = None
        results.append({
            "time": entry.get("time"),
            "coin": coin,
            "is_buy": bool(twap.get("b")),
            "declared_sz": declared_sz,
            "reduce_only": bool(twap.get("r")),
            "minutes": twap.get("m"),
        })
    return results


def match_and_fill_declared(conn, user, twap_id, coin, side, start_ts):
    """新規検出 TWAP に対し Hypurrscan 宣言情報を user+coin+side+時刻近傍でマッチさせて埋める。"""
    declared_list = fetch_hypurrscan_declared(user)
    if not declared_list:
        log(f"  hypurrscan: no data for {user} (twap_id={twap_id}), fallback to progress-based estimate")
        return
    is_buy_target = (side == "B")
    best = None
    best_dt = None
    start_s = (start_ts or 0) / 1000.0
    for d in declared_list:
        if d["coin"] != coin or d["is_buy"] != is_buy_target:
            continue
        t = d.get("time")
        if t is None:
            continue
        dt = abs((t / 1000.0) - start_s)
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = d
    if best is None:
        log(f"  hypurrscan: no matching declared order for {user} coin={coin} side={side} (twap_id={twap_id})")
        return
    with DB_LOCK:
        conn.execute(
            "UPDATE twap_orders SET declared_sz=?, declared_minutes=?, reduce_only=? WHERE twap_id=?",
            (best["declared_sz"], best["minutes"], 1 if best["reduce_only"] else 0, twap_id),
        )
        conn.commit()
    log(f"  hypurrscan matched: user={user} twap_id={twap_id} declared_sz={best['declared_sz']} "
        f"minutes={best['minutes']} reduce_only={best['reduce_only']}")


# ---------------------------------------------------------------------------
# ステップ2+3: TWAP 判定・状態遷移
# ---------------------------------------------------------------------------
def check_user_twaps(conn, user):
    """userTwapSliceFills を取得し twap_orders / twap_fills を更新。新規検出があれば Hypurrscan を叩く。
    戻り値: このユーザーが1件以上 active TWAP を持つか (bool)。
    """
    resp = hl_info({"type": "userTwapSliceFills", "user": user})
    if resp is None or not isinstance(resp, list):
        return False
    if not resp:
        return False

    # twapId ごとに集計
    by_twap = {}
    for entry in resp:
        twap_id = entry.get("twapId")
        fill = entry.get("fill") or {}
        if twap_id is None or not fill:
            continue
        by_twap.setdefault(twap_id, []).append(fill)

    has_active = False
    for twap_id, fills in by_twap.items():
        fills.sort(key=lambda f: int(f.get("time", 0)))
        coin = fills[0].get("coin")
        side = fills[0].get("side")
        cum_sz = 0.0
        cum_notional = 0.0
        first_ts = int(fills[0].get("time", 0))
        last_ts = first_ts
        for f in fills:
            try:
                sz = float(f.get("sz", 0))
                px = float(f.get("px", 0))
            except (TypeError, ValueError):
                continue
            cum_sz += sz
            cum_notional += sz * px
            ts = int(f.get("time", 0))
            if ts > last_ts:
                last_ts = ts

        with DB_LOCK:
            cur = conn.execute("SELECT declared_sz, status, start_ts FROM twap_orders WHERE twap_id=?", (twap_id,))
            row = cur.fetchone()
            is_new = row is None
            declared_sz = row[0] if row else None
            prev_status = row[1] if row else None
            start_ts = row[2] if row else first_ts

            now_gap_s = (now_ms() - last_ts) / 1000.0
            if declared_sz and declared_sz > 0 and cum_sz >= declared_sz * COMPLETED_RATIO:
                status = "completed"
            elif now_gap_s > ENDED_SUSPECTED_GAP_S:
                status = "ended_suspected"
            else:
                status = "active"

            if is_new:
                conn.execute(
                    "INSERT INTO twap_orders(twap_id, user, coin, side, start_ts, last_fill_ts, "
                    "cum_sz, cum_notional, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (twap_id, user, coin, side, start_ts, last_ts, cum_sz, cum_notional, status, now_ms()),
                )
            else:
                conn.execute(
                    "UPDATE twap_orders SET last_fill_ts=?, cum_sz=?, cum_notional=?, status=?, updated_at=? "
                    "WHERE twap_id=?",
                    (last_ts, cum_sz, cum_notional, status, now_ms(), twap_id),
                )

            for f in fills:
                tid = f.get("tid")
                if tid is None:
                    continue
                try:
                    px = float(f.get("px", 0))
                    sz = float(f.get("sz", 0))
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO twap_fills(tid, twap_id, ts, coin, px, sz, side) VALUES (?,?,?,?,?,?,?)",
                    (tid, twap_id, int(f.get("time", 0)), f.get("coin"), px, sz, f.get("side")),
                )
            conn.commit()

        if status == "active":
            has_active = True

        if is_new:
            log(f"  new TWAP detected: twap_id={twap_id} user={user} coin={coin} side={side}")
            with STATE_LOCK:
                already = user in HYPURRSCAN_SEEN_USERS
                HYPURRSCAN_SEEN_USERS.add(user)
            if not already:
                match_and_fill_declared(conn, user, twap_id, coin, side, start_ts)

    with STATE_LOCK:
        if has_active:
            CONFIRMED_TWAP_USERS.add(user)
        else:
            CONFIRMED_TWAP_USERS.discard(user)

    return has_active


# ---------------------------------------------------------------------------
# ステップ5: 対象者スナップショット (clearinghouseState)
# ---------------------------------------------------------------------------
def snapshot_position(conn, user):
    try:
        resp = hl_info({"type": "clearinghouseState", "user": user})
    except Exception as exc:
        resp = None
        request_error = str(exc)
    else:
        request_error = "empty or invalid API response"
    ts = now_ms()
    # A failed request must be distinguished from a confirmed empty account.
    # It never invalidates the previous successful account observation.
    if not isinstance(resp, dict) or not isinstance(resp.get("assetPositions"), list):
        with DB_LOCK:
            ts = next_position_attempt_ts(conn, user, ts)
            conn.execute(
                "INSERT INTO position_attempts(user, observed_ts, api_ts, status, error) VALUES (?,?,?,?,?)",
                (user, ts, None, "failure", request_error),
            )
            conn.commit()
        return
    api_ts = resp.get("time") or resp.get("timestamp")
    try:
        api_ts = int(api_ts) if api_ts is not None else None
    except (TypeError, ValueError):
        api_ts = None
    margin = resp.get("marginSummary") or {}
    if not isinstance(margin, dict):
        margin = {}
    acct_value = margin.get("accountValue")
    try:
        acct_value = float(acct_value) if acct_value is not None else None
    except (TypeError, ValueError):
        acct_value = None

    positions = resp.get("assetPositions") or []
    rows = []
    for ap in positions:
        if (not isinstance(ap, dict) or not isinstance(ap.get("position"), dict)
                or not isinstance(ap["position"].get("coin"), str) or not ap["position"]["coin"]):
            with DB_LOCK:
                ts = next_position_attempt_ts(conn, user, ts)
                conn.execute("INSERT INTO position_attempts(user, observed_ts, api_ts, status, error) VALUES (?,?,?,?,?)",
                             (user, ts, api_ts, "failure", "malformed assetPositions entry"))
                conn.commit()
            return
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        if not coin:
            continue
        try:
            szi = float(pos.get("szi")) if pos.get("szi") is not None else None
        except (TypeError, ValueError):
            szi = None
        try:
            entry_px = float(pos.get("entryPx")) if pos.get("entryPx") is not None else None
        except (TypeError, ValueError):
            entry_px = None
        try:
            position_value = float(pos.get("positionValue")) if pos.get("positionValue") is not None else None
        except (TypeError, ValueError):
            position_value = None
        liq_px_raw = pos.get("liquidationPx")
        try:
            liq_px = float(liq_px_raw) if liq_px_raw is not None else None
        except (TypeError, ValueError):
            liq_px = None
        leverage = pos.get("leverage") or {}
        if not isinstance(leverage, dict):
            leverage = {}
        lev = leverage.get("value")
        try:
            lev = float(lev) if lev is not None else None
        except (TypeError, ValueError):
            lev = None
        lev_type = leverage.get("type")
        szi, entry_px, position_value, liq_px, lev, acct_value = (
            value if value is not None and math.isfinite(value) else None
            for value in (szi, entry_px, position_value, liq_px, lev, acct_value)
        )
        if position_value is not None and position_value < 0:
            position_value = None
        if liq_px is not None and liq_px <= 0:
            liq_px = None
        rows.append((user, ts, coin, szi, entry_px, position_value, liq_px, lev, lev_type, acct_value))

    with DB_LOCK:
        ts = next_position_attempt_ts(conn, user, ts)
        # Keep the rows and their attempt in the same unique observation tick.
        rows = [(user, ts, coin, szi, entry_px, position_value, liq_px, lev, lev_type, acct_value)
                for _, _, coin, szi, entry_px, position_value, liq_px, lev, lev_type, acct_value in rows]
        conn.execute(
            "INSERT INTO position_attempts(user, observed_ts, api_ts, status) VALUES (?,?,?,?)",
            (user, ts, api_ts, "success" if rows else "empty"),
        )
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO watch_positions(user, ts, coin, szi, entry_px, position_value, "
                "liq_px, lev, lev_type, acct_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        conn.commit()


def next_position_attempt_ts(conn, user, proposed_ts):
    """Avoid same-millisecond account reads reusing a position timestamp."""
    row = conn.execute("SELECT MAX(observed_ts) FROM position_attempts WHERE user=?", (user,)).fetchone()
    previous = row[0] if row else None
    return max(proposed_ts, (previous + 1) if previous is not None else proposed_ts)


def latest_position_cte():
    """SQL CTE for positions from each user's latest successful account read.

    A later success (including an empty account or one omitting a closed coin)
    suppresses all older rows; failures intentionally do not.
    """
    return """
        WITH latest_account AS (
            SELECT user, MAX(observed_ts) AS observed_ts
            FROM position_attempts WHERE status IN ('success', 'empty') GROUP BY user
        ), attempted_positions AS (
            SELECT wp.* FROM watch_positions wp
            JOIN latest_account la ON la.user=wp.user AND la.observed_ts=wp.ts
        ), legacy_positions AS (
            SELECT wp.* FROM watch_positions wp
            JOIN (SELECT user, coin, MAX(ts) AS ts FROM watch_positions GROUP BY user, coin) old
              ON old.user=wp.user AND old.coin=wp.coin AND old.ts=wp.ts
            WHERE NOT EXISTS (SELECT 1 FROM position_attempts pa WHERE pa.user=wp.user
                              AND pa.status IN ('success', 'empty'))
        ), latest_positions AS (
            SELECT * FROM attempted_positions UNION ALL SELECT * FROM legacy_positions
        )
    """


# ---------------------------------------------------------------------------
# ステップ6: 集計 (毎分, コイン別 twap_flow)
# ---------------------------------------------------------------------------
def aggregate_flow(conn, coins):
    ts_min = (now_ms() // 60000) * 60000
    five_min_ago = now_ms() - 5 * 60 * 1000
    # Price is an independently observed allMids input, never inferred from fills.
    try:
        mids = fetch_all_mids()
    except Exception as exc:
        log(f"allMids unavailable for observation: {type(exc).__name__}")
        mids = {}
    mid_observed_ts = now_ms()
    with DB_LOCK:
        for coin in coins:
            cur = conn.execute(
                "SELECT twap_id, side, declared_sz, cum_sz, status FROM twap_orders "
                "WHERE coin=? AND status='active'",
                (coin,),
            )
            orders = cur.fetchall()
            active_buy = sum(1 for o in orders if o[1] == "B")
            active_sell = sum(1 for o in orders if o[1] == "A")

            buy_remaining_usd = 0.0
            sell_remaining_usd = 0.0
            buy_has_declared = False
            sell_has_declared = False
            for twap_id, side, declared_sz, cum_sz, status in orders:
                if declared_sz is None or declared_sz <= 0:
                    continue
                remaining_sz = max(declared_sz - cum_sz, 0.0)
                # 直近約定価格で概算 notional 化
                px_row = conn.execute(
                    "SELECT px FROM twap_fills WHERE twap_id=? ORDER BY ts DESC LIMIT 1", (twap_id,)
                ).fetchone()
                px = px_row[0] if px_row else None
                if px is None:
                    continue
                remaining_usd = remaining_sz * px
                if side == "B":
                    buy_remaining_usd += remaining_usd
                    buy_has_declared = True
                else:
                    sell_remaining_usd += remaining_usd
                    sell_has_declared = True

            # 直近5分の実行レート ($/min)
            rate_cur = conn.execute(
                "SELECT tf.side, SUM(tf.px*tf.sz) FROM twap_fills tf "
                "JOIN twap_orders o ON tf.twap_id = o.twap_id "
                "WHERE o.coin=? AND tf.ts >= ? GROUP BY tf.side",
                (coin, five_min_ago),
            )
            sums = {row[0]: row[1] for row in rate_cur.fetchall()}
            buy_rate = (sums.get("B", 0.0) or 0.0) / 5.0
            sell_rate = (sums.get("A", 0.0) or 0.0) / 5.0

            conn.execute(
                "INSERT OR REPLACE INTO twap_flow(coin, ts_min, active_buy, active_sell, "
                "buy_rate_usd_min, sell_rate_usd_min, buy_remaining_usd, sell_remaining_usd) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    coin, ts_min, active_buy, active_sell, buy_rate, sell_rate,
                    buy_remaining_usd if buy_has_declared else None,
                    sell_remaining_usd if sell_has_declared else None,
                ),
            )
        save_observation_frame(conn, coins, ts_min, mids, mid_observed_ts)
        conn.commit()
    log(f"aggregated twap_flow for ts_min={ts_min} coins={coins}")


def _frame_bin(szi, liq_px, mark, band_pct, range_pct):
    """Return a replayable ladder bin label, or None when it is not eligible."""
    if (not szi or liq_px is None or not mark or mark <= 0
            or not all(math.isfinite(x) for x in (szi, liq_px, mark))):
        return None
    pct = (liq_px - mark) / mark * 100.0
    if (szi > 0 and pct >= 0) or (szi < 0 and pct <= 0):
        return None
    if abs(pct) > range_pct:
        return "outside"
    index = min(int(abs(pct) / band_pct), max(int(round(range_pct / band_pct)), 1) - 1)
    return f"{'down' if szi > 0 else 'up'}:{index}"


def _frame_buckets(positions, mid, config):
    """Include explicit exclusion buckets so notional never silently vanishes."""
    totals = {}
    for p in positions:
        value = p.get("position_value")
        if value is None or not math.isfinite(value) or value < 0:
            continue  # Counted separately as an unknown valuation, never zero.
        if value < config.get("min_usd", 0):
            label = "excluded_below_minimum"
        elif p.get("liq_px") is None:
            label = "excluded_null_liquidation"
        elif mid is None or not math.isfinite(mid) or mid <= 0:
            label = "unknown_mid"
        else:
            label = _frame_bin(p.get("szi"), p.get("liq_px"), mid,
                               config.get("band_pct", 1), config.get("range_pct", 30)) or "excluded_direction"
        totals[label] = totals.get(label, 0.0) + value
    return totals


def _frame_decomposition(previous, current, stale_users=(), common_cohort=(), old_mark=None,
                         new_mark=None, config=None, previous_config=None):
    """Per-bin additive change: cohort/positions at old mid, then re-bin at new mid.

    Values are the API's reported positionValue. A common-position value change
    can include mark-to-market; it is not necessarily a trade or capital flow.
    """
    old = {p["user"]: p for p in (previous or [])}
    new = {p["user"]: p for p in current}
    stale_users = set(stale_users)
    common_cohort = set(common_cohort) - stale_users
    config = config or {"band_pct": 1, "range_pct": 30, "min_usd": 0}
    fixed = previous_config or config
    def subset(mapping, users):
        return [p for u, p in mapping.items() if u in users]
    def amounts(mapping, users, mid, cfg):
        return _frame_buckets(subset(mapping, users), mid, cfg)
    old_all = _frame_buckets(old.values(), old_mark, fixed)
    new_fixed = _frame_buckets(new.values(), old_mark, fixed)
    new_all = _frame_buckets(new.values(), new_mark, config)
    joins = amounts(new, set(new) - common_cohort, old_mark, fixed)
    exits = amounts(old, set(old) - common_cohort - stale_users, old_mark, fixed)
    stale = amounts(old, stale_users, old_mark, fixed)
    common_old = amounts(old, common_cohort, old_mark, fixed)
    common_new = amounts(new, common_cohort, old_mark, fixed)
    per_bin = {}
    for label in sorted(set(old_all) | set(new_fixed) | set(new_all)):
        delta = new_all.get(label, 0) - old_all.get(label, 0)
        parts = {"joins_or_refreshed": joins.get(label, 0), "departures": -exits.get(label, 0),
                 "staleness": -stale.get(label, 0),
                 "common_position_change_fixed_mid": common_new.get(label, 0) - common_old.get(label, 0),
                 "mid_or_bin_change": new_all.get(label, 0) - new_fixed.get(label, 0)}
        residual = delta - sum(parts.values())
        per_bin[label] = {"old_notional": old_all.get(label, 0), "new_notional": new_all.get(label, 0),
                          "delta_notional": delta, **parts, "residual_notional": residual}
    retained = set(old) & set(new)
    bin_changed = sum(_frame_buckets([old[u]], old_mark, fixed).keys() !=
                      _frame_buckets([new[u]], new_mark, config).keys() for u in retained)
    fixed_changed = sum(_frame_buckets([old[u]], old_mark, fixed).keys() !=
                        _frame_buckets([new[u]], old_mark, fixed).keys() for u in retained)
    repriced = sum(_frame_buckets([new[u]], old_mark, fixed).keys() !=
                   _frame_buckets([new[u]], new_mark, config).keys() for u in retained)
    residual = sum(p["residual_notional"] for p in per_bin.values())
    unknown_values = sum(p.get("position_value") is None or not math.isfinite(p["position_value"])
                         for p in [*old.values(), *new.values()])
    known_delta = sum(new_all.values()) - sum(old_all.values())
    return {
        "join_notional": sum(joins.values()), "exit_notional": sum(exits.values()),
        "stale_notional": sum(stale.values()),
        "common_cohort_position_change": sum(common_new.values()) - sum(common_old.values()),
        "common_closed_count": len((set(old) - set(new)) & common_cohort),
        "bin_changed_count": bin_changed, "fixed_old_mid_bin_changed_count": fixed_changed,
        "repricing_bin_changed_count": repriced,
        "delta_notional": known_delta if not unknown_values else None,
        "known_subtotal_delta_notional": known_delta,
        "per_bin": per_bin, "residual_notional": residual,
        "price_comparison_available": bool(old_mark and new_mark),
        "unknown_valuation_count": unknown_values,
        "valuation_complete": not unknown_values,
        "reconciled": not unknown_values and all(abs(p["residual_notional"]) < 1e-6 for p in per_bin.values()),
    }


def save_observation_frame(conn, coins, ts_min, mids=None, mid_observed_ts=None):
    """Append one frozen aggregation frame; never synthesise missing history."""
    config = {"band_pct": LEVELS_DEFAULT_BAND_PCT, "range_pct": LEVELS_DEFAULT_RANGE_PCT,
              "min_usd": LEVELS_DEFAULT_MIN_USD, "fresh_min": LEVELS_DEFAULT_FRESH_MIN}
    now = now_ms()
    cutoff = now - int(config["fresh_min"] * 60 * 1000)
    previous_row = conn.execute(
        "SELECT frame_json FROM observation_frames ORDER BY frame_id DESC LIMIT 1"
    ).fetchone()
    previous = json.loads(previous_row[0]) if previous_row else {}
    frame = {"version": 1, "ts_min": ts_min, "captured_ts": now, "ladder_config": config,
             "interpretation": "Distance is relative to saved mid, not the liquidation-trigger mark. Observed cohort only; no market-wide coverage estimate.",
             "coins": {}, "cohort": CANDIDATES.snapshot_with_coins()}
    frame["attempt_cursor"] = conn.execute("SELECT COALESCE(MAX(attempt_id),0) FROM position_attempts").fetchone()[0]
    frame["attempts_since_previous"] = {status: count for status, count in conn.execute(
        "SELECT status,COUNT(*) FROM position_attempts WHERE attempt_id>? AND attempt_id<=? GROUP BY status",
        (previous.get("attempt_cursor", 0), frame["attempt_cursor"])).fetchall()}
    old_cohort = {x["user"] for x in previous.get("cohort", [])}
    cohort = {x["user"] for x in frame["cohort"]}
    # Drain once before decomposition so TTL removals have the same classification
    # in the cohort metadata and every coin's notional-change ledger.
    stale = set(CANDIDATES.drain_stale()) & (old_cohort - cohort)
    mids = mids or {}
    for coin in coins:
        flow = conn.execute(
            "SELECT active_buy, active_sell, buy_rate_usd_min, sell_rate_usd_min, "
            "buy_remaining_usd, sell_remaining_usd FROM twap_flow WHERE coin=? AND ts_min=?",
            (coin, ts_min),
        ).fetchone()
        mark = mids.get(coin)
        if mark is not None and (not math.isfinite(mark) or mark <= 0):
            mark = None
        positions = conn.execute(latest_position_cte() + """
            SELECT user, ts, szi, position_value, liq_px FROM latest_positions WHERE coin=?
            """, (coin,)).fetchall()
        # clearinghouseState observes every coin for each target address.
        candidate_users = [x["user"] for x in frame["cohort"]]
        statuses = {"success": 0, "empty": 0, "failure": 0, "unobserved": 0}
        candidate_states = []
        candidate_ages = []
        success_ages = []
        fresh_users = set()
        for user in candidate_users:
            attempt = conn.execute("SELECT observed_ts, status FROM position_attempts WHERE user=? ORDER BY attempt_id DESC LIMIT 1", (user,)).fetchone()
            if attempt is None:
                statuses["unobserved"] += 1
                state = {"user": user, "status": "unobserved", "observed_ts": None}
            else:
                statuses[attempt[1]] += 1
                state = {"user": user, "status": attempt[1], "observed_ts": attempt[0]}
                candidate_ages.append(max(0, now - attempt[0]))
            success = conn.execute("SELECT observed_ts, status FROM position_attempts WHERE user=? "
                "AND status IN ('success','empty') ORDER BY attempt_id DESC LIMIT 1", (user,)).fetchone()
            state["success_observed_ts"] = success[0] if success else None
            state["success_status"] = success[1] if success else None
            state["success_age_ms"] = max(0, now - success[0]) if success else None
            state["fresh_success"] = bool(success and success[0] >= cutoff)
            if success:
                success_ages.append(state["success_age_ms"])
            if state["fresh_success"]:
                fresh_users.add(user)
            candidate_states.append(state)
        fresh = [row for row in positions if row[0] in fresh_users and row[1] >= cutoff]
        stale_users = (set(candidate_users) - fresh_users) | stale
        fresh_success_addresses = len(fresh_users)
        ladder_inputs = [{"user": u, "ts": t, "szi": szi, "position_value": value, "liq_px": liq}
                         for u, t, szi, value, liq in fresh]
        frozen_positions = []
        null_liq = 0
        for user, ts, szi, value, liq_px in fresh:
            if liq_px is None:
                null_liq += 1
            if value is None or value < config["min_usd"]:
                continue
            b = _frame_bin(szi, liq_px, mark, config["band_pct"], config["range_pct"])
            if b is not None:
                frozen_positions.append({"user": user, "ts": ts, "szi": szi,
                                         "position_value": value, "liq_px": liq_px, "bin": b})
        active, declared = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN declared_sz IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM twap_orders WHERE coin=? AND status='active'", (coin,)
        ).fetchone()
        orders = conn.execute(
            "SELECT twap_id, user, side, declared_sz, declared_minutes, reduce_only, start_ts, cum_sz, cum_notional "
            "FROM twap_orders WHERE coin=? AND status='active' ORDER BY twap_id", (coin,)
        ).fetchall()
        old_coin = (previous.get("coins") or {}).get(coin, {})
        old_mid = old_coin.get("mid") or {}
        old_mid = old_mid.get("value") if isinstance(old_mid, dict) else old_mid
        frame["coins"][coin] = {
            "twap": {"active": active, "declared": declared or 0,
                     "declaration_rate": (declared or 0) / active if active else None,
                     "flow": list(flow) if flow else None,
                     "orders": [{"twap_id": row[0], "user": row[1], "side": row[2], "declared_sz": row[3],
                                 "declared_minutes": row[4], "reduce_only": row[5], "start_ts": row[6],
                                 "cum_sz": row[7], "cum_notional": row[8]} for row in orders]},
            "mid": {"value": mark, "observed_ts": now if mark is not None else None, "price_type": "mid"},
            "freshness": {"candidate_denominator": len(candidate_users), "attempt_status": statuses,
                          "denominator_definition": "all current candidate addresses; every account request covers all coins",
                          "stale_candidates": sum(s["success_age_ms"] is not None and s["success_age_ms"] > config["fresh_min"] * 60 * 1000 for s in candidate_states),
                          "fresh_success_addresses": fresh_success_addresses,
                          "fresh_success_rate": fresh_success_addresses / len(candidate_users) if candidate_users else None,
                          "candidate_states": candidate_states,
                          "age_ms_min": min(candidate_ages) if candidate_ages else None,
                          "age_ms_p50": sorted(candidate_ages)[len(candidate_ages)//2] if candidate_ages else None,
                          "age_ms_max": max(candidate_ages) if candidate_ages else None,
                          "success_snapshot_age_ms_p50": sorted(success_ages)[len(success_ages)//2] if success_ages else None,
                          "latest_positions": len(positions), "fresh_positions": len(fresh),
                          "legacy_or_outside_cohort_positions": len(positions) - len(fresh),
                          "unknown_position_value_count": sum(p["position_value"] is None for p in ladder_inputs),
                          "null_liq": null_liq, "null_liq_rate": null_liq / len(fresh) if fresh else None},
            "ladder_inputs": ladder_inputs,
            "ladder_positions": frozen_positions,
            "buckets": _frame_buckets(ladder_inputs, mark, config),
            "decomposition": _frame_decomposition(old_coin.get("ladder_inputs", old_coin.get("ladder_positions")),
                ladder_inputs, stale_users,
                fresh_users & {s["user"] for s in old_coin.get("freshness", {}).get("candidate_states", []) if s.get("fresh_success")},
                old_mid, mark, config, previous.get("ladder_config")),
        }
        frame["coins"][coin]["mid"]["observed_ts"] = (mid_observed_ts or now) if mark is not None else None
    frame["cohort_changes"] = {"joined": sorted(cohort - old_cohort),
                                "exited": [{"user": user, "departure_observed_ts": now}
                                           for user in sorted((old_cohort - cohort) - stale)],
                                "stale": [{"user": user, "departure_observed_ts": now} for user in sorted(stale)],
                                "reconciled": len(cohort) == len(old_cohort) + len(cohort - old_cohort)
                                - len((old_cohort - cohort) - stale) - len(stale)}
    conn.execute(
        "INSERT INTO observation_frames(ts_min, created_ts, coins_json, candidate_json, frame_json) VALUES (?,?,?,?,?)",
        (ts_min, now, json.dumps(coins, sort_keys=True), json.dumps(frame["cohort"], sort_keys=True),
         json.dumps(frame, sort_keys=True, separators=(",", ":"))),
    )


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------
def run_loop(coins, duration_min, executor):
    conn = get_conn()
    ASSET_RESOLVER.load()

    start_time = time.time()
    end_time = start_time + duration_min * 60 if duration_min else None

    next_discovery = 0.0
    next_position_snapshot = 0.0
    next_aggregation = 0.0
    next_purge = 0.0

    log(f"starting hl-watch run loop: coins={coins} duration_min={duration_min or 'infinite'}")

    while not SHUTDOWN.is_set():
        now = time.time()
        if end_time is not None and now >= end_time:
            log("duration reached, stopping loop")
            break

        futures = []

        # 発見: 対象コインごと recentTrades を20秒毎
        if now >= next_discovery:
            for coin in coins:
                futures.append(executor.submit(discover_candidates, coin))
            next_discovery = now + DISCOVERY_INTERVAL_S

        # 判定: 未確認は5分毎、active保有は60秒毎
        candidates = CANDIDATES.snapshot()
        with STATE_LOCK:
            confirmed = set(CONFIRMED_TWAP_USERS)

        for addr in candidates:
            if SHUTDOWN.is_set():
                break
            if addr in confirmed:
                last = ACTIVE_LAST_CHECK.get(addr, 0)
                if now - last >= ACTIVE_CHECK_INTERVAL_S:
                    ACTIVE_LAST_CHECK[addr] = now
                    futures.append(executor.submit(check_user_twaps, conn, addr))
            else:
                last = UNCONFIRMED_LAST_CHECK.get(addr, 0)
                if now - last >= UNCONFIRMED_CHECK_INTERVAL_S:
                    UNCONFIRMED_LAST_CHECK[addr] = now
                    futures.append(executor.submit(check_user_twaps, conn, addr))

        # 対象者スナップショット: active TWAP保有ユーザーへ clearinghouseState 60秒毎
        if now >= next_position_snapshot:
            with STATE_LOCK:
                confirmed_now = set(CONFIRMED_TWAP_USERS)
            for user in confirmed_now:
                futures.append(executor.submit(snapshot_position, conn, user))
            next_position_snapshot = now + POSITION_SNAPSHOT_INTERVAL_S

        # 候補スナップショット: active TWAP非保有の発見済みアドレス全体へ clearinghouseState 5分毎/addr
        with STATE_LOCK:
            confirmed_for_snapshot = set(CONFIRMED_TWAP_USERS)
        for addr in candidates:
            if SHUTDOWN.is_set():
                break
            if addr in confirmed_for_snapshot:
                continue  # active TWAP保有userは上の60秒毎スナップショットでカバー済み
            last = CANDIDATE_SNAPSHOT_LAST_CHECK.get(addr, 0)
            if now - last >= CANDIDATE_SNAPSHOT_INTERVAL_S:
                CANDIDATE_SNAPSHOT_LAST_CHECK[addr] = now
                futures.append(executor.submit(snapshot_position, conn, addr))

        # 候補パージ
        if now >= next_purge:
            removed = CANDIDATES.purge_stale()
            if removed:
                log(f"purged {removed} stale candidates")
            next_purge = now + 60

        # futures 完了待ち (例外は握りつぶしログのみ; 収集ループを止めない)
        for fut in futures:
            try:
                fut.result(timeout=30)
            except Exception as e:
                log(f"  task error (ignored, loop continues): {e}")

        # 集計: 毎分
        if now >= next_aggregation:
            try:
                aggregate_flow(conn, coins)
            except Exception as e:
                log(f"  aggregation error (ignored): {e}")
            next_aggregation = now + AGGREGATION_INTERVAL_S

        SHUTDOWN.wait(MAIN_LOOP_TICK_S)

    log("run loop exited, closing DB")
    conn.close()


# ---------------------------------------------------------------------------
# CLI: status
# ---------------------------------------------------------------------------
def cmd_status(args):
    conn = get_conn()
    now = now_ms()
    cur = conn.execute(
        "SELECT twap_id, user, coin, side, declared_sz, declared_minutes, cum_sz, "
        "start_ts, last_fill_ts, cum_notional FROM twap_orders WHERE status='active' "
        "ORDER BY coin, side"
    )
    orders = cur.fetchall()
    if not orders:
        print("No active TWAP orders found.")
        conn.close()
        return

    print(f"{'coin':<6}{'side':<5}{'user':<44}{'rate($/min)':>12}{'declared':>12}{'remaining':>12}{'elapsed(min)':>13}"
          f"{'szi':>12}{'entryPx':>12}{'liqPx':>12}{'lev':>6}")
    print("-" * 150)
    for (twap_id, user, coin, side, declared_sz, declared_minutes, cum_sz,
         start_ts, last_fill_ts, cum_notional) in orders:
        elapsed_min = (now - start_ts) / 60000.0 if start_ts else 0.0
        rate_usd_min = (cum_notional / elapsed_min) if elapsed_min > 0.01 else 0.0
        remaining = (declared_sz - cum_sz) if declared_sz is not None else None

        pos = conn.execute(latest_position_cte() + """
            SELECT szi, entry_px, liq_px, lev FROM latest_positions
            WHERE user=? AND coin=? LIMIT 1
            """,
            (user, coin),
        ).fetchone()
        szi, entry_px, liq_px, lev = pos if pos else (None, None, None, None)

        def fmt(v, nd=2):
            return f"{v:.{nd}f}" if v is not None else "-"

        side_label = "BUY" if side == "B" else "SELL"
        print(
            f"{coin:<6}{side_label:<5}{user:<44}{fmt(rate_usd_min):>12}"
            f"{fmt(declared_sz, 4) if declared_sz is not None else '-':>12}"
            f"{fmt(remaining, 4) if remaining is not None else '-':>12}"
            f"{fmt(elapsed_min, 1):>13}"
            f"{fmt(szi, 4):>12}{fmt(entry_px):>12}{fmt(liq_px):>12}{fmt(lev, 1):>6}"
        )
    conn.close()


# ---------------------------------------------------------------------------
# CLI: flow
# ---------------------------------------------------------------------------
def cmd_flow(args):
    conn = get_conn()
    q = "SELECT coin, ts_min, active_buy, active_sell, buy_rate_usd_min, sell_rate_usd_min, " \
        "buy_remaining_usd, sell_remaining_usd FROM twap_flow"
    params = []
    if args.coin:
        q += " WHERE coin=?"
        params.append(args.coin)
    q += " ORDER BY ts_min DESC LIMIT ?"
    params.append(args.limit)
    cur = conn.execute(q, params)
    rows = cur.fetchall()
    if not rows:
        print("No twap_flow rows found.")
        conn.close()
        return

    print(f"{'coin':<6}{'ts(UTC)':<21}{'buy#':>5}{'sell#':>6}{'buyRate$/min':>14}{'sellRate$/min':>14}"
          f"{'buyRemain$':>14}{'sellRemain$':>14}")
    print("-" * 100)
    for (coin, ts_min, active_buy, active_sell, buy_rate, sell_rate, buy_rem, sell_rem) in reversed(rows):
        ts_str = datetime.fromtimestamp(ts_min / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def fmt(v):
            return f"{v:.2f}" if v is not None else "-"

        print(f"{coin:<6}{ts_str:<21}{active_buy:>5}{active_sell:>6}{fmt(buy_rate):>14}{fmt(sell_rate):>14}"
              f"{fmt(buy_rem):>14}{fmt(sell_rem):>14}")
    conn.close()


def cmd_history(args):
    """Show stored aggregate frames only; collection is deliberately never implied."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT frame_id, ts_min, frame_json FROM observation_frames ORDER BY frame_id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("No observation history collected yet. Run `hl-watch run` to collect frames.")
        conn.close()
        return
    for frame_id, ts_min, payload in reversed(rows):
        frame = json.loads(payload)
        if args.coin and args.coin.upper() not in frame.get("coins", {}):
            continue
        if args.json:
            print(json.dumps(public_observation_frame(frame, args.include_addresses) | {"frame_id": frame_id}, sort_keys=True))
            continue
        print(f"{frame_id}\t{ts_min}\tcohort={len(frame.get('cohort', []))}")
        for coin, data in frame.get("coins", {}).items():
            if args.coin and coin != args.coin.upper():
                continue
            quality = data["freshness"]
            twap = data["twap"]
            print(f"  {coin}: mid={data['mid']} active={twap['active']} declared={twap['declaration_rate']} "
                  f"fresh_success={quality['fresh_success_addresses']}/{quality['candidate_denominator']} "
                  f"null_liq={quality['null_liq_rate']}")
    conn.close()


def cmd_replay(args):
    """Render a frozen frame, using no live price, account, or API data."""
    conn = get_conn()
    if args.frame_id is None:
        row = conn.execute("SELECT frame_id, frame_json FROM observation_frames ORDER BY frame_id DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT frame_id, frame_json FROM observation_frames WHERE frame_id=?", (args.frame_id,)).fetchone()
    conn.close()
    if not row:
        print("No matching observation frame. Replay requires collected history.")
        return
    frame_id, payload = row
    frame = json.loads(payload)
    if args.json:
        print(json.dumps(public_observation_frame(frame, args.include_addresses) | {"frame_id": frame_id}, sort_keys=True))
        return
    print(f"replay frame={frame_id} ts_min={frame['ts_min']} config={frame['ladder_config']}")
    print(frame.get("interpretation", "Distances use saved mid, not the liquidation-trigger mark."))
    for coin, data in frame.get("coins", {}).items():
        d = data["decomposition"]
        print(f"{coin}: mid={data['mid']} ladder={len(data['ladder_positions'])} "
              f"join={d['join_notional']:.2f} exit={d['exit_notional']:.2f} stale={d['stale_notional']:.2f} "
              f"bin_changes={d['bin_changed_count']} reconciled={d['reconciled']}")
        for bucket, change in d.get("per_bin", {}).items():
            if change["delta_notional"] or change["mid_or_bin_change"]:
                print(f"  {bucket}: delta={change['delta_notional']:.2f} "
                      f"common_fixed_mid={change['common_position_change_fixed_mid']:.2f} "
                      f"mid/bin={change['mid_or_bin_change']:.2f} residual={change['residual_notional']:.2f}")


def public_observation_frame(frame, include_addresses=False):
    """Aggregate output defaults to counts; raw cohorts require an explicit flag."""
    if include_addresses:
        return frame
    result = json.loads(json.dumps(frame))
    result["cohort_count"] = len(result.pop("cohort", []))
    for key in ("joined", "exited", "stale"):
        changes = result.get("cohort_changes", {})
        if key in changes:
            changes[key + "_count"] = len(changes.pop(key))
    for coin in result.get("coins", {}).values():
        coin["ladder_position_count"] = len(coin.pop("ladder_positions", []))
        coin.pop("ladder_inputs", None)
        coin.get("freshness", {}).pop("candidate_states", None)
        coin.get("twap", {}).pop("orders", None)
    return result


# ---------------------------------------------------------------------------
# analyze: ヘルパー
# ---------------------------------------------------------------------------
def short_addr(addr):
    if not addr or len(addr) < 12:
        return addr or "-"
    return f"{addr[:6]}...{addr[-4:]}"


def fetch_all_mids():
    """{'type': 'allMids'} を取得し coin -> float の dict を返す。失敗時は空dict。"""
    resp = hl_info({"type": "allMids"})
    if not resp or not isinstance(resp, dict):
        return {}
    out = {}
    for coin, px_str in resp.items():
        try:
            value = float(px_str)
            if math.isfinite(value) and value > 0:
                out[coin] = value
        except (TypeError, ValueError):
            continue
    return out


def analyze_maybe_collect_burst(coins, fresh_min, no_collect):
    """DB鮮度をチェックし、必要なら run_loop バーストを実行する。
    戻り値: バーストを実行したか (bool)。
    """
    if no_collect:
        return False

    conn = get_conn()
    placeholders = ",".join("?" for _ in coins)
    cur = conn.execute(
        f"SELECT MAX(ts_min) FROM twap_flow WHERE coin IN ({placeholders})", coins
    )
    row = cur.fetchone()
    conn.close()
    latest_ts_min = row[0] if row else None

    now = now_ms()
    is_stale = latest_ts_min is None or (now - latest_ts_min) / 1000.0 > ANALYZE_FRESHNESS_THRESHOLD_S
    if not is_stale:
        return False

    log(f"data stale (latest_ts_min={latest_ts_min}), running {fresh_min}min collection burst...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        run_loop(coins, fresh_min, executor)
    return True


def analyze_coin_summary(conn, coin, mid_price, now):
    """§1 コイン別需給サマリの1コイン分データを組み立てる。"""
    five_min_ago = now - 5 * 60 * 1000
    new_window_ago = now - ANALYZE_NEW_TWAP_WINDOW_S * 1000

    orders = conn.execute(
        "SELECT twap_id, side, declared_sz, cum_sz, start_ts FROM twap_orders "
        "WHERE coin=? AND status='active'",
        (coin,),
    ).fetchall()

    active_buy = sum(1 for o in orders if o[1] == "B")
    active_sell = sum(1 for o in orders if o[1] == "A")
    new_buy = sum(1 for o in orders if o[1] == "B" and (o[4] or 0) >= new_window_ago)
    new_sell = sum(1 for o in orders if o[1] == "A" and (o[4] or 0) >= new_window_ago)

    # 直近5分の実行レート ($/min)。開始5分未満のTWAPは除外
    mature_twap_ids = [o[0] for o in orders if (o[4] or 0) < new_window_ago]
    buy_rate = 0.0
    sell_rate = 0.0
    if mature_twap_ids:
        ph = ",".join("?" for _ in mature_twap_ids)
        rate_cur = conn.execute(
            f"SELECT tf.side, SUM(tf.px*tf.sz) FROM twap_fills tf "
            f"WHERE tf.twap_id IN ({ph}) AND tf.ts >= ? GROUP BY tf.side",
            (*mature_twap_ids, five_min_ago),
        )
        sums = {r[0]: r[1] for r in rate_cur.fetchall()}
        buy_rate = (sums.get("B", 0.0) or 0.0) / 5.0
        sell_rate = (sums.get("A", 0.0) or 0.0) / 5.0

    buy_remaining_usd = 0.0
    sell_remaining_usd = 0.0
    buy_has_declared = False
    sell_has_declared = False
    for twap_id, side, declared_sz, cum_sz, start_ts in orders:
        if declared_sz is None or declared_sz <= 0:
            continue
        remaining_sz = max(declared_sz - cum_sz, 0.0)
        px_row = conn.execute(
            "SELECT px FROM twap_fills WHERE twap_id=? ORDER BY ts DESC LIMIT 1", (twap_id,)
        ).fetchone()
        px = px_row[0] if px_row else mid_price
        if px is None:
            continue
        remaining_usd = remaining_sz * px
        if side == "B":
            buy_remaining_usd += remaining_usd
            buy_has_declared = True
        else:
            sell_remaining_usd += remaining_usd
            sell_has_declared = True

    return {
        "coin": coin,
        "mid": mid_price,
        "active_buy": active_buy,
        "active_sell": active_sell,
        "new_buy": new_buy,
        "new_sell": new_sell,
        "buy_rate": buy_rate,
        "sell_rate": sell_rate,
        "buy_remaining_usd": buy_remaining_usd if buy_has_declared else None,
        "sell_remaining_usd": sell_remaining_usd if sell_has_declared else None,
    }


def bias_label(a, b, name_a, name_b):
    """a と b (両方 >=0) を比較し、偏りラベルを返す。"""
    if a <= 0 and b <= 0:
        return "拮抗(データなし)"
    if b <= 0:
        return f"{name_a}優勢(∞倍)" if a > 0 else "拮抗"
    if a <= 0:
        return f"{name_b}優勢(∞倍)" if b > 0 else "拮抗"
    ratio = a / b if a >= b else b / a
    if ratio < ANALYZE_BIAS_MILD_RATIO:
        return f"拮抗({ratio:.1f}倍)"
    winner = name_a if a >= b else name_b
    if ratio >= ANALYZE_BIAS_STRONG_RATIO:
        return f"{winner}優勢({ratio:.1f}倍)"
    return f"{winner}優位気味({ratio:.1f}倍)"


def render_section1(conn, coins, mids, now, lines):
    lines.append("=" * 78)
    lines.append("§1 コイン別需給サマリ")
    lines.append("=" * 78)
    summaries = {}
    for coin in coins:
        mid = mids.get(coin)
        if mid is None:
            lines.append(f"  {coin}: mid価格取得不可のためスキップ (allMids に {coin} が見つからない)")
            continue
        s = analyze_coin_summary(conn, coin, mid, now)
        summaries[coin] = s
        rate_bias = bias_label(s["buy_rate"], s["sell_rate"], "買い", "売り")
        rem_bias = bias_label(
            s["buy_remaining_usd"] or 0.0, s["sell_remaining_usd"] or 0.0, "買い残", "売り残"
        )
        rem_buy_str = f"${s['buy_remaining_usd']:,.0f}" if s["buy_remaining_usd"] is not None else "不明"
        rem_sell_str = f"${s['sell_remaining_usd']:,.0f}" if s["sell_remaining_usd"] is not None else "不明"
        lines.append(
            f"  {coin:<5} mid=${mid:,.2f}  active TWAP buy={s['active_buy']}(うち新規{s['new_buy']}本) "
            f"sell={s['active_sell']}(うち新規{s['new_sell']}本)"
        )
        lines.append(
            f"        実行レート: buy=${s['buy_rate']:,.0f}/min sell=${s['sell_rate']:,.0f}/min -> {rate_bias}"
        )
        lines.append(
            f"        宣言残量:   buy={rem_buy_str} sell={rem_sell_str} -> {rem_bias}"
        )
    return summaries


def render_section2(conn, coins, mids, now, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"§2 注目TWAP (対象コイン全体で上位{ANALYZE_TOP_TWAP_LIMIT}件)")
    lines.append("=" * 78)
    if not coins:
        lines.append("  対象コインなし")
        return
    ph = ",".join("?" for _ in coins)
    orders = conn.execute(
        f"SELECT twap_id, user, coin, side, declared_sz, cum_sz, start_ts, reduce_only "
        f"FROM twap_orders WHERE coin IN ({ph}) AND status='active'",
        coins,
    ).fetchall()
    if not orders:
        lines.append("  active TWAP なし")
        return

    ranked = []
    for twap_id, user, coin, side, declared_sz, cum_sz, start_ts, reduce_only in orders:
        mid = mids.get(coin)
        px_row = conn.execute(
            "SELECT px FROM twap_fills WHERE twap_id=? ORDER BY ts DESC LIMIT 1", (twap_id,)
        ).fetchone()
        last_px = px_row[0] if px_row else mid

        elapsed_min = (now - start_ts) / 60000.0 if start_ts else 0.0
        rate_usd_min = None
        if elapsed_min > 0.01 and last_px is not None:
            cum_notional_row = conn.execute(
                "SELECT SUM(px*sz) FROM twap_fills WHERE twap_id=?", (twap_id,)
            ).fetchone()
            cum_notional = cum_notional_row[0] if cum_notional_row and cum_notional_row[0] else 0.0
            rate_usd_min = cum_notional / elapsed_min

        declared_str = f"{declared_sz:.4f}" if declared_sz is not None else "不明"
        if declared_sz is not None and declared_sz > 0:
            remaining_sz = max(declared_sz - cum_sz, 0.0)
            progress_pct = min(cum_sz / declared_sz * 100.0, 100.0)
            remaining_usd = remaining_sz * last_px if last_px is not None else None
            has_declared = True
        else:
            remaining_sz = None
            progress_pct = None
            remaining_usd = None
            has_declared = False

        rank_key = (
            1 if has_declared else 0,
            remaining_usd if remaining_usd is not None else 0.0,
            rate_usd_min if rate_usd_min is not None else 0.0,
        )
        ranked.append({
            "twap_id": twap_id, "user": user, "coin": coin, "side": side,
            "declared_str": declared_str, "remaining_sz": remaining_sz,
            "remaining_usd": remaining_usd, "elapsed_min": elapsed_min,
            "progress_pct": progress_pct, "reduce_only": reduce_only,
            "rate_usd_min": rate_usd_min, "rank_key": rank_key,
        })

    ranked.sort(key=lambda r: r["rank_key"], reverse=True)
    top = ranked[:ANALYZE_TOP_TWAP_LIMIT]

    for r in top:
        side_label = "BUY" if r["side"] == "B" else "SELL"
        rate_str = f"${r['rate_usd_min']:,.0f}/min" if r["rate_usd_min"] is not None else "不明"
        rem_str = (
            f"{r['remaining_sz']:.4f}枚(${r['remaining_usd']:,.0f})"
            if r["remaining_sz"] is not None and r["remaining_usd"] is not None
            else "不明"
        )
        prog_str = f"{r['progress_pct']:.1f}%" if r["progress_pct"] is not None else "不明"
        ro_str = "reduce_only" if r["reduce_only"] else "-"

        pos = conn.execute(latest_position_cte() + """
            SELECT szi FROM latest_positions WHERE user=? AND coin=? LIMIT 1
            """,
            (r["user"], r["coin"]),
        ).fetchone()
        szi = pos[0] if pos and pos[0] is not None else None
        is_buy = r["side"] == "B"
        if szi is None:
            ctx = "新規/flip?"
        elif szi < 0 and is_buy:
            ctx = "ショート買い戻し(closing)"
        elif szi > 0 and not is_buy:
            ctx = "ロング利確/縮小(closing)"
        elif szi > 0 and is_buy:
            ctx = "ロング積み増し(building)"
        elif szi < 0 and not is_buy:
            ctx = "ショート積み増し(building)"
        else:
            ctx = "新規/flip?"

        lines.append(
            f"  {r['coin']:<5}{side_label:<5}{short_addr(r['user']):<17} rate={rate_str:<14} "
            f"宣言={r['declared_str']:<10} 残={rem_str:<20} 経過={r['elapsed_min']:.1f}min "
            f"進捗={prog_str:<7} {ro_str:<12} [{ctx}]"
        )
    return


def render_section3(conn, coins, now, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"§3 清算近接ポジション (上位{ANALYZE_TOP_LIQ_LIMIT}件)")
    lines.append("=" * 78)
    if not coins:
        lines.append("  対象コインなし")
        return []
    ph = ",".join("?" for _ in coins)
    rows = conn.execute(
        latest_position_cte() + f"""
        SELECT wp.user, wp.coin, wp.szi, wp.entry_px, wp.position_value, wp.liq_px, wp.lev
        FROM latest_positions wp
        WHERE wp.coin IN ({ph}) AND wp.liq_px IS NOT NULL
        """,
        coins,
    ).fetchall()

    near = []
    for user, coin, szi, entry_px, position_value, liq_px, lev in rows:
        mid_row = conn.execute(
            "SELECT px FROM twap_fills WHERE coin=? ORDER BY ts DESC LIMIT 1", (coin,)
        ).fetchone()
        mark = mid_row[0] if mid_row else entry_px
        if not mark or mark <= 0:
            continue
        distance = abs(mark - liq_px) / mark
        if distance <= ANALYZE_LIQ_PROXIMITY_RATIO:
            near.append({
                "user": user, "coin": coin, "szi": szi, "entry_px": entry_px,
                "position_value": position_value, "liq_px": liq_px, "lev": lev,
                "distance": distance, "mark": mark,
            })

    near.sort(key=lambda r: r["distance"])
    top = near[:ANALYZE_TOP_LIQ_LIMIT]
    if not top:
        lines.append("  該当なし")
        return near

    for r in top:
        direction = "LONG" if (r["szi"] or 0) > 0 else "SHORT"
        has_active_twap = conn.execute(
            "SELECT 1 FROM twap_orders WHERE user=? AND coin=? AND status='active' LIMIT 1",
            (r["user"], r["coin"]),
        ).fetchone()
        star = " ★TWAP中" if has_active_twap else ""
        pv_str = f"${r['position_value']:,.0f}" if r["position_value"] is not None else "-"
        entry_str = f"{r['entry_px']:,.2f}" if r["entry_px"] is not None else "-"
        lev_str = f"{r['lev']:.1f}x" if r["lev"] is not None else "-"
        lines.append(
            f"  {short_addr(r['user']):<17}{r['coin']:<5}{direction:<6} サイズ={pv_str:<12} "
            f"entry={entry_str:<12} liqPx={r['liq_px']:,.2f} 距離={r['distance']*100:.1f}% "
            f"lev={lev_str}{star}"
        )
    return near


def render_section4(conn, coins, mids, summaries, near_liq, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append("§4 複合シグナル")
    lines.append("=" * 78)
    signals = []

    for coin in coins:
        s = summaries.get(coin)
        mid = mids.get(coin)
        if s is None or mid is None:
            continue

        # ショートスクイーズ素地
        if s["sell_rate"] > 0 and s["buy_rate"] >= s["sell_rate"] * ANALYZE_BIAS_STRONG_RATIO:
            band_hi = mid * (1 + ANALYZE_SQUEEZE_BAND_RATIO)
            total_short_val = sum(
                r["position_value"] or 0.0 for r in near_liq
                if r["coin"] == coin and (r["szi"] or 0) < 0 and mid <= r["liq_px"] <= band_hi
            )
            if total_short_val > 0:
                signals.append(
                    f"  - ショートスクイーズ素地: {coin} buyレート${s['buy_rate']:,.0f}/min が "
                    f"sellレート${s['sell_rate']:,.0f}/min の{s['buy_rate']/s['sell_rate']:.1f}倍。"
                    f"mid上方15%以内(${mid:,.2f}〜${band_hi:,.2f})に清算近接ショート合計"
                    f"${total_short_val:,.0f}"
                )

        # ロングスクイーズ素地
        if s["buy_rate"] > 0 and s["sell_rate"] >= s["buy_rate"] * ANALYZE_BIAS_STRONG_RATIO:
            band_lo = mid * (1 - ANALYZE_SQUEEZE_BAND_RATIO)
            total_long_val = sum(
                r["position_value"] or 0.0 for r in near_liq
                if r["coin"] == coin and (r["szi"] or 0) > 0 and band_lo <= r["liq_px"] <= mid
            )
            if total_long_val > 0:
                signals.append(
                    f"  - ロングスクイーズ素地: {coin} sellレート${s['sell_rate']:,.0f}/min が "
                    f"buyレート${s['buy_rate']:,.0f}/min の{s['sell_rate']/s['buy_rate']:.1f}倍。"
                    f"mid下方15%以内(${band_lo:,.2f}〜${mid:,.2f})に清算近接ロング合計"
                    f"${total_long_val:,.0f}"
                )

    # 大口の投げ/買い疑い: 単一TWAPのレートがコイン合計の50%超
    for coin in coins:
        s = summaries.get(coin)
        if s is None:
            continue
        total_rate = s["buy_rate"] + s["sell_rate"]
        if total_rate <= 0:
            continue
        five_min_ago = now_ms() - 5 * 60 * 1000
        new_window_ago = now_ms() - ANALYZE_NEW_TWAP_WINDOW_S * 1000
        orders = conn.execute(
            "SELECT twap_id, user, side, start_ts FROM twap_orders "
            "WHERE coin=? AND status='active' AND start_ts < ?",
            (coin, new_window_ago),
        ).fetchall()
        for twap_id, user, side, start_ts in orders:
            notional_row = conn.execute(
                "SELECT SUM(px*sz) FROM twap_fills WHERE twap_id=? AND ts >= ?",
                (twap_id, five_min_ago),
            ).fetchone()
            notional = notional_row[0] if notional_row and notional_row[0] else 0.0
            twap_rate = notional / 5.0
            if twap_rate > total_rate * 0.5:
                side_label = "買い" if side == "B" else "売り"
                signals.append(
                    f"  - 大口の{'買い' if side == 'B' else '投げ'}疑い: {coin} user={short_addr(user)} "
                    f"side={side_label} 実行レート${twap_rate:,.0f}/min が"
                    f"コイン合計${total_rate:,.0f}/minの{twap_rate/total_rate*100:.0f}%"
                )

    # 清算予備軍がTWAP脱出中
    for r in near_liq:
        szi = r["szi"] or 0
        if szi == 0:
            continue
        want_side = "B" if szi < 0 else "A"
        exit_orders = conn.execute(
            "SELECT twap_id, declared_sz, cum_sz FROM twap_orders "
            "WHERE user=? AND coin=? AND status='active' AND side=? AND reduce_only=1",
            (r["user"], r["coin"], want_side),
        ).fetchall()
        for twap_id, declared_sz, cum_sz in exit_orders:
            if declared_sz is not None and declared_sz > 0:
                remaining_sz = max(declared_sz - cum_sz, 0.0)
                px_row = conn.execute(
                    "SELECT px FROM twap_fills WHERE twap_id=? ORDER BY ts DESC LIMIT 1", (twap_id,)
                ).fetchone()
                px = px_row[0] if px_row else r["mark"]
                rem_str = f"${remaining_sz * px:,.0f}" if px else "不明"
            else:
                rem_str = "不明(宣言未判明)"
            dir_label = "ショート" if szi < 0 else "ロング"
            signals.append(
                f"  - 清算予備軍がTWAP脱出中: {short_addr(r['user'])} {r['coin']} {dir_label}保有 "
                f"(距離{r['distance']*100:.1f}%) が逆方向 reduce_only TWAP実行中, 残={rem_str}"
            )

    if not signals:
        lines.append("  なし")
    else:
        lines.extend(signals)


def render_section5(conn, coins, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append("§5 データ品質フッタ")
    lines.append("=" * 78)

    if coins:
        ph = ",".join("?" for _ in coins)
        latest_row = conn.execute(
            f"SELECT MAX(ts_min) FROM twap_flow WHERE coin IN ({ph})", coins
        ).fetchone()
        latest_ts_min = latest_row[0] if latest_row else None
    else:
        latest_ts_min = None

    if latest_ts_min:
        age_min = (now_ms() - latest_ts_min) / 60000.0
        lines.append(f"  データ最終更新: {age_min:.1f}分前 (twap_flow ts_min ベース)")
    else:
        lines.append("  データ最終更新: 不明 (twap_flow に対象コインの行なし)")

    tracked_users = conn.execute(
        "SELECT COUNT(DISTINCT user) FROM (SELECT user FROM twap_orders UNION SELECT user FROM watch_positions)"
    ).fetchone()[0]
    lines.append(f"  追跡中アドレス数: {tracked_users} (twap_orders/watch_positions の DISTINCT user)")

    if coins:
        ph = ",".join("?" for _ in coins)
        active_total = conn.execute(
            f"SELECT COUNT(*) FROM twap_orders WHERE coin IN ({ph}) AND status='active'", coins
        ).fetchone()[0]
        declared_known = conn.execute(
            f"SELECT COUNT(*) FROM twap_orders WHERE coin IN ({ph}) AND status='active' "
            f"AND declared_sz IS NOT NULL",
            coins,
        ).fetchone()[0]
    else:
        active_total = 0
        declared_known = 0
    lines.append(f"  active TWAP総数: {active_total} (対象コイン)")
    coverage_pct = (declared_known / active_total * 100.0) if active_total > 0 else 0.0
    lines.append(
        f"  Hypurrscan補完率: {declared_known}/{active_total} ({coverage_pct:.0f}%) "
        f"— declared_sz が判明している active TWAP の割合"
    )
    lines.append(
        "  注意: 実行レートは直近5分の約定に基づく概算であり、TWAPのペーシングや"
        "市場状況変化により今後変動しうる。"
    )



# ---------------------------------------------------------------------------
# levels: 価格帯別清算ウォール (OI様ラダー)
# ---------------------------------------------------------------------------
def compute_liq_levels(conn, coin, mark, band_pct, range_pct, min_usd, fresh_min, now):
    """coin の清算ウォールをバケット集計する。

    watch_positions の各(user,coin)最新スナップショット (fresh_min分以内、liq_px NOT NULL、
    position_value >= min_usd) を対象に、ロングは mark 下方・ショートは mark 上方のバケットへ分類する。

    戻り値: dict
      buckets_down / buckets_up: [{"lo_pct","hi_pct","lo_px","hi_px","notional","count","twap_notional"}]
                                  (mark に近い順)
      outside_notional / outside_count: range_pct 圏外の合計
      n_addr: 対象化されたポジション件数
      total_notional: 対象化された観測総ノーショナル$
      excluded_null_liq: liq_px NULL で除外した件数
    """
    fresh_cutoff = now - fresh_min * 60 * 1000
    rows = conn.execute(
        latest_position_cte() + """
        SELECT wp.user, wp.szi, wp.position_value, wp.liq_px
        FROM latest_positions wp
        WHERE wp.coin=? AND wp.ts >= ?
        """,
        (coin, fresh_cutoff),
    ).fetchall()

    n_bands = max(int(round(range_pct / band_pct)), 1)
    buckets_down = [
        {"lo_pct": -(i + 1) * band_pct, "hi_pct": -i * band_pct, "notional": 0.0, "count": 0, "twap_notional": 0.0}
        for i in range(n_bands)
    ]
    buckets_up = [
        {"lo_pct": i * band_pct, "hi_pct": (i + 1) * band_pct, "notional": 0.0, "count": 0, "twap_notional": 0.0}
        for i in range(n_bands)
    ]
    outside_notional = 0.0
    outside_count = 0
    n_addr = 0
    total_notional = 0.0
    excluded_null_liq = 0

    for user, szi, position_value, liq_px in rows:
        if liq_px is None:
            excluded_null_liq += 1
            continue
        if position_value is None or position_value < min_usd:
            continue
        if not mark or mark <= 0:
            continue
        szi = szi or 0.0
        is_long = szi > 0
        is_short = szi < 0
        if not is_long and not is_short:
            continue
        # ロング: liq_px は mark 下方が正常。ショート: liq_px は mark 上方が正常。
        pct = (liq_px - mark) / mark * 100.0
        if is_long and pct >= 0:
            continue  # 想定外方向 (データ異常/一時的な逆転) はスキップ
        if is_short and pct <= 0:
            continue

        n_addr += 1
        total_notional += position_value

        has_active_twap = conn.execute(
            "SELECT 1 FROM twap_orders WHERE user=? AND coin=? AND status='active' LIMIT 1",
            (user, coin),
        ).fetchone()

        abs_pct = abs(pct)
        if abs_pct > range_pct:
            outside_notional += position_value
            outside_count += 1
            continue

        idx = min(int(abs_pct / band_pct), n_bands - 1)
        bucket = buckets_down[idx] if is_long else buckets_up[idx]
        bucket["notional"] += position_value
        bucket["count"] += 1
        if has_active_twap:
            bucket["twap_notional"] += position_value

    return {
        "coin": coin,
        "mark": mark,
        "buckets_down": buckets_down,
        "buckets_up": buckets_up,
        "outside_notional": outside_notional,
        "outside_count": outside_count,
        "n_addr": n_addr,
        "total_notional": total_notional,
        "excluded_null_liq": excluded_null_liq,
        "band_pct": band_pct,
        "range_pct": range_pct,
    }


def render_liq_levels_ladder(levels, lines):
    """compute_liq_levels() の結果を1コイン分のフルラダーとして lines に追記する。"""
    coin = levels["coin"]
    mark = levels["mark"]
    lines.append(f"  {coin}  mid=${mark:,.2f} (清算判定の mark/oracle とは異なる参照価格)")
    lines.append("  " + "-" * 74)

    max_notional = max(
        [b["notional"] for b in levels["buckets_down"] + levels["buckets_up"]] + [1.0]
    )

    def fmt_row(b, cum, danger):
        width = int(round(b["notional"] / max_notional * LEVELS_BAR_MAX_WIDTH)) if b["notional"] > 0 else 0
        bar = "█" * width
        mark_flag = "!" if danger else " "
        price_lo = mark * (1 + b["lo_pct"] / 100.0)
        price_hi = mark * (1 + b["hi_pct"] / 100.0)
        row = (
            f"  {mark_flag}{b['lo_pct']:+6.1f}%〜{b['hi_pct']:+6.1f}% "
            f"(${price_lo:,.2f}〜${price_hi:,.2f})  ${b['notional']:>12,.0f} ({b['count']:>3}件)"
            f"  累積${cum:>12,.0f}  {bar}"
        )
        if b["twap_notional"] > 0:
            row += f"   ★${b['twap_notional']:,.0f} ← TWAP実行中userの清算ノーショナル"
        return row

    lines.append("  [上方 (ショート清算)]")
    cum = 0.0
    for b in reversed(levels["buckets_up"]):
        cum += b["notional"]
        danger = b["hi_pct"] <= LEVELS_DANGER_BAND_PCT
        lines.append(fmt_row(b, cum, danger))
    lines.append(f"  {'':>1}--- mid=${mark:,.2f} ---")
    cum = 0.0
    for b in levels["buckets_down"]:
        cum += b["notional"]
        danger = abs(b["lo_pct"]) <= LEVELS_DANGER_BAND_PCT
        lines.append(fmt_row(b, cum, danger))
    lines.append("  [下方 (ロング清算)]")

    lines.append(
        f"  圏外(|距離|>{levels['range_pct']:.0f}%): 累積${levels['outside_notional']:,.0f} "
        f"({levels['outside_count']}件)"
    )
    lines.append(
        f"  対象アドレス数={levels['n_addr']}  観測総ノーショナル=${levels['total_notional']:,.0f}  "
        f"liq_px NULL除外={levels['excluded_null_liq']}件"
    )
    lines.append(
        "  ※ これは観測サンプル(発見済みアドレス)ベースであり市場全体のOIではない"
    )


def liq_levels_summary_line(levels):
    """§3.5 用の1-2行サマリ文字列を生成する。"""
    coin = levels["coin"]
    down_5 = sum(b["notional"] for b in levels["buckets_down"] if abs(b["lo_pct"]) <= 5.0)
    down_5_n = sum(b["count"] for b in levels["buckets_down"] if abs(b["lo_pct"]) <= 5.0)
    up_5 = sum(b["notional"] for b in levels["buckets_up"] if b["hi_pct"] <= 5.0)
    up_5_n = sum(b["count"] for b in levels["buckets_up"] if b["hi_pct"] <= 5.0)

    all_buckets = [(-abs((b["lo_pct"] + b["hi_pct"]) / 2.0), b) for b in levels["buckets_down"]]
    all_buckets += [((b["lo_pct"] + b["hi_pct"]) / 2.0, b) for b in levels["buckets_up"]]
    thickest = max(all_buckets, key=lambda t: t[1]["notional"], default=None)

    if thickest is None or thickest[1]["notional"] <= 0:
        thickest_str = "該当なし"
    else:
        mid_pct, b = thickest
        thickest_str = f"{mid_pct:+.1f}%に${b['notional']:,.0f}"

    return (
        f"  {coin:<5} 下方5%以内 累積${down_5:,.0f} ({down_5_n}件) / "
        f"上方5%以内 累積${up_5:,.0f} ({up_5_n}件)、最厚帯: {thickest_str}"
    )


def cmd_levels(args):
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    conn = get_conn()
    now = now_ms()
    mids = fetch_all_mids()

    lines = []
    lines.append("=" * 78)
    lines.append("価格帯別 清算ウォール (OI様ラダー)")
    lines.append("=" * 78)

    for coin in coins:
        mark = mids.get(coin)
        if mark is None:
            lines.append(f"  {coin}: mid価格取得不可のためスキップ (allMids に {coin} が見つからない)")
            lines.append("")
            continue
        levels = compute_liq_levels(
            conn, coin, mark, args.band_pct, args.range_pct, args.min_usd, args.fresh_min, now
        )
        render_liq_levels_ladder(levels, lines)
        lines.append("")

    print("\n".join(lines))
    conn.close()


def render_section3_5(conn, coins, mids, now, lines):
    lines.append("")
    lines.append("=" * 78)
    lines.append("§3.5 清算ウォール要約")
    lines.append("=" * 78)
    any_coin = False
    for coin in coins:
        mark = mids.get(coin)
        if mark is None:
            continue
        any_coin = True
        levels = compute_liq_levels(
            conn, coin, mark,
            LEVELS_DEFAULT_BAND_PCT, LEVELS_DEFAULT_RANGE_PCT, LEVELS_DEFAULT_MIN_USD,
            LEVELS_DEFAULT_FRESH_MIN, now,
        )
        lines.append(liq_levels_summary_line(levels))
    if not any_coin:
        lines.append("  対象コインなし (mid価格取得不可)")
    lines.append("  フルラダーは `hl_watch.py levels` コマンドを参照。")


def cmd_analyze(args):
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]

    burst_ran = analyze_maybe_collect_burst(coins, args.fresh, args.no_collect)

    conn = get_conn()
    now = now_ms()
    mids = fetch_all_mids()

    lines = []
    if burst_ran:
        lines.append(f"[収集バースト実行: {args.fresh}分]")
    elif args.no_collect:
        lines.append("[収集スキップ: --no-collect 指定]")
    else:
        lines.append("[収集スキップ: データは十分新しい (10分以内)]")
    lines.append("")

    summaries = render_section1(conn, coins, mids, now, lines)
    render_section2(conn, coins, mids, now, lines)
    near_liq = render_section3(conn, coins, now, lines)
    render_section3_5(conn, coins, mids, now, lines)
    render_section4(conn, coins, mids, summaries, near_liq, lines)
    render_section5(conn, coins, lines)

    print("\n".join(lines))
    conn.close()


# ---------------------------------------------------------------------------
# CLI: run
# ---------------------------------------------------------------------------
def cmd_run(args):
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    with ThreadPoolExecutor(max_workers=8) as executor:
        run_loop(coins, args.duration_min, executor)


def main():
    ap = argparse.ArgumentParser(description="hl-watch: Hyperliquid TWAP watcher")
    sub = ap.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="収集ループを実行")
    p_run.add_argument("--coins", default=",".join(DEFAULT_COINS))
    p_run.add_argument("--duration-min", type=float, default=None,
                        help="指定した分数で正常終了 (テスト用)。未指定は無限ループ")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="現在の対象者リストを表示")
    p_status.set_defaults(func=cmd_status)

    p_flow = sub.add_parser("flow", help="twap_flow 時系列の直近を表示")
    p_flow.add_argument("--coin", default=None)
    p_flow.add_argument("--limit", type=int, default=60)
    p_flow.set_defaults(func=cmd_flow)

    p_history = sub.add_parser("history", help="収集済みの集計観測履歴を表示")
    p_history.add_argument("--coin", default=None)
    p_history.add_argument("--limit", type=int, default=60)
    p_history.add_argument("--json", action="store_true", help="凍結フレームを JSON で出力")
    p_history.add_argument("--include-addresses", action="store_true", help="JSON に生のアドレスを含める")
    p_history.set_defaults(func=cmd_history)

    p_replay = sub.add_parser("replay", help="凍結した観測フレームをライブデータなしで再生")
    p_replay.add_argument("--frame-id", type=int, default=None, help="既定は最新フレーム")
    p_replay.add_argument("--json", action="store_true", help="凍結フレームを JSON で出力")
    p_replay.add_argument("--include-addresses", action="store_true", help="JSON に生のアドレスを含める")
    p_replay.set_defaults(func=cmd_replay)

    p_analyze = sub.add_parser("analyze", help="需給・清算近接・複合シグナルを解析表示")
    p_analyze.add_argument("--coins", default=",".join(DEFAULT_COINS))
    p_analyze.add_argument("--fresh", type=int, default=ANALYZE_DEFAULT_BURST_MIN,
                            help="鮮度不足時に実行する収集バーストの時間 (分, 既定3)。"
                                 "鮮度チェック自体の閾値は固定10分")
    p_analyze.add_argument("--no-collect", action="store_true",
                            help="収集バーストを完全にスキップする")
    p_analyze.set_defaults(func=cmd_analyze)

    p_levels = sub.add_parser("levels", help="価格帯別 清算ウォール (OI様ラダー) を表示")
    p_levels.add_argument("--coins", default=",".join(DEFAULT_COINS))
    p_levels.add_argument("--band-pct", type=float, default=LEVELS_DEFAULT_BAND_PCT,
                           help=f"バケット刻み幅 (mid比 %%, 既定{LEVELS_DEFAULT_BAND_PCT})")
    p_levels.add_argument("--range-pct", type=float, default=LEVELS_DEFAULT_RANGE_PCT,
                           help=f"集計対象範囲 (mid比 ±%%, 既定{LEVELS_DEFAULT_RANGE_PCT})")
    p_levels.add_argument("--min-usd", type=float, default=LEVELS_DEFAULT_MIN_USD,
                           help=f"対象化する position_value 下限 ($, 既定{LEVELS_DEFAULT_MIN_USD})")
    p_levels.add_argument("--fresh-min", type=float, default=LEVELS_DEFAULT_FRESH_MIN,
                           help=f"watch_positions 鮮度閾値 (分, 既定{LEVELS_DEFAULT_FRESH_MIN})")
    p_levels.set_defaults(func=cmd_levels)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
