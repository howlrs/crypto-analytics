# hl-watch コマンドリファレンス

対象: `/home/o9oem/workspace/crypto/analytics/hl-watch/hl_watch.py` (1642行, 2026-08-11時点)。
5サブコマンド: `run` / `status` / `flow` / `analyze` / `levels`。全オプション名・デフォルト値は
`python3 hl_watch.py <sub> --help` を実行して転記 (2026-08-11実測)。

## サブコマンド一覧

### run — 収集ループ

```
usage: hl_watch.py run [-h] [--coins COINS] [--duration-min DURATION_MIN]

  --coins COINS                既定: BTC,ETH,SOL,HYPE
  --duration-min DURATION_MIN  指定した分数で正常終了 (テスト用)。未指定は無限ループ
```

5段パイプライン (下記「仕組み」参照) を5秒粒度のメインループで回し続ける。`Ctrl-C`/`SIGTERM` で
graceful shutdown (`SHUTDOWN` フラグをセットし進行中タスクの完了を待つ)。

### status — 対象者リスト表示

```
usage: hl_watch.py status [-h]
```

`twap_orders` の `status='active'` 行と `watch_positions` の同一 (user, coin) 最新行を JOIN し、
コイン/サイド/user/実行レート($/min)/宣言量/残量/経過時間/szi/entryPx/liqPx/lev を固定幅で一覧表示する。
active TWAP が1件もなければ `No active TWAP orders found.` を表示。

### flow — twap_flow 時系列表示

```
usage: hl_watch.py flow [-h] [--coin COIN] [--limit LIMIT]

  --coin COIN     既定: 指定なし (全コイン)
  --limit LIMIT   既定: 60
```

`twap_flow` テーブルを `ts_min` 降順で `--limit` 件取得し、時系列順 (古い→新しい) に表示する。
列: coin / ts(UTC) / buy#・sell# (active TWAP本数) / buyRate・sellRate ($/min) / buyRemain・sellRemain ($)。

### analyze — 需給・清算近接・複合シグナル解析

```
usage: hl_watch.py analyze [-h] [--coins COINS] [--fresh FRESH] [--no-collect]

  --coins COINS   既定: BTC,ETH,SOL,HYPE
  --fresh FRESH   鮮度不足時に実行する収集バーストの時間 (分, 既定3)。鮮度チェック自体の閾値は固定10分
  --no-collect    収集バーストを完全にスキップする
```

`status`/`flow` の生データをルールベースで解析しレポート化するワンショットコマンド。LLM呼び出しなし。

**鮮度制御**: 対象コインの `twap_flow.ts_min` 最新値が **固定10分** (`ANALYZE_FRESHNESS_THRESHOLD_S`)
より古い、または存在しない場合、自動で `run_loop(coins, duration_min=--fresh分, executor)` を
バースト実行してから解析する。`--no-collect` 指定時はバーストを完全スキップ。出力冒頭に
`[収集バースト実行: N分]` / `[収集スキップ: --no-collect 指定]` / `[収集スキップ: データは十分新しい (10分以内)]`
のいずれかが表示される (`hl_watch.py:1567-1573`)。

**出力構成 (§1〜§5)**:

- **§1 コイン別需給サマリ**: コイン毎に mark価格 (allMids由来)・active TWAP数 (buy/sell、うち
  開始5分未満の新規本数)・直近5分実行レート($/min、開始5分未満のTWAPは除外)・宣言残量($、
  Hypurrscanで判明した分のみ)・買い/売りの偏り判定
- **§2 注目TWAP**: 対象コイン全体で上位10件 (`ANALYZE_TOP_TWAP_LIMIT=10`)。宣言判明分は残ノーショナル
  降順優先、それ以外は実行レート降順。同user最新ポジションとの突合で4パターンのポジション文脈
  (「ショート買い戻し(closing)」「ロング利確/縮小(closing)」「ロング積み増し(building)」
  「ショート積み増し(building)」、判定不能時「新規/flip?」) を注記
- **§3 清算近接ポジション**: 各(user,coin)最新スナップショットのうち `|mark-liqPx|/mark <= 0.25`
  (`ANALYZE_LIQ_PROXIMITY_RATIO`) を距離の近い順に上位15件 (`ANALYZE_TOP_LIQ_LIMIT=15`)。
  active TWAP保有者には `★TWAP中` を付記
- **§3.5 清算ウォール要約**: `levels` と同じ `compute_liq_levels()` を既定パラメータ (band=1%,
  range=30%, min_usd=500, fresh_min=60) で呼び出し、コイン毎に「下方5%以内累積$/件数」
  「上方5%以内累積$/件数」「最厚帯」を1行サマリ表示 (`render_section3_5`, `hl_watch.py:1536-1555`)
- **§4 複合シグナル**: 4種類をルールベースで検出し根拠数値付き箇条書き (該当なしなら「なし」)
- **§5 データ品質フッタ**: 最終更新経過時間・追跡中アドレス数・active TWAP総数・Hypurrscan補完率

#### バイアス判定ルール (`bias_label()`, hl_watch.py:961-975)

buy/sell 両方 0 以下 → 「拮抗(データなし)」。片方のみ 0 → 「◯優勢(∞倍)」。両方 > 0 の場合、
大きい方/小さい方の比率 `ratio` を計算し:

| 条件 | 判定 |
|---|---|
| `ratio < 1.5` (`ANALYZE_BIAS_MILD_RATIO`) | 拮抗(x.x倍) |
| `1.5 <= ratio < 2.0` | ◯優位気味(x.x倍) |
| `ratio >= 2.0` (`ANALYZE_BIAS_STRONG_RATIO`) | ◯優勢(x.x倍) |

「開始5分未満は除外」は §1 実行レート計算 (`analyze_coin_summary`) で `mature_twap_ids` として
`start_ts < now - ANALYZE_NEW_TWAP_WINDOW_S(300秒)` のTWAPのみをレート集計対象にすることで実現。

#### §4 複合シグナル4種の発火条件 (`render_section4`, hl_watch.py:1172-1275)

1. **ショートスクイーズ素地**: `sell_rate > 0` かつ `buy_rate >= sell_rate * 2.0` のとき、
   mark〜mark×1.15 (`ANALYZE_SQUEEZE_BAND_RATIO=0.15`) の範囲に liqPx を持つ清算近接ショート
   (§3の `near_liq` から szi<0 かつ liqPx がそのレンジ内) の position_value 合計が正なら発火
2. **ロングスクイーズ素地**: 上記の buy/sell・long/short を反転した条件 (mark×0.85〜mark)
3. **大口の買い/投げ疑い**: 開始5分以上経過したactiveTWAP毎に直近5分実行レートを計算し、
   それが同コイン合計レート (buy_rate+sell_rate) の50%を超えたら発火 (`twap_rate > total_rate * 0.5`)
4. **清算予備軍がTWAP脱出中**: §3の清算近接ポジション (`near_liq`) の中で、保有方向と逆側かつ
   `reduce_only=1` の active TWAP を持つユーザーを検出 (ロング保有なら sell側reduce_only、
   ショート保有なら buy側reduce_only)

## 5段パイプラインの説明 (コードで裏取り済み)

`run_loop()` (hl_watch.py:667-760) が単一の `ThreadPoolExecutor(max_workers=8)` 上で以下を
それぞれの間隔でディスパッチする:

1. **発見** (`discover_candidates`, 20秒毎/コイン, `DISCOVERY_INTERVAL_S`): `recentTrades` の
   `users` フィールドから候補アドレスを `CandidateSet` (LRU上限300 `CANDIDATE_MAX`, TTL30分
   `CANDIDATE_TTL_S`) に追加
2. **判定** (`check_user_twaps`): `userTwapSliceFills` を取得し `twapId` ごとに fill を集計。
   未確認アドレスは5分毎 (`UNCONFIRMED_CHECK_INTERVAL_S=300`)、active TWAP保有アドレスは
   60秒毎 (`ACTIVE_CHECK_INTERVAL_S=60`) にチェック
3. **状態遷移**: 宣言総量判明かつ `cum_sz >= declared_sz*0.98` (`COMPLETED_RATIO`) → `completed`。
   最終fillから120秒 (`ENDED_SUSPECTED_GAP_S`) 超過 → `ended_suspected`。それ以外 `active`
4. **宣言補完** (`match_and_fill_declared`): TWAP新規検出時のみ (`HYPURRSCAN_SEEN_USERS` で
   user単位1回のみ) Hypurrscan `/twap/{addr}` を叩き、`meta`/`spotMeta` 由来の asset id→coin
   変換後、user+coin+side+時刻近傍でマッチさせ `declared_sz`/`declared_minutes`/`reduce_only` を
   埋める。マッチ失敗時は宣言なしのまま進捗ベース推定にフォールバック
5. **対象者スナップショット** (`snapshot_position`, `clearinghouseState`):
   - active TWAP保有ユーザー (`CONFIRMED_TWAP_USERS`) へ **60秒毎** (`POSITION_SNAPSHOT_INTERVAL_S`)
   - **それ以外の発見済み候補アドレス全体** (LRU全体) へ **5分毎** (`CANDIDATE_SNAPSHOT_INTERVAL_S=300`,
     `CANDIDATE_SNAPSHOT_LAST_CHECK` dict でアドレス毎に管理, hl_watch.py:722-733)
   - どちらも `watch_positions` へ `INSERT OR IGNORE` で追記
6. **集計** (`aggregate_flow`, 毎分 `AGGREGATION_INTERVAL_S=60`): コイン別に active buy/sell数・
   直近5分実行レート・宣言判明分の残量を `twap_flow` へ `INSERT OR REPLACE`

全 HL info API リクエストは共通の `RateLimiter` (`MAX_REQ_PER_SEC=4.0`) でペーシングされ、
429/5xx/ネットワークエラーは指数バックオフ (最大5回リトライ, `http_post_json`) で吸収する。
個別タスクの例外は `run_loop` のメインループでキャッチしログのみ出力し、収集ループ自体は止めない
(hl_watch.py:742-747)。

## levels — 価格帯別清算ウォール (OI様ラダー)

```
usage: hl_watch.py levels [-h] [--coins COINS] [--band-pct BAND_PCT]
                          [--range-pct RANGE_PCT] [--min-usd MIN_USD]
                          [--fresh-min FRESH_MIN]

  --coins COINS           既定: BTC,ETH,SOL,HYPE
  --band-pct BAND_PCT     バケット刻み幅 (mark比 %, 既定1.0)
  --range-pct RANGE_PCT   集計対象範囲 (mark比 ±%, 既定30.0)
  --min-usd MIN_USD       対象化する position_value 下限 ($, 既定500.0)
  --fresh-min FRESH_MIN   watch_positions 鮮度閾値 (分, 既定60)
```

`allMids` で現在mark取得後、`watch_positions` の最新スナップショット (`--fresh-min` 分以内、
`liq_px IS NOT NULL`、`position_value >= --min-usd`) を対象に、ロングは mark下方・ショートは
mark上方へ `szi` 符号で分類し `--band-pct` 刻みでバケット集計する (`compute_liq_levels`,
hl_watch.py:1333-1429)。

### ラダーの読み方

- **[上方 (ショート清算)]**: mark より高い価格帯。ショートポジションの liqPx が刺さる帯。
  価格が上昇してこの帯に到達するとショート勢の強制清算 (買い戻し) が発生しうる = **踏み上げ燃料**
- **[下方 (ロング清算)]**: mark より低い価格帯。ロングポジションの liqPx が刺さる帯。
  価格下落でロング勢の強制清算 (売り) が発生しうる帯
- 各行は `距離レンジ (価格レンジ) ノーショナル$ (件数) 累積$ ASCIIバー` の形式。累積$は
  mark側 (中央) から遠ざかる方向に積算 (上方は下から上へ、下方は上から下へ)
- **mark±5% (`LEVELS_DANGER_BAND_PCT`) 以内の帯には `!` フラグが付く** (危険帯強調、
  `render_liq_levels_ladder` の `danger` 判定, hl_watch.py:1462,1468)
- `★$N ← TWAP実行中userの清算ノーショナル`: そのバケット内で active TWAP を実行中のユーザーが
  保有する清算ノーショナルの内訳 (`twap_notional`)
- 圏外 (`|距離|>range-pct%`) の合計は「圏外」行に集計され、ラダーには含まれない
- フッタに対象アドレス数・観測総ノーショナル・liq_px NULL除外件数を表示。**観測サンプル
  (発見済みアドレス) ベースであり市場全体のOIではない**旨が明記される (詳細は
  [docs/limitations.md](limitations.md))

### 実行時間についての注意

`analyze` §3 (清算近接ポジション) は `watch_positions` の対象行1件ごとに `twap_fills` へ
サブクエリを発行する実装のため、`twap_fills`/`watch_positions` が肥大化すると実行時間が
数十秒〜数分規模になりうる (2026-08-11実測: `twap_fills` 約13万行の状態で `analyze --no-collect`
が完走まで約108秒)。DBが小さいうち (数千行規模) は数秒で完了する。

## 実行例 (2026-08-11 動作確認済み)

```bash
cd /home/o9oem/workspace/crypto/analytics/hl-watch
python3 hl_watch.py run --coins BTC,ETH,SOL,HYPE --duration-min 1
python3 hl_watch.py status
python3 hl_watch.py flow --coin BTC --limit 10
python3 hl_watch.py analyze --no-collect
python3 hl_watch.py levels --coins BTC,ETH
```
