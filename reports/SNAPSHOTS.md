# 公開リポジトリに入れていない生データ

各レポートが根拠にした API の応答（`public_snapshots.zip`、`evidence.zip`）は、取引所・DEX・集計サイトの生の応答をそのまま固めたもので、
第三者のデータの再配布になり得るうえ、合計で約 19MB ある。そのためリポジトリには入れず（`.gitignore`）、手元の作業ツリーに残している。
レポート本文の数値は `data.json` などの派生ファイルと本文にあり、zip がなくても読める。

zip の中身がレポート作成時のものと同じかは、次の SHA-256 で確かめられる。

| ファイル | バイト数 | SHA-256 |
|---|---:|---|
| `2026-09-21-btc-eth-hype-long/public_snapshots.zip` | 1,210,048 | `581edd1b4a484c5910d0adfa03274a93068c11282bc22ed165828fc773b675c6` |
| `2026-09-21-btc-eth-hype-long/verification-1747/public_snapshots.zip` | 102,376 | `190907f59537ae86cd394cad68750e4d6de02f8753c8c8fee13e62b3e11f84ad` |
| `2026-09-21-current-strength-1839/baseline/public_snapshots.zip` | 633,810 | `9367287848f975e93ad0d5a916696e596b10a6b21059ec727e3c05393ecfd87b` |
| `2026-09-21-current-strength-1839/latest/public_snapshots.zip` | 310,845 | `65422c11e2b2a4b545660183fa7c74af801a916f651860c096a69a22dc3c726c` |
| `2026-09-21-long-limit-plan/analysis-input/public_snapshots.zip` | 634,648 | `1b7a9a72a096426e6c2be01f379325ef091a33fb69de90c50e6ca761e26485c5` |
| `2026-09-21-long-limit-plan/final-check/public_snapshots.zip` | 311,446 | `c651bdf91456965ba2ef0e2814290b401f444f19df3c17a7d0f32093cc177620` |
| `2026-09-21-long-overheat-2013/public_snapshots.zip` | 635,165 | `6e297dac83552468e5abb33f79bdfde98e7ba31026799ede09dd3c17c0dd57c3` |
| `2026-09-22-chain-activation/evidence.zip` | 7,743,924 | `04f3ad1aaee5a1cca1bbffb412cef92720f573988b59783ba3755e4549fcfd5c` |
| `2026-09-22-lp-heat/evidence.zip` | 4,928,825 | `c9b1c5bcd05bb0844bb1f91c8152f6c58045c065383a82b2d9c1a82ede20f05a` |
| `2026-09-22-overheat-0120/public_snapshots.zip` | 640,808 | `330b31aa950a8a15d7a440c1561290d9018f955150096f63eaee5781fc025ccb` |
| `2026-09-22-relative-strength-0100/initial/public_snapshots.zip` | 640,228 | `55404b56b0ab384eb067d175e0ae6855890d92f4d078ffc8ec72646eef1787a5` |
| `2026-09-22-relative-strength-0100/latest/public_snapshots.zip` | 640,294 | `b12b9f84824e0fd1413bcf46830feaec0953e918ed11c8f3487ccb957b7be61e` |

公開してよいと判断した場合は、`.gitignore` の該当行を外して別のコミットで追加する。
