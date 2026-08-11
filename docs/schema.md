# スキーマリファレンス

`.schema` 実物からの転記 (2026-08-11実測)。差分確認済み (下記「検証」参照)。

## hl_watch.db (`/mnt/e/Datas/market/hl_watch.db`, SQLite WAL)

`hl_watch.py` の `SCHEMA` 定数 (hl_watch.py:215-239) と実DBの `.schema` が完全一致することを
確認済み (2026-08-11)。

```sql
CREATE TABLE twap_orders(
  twap_id INTEGER PRIMARY KEY,
  user TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
  declared_sz REAL, declared_minutes INTEGER, reduce_only INTEGER,
  start_ts INTEGER, last_fill_ts INTEGER,
  cum_sz REAL DEFAULT 0, cum_notional REAL DEFAULT 0,
  status TEXT DEFAULT 'active',
  updated_at INTEGER);
CREATE TABLE twap_fills(
  tid INTEGER PRIMARY KEY, twap_id INTEGER, ts INTEGER, coin TEXT, px REAL, sz REAL, side TEXT);
CREATE TABLE watch_positions(
  user TEXT, ts INTEGER, coin TEXT, szi REAL, entry_px REAL, position_value REAL,
  liq_px REAL,
  lev REAL, lev_type TEXT, acct_value REAL, PRIMARY KEY(user, ts, coin));
CREATE TABLE twap_flow(
  coin TEXT, ts_min INTEGER, active_buy INTEGER, active_sell INTEGER,
  buy_rate_usd_min REAL, sell_rate_usd_min REAL,
  buy_remaining_usd REAL, sell_remaining_usd REAL,
  PRIMARY KEY(coin, ts_min));
CREATE INDEX idx_twap_orders_user ON twap_orders(user);
CREATE INDEX idx_twap_orders_status ON twap_orders(status);
CREATE INDEX idx_twap_fills_twapid ON twap_fills(twap_id);
CREATE INDEX idx_watch_positions_user ON watch_positions(user);
```

### twap_orders — TWAP注文の状態

| カラム | 意味・NULL条件 |
|---|---|
| `twap_id` (PK) | `userTwapSliceFills` の外側 `twapId`。TWAP注文の一意識別子 |
| `user` | 実行アドレス (0x...) |
| `coin` | コイン名 (BTC/ETH等。監視対象外コインも混在しうる、`docs/hl-api-notes.md`参照) |
| `side` | `'B'`=buy / `'A'`=sell |
| `declared_sz` | 宣言総量。Hypurrscanでマッチできた場合のみ非NULL |
| `declared_minutes` | 宣言実行時間(分)。同上、不明ならNULL |
| `reduce_only` | 1=reduce_only、0=通常、Hypurrscan不明ならNULL |
| `start_ts` / `last_fill_ts` | unix ms。最初/最後のfill時刻 |
| `cum_sz` / `cum_notional` | 累積約定数量/ノーショナル($) |
| `status` | `active` / `completed` (充足率98%以上) / `ended_suspected` (最終fillから120秒超fillなし) |
| `updated_at` | unix ms、最終更新時刻 |

### twap_fills — TWAP個別スライス約定

| カラム | 意味 |
|---|---|
| `tid` (PK) | fillの一意ID (HL側採番) |
| `twap_id` | 親TWAP注文 (twap_orders.twap_id への論理参照、FK制約なし) |
| `ts` | unix ms |
| `coin` / `px` / `sz` / `side` | 約定コイン/価格/数量/方向 |

### watch_positions — ポジションスナップショット (時系列)

| カラム | 意味・NULL条件 |
|---|---|
| `user`, `ts`, `coin` (複合PK) | ユーザー×スナップショット時刻×コイン |
| `szi` | 符号付きサイズ (正=ロング、負=ショート) |
| `entry_px` | 平均建値 |
| `position_value` | ポジション評価額($) |
| `liq_px` | 清算価格。**低レバレッジ・ヘッジ (両建て) 時は NULL** (仕様上想定内) |
| `lev` / `lev_type` | レバレッジ倍率 / `cross` or `isolated` |
| `acct_value` | 口座全体の accountValue($)、スナップショット時点 |

収集頻度: active TWAP保有ユーザーへ60秒毎、それ以外の発見済み候補アドレス全体へ5分毎
(2026-08-11 levels機能追加で拡大)。

### twap_flow — コイン別・分足の需給集計

| カラム | 意味・NULL条件 |
|---|---|
| `coin`, `ts_min` (複合PK) | コイン×分単位タイムスタンプ(unix ms) |
| `active_buy` / `active_sell` | その時点の active TWAP本数 (buy/sell) |
| `buy_rate_usd_min` / `sell_rate_usd_min` | 直近5分の実行レート($/min) |
| `buy_remaining_usd` / `sell_remaining_usd` | 宣言判明分の残量合計($)。宣言が1件も判明していなければNULL |

## market.db (`/mnt/e/Datas/market/market.db`, SQLite, 約2.3GB)

`load_to_sqlite.py` の `SCHEMA` (`CREATE TABLE IF NOT EXISTS`のみ、既存 `index_daily` は
このスクリプトが作成したものではなく事前存在) と実DBの `.schema` を突合し一致を確認済み
(klines/funding/oi_metrics/dataset_meta の4テーブルは完全一致。`index_daily` と
`sqlite_stat1` は `load_to_sqlite.py` の管理外)。

```sql
CREATE TABLE index_daily (
  source TEXT NOT NULL, symbol TEXT NOT NULL,
  date TEXT NOT NULL, close REAL,
  PRIMARY KEY (source, symbol, date));
CREATE TABLE klines (
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
CREATE TABLE funding (
  venue TEXT NOT NULL, symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  rate REAL NOT NULL,
  interval_hours REAL,
  PRIMARY KEY (venue, symbol, ts)
);
CREATE TABLE oi_metrics (
  venue TEXT NOT NULL, symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  open_interest REAL, oi_value REAL,
  long_short_ratio REAL, top_trader_ls_ratio REAL,
  PRIMARY KEY (venue, symbol, ts)
);
CREATE TABLE dataset_meta (
  dataset TEXT PRIMARY KEY,
  period_start TEXT, period_end TEXT, rows INTEGER,
  gaps INTEGER, source TEXT, fetched_at TEXT
);
CREATE INDEX idx_klines_ts ON klines(ts);
```

### 概要

- **`ts` は全テーブル共通で UNIX ms UTC** (`load_to_sqlite.py` docstring、`normalize_ms()` で
  Binance 2025+ の一部CSVがマイクロ秒単位になっている異常値を ms へ正規化)
- `klines`: venue (`binance`/`bybit`) × market (`spot`/`perp`) × symbol の1分足OHLCV。
  実測 (2026-08-11): binance perp/spot 各2 symbol・2021-01〜2026-07 (約587万行/venue)、
  bybit perp 3 symbol・2024-09〜、bybit spot 1 symbol・2025-07〜
- `funding`: venue (`binance`/`bybit`/`hyperliquid`) × symbol のfunding rate履歴。実測:
  hyperliquid が3 symbol・約7万行と最多 (funding間隔が短いため)
- `oi_metrics`: open interest / long-short ratio (Binance daily metrics由来、2023-01以降限定)
- `dataset_meta`: 各データセットの取得範囲・行数・gap数・取得日時のメタ情報。
  `validate_and_manifest.py` が生成する `manifest.json` と対になる
- `index_daily`: FRED等の指数系日次データ (`load_to_sqlite.py` の管理外、事前に別途投入済み)

## 検証 (2026-08-11)

```bash
~/.local/bin/sqlite3 /mnt/e/Datas/market/hl_watch.db ".schema"
~/.local/bin/sqlite3 /mnt/e/Datas/market/market.db ".schema"
```
上記コマンドの出力をこのファイルに転記した (diffレベルで一致確認済み)。hl_watch.py の `SCHEMA`
定数、load_to_sqlite.py の `SCHEMA` 定数とも一致 (`CREATE TABLE IF NOT EXISTS` の `IF NOT EXISTS`
句を除き同一)。
