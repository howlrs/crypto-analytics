# 観測履歴と再生

`hl_watch.py run` は、通常の `twap_flow` 集計を行うたびに、同じ集計サイクルで得た観測入力を SQLite の `observation_frames` へ追記します。この履歴は append-only です。既存の `twap_orders`、`watch_positions`、`twap_flow` を後から変換して履歴を作ることはしません。したがって履歴は、この collector を導入して実際に収集した後から始まります。

`position_attempts` は clearinghouseState の各取得を記録します。成功、正常な空レスポンス、失敗を区別し、取得時刻と利用可能な API 時刻を保持します。空レスポンスや後続レスポンスからコインが消えた場合は、以前のポジションを現在値として復活させません。失敗は直近の成功を無効化しません。

古い DB の `watch_positions` は既存の `status`、`analyze`、`levels` の読み取り互換性のため、まだ新しい成功取得が一度もないアドレスに限って読めます。ただしそれらは再現可能な観測フレームには含めず、過去の履歴も再構成しません。

## 使い方

```bash
python3 hl-watch/hl_watch.py history
python3 hl-watch/hl_watch.py history --coin BTC --limit 120
python3 hl-watch/hl_watch.py history --json
python3 hl-watch/hl_watch.py replay
python3 hl-watch/hl_watch.py replay --frame-id 42 --json
```

`history` は保存済みの集計フレームを表示し、収集を開始しません。`replay` も保存済みフレームだけを使うため、ライブ API、現在の DB 値、現在価格には依存しません。フレームがなければ、どちらも履歴が未収集であることを表示します。

JSON の既定出力は集計値です。候補アドレス、個別 ladder 入力、候補別 freshness、個別 TWAP 注文は外します。調査上必要な場合だけ `--include-addresses` を明示してください。SQLite 内の生フレームには再生のためこれらの入力が保存されます。

## 凍結する内容と解釈

各フレームは候補コホート（coin、first/last seen 時刻を含む）、候補の参加・離脱・stale 時刻、各コインの active TWAP と宣言値・注文状態、flow、ladder 設定、清算 ladder 入力、bucket、品質指標を保存します。分母は現在の候補アドレス全体です。取得状態、成功スナップショット時刻と age、fresh 成功数、空・失敗・未観測、null liquidation、宣言率を保存します。分母がゼロの場合の率は `null` です。

価格は集計前に一度取得した `allMids` の **mid** を、取得時刻と `price_type: "mid"` とともに保存します。これは liquidation の発火に使われる mark price ではありません。mid が取れない、有限の正数でない場合は未知として保存し、距離・bin を推測しません。

再生用の bucket 差分は、まず前フレームの mid と設定に固定して cohort 加入、離脱、stale、共通コホートの reported position value 変化を加算し、次に新しい mid と設定への bin 再配置を加えます。各 bin は residual を持ち、加算の検算が可能です。共通候補アドレスでポジションが空になった場合は address の離脱ではなく共通コホートの position change です。

初回フレーム、または前フレームに存在しなかったコインは `comparison_available=false` とし、
変化額は `null` にします。観測開始前をゼロ残高や空コホートとみなして加入・増加を作りません。
現在の snapshot/bucket 自体は表示できます。実際に保存した空の前フレームがある場合は、
その空状態からの変化を比較できます。

各コインの `discovery_candidate_count` は、そのコインで発見された現在候補の件数です。
`discovery_fresh_success_addresses` と `discovery_fresh_success_rate` はその集合に限った鮮度指標です。
一方、従来の `candidate_denominator` / `fresh_success_rate` は全候補アドレスを分母に維持します。
1回の口座取得が全コインを観測するためで、両方の分母の定義を保存します。
コイン別の発見集合は重複し得るため、件数の和を全候補数としてはいけません。

`position_value` は API が報告する評価額です。共通コホートの value 変化には mark-to-market が含まれ得るため、純粋な売買数量や資金フローと解釈してはいけません。

評価額が不明な入力があれば `valuation_complete=false` とし、全体の `delta_notional` は
`null` にします。`known_subtotal_delta_notional` と bin 内訳は既知部分だけで、全体が説明できた
とは表示しません。清算価格 NULL、少額、方向不整合、mid 不明も別 bucket に保存します。
候補の first/last seen は epoch 秒、フレーム・API 試行時刻は epoch ミリ秒です。

## 容量と保持

自動削除は行いません。運用中の DB で概算するには、実測済み frame JSON の平均バイト数に 1 日 1,440 フレームを掛けます。

```sql
SELECT AVG(LENGTH(frame_json)) AS avg_frame_bytes,
       AVG(LENGTH(frame_json)) * 1440 AS estimated_bytes_per_day
FROM observation_frames;
```

コホートや ladder 入力の規模で容量は変わります。長期保持の整理は、バックアップと再生要件を確認した上で、別途明示的に承認した archival 操作として実施してください。この機能自体は履歴を削除しません。

テストは一時 SQLite とモック HTTP を使い、実 DB への接続や実収集を行いません。
