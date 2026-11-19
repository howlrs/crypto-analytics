# Early warning: current diagnostics, not a validated strategy

BTC / ETH / HYPE の短時間の変化を、従来の時間足中心の分析に追加するための
**オフライン判定器**。`live_signals` は凍結済みの `backtests` と分離している。
データ取得、常駐監視、通知、発注、既存レポートの自動書換えは行わない。
現在値を取得する機能が追加されたわけではない。

## 何を見落とさないようにするか

| 診断 | v1 の既定条件 | 意味しないこと |
| --- | --- | --- |
| `sell_resilience` | 直近15分または60分の価格変化が非負、taker imbalance ≤ −10%。spot/perp は別々に評価 | 売りの吸収主体、将来の上昇、ショート清算の証明 |
| `compression_near_resistance` | 直近15分の高安幅 ÷ それ以前の4本の15分高安幅の中央値 ≤ 0.75。終値が抵抗線以下、距離が過去の5分 true range 中央値の1倍以内 | 今回のBTCで必ず成立していた条件 |
| `buy_pressure_near_resistance` | 同じ近接条件、直近15分の価格変化 > 0、imbalance ≥ +10% | 現物方向不明のHYPEを現物買い優勢と認定すること |
| `support_reclaimed` | 直近15分内で支持線下の終値があり、その後最後の3本の1分終値が支持線上 | 1回のヒゲ、突破完了、ロングの期待収益 |
| `price_breakout_confirmed` | 抵抗線が既知となった後の、完了した5分足2本の終値が連続して抵抗線上。最新1分終値も上 | 出来高・フローまで確認済み、売買推奨 |

`imbalance = 2 × taker買いquote出来高 / 全quote出来高 − 1`。
出来高の有無と売買方向の有無は別。HYPE Bybit spot の売買方向は不明のまま残す。
価格確認とは別に、確認足の出来高が各足以前12本の5分quote出来高中央値の
1.5倍以上かを `confirmation.volume_confirmed` に出す。未知・ゼロ基準値を成功扱いしない。
`spot_flow_confirmation` は直近15分の数値またはnullであり、真偽の合格判定ではない。
true range は高安幅と前足終値からのギャップを含み、直近15分より前の12本の完了5分足を使う。

既定値は説明可能な仮置きで、最適化済みでも事前登録済みでもない。
今回の上昇を見た後に作った機能であり、今回の再生は予測性能の証明にならない。
ルール版・全パラメータ・設定ハッシュを出力する。API の `WarningConfig` で変更できるが、
一事例に合わせて値を探索しない。

## 時刻、鮮度、欠損の扱い

- `ts` は1分足開始のUTC epochミリ秒。`ts + 60秒 ≤ as_of_ms` の完了足だけを使う。
- `observed` は各足の実受信時刻 `received_ts ≤ as_of_ms` も必要。受信時刻のない足、
  後日取得した足を当時利用できたとは扱わない。受信時刻は取得側が真実を記録する必要がある。
- `market_time_replay` は後日取得足の市場時刻による再構成。常に `usable_for_live=false`、
  `publishable_state=historical_replay`（必要データが揃う場合）。
- 現在の表示可否は `state` だけでなく **`usable_for_live` と `publishable_state`** で判断する。
  `state` は過去の診断を保存するため、古いデータでも警戒を示す場合がある。
- `max(generated_at_ms, 実際の時計) − 最新完了spot足の終了時刻 ≤ 120秒` が現在の表示条件。
  取得ファイルの新しさでは代用しない。生成時刻は判断時刻以降。
  CLI は実時刻を使い、`--generated-at` の指定はreplayモードだけで許可する。
  observedモードは実時計も独立に確認し、生成・判断時刻の先付けでは現在表示を許可しない。
  `publication_checked_at_ms` が実時計の確認時刻。水準の期限は判断時だけでなく表示時にも確認し、
  途中で期限が来たら `expired_reference` とする。システム時計自体の正しさは運用側の前提。
- 既定では連続85分のspot足を必要とする。欠損を埋めず、重複・不正なOHLC/出来高は拒否。
  perp はspotと同一終了時刻までの連続60分がない場合、補助材料から除外する。
- `support_lost` が肯定的な材料に優先する。確認後、最新価格が抵抗線以下なら
  `breakout_faded`。これは撤退注文でも、継続的な状態管理でもない。

## 同じセットアップを追う

指定がなければ確認対象の10分より**前**の60分高安を参考水準として自動算出する。
`observed` で自動水準の `known_at` は元の足の最終受信時刻より前にしない。
一括で後から取得したデータだけでは、受信以前に水準が既知だったとは認定できない。

自動水準は毎回動くため、同じブレイクアウトの追跡には固定水準を渡す。
前回レポートの4時間高安を使う例（数値は当日の検証専用、現在の推奨水準ではない）:

```json
{
  "venue": "binance",
  "symbol": "BTCUSDT",
  "support": 81228.15,
  "resistance": 81878.98,
  "known_at": 1789976400000,
  "expires_at": 1789992000000
}
```

`known_at` は水準を実際に確定して利用できた時刻を指定する。上記は形式例。
別venue/symbol、未来に既知となる水準、期限切れは拒否する。水準の根拠・期限は利用者側で管理。
将来の価格を見て水準を変更した再生結果を、当初のルールの実績と混ぜない。

## 実行

リポジトリルートから標準Pythonだけで実行できる。出力先ディレクトリはあらかじめ存在する必要があり、
既存の出力ファイルは上書きしない。

```bash
python3 -m live_signals.cli \
  --archive reports/2026-09-21-btc-eth-hype-long/verification-1747/public_snapshots.zip \
  --coin BTC --as-of 2026-09-21T16:58:00+09:00 \
  --mode market_time_replay --output /tmp/btc-1658-warning.json
```

これは保存ZIPを再生するだけで、APIアクセスしない。ZIPには隣接する `sources.json` が必要。
元データのSHA256と、取得URLのvenue・市場・symbol・足の間隔を照合する。
入力ハッシュはZIPだけでなく、受信時刻の根拠となるmanifestと指定水準ファイルも出力に残す。
元レポートのZIPもBTC/ETHに対応するが、元ZIPにHYPE spot 1分足はない。
上記は自動水準。固定水準は `--reference-json levels.json` で渡す。

取得器から使う場合の正規化JSONは次の形（実際には必要本数を渡す）:

```json
{
  "spot": {
    "venue": "binance", "market": "spot", "symbol": "BTCUSDT",
    "bars": [{"ts": 1789977420000, "open": 81700, "high": 81740, "low": 81690,
              "close": 81725.74, "quote_volume": 100000, "taker_buy_quote": 30000,
              "received_ts": 1789977480500}]
  },
  "perp": null
}
```

```bash
python3 -m live_signals.cli snapshot.json \
  --as-of 2026-09-21T16:58:01+09:00 --mode observed --output /tmp/warning.json
```

既存レポート作成に組み込む際は、**作成直前に再取得→判定→鮮度を再確認**し、
`early_watch` の理由と反証を本文に載せる。現在のバージョンではこの取得・レポート組込み・
常駐監視は未実装。古いデータでのロング評価をこのCLIだけで更新できるわけではない。

## 検証の範囲

`python3 -m unittest discover -s tests -p 'test_early_warning*.py' -v`

未来足変更時のprefix不変性、受信遅延、未完了足、鮮度、欠損、売買方向不明、
固定水準の時刻・表示前の失効、生成時刻の改変、銘柄の取り違え、支持線回復、
ヒゲのみの不成立、確認後の失速を合成データで検証する。

まだ分からないのは誤警報率と、4/12/24/48時間での優位性。次段階では閾値を固定し、
警戒が出なかった時間も含めて前向き記録する。重複警報を同一エピソードにまとめ、
同条件の比較群、発生率、逆行幅、取引コスト、期間満了を揃えて評価する必要がある。
凍結済み研究・事前登録の結果には混ぜない。
