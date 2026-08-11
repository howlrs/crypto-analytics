# crypto-analytics

暗号資産市場 (主に Hyperliquid / Binance / Bybit) を対象にした観測・データ基盤・バックテストのモノレポ。

## プロジェクト概要

3本柱から構成される:

1. **hl-watch/** — Hyperliquid のオンチェーン TWAP 注文・大口ポジションを継続観測するツール。
   `recentTrades` で大口アドレスを発見し、`userTwapSliceFills`/`clearinghouseState` で
   TWAP実行状況・ポジション・清算価格を SQLite (`hl_watch.db`) に蓄積する。単一プロセス・
   stdlib のみ (Python 3.12)。
2. **data-tools/** — Binance/Bybit/Hyperliquid の klines・funding・OI と FRED等の指数データを
   取得し、`market.db` (SQLite) に統合するスクリプト群。2021-01〜のヒストリカルデータを蓄積。
3. **backtests/** — funding capture (デルタニュートラル戦略)・証拠金サイジング・清算リバージョン・
   クラウディングシグナルなどのバックテスト/イベントスタディ。`market.db` を read-only で参照する。

## ディレクトリマップ

```
crypto/analytics/
├── hl-watch/          Hyperliquidオンチェーン観測 (hl_watch.py, README.md)
├── data-tools/         market.db 構築スクリプト群 (fetch_*.py, load_to_sqlite.py, common.py, validate_and_manifest.py)
├── backtests/          バックテスト/イベントスタディ (funding_capture*.py, margin_sizing.py, liq_reversion.py, crowding_signals.py)
├── docs/                本README以下のドキュメント群
└── results/             backtests の出力 (CSV/JSON, スクリプト実行毎に生成)
```

## クイックスタート

hl-watch を6分間だけ回してから analyze/levels を見る例 (2026-08-11 実測、実行して動作確認済み):

```bash
cd /home/o9oem/workspace/crypto/analytics/hl-watch

# 収集ループを短時間だけ回す (テスト用 --duration-min)
python3 hl_watch.py run --coins BTC,ETH,SOL,HYPE --duration-min 1

# 現在の対象者リスト (active TWAP × 最新ポジションの JOIN)
python3 hl_watch.py status

# 需給・清算近接・複合シグナルのルールベース解析 (既存DBのみ、収集バーストなし)
python3 hl_watch.py analyze --no-collect

# 価格帯別 清算ウォール (OI様ラダー)
python3 hl_watch.py levels --coins BTC,ETH
```

data-tools で市場データを取得する例 (実行には外部API到達性が必要、コマンド構文のみ記載):

```bash
cd /home/o9oem/workspace/crypto/analytics/data-tools
python3 fetch_binance_klines.py --market spot --symbol BTCUSDT --start 2021-01 --end 2026-07
python3 load_to_sqlite.py
python3 validate_and_manifest.py
```

## データの流れ図

```
[Hyperliquid info API]                [Binance/Bybit REST/data.vision]
   recentTrades                          klines / funding / metrics
   userTwapSliceFills                            │
   clearinghouseState                            ▼
   allMids, meta/spotMeta            data-tools/fetch_*.py (CSV/JSON, /mnt/e/Datas/market/配下)
        │                                          │
        ▼                                          ▼
  hl_watch.py run (5段パイプライン)         load_to_sqlite.py
        │                                          │
        ▼                                          ▼
  hl_watch.db (SQLite, WAL)              market.db (SQLite, klines/funding/oi_metrics)
   twap_orders / twap_fills                        │
   watch_positions / twap_flow                     ▼
        │                                   validate_and_manifest.py
        ▼                                   → manifest.json (整合性検証)
  status / flow / analyze / levels                 │
   (CLIレポート)                                    ▼
                                        backtests/*.py (read-only 参照)
                                        → results/*.csv, *.json
```

hl_watch.db と market.db は完全に独立したファイルであり、相互に書き込みはしない。

## docs/ への導線

- [docs/hl-watch.md](docs/hl-watch.md) — hl-watch コマンドリファレンスと5段パイプラインの仕組み
- [docs/hl-api-notes.md](docs/hl-api-notes.md) — Hyperliquid info API / Hypurrscan / L1 explorer の実測済み仕様
- [docs/schema.md](docs/schema.md) — hl_watch.db / market.db の実スキーマ
- [docs/data-pipeline.md](docs/data-pipeline.md) — data-tools 各スクリプトの役割と backtests の目的
- [docs/limitations.md](docs/limitations.md) — 観測サンプルバイアス・清算価格の動的性質など解釈上の注意

## 環境要件

- Python 3.12 (**data-tools / hl-watch とも標準ライブラリのみ**: urllib.request, sqlite3,
  concurrent.futures, json, argparse, csv 等。外部パッケージ不要)
- `sqlite3` CLI (スキーマ確認・手動クエリ用。本リポジトリでは `~/.local/bin/sqlite3` を使用)
- データ置き場: `/mnt/e/Datas/market/` (market.db 約2.3GB, hl_watch.db 約11MB, 2026-08-11時点)
  - hl_watch.db: `/mnt/e/Datas/market/hl_watch.db`
  - market.db: `/mnt/e/Datas/market/market.db`
