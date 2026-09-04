# market_regimes.py 利用案内

`backtests/market_regimes.py` は、日次終値から市場環境を4分類し、翌日リターンとクロスアセット依存性を記述する診断分析です。`market.db` は read-only で参照します。
既定では Binance perpetual の BTCUSDT / ETHUSDT と、FRED の SP500 / NASDAQ100 / DJIA /
NIKKEI225 を対象にします。対象、期間、窓幅、推定回数はCLI引数で変更できます。

## 判定と評価

UTC 23:59 の1分足が存在する完全なUTC日の終値だけを日次終値として採用します。各日 t の終値までで、過去90日（`--trend-days`）のトレンドと、過去30日（`--vol-days`）の年率ボラティリティを計算します。ボラティリティ境界は過去推定値の中央値（既定、最大730日）を1日シフトして使います。

| レジーム | 条件 |
|---|---|
| `uptrend_low_vol` | トレンド >= 0、低ボラティリティ |
| `uptrend_high_vol` | トレンド >= 0、高ボラティリティ |
| `downtrend_low_vol` | トレンド < 0、低ボラティリティ |
| `downtrend_high_vol` | トレンド < 0、高ボラティリティ |

レジームは t に判定し、評価対象は close(t) から close(t+1) へのリターンです。`--start` 指定時は最初の判定を因果的に行えるだけのウォームアップ期間（既定設定では `max(trend_days, vol_days + threshold_lookback_days + 1)` 日）を内部ロードし、出力は指定日から始めます。`--end` の翌日もロードして最終日の t→t+1 を計算します。

## 推定方法

- レジーム別平均の区間・p値は、時系列のまとまりを保つ循環移動ブロック bootstrap（既定14日ブロック）で推定します。
- 検定ファミリーごとの多重比較は Benjamini–Hochberg FDR で補正します。
- ペア分析は両資産に共通する終値日のみを使い、同じ開始日・終了日の forward close interval に揃えます。条件付き分析では interval 開始日の1日前の anchor レジームを付与します。
- クロスアセット beta の HAC（Newey–West）ラグは、観測された共通リターン interval の順序に対するラグです（暦日数ではありません）。

## 出力

既定の `results/market_regimes/` に次の6ファイルを書き出します。

- `daily_regimes.csv`: 日次終値、指標、レジーム、翌日リターン。
- `regime_summary.csv`: レジーム別の件数、平均・分位点、bootstrap区間、仮想レジーム戦略の統計。
- `transition_matrix.csv`: 連続する完全な暦日間のレジーム遷移数・確率。
- `regime_episodes.csv`: レジームの連続エピソードと長さ。
- `cross_asset_dependence.csv`: 共通intervalの相関、HAC beta、下方テール指標。
- `analysis_summary.json`: 設定、データ範囲、手法、制約、要約所見。

## 解釈上の注意

これは非売買推奨の記述的・診断的分析です。手数料、スリッページ、約定遅延、日中経路、清算制約を含みません。venueごとに終値時刻が異なるため、同日クロスアセット比較は完全同期ではありません。短いレジームやペアでは tail 推定・HAC p値が不安定になり得るため、件数とFDR補正後の q値を併読してください。
