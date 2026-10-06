# 板スナップショットの収集と分析

`orderbook/` は、Hyperliquid・Binance・Bybit の公開 REST 板を同じ時刻で取得して専用 SQLite に保存し、
後から深さ・執行コスト・大口注文（壁）・板の偏りを分析するパッケージ。
これまでレポートごとに一度だけ取得していた板を、時系列で比較できる形で残す。

答えられる問い:

- 各銘柄・取引所で、仲値から ±5/10/25/50/100bp に何ドルの注文が並んでいるか。どの時間帯に薄くなるか。
- 1万・10万・100万ドルを成行で約定させたときの平均コスト（bp）はいくらで、どの取引所が安いか。
  `position_ev.py` の `--slippage-bp` に入れる値を、仮定ではなく観測分布から選べる。
- 大きな指値（壁）はどれだけ残り、価格が近づく前に消えたのか、価格に抜かれたのか。
- 板の偏り（買い深さと売り深さの差）の後に、仲値はどちらへ動いたか（記述統計。戦略の検証ではない）。

## 構成

| ファイル | 役割 | 依存 |
|---|---|---|
| `orderbook/book.py` | 検証、保存範囲の切り詰め、深さ、インパクト、偏り、壁の計算 | 標準ライブラリ |
| `orderbook/store.py` | SQLite スキーマと圧縮保存 | 標準ライブラリ |
| `orderbook/collect.py` | 取得 CLI（`run` / `estimate` / `status`） | 標準ライブラリ |
| `orderbook/analyze.py` | 分析 CLI | numpy / pandas |

保存先は `market.db` や `hl_watch.db` とは別のファイル。分析は読み取り専用で開く。

## 取得

ストリームは `venue:market:symbol[:nSigFigs]` で指定する。

| venue | market | 例 | 取得段数（既定） |
|---|---|---|---|
| `hyperliquid` | `perp` / `spot` | `hyperliquid:perp:BTC`、`hyperliquid:perp:BTC:4`、`hyperliquid:spot:@107` | 片側 20 段（API 固定） |
| `binance` | `perp` / `spot` | `binance:perp:BTCUSDT` | 1000 段（`--binance-limit`） |
| `bybit` | `perp` / `spot` | `bybit:perp:HYPEUSDT` | perp 500 段、spot 200 段（`--bybit-limit`） |

Hyperliquid は 20 段しか返さないため、全桁では BTC で ±2bp 程度しか見えない。末尾の数字は
`nSigFigs`（有効桁）による価格帯のまとめで、BTC（約 8.6 万ドル）は `:4` で 10 ドル刻み（約 ±23bp）、
HYPE は `:4` で 0.01 刻み（約 ±20bp）になる。BTC の `:5` は全桁と同じなので意味がない。
全桁とまとめ表示は別ストリームとして保存・分析する。

```bash
cd /home/o9oem/workspace/crypto/analytics
STREAMS=hyperliquid:perp:BTC,hyperliquid:perp:BTC:4,binance:perp:BTCUSDT,binance:spot:BTCUSDT,\
hyperliquid:perp:ETH,binance:perp:ETHUSDT,binance:spot:ETHUSDT,\
hyperliquid:perp:HYPE,hyperliquid:perp:HYPE:4,bybit:perp:HYPEUSDT,bybit:spot:HYPEUSDT

# 1回だけ取得して容量を見積もる（DB には書かない）
python3 -m orderbook.collect estimate --streams "$STREAMS" --interval-sec 60 \
  --source-interface eth1 --dns-server 100.64.100.1 --db /mnt/e/Datas/market/orderbook-2026-10.db

# 上限つきで収集（--count か --duration-min のどちらかが必須）
python3 -m orderbook.collect run --streams "$STREAMS" --interval-sec 60 --duration-min 1440 \
  --source-interface eth1 --dns-server 100.64.100.1 --db /mnt/e/Datas/market/orderbook-2026-10.db

# 保存状況（読み取り専用）
python3 -m orderbook.collect status --db /mnt/e/Datas/market/orderbook-2026-10.db
```

- 時刻は `--interval-sec` の倍数にそろえ、全ストリームを並列に取得する。同じ `tick_ms` の行は
  同じ時刻の板として取引所間で比較できる。処理が遅れた分は取り直さず飛ばし、件数を `skipped_ticks` に出す。
- 1 ストリームの失敗（HTTP エラー、不正な板、交差した板、仲値から保存帯までに最良気配が入らない板）は
  `fetch_errors` に記録し、他のストリームは続ける。全ストリームが 3 時刻続けて失敗した場合は停止する。
- HTTP 403/418/429（Bybit の IP 制限、Binance の WAF・レート制限）では再試行せず停止する。Binance の重みは
  1000 段で現物 50、先物 20（上限は毎分 6,000 / 2,400）で、60 秒間隔なら余裕がある。間隔は 5 秒未満にできない。
- 停止理由は `runs.stop_reason` に残る（`completed`、`interrupted`、`rate_limited: 詳細`、
  `transport_unavailable: 詳細`、`all_streams_failing`、`db_budget_reached`、`free_space_floor_reached`、
  想定外の例外は `error: ...`）。件数はその時刻の書き込みが確定してから数える。
- `collect run` 自体は常駐しない。継続収集は下記の `orderbook.supervise` が上限つきの実行を繰り返す。

### 通信経路（VPN）

`--source-interface` を指定すると、HTTPS と DNS のソケットをすべてそのインターフェースに固定する。
固定には二つの仕組みを重ねる。

- **デバイスへの固定**（`SO_BINDTODEVICE`）。カーネルが許す場合（Linux 5.7 以降は非特権でも可）は、
  経路表が変わってもそのデバイス以外からは送信されない。
- **送信元アドレスの固定と経路の再確認**。そのインターフェースの IPv4 アドレスを送信元にし、
  名前解決した全宛先と DNS サーバーについて、`ip route get` で経路がそのインターフェースを通ることを確認する。
  確認は各時刻の取得前に毎回やり直す。

デバイス固定が使えたかは `runs.config_json` の `transport.device_bound` に残る。

`--dns-server` は `--source-interface` と併用するときは必須で、DNS も同じソケット固定のまま指定サーバーへ送る。
システムのリゾルバを使うには `--system-dns` を明示する。上の例の `eth1` / `100.64.100.1` はこの環境の
ExpressVPN の値なので、環境ごとに確認が必要。

インターフェースのアドレスが消えたり変わったりした場合や、経路がそのインターフェースを外れた場合は、
時刻前の確認でも取得中でも `transport_unavailable: 詳細` で停止し、既定経路へは戻らない。経路を固定せずに動かすには `--allow-default-route` を
明示する。IPv4 のみを使う。

### 保存形式と容量

保存するのは仲値から `--band-bp`（既定 100bp）以内の段で、`--max-levels` で片側の段数も制限できる。
価格・数量（Hyperliquid は注文数も）を zlib 圧縮 JSON で 1 行に入れ、最良気配、取得段数、
受信・取引所時刻、遅延、**被覆範囲**を列として持つ。

被覆範囲（`bid_coverage_bp` / `ask_coverage_bp`）は、その側の深さを完全に知っている仲値からの距離。
取引所が要求段数より少なく返した側は全段取得済みなので保存帯まで、要求段数ちょうど返した側は
最も遠い段までとなる。分析はこの範囲を超える深さ・インパクトを推定せず欠測にする。

2026-10-06 に `estimate` で実測した 1 回分のサイズ（行の付帯分約 180 バイトを含む）。
`:4` の 2 行は直後の短期収集（6 回平均）の値:

| ストリーム | バイト | 被覆（bid / ask） |
|---|---:|---:|
| `hyperliquid:perp:BTC` | 515 | 2.3 / 2.3bp |
| `hyperliquid:perp:BTC:4` | 約 560 | 22.7 / 22.7bp |
| `binance:perp:BTCUSDT` | 8,104 | 15.6 / 14.2bp |
| `binance:spot:BTCUSDT` | 9,334 | 37.5 / 15.3bp |
| `hyperliquid:perp:ETH` | 533 | 7.2 / 7.2bp |
| `binance:perp:ETHUSDT` | 10,459 | 41.0 / 38.5bp |
| `binance:spot:ETHUSDT` | 9,561 | 77.7 / 51.2bp |
| `hyperliquid:perp:HYPE` | 444 | 3.3 / 2.3bp |
| `hyperliquid:perp:HYPE:4` | 約 510 | 20.6 / 20.6bp |
| `bybit:perp:HYPEUSDT` / `bybit:spot:HYPEUSDT` | 1,279 / 1,164 | 100 / 100bp（保存帯まで） |

上の 11 ストリーム（BTC は `:5` で見積もり、`:4` でもほぼ同じ）を 60 秒間隔で取ると **約 61MB/日、30 日で約 1.8GB**。同日の `/mnt/e` の空きは約 1,987GB。容量は主に
Binance の 1000 段で決まり、間隔を 10 秒にすると約 6 倍になる。

容量の上限は二重にかける。`--max-db-mb`（既定 5,000MB、WAL を含む）に達するか、保存先ディスクの空きが
`--min-free-gb`（既定 50GB）を下回ると、次の時刻の取得前に停止して `runs.stop_reason` に理由を残す。
`estimate` は 1 回分の実測から日量、30 日量、上限までの日数を出す。
DB を `orderbook-YYYY-MM.db` のように月ごとに分けると、保管・削除を月単位で行える。分析は `--db` を複数受け取り、
ストリームごとに時刻順に統合するので、月をまたぐ壁も 1 つのエピソードとして追える。

## 継続収集

`python3 -m orderbook.supervise` は `collect` を `--chunk-min`（既定 360 分）ずつ繰り返し、UTC の月ごとに
`<data-dir>/orderbook-YYYY-MM.db` へ保存する。1 回の実行は月の境界をまたがない。

停止理由ごとに、次の実行までの待ち時間を変える。

| 停止理由 | 次の実行 |
|---|---|
| `completed` | すぐ |
| `transport_unavailable`（VPN 断など） | 2 分後（インターフェースが戻るまで既定経路は使わない） |
| `rate_limited` | 30 分後 |
| `all_streams_failing` | 10 分後 |
| 想定外の例外 | 5 分後 |
| 容量上限（`db_budget_reached` / `free_space_floor_reached`） | 停止。終了コード 3 で、systemd も再起動しない |
| `--until` の時刻 | 終了 |

この環境では systemd のユーザーサービスとして動かす。テンプレートは
`orderbook/systemd/orderbook-collector.service`。コードはリポジトリの作業ツリーではなく、
特定のコミットに固定した worktree（`~/.local/share/crypto-analytics/orderbook-collector`）から実行する。
このため、開発中にブランチを切り替えても収集中のコードは変わらない。

```bash
# 導入（コミットを固定した worktree とユニット）
git -C ~/workspace/crypto/analytics worktree add --detach ~/.local/share/crypto-analytics/orderbook-collector <commit>
mkdir -p ~/.local/state/crypto-analytics
cp ~/.local/share/crypto-analytics/orderbook-collector/orderbook/systemd/orderbook-collector.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now orderbook-collector

# 状態・ログ・保存状況
systemctl --user status orderbook-collector
tail ~/.local/state/crypto-analytics/orderbook-collector.log          # 実行ごとに 1 行の JSON
python3 -m orderbook.collect status --db /mnt/e/Datas/market/orderbook-2026-10.db

# 停止（実行中の回は interrupted として記録される）
systemctl --user disable --now orderbook-collector

# コードの更新（PR のマージ後など）
git -C ~/.local/share/crypto-analytics/orderbook-collector checkout --detach <new-commit>
systemctl --user restart orderbook-collector
```

2026-10-06 から 11 ストリーム・60 秒間隔で、`--until 2026-11-06T00:00:00Z` までの 1 か月を試行として収集する
（見込み約 1.8GB）。延長するときは、インストール済みユニットの `--until` を書き換えて
`daemon-reload` と `restart` を行う。

## 分析

```bash
python3 -m orderbook.analyze --db /mnt/e/Datas/market/orderbook-2026-10.db \
  --output-dir /tmp/orderbook-analysis [--streams hyperliquid:perp:HYPE:4,bybit:perp:HYPEUSDT] \
  [--start 2026-10-07 --end 2026-10-14]
```

出力先は新規または空のディレクトリ。`results/` は research manifest の固定対象なので使わない。

| 出力 | 内容 |
|---|---|
| `book_metrics.csv` | スナップショットごとの仲値、スプレッド、帯別の買い・売り深さ（USD）と偏り、名目額別の買い・売りインパクト、遅延 |
| `book_summary.csv` | ストリーム別の分布（p10/p50/p90）と、深さ・インパクトが計算可能だった割合。分位の扱いは下記 |
| `book_by_hour.csv` | ストリーム × UTC 時間帯の中央値 |
| `book_cross_venue.csv` | 同じ資産のストリームを 2 つずつ、両方が値を持つ時刻だけで比べる。インパクトの中央値と差、安かった割合、仲値の乖離 |
| `book_walls.csv` / `book_wall_summary.csv` | 壁の持続区間と終わり方 |
| `book_imbalance.csv` | 偏りの固定区間ごとの将来の仲値変化 |
| `book_manifest.json` | 入力 DB（サイズ・更新時刻・期間条件で読んだ件数・ストリーム指定後に使った件数・最大 ID）、パラメータ、コードと出力の SHA-256 |

定義:

- **深さ**: 仲値から帯以内にある段の名目額の合計。帯が被覆範囲を超えると欠測。
- **インパクト**: その名目額を成行で約定させた平均価格と仲値の差（bp、スプレッドの半分を含む）。
  被覆範囲内の段で足りなければ欠測。手数料、約定までの遅延、自分の注文による変化は含まない。
- **分位の扱い**: インパクトの欠測は「見えている板では足りないほど高い」ので、既知のどの値より高いものとして順位を付け、
  分位が欠測側に入れば欠測とする（薄い時点を除いてコストを過小評価しないため）。深さとスプレッドは、
  欠測と既知値の大小が決まらないので、対象の全時点が既知のときだけ分位を出す。
- **まとめ表示のストリーム**（`:4` など）は価格が刻みに丸められ、仲値が刻みの半分ほどずれ得る。
  インパクトは比較に含めるが、この分だけ高めに出ることがある。仲値の乖離の比較からは除く。
- **偏り**: `(買い深さ − 売り深さ) / 合計`。既定は 10bp 帯。
- **壁**: 既定では仲値から 100bp（被覆範囲がそれより狭ければ被覆範囲）以内の段のうち、
  10 万ドル以上、その側の段の中央値の 5 倍以上、その範囲の片側合計の 5% 以上を満たすもの。
  比率の条件は、Binance のように細かい段が多い板で普通の段まで壁とみなすことを防ぐ。
- **壁の追跡**: 同じ側・同じ価格の段を連続するスナップショットでたどり、最大時の 50% 以上残っていれば継続とする。
  終わり方は次のいずれか。
  - `price_crossed`: 反対側の最良気配がその価格に届いた（買い壁なら最良売り気配がその価格以下）
  - `removed_while_mid_away`: エピソード中、仲値が一度も 5bp 以内に近づかないまま消えた
  - `removed_after_approach`: エピソード中に仲値が 5bp 以内に近づいたことがあり、その後に消えた
    （最良気配にあった壁の取消もここに入る）
  - `out_of_view`: 被覆範囲外へ出た
  - `observation_gap`: 観測間隔が、その収集実行で設定した間隔の 2.5 倍を超えた
  - `censored_at_end`: 期間終了時点で残っていた

  **スナップショットの間に起きた約定と取消は区別できない**。「仲値が離れたまま消えた」も、間に価格が触れて
  約定した可能性を排除しない。
- **偏りと将来の仲値**: 偏りを事前に固定した 5 区間（−1〜−0.6、−0.6〜−0.2、−0.2〜0.2、0.2〜0.6、0.6〜1）に分け、
  60/300/900 秒後の仲値変化を集計する。照合には受信時刻ではなく収集時刻（`tick_ms`）を使い、
  目標時刻の前後で最も近い後続スナップショットが観測間隔の半分以内にある場合だけ数える。
  最上位と最下位の区間の差には、日をクラスターとする bootstrap の 95% 区間を付ける。観測が 5 日未満か、
  どちらかの区間が 30 件未満なら `insufficient_sample` とする。仲値の変化はスプレッドと手数料を払う前の値で、
  取引可能な利益ではない。

## 実データでの確認（2026-10-06、10 秒間隔 × 6 回、8 ストリーム）

動作確認用の 1 分間で、代表値ではない。48 件を欠測なしで保存し（0.21MB）、分析まで通した。

- **HYPE を 100 万ドル成行で買う場合**: 共通 6 時点の中央値は Hyperliquid perp（`:4`）6.9bp、Bybit perp 8.2bp で、
  6 時点すべてで Hyperliquid の方が安かった。Bybit spot と Hyperliquid の全桁ストリームは被覆不足で欠測になった。
- **BTC を 100 万ドル成行で買う場合**: Binance perp（中央値 0.42〜0.47bp）は、他の 3 ストリームとの
  各ペア比較で 83〜100% の時点で安かった。
- **Binance BTC perp の被覆**: 1000 段でも ±15bp 程度しか見えず、25bp 帯の深さは常に欠測になった。

## 限界

- REST の板は、要求した時点の一瞬の状態にすぎない。スナップショットの間の変化、隠し注文、
  取引所外の流動性は見えない。Binance の REST 板は段数の上限があり、遠い深さは差分配信（WebSocket）でないと得られない。
- 時刻は受信時のローカル時計で、時計自体の正しさは前提とする。取引所の時刻は Binance perp・Bybit・
  Hyperliquid だけが返し、Binance spot にはない。
- USDT と USDC を区別せず USD として扱う。取引所間の仲値の乖離には、建値通貨の差と取引所ごとの価格形成が含まれる。
- 偏りと将来の仲値の関係は探索的な記述であり、`prospective_validation` の封印検証とは無関係。
  良い区間を見つけた場合は、規則を固定してから新しい期間で確認する。

## 今後の拡張候補

1. `book_summary.csv` のインパクト分布を `position_ev.py` に渡し、名目額に応じたスリッページを自動で設定する。
2. 約定履歴（trades）も同じ時刻で保存し、壁が消えた理由を約定と取消に分ける。
3. Binance の差分配信から、REST の段数上限を超える深さを再構成する（常駐が必要になるため、容量と運用を別途決める）。
4. hl-watch の清算ウォール（`levels`）と板の壁を同じ価格帯で突き合わせる。
