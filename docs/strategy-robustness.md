# 戦略イベントスタディの頑健性検証

`backtests/strategy_robustness.py` は、`liq_reversion.py` と `crowding_signals.py` が出力したイベントを同じ検証単位に正規化し、重複ポジション、多重比較、時系列分割、相場環境の差をまとめて監査する後処理分析です。入力CSVは変更せず、結果を `results/strategy_robustness/` に出力します。新しい売買戦略を探索するものではなく、既存のイベントスタディで見えた候補を厳しめの条件で再評価します。

## 前提となる実行時点の修正

1分足 kline の `ts` はバー始値です。`liq_reversion.py` は検出バーの終値を観測できる時刻を `detect_ts` とし、指定遅延後の最初のバー**始値**でエントリーします。`crowding_signals.py` はfunding/OIシグナルの観測時刻を `ts` とし、その時刻より後の最初のバー始値でエントリーします。どちらも、決済は実際のエントリー時刻から保有期間を経た後の最初の利用可能なバー始値です。イベントCSVには `detect_ts`（crowding は `ts`）、`entry_ts`、`exit_ts` を保存し、本分析はこれらがない旧形式のCSVを拒否します。

`crowding_signals.py` の日次OIは、資金調達時点より後の値を使わない実時刻 backward-as-of 結合です。B3イベントには実際に参照した `oi_signal_ts` と両percentileも保存し、`oi_signal_ts <= ts` を監査できます。資金調達値を使って同じ決済前の取引を選別していた C trial は、事前に利用できない情報を含むため検証対象から外しています。

## 検証手順

342個のsource event CSVを読み、B2の保有期間別分割を含む346個の正規化戦略にします。各戦略では `entry_ts` 順に、直前に採用した取引の `exit_ts` 以降に入る最初のイベントだけを残す exact interval greedy purge を適用します。これは同一資本で同時保有できないイベントを重ねて平均することを防ぐ、保守的な単一ポジション仮定です。

各年の検証では2023-01-01から開始する expanding train を用います。正式な評価は、過去を学習期間、2025年を時系列順の retrospective chronological holdout とする `formal_oos` です。戦略ごとの実保有期間を両側に embargo し、学習側は `fold_start - horizon` より前に決済済み、テスト側は `fold_start + horizon` 以降にエントリーし、かつ年内に決済したイベントだけに限定します。2026年は年途中データを含む monitoring only であり、正式なOOS確認には使用しません。

月をクラスタとして再標本化する bootstrap（既定2,000回）で平均、95%区間、片側の正収益p値、両側p値を推定します。適格性は train が30イベントかつ12か月、test が10イベントかつ5か月です。選定は**学習側だけ**で行い、fold内の全適格戦略に対する global Benjamini–Yekutieli（BY）q値が0.10以下、かつ train 平均が正であることを primary rule とします。任意の検定間依存に対して保守的なBYを主判定とし、global BHとfamily別BH/BYは補助的な探索指標です。test側のp値・q値は選定には使いません。

レジームはイベントの当日ではなく、直前UTC日の `daily_regimes.csv` を結合します。uptrend/downtrend の2分類を primary、上昇/下降 × 低/高ボラティリティの4分類を exploratory とします。4分類はサンプルと月数をさらに分割し、検定数も増えるためです。

## 出力

既定の `results/strategy_robustness/` に次の5ファイルを出力します。

- `strategy_inventory.csv`: source、正規化戦略、purge前後のイベント数、保有期間、元のvariantメタデータ。
- `walk_forward_folds.csv`: 2025 formal OOS と2026 monitoringのtrain/test統計、適格性、train/test FDR、選定結果。
- `family_walk_forward.csv`: family単位に集約したvariant数、適格数、選定数、test結果。
- `formal_oos_by_regime.csv`: 2025 holdoutの全体・trend・4-state別の月cluster bootstrapとFDR。
- `analysis_summary.json`: 設定、母数、purge集計、主判定、出力一覧と解釈上の注記。

## 実行

```bash
cd /home/o9oem/workspace/crypto/analytics
python3 backtests/strategy_robustness.py
```

主な引数は `--results-root`、`--regimes`、`--output-dir`、`--train-start`、`--formal-year`、`--monitoring-year`、`--bootstrap-samples`、`--seed`、`--selection-fdr`、`--min-train-events`、`--min-train-months`、`--min-test-events`、`--min-test-months` です。先に修正済みの `liq_reversion.py`、`crowding_signals.py`、`market_regimes.py` を実行し、対応するイベントCSVと日次レジームを生成してください。

## 今回の結果（既定設定）

以下は2026-09-04に、2026-07-31までのCEXイベント入力を使って再生成したスナップショットです。

全346戦略で80,988イベントのうち57,113件を保持し、23,875件（29.48%）を重複保有としてpurgeしました。2025 formal OOS の学習側で適格なのは208戦略でした。正の平均かつ未補正片側p値0.05以下は44候補、global BH q値0.10以下は6候補でしたが、primary ruleであるglobal BYでは選定0件です。2025 test側ではglobal BH q値0.10以下が7件あった一方、global BY最小q値は0.13118で、有意な戦略は0件でした。

2025 holdout のレジーム別検定でも、2-state trend primary と4-state exploratory のいずれもFDR有意は0件です。2026 monitoringの学習側global BY最小q値は0.10939で選定0件です。test側だけを見るとglobal BY有意が10件ありますが、事前のtrain選定を通っておらず、年途中の監視結果です。2026の数値を戦略の有効性確認や否定の正式根拠には使いません。

`liq_reversion/summary.csv` の `exploratory_uncorrected_flag` 列は、各variantを全期間で個別にnull分布と比較した未補正フラグです（現在209/324件）。同CSVの `inference_scope=exploratory_uncorrected`、crowding側のinferential CSVの同列、および両sourceの `run_manifest.json` の警告は、これらが探索的source診断であることを示します。重複、時系列holdout、variant間の多重比較を反映しないため、確認的な推論には使いません。確認的な判定は `strategy_robustness.py` のglobal BY出力だけを使用します。

この結果から固定した将来評価は [prospective-validation.md](prospective-validation.md) を参照してください。
global BYを通るprimaryは0件のまま維持し、global BHを通った17件だけを非確認的shadowとして
2026-10-01以降に追跡します。

## 解釈上の限界

2025分割は事後に固定した時系列holdoutであり、事前登録・将来時点で封印された実験ではありません。月cluster bootstrapは月内依存を保ちますが、隣接月間の依存を明示的な連続ブロックとしてはモデル化せず、月数が少ない戦略・レジームでは不安定になり得ます。コストは定めた簡略モデルであり、板の深さ、実際の注文分割、約定失敗、資金調達や借入、ポジションサイズによる市場インパクトを網羅しません。結果は入力データの時刻整合性・欠損・venue固有の定義にも依存するため、実運用の収益性や将来の再現性を保証しません。
