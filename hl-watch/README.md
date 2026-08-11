# hl-watch

Hyperliquid 上の大口 TWAP 実行者を常時観測する収集システム (MVP)。

- コイン別の「コミット済み将来フロー」を OI 様の時系列 (`twap_flow`) として蓄積
- TWAP 実行者のポジション・清算価格を「プレ取引対象者リスト」(`status` コマンド) として管理

単一プロセス・単一ファイル (`hl_watch.py`)、Python 3.12 **標準ライブラリのみ** (urllib.request, sqlite3,
concurrent.futures, json, argparse 等)。`/home/o9oem/workspace/crypto/analytics/data-tools/` の流儀
(stdlib only, 素朴な CLI, `log()` タイムスタンプ付き print) に合わせている。

## 使い方

```bash
# 収集ループ (無限、Ctrl-C / SIGTERM で graceful shutdown)
python3 hl_watch.py run --coins BTC,ETH,SOL,HYPE

# テスト用: 指定分数で正常終了
python3 hl_watch.py run --coins BTC,ETH,SOL,HYPE --duration-min 5

# 現在の対象者リスト (active TWAP × 最新ポジションの JOIN)
python3 hl_watch.py status

# twap_flow 時系列の直近を表示
python3 hl_watch.py flow --coin BTC --limit 60

# 需給・清算近接・複合シグナルのルールベース解析 (ワンショット、日本語プレーンテキスト)
python3 hl_watch.py analyze --coins BTC,ETH,SOL,HYPE

# 収集バーストをスキップして既存DBのみで解析
python3 hl_watch.py analyze --no-collect

# 収集バースト時間を5分に変更 (鮮度チェック自体の閾値は固定10分)
python3 hl_watch.py analyze --fresh 5

# 価格帯別 清算ウォール (OI様ラダー)
python3 hl_watch.py levels --coins BTC,ETH,SOL,HYPE

# ラダーの刻み幅・範囲・最小ノーショナルを変更
python3 hl_watch.py levels --coins BTC --band-pct 0.5 --range-pct 15 --min-usd 1000
```

DB は `/mnt/e/Datas/market/hl_watch.db` (SQLite, WAL)。**既存の `market.db` とは完全に別ファイル**で、
本システムは `market.db` に一切書き込まない。

## アーキテクチャ

単一プロセス + `ThreadPoolExecutor(max_workers=8)`。5秒粒度のメインループが以下のフェーズを
それぞれの間隔でディスパッチする:

1. **発見** (20秒毎/コイン): `recentTrades` の `users` から候補アドレスを LRU (上限300, TTL30分) に追加
2. **判定**: `userTwapSliceFills` を叩き `twapId` ごとに集計。未確認アドレスは5分毎、
   active TWAP 保有アドレスは60秒毎にチェック
3. **状態遷移**: 最終スライスから120秒 (30秒間隔×4) fill なし → `ended_suspected`。
   宣言総量が判明していて `cum_sz >= 宣言×0.98` → `completed`。それ以外 `active`
4. **宣言補完**: TWAP 新規検出時のみ Hypurrscan (`/twap/{address}`) を1回叩き、
   `meta`/`spotMeta` で asset id → コイン名変換した上で user+coin+side+時刻近傍でマッチ。
   失敗/データなしなら宣言なしのまま進捗ベース推定にフォールバック (呼び出しは2秒以上間隔)
5. **対象者スナップショット**: active TWAP を持つ user には **60秒毎**に、それ以外の発見済み
   候補アドレス全体 (LRU全体) にも **5分毎**に `clearinghouseState` を叩き `watch_positions` へ
   追記する (2026-08-11 levels機能追加でカバレッジ拡大。旧仕様はactive TWAP保有userのみ60秒毎)
6. **集計** (毎分): コイン別に `twap_flow` へ1行 (active buy/sell 数、直近5分実行レート、
   宣言判明分の残量)

全 HL info API リクエストは共通の `RateLimiter` (最大 ~4 req/s) でペーシングされ、429/5xx/
ネットワークエラーは指数バックオフ (最大5回リトライ) で吸収する。個別タスクの例外はメイン
ループでキャッチしてログのみ出力し、収集ループ自体は止めない。

## スキーマ

```sql
CREATE TABLE twap_orders(
  twap_id INTEGER PRIMARY KEY,   -- userTwapSliceFills の外側 twapId
  user TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL, -- 'B'=buy / 'A'=sell
  declared_sz REAL, declared_minutes INTEGER, reduce_only INTEGER, -- Hypurrscan由来, 不明ならNULL
  start_ts INTEGER, last_fill_ts INTEGER,   -- unix ms
  cum_sz REAL DEFAULT 0, cum_notional REAL DEFAULT 0,
  status TEXT DEFAULT 'active',  -- active / completed / ended_suspected
  updated_at INTEGER);

CREATE TABLE twap_fills(
  tid INTEGER PRIMARY KEY, twap_id INTEGER, ts INTEGER, coin TEXT, px REAL, sz REAL, side TEXT);

CREATE TABLE watch_positions(
  user TEXT, ts INTEGER, coin TEXT, szi REAL, entry_px REAL, position_value REAL,
  liq_px REAL,               -- NULL許容 (低レバ・ヘッジ時)
  lev REAL, lev_type TEXT, acct_value REAL, PRIMARY KEY(user, ts, coin));

CREATE TABLE twap_flow(
  coin TEXT, ts_min INTEGER, active_buy INTEGER, active_sell INTEGER,
  buy_rate_usd_min REAL, sell_rate_usd_min REAL,
  buy_remaining_usd REAL, sell_remaining_usd REAL,  -- 宣言判明分のみ, なければNULL
  PRIMARY KEY(coin, ts_min));
```

## analyze コマンド

`status`/`flow` の生データを人間が読める解析レポートに変換するワンショットコマンド。LLM 呼び出しは
一切なく、決定論的なルールベース判定のみ。stdlib のみ・既存 `hl_info()`/`get_conn()`/`DB_LOCK`/`log()`
を再利用し、新規テーブルは追加しない (read-only クエリのみ)。

### 鮮度制御

対象コインの `twap_flow.ts_min` 最新値が **固定10分** より古い (または存在しない) 場合、自動で
収集バーストを実行してから解析する。バーストは `run_loop(coins, duration_min=fresh分, executor)` を
そのまま呼び出す (`cmd_run` と同様に `ThreadPoolExecutor` を都度生成; signal ハンドラ登録はワンショット
実行のため不要)。

- `--fresh MIN`: バースト実行時間 (分, 既定3)。**鮮度チェックの閾値 (10分) 自体は変更されない**
- `--no-collect`: バーストを完全にスキップし、既存 DB のみで解析する
- バーストを実行した場合、出力冒頭に `[収集バースト実行: N分]` と明示される。スキップした場合は
  `[収集スキップ: --no-collect 指定]` または `[収集スキップ: データは十分新しい (10分以内)]` と表示

### 現在価格取得

`{"type": "allMids"}` を `hl_info()` に POST し、コイン名 → mid価格 (文字列) の flat dict を取得する
(実測: `{"BTC": "64221.5", "ETH": "1884.15", ...}`。spot ペアは `@N`/`#N` 形式のキーも混在するが対象
コイン名は素朴にそのまま引ける)。対象コインの価格が見つからない場合はそのコインをスキップし、
その旨を §1 に注記する。

### 出力構成

1. **§1 コイン別需給サマリ**: コイン毎に mark価格・active TWAP数(buy/sell、うち開始5分未満の新規本数)・
   直近5分実行レート($/min、新規TWAPは除外)・宣言残量($、判明分のみ)・買い/売りの偏り判定
   (2倍以上で「優勢」、1.5倍未満で「拮抗」、中間は「優位気味」)
2. **§2 注目TWAP**: 対象コイン全体で上位10件。宣言判明分はノーショナル降順優先、それ以外は実行レート
   降順。同user最新ポジションとの突合でポジション文脈 (「ショート買い戻し」等4パターン) を注記
3. **§3 清算近接ポジション**: 各(user,coin)の最新スナップショットのうち `|mark-liqPx|/mark<=25%` を
   距離の近い順に上位15件。active TWAP保有者には `★TWAP中` を付記
3.5. **§3.5 清算ウォール要約**: `levels` コマンドと同じロジック (`compute_liq_levels()`) を
   既定パラメータ (band=1%, range=30%, min_usd=$500, fresh_min=60分) で呼び出し、コイン毎に
   「下方5%以内累積$/件数」「上方5%以内累積$/件数」「最厚帯」を1行サマリ表示。フルラダーは
   `levels` コマンドを参照
4. **§4 複合シグナル**: ショート/ロングスクイーズ素地・大口投げ/買い疑い・清算予備軍のTWAP脱出、を
   ルールベースで検出し根拠数値付きで箇条書き (該当なしなら「なし」)
5. **§5 データ品質フッタ**: 最終更新時刻・追跡中アドレス数・active TWAP総数・Hypurrscan補完率等

## levels コマンド (価格帯別 清算ウォール)

`allMids` で現在mark取得後、`watch_positions` の最新スナップショット (`--fresh-min` 分以内、
`liq_px IS NOT NULL`、`position_value >= --min-usd`) を対象に、ロングは mark下方・ショートは
mark上方へ `szi` 符号で分類し `--band-pct` 刻みでバケット集計・ASCIIバーラダー表示する
(`compute_liq_levels()`/`render_liq_levels_ladder()`、`analyze` §3.5 とロジック共通化)。
mark±5%以内の帯には危険帯として `!` フラグが付き、TWAP実行中userの清算ノーショナルは
`★` 注記される。範囲外 (`--range-pct` 超) は「圏外」として合計表示。詳細は
`../docs/hl-watch.md` を参照。

## 既知の制約

- **`userTwapSliceFills` は全履歴を返す**: 呼び出し毎に過去の完了済み TWAP (数ヶ月前・監視対象外
  コイン含む) も含めて返ってくるため、`twap_orders` には監視対象4コイン以外や `xyz:*` (株式 perp)・
  `@N` (未解決 asset id) 等の行も混在する。`status`/`flow` は監視コインでフィルタしているため
  実害はないが、DB全体のレコード数は本来の active TWAP 数より大幅に多くなる
- **Hypurrscan の可用性が低い**: 新規検出 TWAP の相当数で `no data` になる (実測: 5分ランで
  約半数)。この場合 `declared_sz` 等は NULL のまま (`status` では `-` 表示)。宣言不明な TWAP は
  `twap_flow.buy_remaining_usd`/`sell_remaining_usd` の集計から除外される
- **asset id 未解決コイン (`@N`)**: `meta`/`spotMeta` の universe に存在しない/デリスト済み等の
  asset id は `@N` のまま表示される (resolver 未対応)
- **`liquidationPx` は低レバ・ヘッジ時 null**: `status` では `-` 表示。これは仕様上想定内
- **429 発生時は該当タスクをスキップ**: 5回リトライで諦めた場合そのポーリング周期は欠測となるが
  次周期で再試行されるため長期的な観測には影響しない
- **`status`/`flow` の表示幅**: 長いコイン名 (`xyz:PLATINUM` 等) や user アドレスで列がずれて
  見える場合があるが、内容自体は正しい (簡易固定幅フォーマットの限界)
- **重複整合性**: `INSERT OR IGNORE` (fills) / `INSERT OR REPLACE` (flow) により再起動時の
  重複挿入・上書きは安全

## 検証実績 (2026-08-11)

`python3 hl_watch.py run --coins BTC,ETH,SOL,HYPE --duration-min 5` を実行し無例外で完走
(exit code 0)。429 発生を実際に観測しバックオフ後正常継続を確認。

- `twap_orders`: 525 rows / `twap_fills`: 70,643 rows / `watch_positions`: 389 rows /
  `twap_flow`: 20 rows (4コイン×5分)
- `status` で `0xc79bcc10d7b040547cfc9b30b1fbf163199a3aeb` の BTC buy TWAP
  (`declared_sz=40.0, declared_minutes=2771, reduce_only=1`) と、その user の BTC ポジション
  (`liqPx=115595.78, lev=20.0`) を含む JOIN 表示を確認
- `PRAGMA integrity_check` = `ok`
