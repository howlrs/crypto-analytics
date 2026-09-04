# データパイプライン (data-tools / backtests)

2026-08-11時点、各スクリプトの冒頭docstring・`add_argument`・実DB (`market.db`) の
`dataset_meta`/実データ範囲と突合して記載。

## data-tools/ — 取得スクリプト

全スクリプト共通: `common.py` の `http_get`/`http_post_json`/`log()` 等を再利用する stdlib only
実装。保存先ベースは `/mnt/e/Datas/market/`。

| スクリプト | 役割 | データ範囲 (実測) | 保存先 |
|---|---|---|---|
| `fetch_binance_klines.py` | Binance spot/perp 1分足klines月次zip取得・sha256検証・CSV展開 | 2021-01〜2026-07 (実測 dataset_meta) | `binance/{spot,perp}/klines_1m/<SYMBOL>/` |
| `fetch_binance_funding.py` | Binance USDS-M perp funding rate月次zip取得 | 2021-01〜 | `binance/perp/funding/<SYMBOL>/` |
| `fetch_binance_metrics.py` | Binance USDS-M perp 日次metrics (OI等) zip取得。**2023-01-01以降限定** (仕様上の下限) | 2023-01-01〜 | `binance/perp/metrics/<SYMBOL>/` |
| `fetch_bybit_klines.py` | Bybit linear perp 1分足klines をREST `/v5/market/kline` で取得 (`endTime`カーソルを逆方向にページング) | 実測 2024-09〜 | `bybit/perp/klines_1m/<SYMBOL>/` |
| `fetch_bybit_klines_spot.py` | Bybit **spot** 1分足klines (`fetch_bybit_klines.py`と同ロジックのspot版、HYPEUSDT spot履歴のため追加) | 実測 2025-07〜 | `bybit/spot/klines_1m/<SYMBOL>/` |
| `fetch_bybit_funding.py` | Bybit linear perp funding rate全履歴をREST `/v5/market/funding/history` で取得 (`endTime`逆ページング、floor=2019-01-01) | 実測 2020-04〜 | `bybit/perp/funding/<SYMBOL>/` (JSON) |
| `fetch_hyperliquid_funding.py` | Hyperliquid funding history を POST `/info` `fundingHistory` で取得 (`startTime`昇順ページング) | 既定 `--start 2023-01-01` | `hyperliquid/funding/<COIN>/` (JSON) |
| `common.py` | 共通ヘルパー (`http_get`/`http_post_json`/`verify_checksum`/`extract_single_csv_from_zip`/`month_range`/`day_range`/`log`) | - | - |
| `load_to_sqlite.py` | 上記で取得したCSV/JSONを `market.db` へロード | - | `market.db` |
| `validate_and_manifest.py` | 全データセットの行数・実期間・gap検出・sha256検証状況を集計し `manifest.json` 生成 | - | `manifest.json` |
| `check_learn_sdb_venue.py` | `/mnt/e/Datas/learn.sdb` のOHLCVがどのvenue/market由来か、Binance/Bybit close価格とのMAEで特定する調査スクリプト (一回限りの調査用途) | - | - |

### load_to_sqlite.py での market.db 構築

`SCHEMA` (klines/funding/oi_metrics/dataset_meta の `CREATE TABLE IF NOT EXISTS`) を実行後、
`/mnt/e/Datas/market/` 配下のCSV/JSONを走査して `INSERT OR REPLACE` (自然キーで冪等) する。
既存の `index_daily` テーブルは事前存在するものであり、このスクリプトは触らない (DROP/ALTER禁止、
docstringに明記)。Binance 2025+ の一部klines CSVで `open_time` がマイクロ秒単位になっている
既知の不整合を `normalize_ms()` (値が 1e14 超なら1000で割る) で ms に正規化してから格納する。

### validate_and_manifest.py

`/mnt/e/Datas/market/` 配下の生CSV/JSONファイルを直接読み (pandas不使用)、データセット毎に
行数・実際の期間 (min/max timestamp)・gap検出 (1分足klines/funding/metricsの期待本数 vs 実本数)・
sha256検証状況を集計し `manifest.json` を生成する。read-only (元データを変更しない)。

## backtests/ — バックテスト・イベントスタディ

全スクリプト `market.db` を read-only (`sqlite3.connect("file:...?mode=ro", uri=True)`) で参照。

| スクリプト | 目的 |
|---|---|
| `funding_capture.py` | デルタニュートラル (spot買い+perp売り、q固定・リバランスなし) のfunding capture戦略バックテスト。always_on / 条件付きエントリー等のvariant比較 |
| `funding_capture_v2.py` | v1の発展形。NAVベースでmonthly/drift閾値リバランスを行うunified-account型モデル。3パート構成: (1)清算距離検証付きリバランス型funding capture (2)3venue funding rotation (3)HYPEクロスvenue funding spread |
| `margin_sizing.py` | funding_capture_v2 を前提に、証拠金サイジング分析。ワースト連続マイナスfunding期間・Model S(区分証拠金)のPnL分布・Model U(統合証拠金)との資本効率比較・資本規模別実行可否 (4タスク構成) |
| `liq_reversion.py` | 大規模清算カスケード後の平均回帰仮説を検証するイベントスタディ。1h-1dスケールでの価格反応を324variantで検証 (純粋な検証スクリプト、有意でない結果もそのまま報告する方針が明記) |
| `crowding_signals.py` | funding/OI過熱 (混雑したレバレッジロング) がコントラリアン/モメンタムシグナルとして予測力を持つかを検証するバリデーションスクリプト |
| `market_regimes.py` | BTC等のトレンド (上昇/下降) × ボラティリティ (低/高) の因果的な日次レジームを構築し、レジーム別リターンとクロスアセット依存性を分析する。`numpy`/`pandas`/`scipy` が必要。出力は `results/market_regimes/` |
| `strategy_robustness.py` | 修正済みの清算リバージョン/クラウディングイベントを346戦略に正規化し、重複保有の排除、expanding train、2025 chronological OOS、月cluster bootstrap、FDR、直前UTC日レジーム別の頑健性を監査する。出力は `results/strategy_robustness/` |
| `prospective_validation.py` | 2026 foldのtrain列だけから将来仮説を固定し、canonical registry、baseline hash、quarantineを使って12 UTC暦月の封印評価を行う。完了前は収益統計を非開示。出力は `results/prospective_validation/` |
| `research_manifest.py` | 分析コード・テスト・文書・全成果物と外部 `market.db`、Python依存バージョンをSHA-256で固定し、配布・レビュー時に同一スナップショットかをfail closedで検証する |

`market_regimes.py` は UTC 日次の終値 t まででレジームを判定し、t→t+1 のリターンを評価するため、将来情報を境界判定に使わない。レジーム別平均の不確実性には循環移動ブロック bootstrap (既定14日ブロック)、クロスアセット beta には Newey–West/HAC 推定を用い、検定ファミリー内の多重比較は Benjamini–Hochberg FDR で補正する。既定の出力は `daily_regimes.csv`、`regime_summary.csv`、`transition_matrix.csv`、`regime_episodes.csv`、`cross_asset_dependence.csv`、`analysis_summary.json` の6ファイル。

詳細は [market-regimes.md](market-regimes.md) を参照。

`liq_reversion.py`/`crowding_signals.py` は共通のコスト規約 (taker 5bp + slippage 2bp =
片道7bp、往復14bp) を明記している。klineの `ts` はバー始値である。`liq_reversion.py` は検出バーの
終値を観測できる時刻から指定遅延後、`crowding_signals.py` はfunding/OIの観測時刻より後に利用可能な
最初のバー始値でエントリーする。決済は実エントリー時刻から保有期間後の最初の利用可能なバー始値とし、
イベントCSVには意思決定・エントリー・決済の各時刻を保存する。
`crowding_signals.py` の日次OIは資金調達時点以前の実時刻値だけを backward-as-of 結合し、同一決済前の
資金調達を使って取引を選ぶ C trial は事前利用不能のため検証対象から除外している。
両sourceスクリプトは `--end-ts <epoch-ms|latest>` を受け付ける。`latest` は未完成の現在1分足を除いた
BTC/ETH共通終端を選び、`run_manifest.json` にsymbol/input別の被覆、連続tail、生成スクリプトhash、
全event CSVのhashを記録する。short損益はUSDT建て線形先物としてエントリー元本基準
`1 - exit / entry` で計算する。封印済み将来検証では、event CSV自体が個別収益を含むため観測中の
再生成を禁止し、両sourceスクリプトもregistryの整合性と境界を検証して該当実行を拒否する。DB収集だけを
継続し、source再生成と最終評価は登録済みfollow-up end到達後に一度だけ行う。

`strategy_robustness.py` は、同一戦略内で重なる `entry_ts`〜`exit_ts` を exact interval greedy purge
で1本にし、2023-01-01からの expanding train と2025年の時系列順holdoutを使う。戦略の実保有期間を
年境界の両側でembargoし、train/testはそれぞれ30件・12か月、10件・5か月以上の場合だけ適格とする。
月cluster bootstrapの片側p値を使い、選定はtrain側だけのfold-global BY q <= 0.10を主判定、global BHと
family別BH/BYを補助指標とする。レジームはイベント当日ではなく直前UTC日を結合し、uptrend/downtrendをprimary、4-stateを
exploratoryとする。2026年はmonitoring onlyで正式なOOS確認には使用しない。詳細は
[strategy-robustness.md](strategy-robustness.md) を参照。

`prospective_validation.py` は2026 foldのtrain側global BYをprimary、train側global BHをshadow候補の
根拠として再計算し、入力の選定フラグとの不一致を拒否する。現在はprimary 0件、shadow 17件。
登録時のsource data cutoffから最長detect-to-exit lagと1分のgap許容を引いた位置までを安定baseline
としてhash固定し、それ以降2026-10-01まではpurge状態だけを引き継ぐquarantineとする。評価窓は
2026-10-01〜2027-10-01、最終follow-upは2027-10-04 01:00 UTC。必要sourceの被覆・連続性・event
hash・コードhashが揃うまでfail closedとし、平均・区間・p/q値を出力しない。詳細は
[prospective-validation.md](prospective-validation.md) を参照。

リポジトリへ固定した今回の数値と入力DBの同一性は `research_manifest.py verify` で検証する。
全DBのSHA-256、分析コード・テスト・文書・成果物のSHA-256、依存バージョンを記録するため、詳細は
[reproducibility.md](reproducibility.md) を参照。
