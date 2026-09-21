# クロス取引所 basis の記述分析（issue #5）

`backtests/venue_basis.py` は Binance / Bybit の BTCUSDT・ETHUSDT perp の固有価格を比較する。
既存 rotation の価格代理モデルには手を加えない。取引所間の価格差と funding を分離する目的は
妥当だが、1分足は同時約定や流動性を保証しないため、実行可能な裁定とは呼ばない。

```bash
python3 backtests/venue_basis.py --start 2024-09-01 --end 2024-10-01 \
  --output-dir results/venue_basis
python3 -m unittest discover -s tests -p test_venue_basis.py -v
```

DB は read-only。出力先は新規または空のディレクトリに限る。入力・entry・exit は
2026-08-01 UTC 未満、完了足の観測時刻は同時刻まで。期間上限を超える指定は拒否する。
既存の封印済み source / registry / 成果物 / research manifest は変更しない。

## 時刻と会計

- 同一 UTC 分の有効な close だけで `10000 * (Bybit / Binance - 1)` を計算する。
  片側欠測の価格を持ち越さない。Binance spot は同時刻の参考系列のみ。
- `signal_ts = bar.ts + 60000`。固定閾値以上の乖離が始まった時点でイベントを作る。
  欠測と方向反転で連続区間を分割する。entry は signal より厳密に後の共通 open、
  exit は entry の1時間/4時間後以降の共通 open。既定の待機許容は2分。
  entry 足の close が欠測でも、open が有効なら entry を妨げない。
- 正の basis は Binance long / Bybit short、負の場合は逆。両脚は同じ原資産数量 Q。
  各脚の価格損益は `direction * Q * (exit - entry)`。
- bp 分母は両脚 entry 名目額の合計 `Q * (entry_Binance + entry_Bybit)`。
  `net_ret_on_gross_notional_bp` は証拠金利益率ではない。
- funding は `[entry_ts, exit_ts)`。entry 一致を含み、exit 一致を含めない。
  UTC 8時間間隔という明示的な規約の前後を含めて観測済みであることを要求する。
  被覆確認では API 時刻の最大1秒のずれを許すが、保有判定は保存された実時刻を使う。
  台帳に対象の決済時刻一覧を残す。間隔変更・許容を超える時刻ずれ・欠測は不明とする。
  決済対象のない区間も観測被覆が必要で、終端では保守的に不明となることがある。
  正の rate は long が払い、short が受け取る。
- funding 評価額には当該 venue の決済時刻の1分足 open を使用する。実際の決済 mark の
  代理値であり、他 venue の価格や欠測値のゼロ補完は使用しない。
- fee と slippage は entry / exit の各脚名目額に課す。既定値は Binance 5bp、
  Bybit 5.5bp、slippage 2bp の仮定で、現在の利用者の料率・実測 impact を表すものではない。
  CLI で変更でき、manifest に保存する。

## 成果物と限界

`basis_panel.csv` は同期価格・basis・シグナルと1/4時間後の変化を含む。
将来の変化列は事後アウトカムであり、シグナル入力には使わない。
`two_leg_events.csv` は欠測による除外理由、各脚の価格損益・費用・funding・待機時間を残す。
`summary.json` は分布、観測された乖離の継続分数、完全/不完全イベントの件数を分ける。
端点や欠測で打ち切られた継続時間を、真の終了までの時間とは扱わない。
`manifest.json` は設定、被覆・除外数、コード/出力 hash、DB の場所・size・mtime を記録する。
DB メタデータは内容全体の暗号学的同一性証明ではない。

イベントは重複保有し得る。損益小計をポートフォリオ収益として解釈しない。
funding 不明イベントを完全な net 損益の集計に混ぜない。
信用・資金移動・bid/ask・注文分割・約定失敗は対象外。

2026-09-21 のローカル DB では、Bybit BTC/ETH の価格被覆は2024年9月のみ。
同月の実行で両銘柄とも43,200共通足、片側価格/spot欠測0を確認した。
既定の10bp閾値ではイベント0件。これはこの期間と設定での結果であり、
長期の収束性や収益性を裏付けるものではない。
