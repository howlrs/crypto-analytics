# issue #2〜#6 の評価と対応

2026-09-21 に issue 本文、現在の実装、ローカル DB を照合した。
5件とも目的は妥当。新しい診断が有効であることと、戦略の収益性が実証されることは別である。
既存の封印済み検証への昇格や判定変更は行わない。

| issue | 評価・優先度 | 対応と有効性の限界 |
|---|---|---|
| [#2 約定フローと OI](https://github.com/howlrs/crypto-analytics/issues/2) | 妥当・高 | [需給診断](taker-flow.md)を追加。Binance の保存済み taker-buy quote を使える。OI の時刻・古さを併記し、signed flow を新規資金流入と解釈しない。記述分類の閾値は仮定であり予測力の証拠ではない。 |
| [#3 最大逆行・最大順行](https://github.com/howlrs/crypto-analytics/issues/3) | 妥当・高 | [価格経路診断](event-paths.md)を追加。終点損益で見えない途中リスクを把握できる。分足内の両 barrier 到達順は不明、口座の清算確率は求めない。 |
| [#4 サイズ・コスト感度](https://github.com/howlrs/crypto-analytics/issues/4) | 妥当・高 | [執行感度分析](execution-sensitivity.md)を追加。事前出来高だけで判断し、entry 見送り・exit 超過・不明を区別する。出来高は板厚ではなく、impact は未較正のシナリオ仮定。 |
| [#5 取引所間 basis](https://github.com/howlrs/crypto-analytics/issues/5) | 妥当・中 | [固有価格による二脚分析](venue-basis.md)を追加。価格損益と funding を分離できる。現状の共通価格被覆は2024年9月のみで、長期検証としての有効性は限定的。 |
| [#6 観測母集団・鮮度](https://github.com/howlrs/crypto-analytics/issues/6) | 妥当・高 | [観測履歴と再生](observation-history.md)を追加。空ポジション取得成功と失敗を区別し、閉鎖済みポジションの再表示を防ぐ。新規収集以前の観測母集団や mid は遡及再現できない。 |

## 既存研究との境界

分析用 DB は read-only で参照する。新規分析の初期対象は2026-08-01 UTC より前に
決済まで完了した履歴。時刻上限をコードで検証し、欠測をゼロや他 venue の値で補完しない。
`liq_reversion.py`、`crowding_signals.py`、封印済み registry、既存 results は変更しない。

ルート README と既存の主要ドキュメントも `results/research_manifest.json` の固定対象なので、
説明はこの文書と新規の個別文書へ分離した。今回追加したコード・テスト・文書は、既存の
research manifest が保証するスナップショットの対象外。新規分析スクリプトの出力には独立した manifest を付け、
hl-watch は設定・入力を含む観測フレームを保存する。
既存研究の再現性マニフェストを再発行して、過去の検証を今回の実装まで拡張したようには扱わない。

## 検証方法

```bash
python3 -m unittest discover -s tests -v
python3 backtests/research_manifest.py verify
python3 backtests/prospective_validation.py verify
git diff --check
```

新規テストは手計算できる価格・出来高・OI、未来行の変更、欠測、ゼロ、時刻境界、
空口座/失敗、保存後のライブ値変更を使う。#2〜#5 は実 DB を読み取り専用で使う限定実行も行う。
#6 は一時 DB と模擬 API で検証し、本番 DB の更新や継続収集の開始は行わない。

最終確認では全118テストが成功し、`git diff --check` も通過した。
既存 research manifest（コード・文書23件、成果物406件）と prospective validation registry の
検証も成功し、封印対象は変更していない。

実データの検証出力は一時ディレクトリへ保存する。#5 は2024年9月のBTC/ETH各43,200足で
同期被覆を確認した。既定10bpではイベント0件であり、動作検証用の別設定（1bp・1日）では
二脚台帳も検証した。後者の閾値変更を戦略選定や収益性の主張には利用しない。

## レビューで重視した点

Gemini による初回レビューとローカル検証を併用した。追加の Gemini API 呼び出しは失敗したため、
同一プロバイダーの Codex を別コンテキストで起動する読み取り専用レビューで補完した。
これは会話コンテキストの独立性を確保するもので、異なるモデル提供元のレビューと同一ではない。
指摘は仕様と実データで判断した。
たとえば、参考用 spot の欠測で有効な二つの perp 価格まで除外する必要はない。
一方、欠測 funding を「その期間に決済がなかった」と推定してゼロにすることも避ける。
被覆不足は不明とし、既知小計の件数と完全評価の件数を分ける。

コード確認では、short の逆行幅を entry 名目額に対する線形損益と揃えること、
exit 足の high/low/close を使わず exit open のみ終端に含めること、capacity 超過でも
計算可能な仮定上の損益を台帳に残すことを確認対象とした。
レビューで見つかった共通被覆の範囲表示、空入力の欠測数、非有限値、イベントIDの衝突、
空結果の列定義、manifest の入力追跡、TTL による候補失効の金額分類は修正し、回帰テストを追加した。
