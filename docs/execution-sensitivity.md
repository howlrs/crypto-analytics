# 実行容量・コスト感度

`backtests/execution_sensitivity.py` は既存の liquidation/crowding event CSV を読み、固定ノーショナルの執行可能性とコストを再評価します。入力CSVと `market.db` は read-only です。対象は Binance perpetual の BTCUSDT/ETHUSDT だけで、イベントの `exit_ts` は **2026-08-01 00:00:00 UTC より厳密に前**でなければ失敗します。

各scenarioの `notional_usd` は entry と exit の各片道に同じ固定額で適用します。結果の平均はイベント単位の all-target 診断であり、重複イベントを資本配分して合算したportfolio PnLではありません。sourceの14bp baseline以外に funding/borrow cost は加えていません。

各 entry/exit バーについて、当該バーより前に完成していた60本の1分足 `quote_volume` の中央値を使います。同時刻の `quote_volume` は同じ結果を変えない監査列として別に保存します。60本のどれかが欠損・ゼロなら unknown とし、entry は `skipped_entry`、exit は `incomplete_exit` として ledger に残します。容量上限超過も同様に行を削除しません。

既定scenarioは $10,000 baseline、$100,000 impact、$1,000,000 impact の3本です。baseline は最大参加率10%、片道 fee 5bp + slippage 2bp、impact 0bp です。既存sourceの `net_bp` / `net_ret_bp` はすでに往復14bpを控除しているため、再評価は `source net + 14bp - scenario costs` で計算します。このため既知出来高のbaseline行は元のsource netを再現し、コストを二重控除しません。impact は意図的に単純な片道 `coefficient bp × sqrt(notional / prior median quote volume)` proxy であり、実測された板impactではありません。

```bash
python3 backtests/execution_sensitivity.py \
  --event-files 'results/liq_reversion/events_btc_long_ret-3_oi-2_entry30_exit4h.csv' \
                'results/crowding/A2_contrarian_events_BTCUSDT_24h.csv' \
  --db /mnt/e/Datas/market/market.db \
  --output-dir /tmp/execution-sensitivity
```

`--event-files` は明示的なCSVパスまたは引用したglobを受け取ります。`--scenario-config` には `name`、`notional_usd`、`max_participation`、`fee_bp_one_way`、`slippage_bp_one_way`、`impact_coefficient_bp` を持つJSON配列を渡せます。`--cutoff-exclusive-ts` はより早い境界だけを指定でき、2026-08-01 UTCより後へは延長できません。出力先は新規または空ディレクトリでなければなりません。

- `execution_ledger.csv`: 全source行×全scenario。historical volume、actual-bar監査、参加率、fee/slippage/impactの片道内訳、skip/breach/unknown状態を保存します。既知出来高のcapacity breachにも仮想netを保持します。
- `scenario_summary.csv`: entry skip率、exit breach率、unknown、cap breach、既知の仮想net合計・件数を保存します。全対象が価格付け済みの場合だけ `full_hypothetical_all_targets` の平均netを出し、それ以外は `incomplete_overall_net` とします。
- `common_set_deltas.csv`: 全scenarioでcompleteだった共通イベント集合に限定したbaselineとの差分です。シナリオごとに都合のよい行だけで差を比較しません。
- `run_manifest.json`: cutoff、既定コスト、入力、scenario設定、script/input/quote-volume/output hash、read-only DBの前後size/mtimeです。DBが分析中に変われば失敗します。

この分析は過去のquote volumeによる単純な容量proxyです。注文帳の深さ、部分約定、注文分割、約定順位、funding、実際の市場impactを観測したものではありません。unknownやexit不成立を収益ゼロと仮定した全体成績ではありません。
