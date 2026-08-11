# Hyperliquid API 実測ノート

**2026-08-11時点の実測。** `hl-watch/hl_watch.py` の実装・コメント・既存README・実行検証と突合済み。
API仕様は変更されうるため、鮮度を意識して参照すること。

## info API (POST https://api.hyperliquid.xyz/info, 認証不要)

`hl_watch.py` の `hl_info()` (line 208-209) が全リクエストの入口。共通 `RateLimiter`
(`MAX_REQ_PER_SEC=4.0`) でペーシングし、429/5xx/ネットワークエラーは指数バックオフ
(`base_backoff=1.0`, 最大5回リトライ) で吸収する (`http_post_json`, hl_watch.py:143-176)。

### recentTrades

```json
{"type": "recentTrades", "coin": "BTC"}
```

各 trade に `users` フィールドがあり、その約定の当事者アドレス配列を返す (`discover_candidates`,
hl_watch.py:340-352)。これが候補アドレス発見の唯一の経路。20秒毎/コインでポーリング。

### userTwapSliceFills

```json
{"type": "userTwapSliceFills", "user": "0x..."}
```

- 外側の `twapId` が TWAP注文の識別子 (`entry.get("twapId")`)。fill内 (`entry["fill"]`) にも
  `twapId` フィールドが存在するがこちらは常に `null` — 外側の `twapId` のみが有効な識別子
  (hl_watch.py:449-454 のコメントで実装上明示)
- **全履歴を返す**: 呼び出し毎に過去の完了済みTWAP (数ヶ月前・監視対象外コイン含む) も含めて
  返ってくるため、`twap_orders` には監視対象4コイン以外の行や `xyz:*` (株式perp)・`@N`
  (未解決asset id) 等も混在する。`status`/`flow`/`analyze` は監視コインでフィルタしているため
  実害はない
- 未確認アドレスは5分毎、active TWAP保有アドレスは60秒毎にポーリング

### clearinghouseState

```json
{"type": "clearinghouseState", "user": "0x..."}
```

各ポジションの `liquidationPx`・`entryPx`・`szi` (符号付きサイズ)・`positionValue`・
`leverage.value`/`leverage.type`・口座の `marginSummary.accountValue` を直接返す。認証不要。
**`liquidationPx` は低レバレッジ時・ヘッジ (両建て) 時に `null` になりうる**
(`snapshot_position`, hl_watch.py:544-596)。

呼び出し頻度: active TWAP保有ユーザーへ60秒毎、それ以外の発見済み候補アドレス全体へ5分毎
(2026-08-11 levels機能追加で拡大、`CANDIDATE_SNAPSHOT_INTERVAL_S=300`)。

### allMids

```json
{"type": "allMids"}
```

コイン名 → 価格文字列の flat dict を返す (実測例: `{"BTC": "64221.5", "ETH": "1884.15", ...}`)。
spotペアは `@N`/`#N` 形式のキーも混在するが、対象コイン名 (BTC/ETH/SOL/HYPE等) は素朴にそのまま
引ける (`fetch_all_mids`, hl_watch.py:853-864)。

### meta / spotMeta

起動時に1回だけ `AssetIdResolver.load()` (hl_watch.py:264-277) が取得しキャッシュする。

- `meta` の `universe` 配列の **インデックスがそのまま perp asset id**
- `spotMeta` の `tokens` (index→name) と `universe` (spot pair定義) から
  **spot asset id = 10000 + universe内インデックス** で解決 (例: `@107` = `universe[107]` の
  ペア。実装コメントに「per HL convention spot asset id = 10000 + index」とあるが、実際に
  ID=10107 のような大きな数の生成には至っておらず `@N` 表記のまま残る asset id もある
  = 「asset id 未解決コイン」として既存README/`docs/limitations.md` に既知の制約として記載)

## Hypurrscan (GET https://api.hypurrscan.io/twap/{tokenOrAddress})

第三者API。TWAP宣言の補完用途のみ (`fetch_hypurrscan_declared`, hl_watch.py:371-398)。

- レスポンスは `action.twap` オブジェクトの配列。フィールド: `a`=assetId, `b`=isBuy (bool),
  `s`=宣言総量 (`declared_sz`), `r`=reduceOnly (bool), `m`=分数 (`minutes`), `time`=時刻(ms)
- `match_and_fill_declared()` が user+coin+side+時刻近傍 (`time` の絶対差が最小) でマッチさせ、
  `twap_orders.declared_sz`/`declared_minutes`/`reduce_only` を埋める
- **可用性が低い**: TWAP新規検出時に1回だけ叩く (`HYPURRSCAN_SEEN_USERS` でuser単位1回のみ)が、
  既存README記載の実測 (5分ラン) で新規TWAPの**約半数が `no data`**。この場合 `declared_sz` 等は
  `NULL` のまま、進捗ベース推定にフォールバックする
- 呼び出し間隔下限 `HYPURRSCAN_MIN_INTERVAL_S=2.0` 秒 (専用の pacing lock, hl_watch.py:358-368)
- **第三者API (公式ではない) なので補助情報として扱い、info APIの実測値を優先する**

## L1 explorer (POST https://rpc.hyperliquid.xyz/explorer, blockDetails)

`twapOrder` アクションが全ブロックに載るため理論上は自前でTWAP宣言を完全捕捉できるが、
Hyperliquid L1 は **~14 blocks/秒** で生成されるため、自前でブロック走査するのは重く不採用。
`recentTrades` によるアドレス発見 + `userTwapSliceFills` によるTWAP判定という現行アーキテクチャで
代替している (既存README/知見に基づく設計上の経緯。実装には含まれていない = 不採用の記録)。

## 既知の罠

- **stats-data leaderboard (stats-data.hyperliquid.xyz) はキャッシュが古い**: 実残高と乖離する
  実測あり ($19.5M 表示 → 実残高0)。`recentTrades.users` 経由の発見が確実
- **MM/HFT はサブアカウント運用でmasterアドレス残高0**: リーダーボード上位アドレスの
  `clearinghouseState` を直接叩いても中身が空のケースがある
- **レート制限はIPごとのweight制**、429実測あり → 全API共通ペーシング ~4 req/s
  (`MAX_REQ_PER_SEC`) + 指数バックオフで対応。既存README「検証実績」に429発生からの
  正常継続確認あり
