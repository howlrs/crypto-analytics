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

`liq_reversion.py`/`crowding_signals.py` は共通のコスト規約 (taker 5bp + slippage 2bp =
片道7bp、往復14bp) を明記しており、ルックアヘッドバイアス回避のため意思決定時点までの
観測のみを `.shift(1)` 等で使う方針を徹底している (各スクリプトdocstringに明記)。
