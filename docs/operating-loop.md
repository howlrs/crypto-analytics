# 運用ループ: 観測 → パラメータ → 仮説 → 検証 → 計測

このリポジトリを 1 つのループとして読むための地図と、その状態を持つ 3 つの台帳（`research_loop/`）の使い方。
売買推奨ではない。封印済みの registry、既存の `results/`、`market.db` は変更しない。

個別の手法は短い期間で効かなくなる。残るのは、測る仕組みと、測った値を次の判断に戻す経路である。
そのため、新しい分析や記事を読むときは次の 4 つを問う。

| 問い | 見るところ |
|---|---|
| 何を観測したか | 板、約定、遅延、建玉、自分の取引記録。受信時刻まで残しているか |
| どのパラメータに落としたか | 遅延（ms）、必要な価格差（%）、コスト（bp）、歩留まり（%）など、次の判断に使える数になっているか |
| 仮説 → 検証 → 運用 → 計測がどう回っているか | 周期、通過の条件、止める条件、知見の置き場 |
| 成績の裏付けは何か | 検定があるか、期間と回数は足りるか、コストは入っているか |

判断の原則と、新しい仮説を受け付けるときの問いは [playbook.md](playbook.md) にある。

## 段と、このリポジトリの対応

| 段 | 担っているもの | まだないもの |
|---|---|---|
| ためる | `data-tools/` → `market.db`（足、funding、建玉）。`hl-watch/` → `hl_watch.db`（取得の成功・空・失敗を区別した観測フレーム）。`orderbook/` → 板スナップショット（取引所の時刻と受信時刻つき）。`live_signals/` は受信時刻のない足を「当時使えた」と扱わない | 約定・板の配信を受ける記録器（受信遅延とそのばらつき）。自分の取引イベント |
| パラメータにする | `research_loop/parameters.json`。板の深さと成行コストは `orderbook.analyze`、出来高に対する参加率は `execution_sensitivity.py` | 実測の往復コスト、注文が板に反映されるまでの時間、約定後の値動き |
| 仮説を立てて判定する | `research_loop/hypotheses.json`。検証は `strategy_robustness.py`（重複の除外、時系列の分割、多重比較）、`prospective_validation.py`（事前登録と封印）、`event_paths.py`（途中の逆行） | — |
| 運用して計測する | `research_loop/annotations.jsonl`（いつ何をしたか）。`live_signals/`（現在の状態の診断、オフライン）、`japan_perp_monitor.py`（追跡中の受取 funding と清算余裕）。板の収集が止まったときのメール（`orderbook/deploy/gce/heartbeat.sh`、`alerting.sh`） | 実取引、グラフ（施策と結果を同じ時間軸で見る画面）、自動停止 |
| 振り返る | `python3 -m research_loop.registry review` の出力を入力にして、下の手順で行う | 定期実行の設定 |

## 3 つの台帳

```bash
python3 -m research_loop.registry check                      # 3 ファイルと相互参照の検査
python3 -m research_loop.registry params --status unknown    # まだ測っていないもの
python3 -m research_loop.registry hypotheses --status unverified,registered_forward
python3 -m research_loop.registry annotations --since 2026-10-01
python3 -m research_loop.registry review                     # 振り返りで見るものの一覧（--json も可）
python3 -m research_loop.registry annotate --kind deploy --title "…" --refs H-017,liquidity.impact_bp
```

標準ライブラリだけで動く。取得や発注はせず、`results/` にも書かない。

### parameters.json: 分析が依存している定数

| status | 意味 |
|---|---|
| `measured` | この環境で測った値。`source` に方法と期間を書く |
| `documented` | 取引所の文書や API から写した値。`as_of` の日付で古くなる |
| `assumed` | 検証のために置いた値。測定で置き換える対象 |
| `unknown` | まだ値がない。**値を入れず**、`how_to_measure` に測り方を書く |

- 分からない値を、もっともらしい数字で埋めない。`unknown` に値が入っていると検査が失敗する。
- 台帳はコードの定数を写したもので、コードは台帳を読まない。封印済みのスクリプト（`liq_reversion.py`、`crowding_signals.py`）の定数は変えない。
  新しい分析で既定値を置くときは、根拠にした台帳の id を文書に書く。
- `assumed` を `measured` に置き換えたら、`as_of` と `source` を更新し、`annotate --kind data` を 1 行残す。
- `used_by` は、その値に依存するファイル。値が変わったときに見直す範囲になる。

### hypotheses.json: 仮説と判定

- `payer` に「その利益を払っているのは誰か」を書く。言えなければ「未特定」と書く。未特定のまま採用しない。
- `kill` に、終わる条件と止める条件を書く。規則に依存する収益は、どの規則が変わると終わるかを書く。
- `status`: `unverified` → `testing` → `registered_forward` → `adopted_on_hold` / `adopted`、または `rejected`。
  記述の道具は `descriptive_only`、役目を終えたものは `retired`。
- 判定を変えるときは行を書き換え、`annotate --kind decision` を残す。棄却した仮説は消さない（同じ検証を繰り返さないため）。
- `registered_forward` の仮説は、登録した最終時点まで収益の統計を見ない（[prospective-validation.md](prospective-validation.md)、[japan-perp-forward.md](japan-perp-forward.md)）。
  台帳の更新が、封印済みの登録や判定を変えることはない。

### annotations.jsonl: いつ何をしたか

収集の開始と停止、登録、判定、規則の変更、障害、振り返りを 1 行ずつ追記する。観測した系列の変化を、後から「その時に何をしたか」と並べて読むためのもの。

- 追記だけ。前の行は書き換えない。
- `ts` は実際に起きた時刻（UTC）。記録した時刻は `recorded_at` に別に残る。起きる前の時刻では記録できない。
- `kind`: `decision` `deploy` `stop` `registration` `data` `rule_change` `incident` `review` `note`。
- `refs` に仮説や パラメータの id を入れると、存在するかを検査する。

グラフに印を付ける仕組みを入れるときは、この行（`ts`、`title`、`refs`）をそのまま流せる。

## 定期の振り返り

1. `python3 -m research_loop.registry check` が通ることを確かめる。
2. `python3 -m research_loop.registry review` を読む。
3. `unknown` のうち、手元のデータだけで測れるものを 1 つ測り、台帳を更新する。
4. 古い `documented` / `measured`（既定で 90 日超）を確かめ直す。担保掛目、料率、funding の上限は取引所が予告なく変える。
5. 開いている仮説の `next_check` を見る。新しい仮説は [playbook.md](playbook.md) の受付の問いに答えてから追加する。
6. 封印が保たれていることを確かめる（`python3 backtests/prospective_validation.py verify`）。収益は見ない。
7. 続けている収集が止まっていないことを確かめる（板は `orderbook/deploy/gce/check-freshness.sh`、[orderbook.md](orderbook.md)）。止まっていた期間は後から埋められない。
8. `annotate --kind review` で記録する。`note` に仮説の status の内訳を残すと、歩留まりの推移になる。

間隔はまだ決めていない（`loop.review_interval_days` は `unknown`）。

## 2026-11-19 時点の状態

パラメータ 42 件のうち、`measured` 16、`documented` 8、`assumed` 8、`unknown` 10。
仮説 19 件のうち、保留中の採用 2、将来検証に登録済み 3、未検証 2、記述のみ 4、棄却 8。

この日に測ったもの:

- **板の 1 か月分**（2026-10-06〜2026-11-05）を集計し、`liquidity.impact_bp`、`latency.rest_book_ms`、`latency.exchange_timestamp_lag_ms` を実測値にした。
  滑り片道 2bp の仮定（`cost.event_study.slippage_bp`）は、BTC・ETH では保守的、HYPE の 10 万ドル以上では不足だった（[orderbook.md](orderbook.md)）。
- **銘柄間の伝播の遅れ**（H-010）を、設計を先に固定して検証し、この形では棄却した（[propagation-lag.md](propagation-lag.md)）。

次に測れるもの:

1. **文書で確かめられるもの**: `funding.formula_constants`、`liquidation.unit_by_venue`。
2. **手元の観測で進められるもの**: `liquidation.wall_response`（H-008）。`hl_watch.db` の観測フレームがたまっていることが前提。
3. **記録器が要るもの**: `latency.stream_receive_ms`、`liquidity.cross_venue_lead_lag`（秒単位）。配信を受けて、取引所の時刻と受信時刻を並べて保存する。
4. **実際の取引が要るもの**: `cost.realized_round_trip_bp`、`latency.order_to_book_ms`、`liquidity.post_fill_move_bp`。

- **板スナップショットの収集を再開した。** 試行用の終了時刻（2026-11-06）で止まっていたのを、終了時刻なしで動かし直した。2026-11-06〜2026-11-19 は欠測（`data.orderbook.coverage`）。
- **止まったらメールが届くようにした。** VM が 10 分ごとに状態を記録し、正常な記録が 30 分届かないと通知が出る（[orderbook.md](orderbook.md)）。

決まっていないこと: 振り返りの間隔（`loop.review_interval_days`）。

## 範囲と限界

- 台帳は状態の記録であり、値や判定の正しさを保証しない。値の根拠は `source` と `evidence` の先にある。
- 可視化の基盤（時系列 DB とグラフ）と、振り返りの定期実行は、このリポジトリにはまだない。通知は板の収集の停止だけが対象。
- `research_loop/`、本書、`playbook.md` は、`results/research_manifest.json` が固定するスナップショットの対象ではない。
- テストは `tests/test_research_loop.py`。一時ディレクトリと合成の値を使い、リポジトリの台帳は読むだけで書き換えない。
