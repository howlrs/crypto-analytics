# 将来検証の事前登録と封印評価

`backtests/prospective_validation.py` は、探索済みイベント戦略を将来データで評価するための
事前登録簿を作成・検証し、観測期間が完了するまで収益統計を開示しない評価器です。
`strategy_robustness.py` の2026 foldについて、**train列だけ**から候補を固定します。
test列は候補選定にも登録された根拠にも使いません。

## 現在の登録設計

2026-09-04時点の入力では、確認的なprimary候補は0件です。これはglobal BY q <= 0.10を
通る学習側候補がなかったというno-go判定を、そのまま維持したものです。global BH q <= 0.10を
通った17件はshadowとして登録しますが、非確認的な追跡に限定し、`pass` 判定もprimaryへの
自動昇格も行いません。17件はすべて `liq_reversion` で、11件がOI条件付き、6件がOI条件なしです。
short損益式と封印ガードを修正して全成果物を再生成した後、登録時刻
2026-09-04 08:35:38 UTCで再封印しました。registry SHA-256は
`cd71b2b7876480fe610cf6d4024835e1eb01db31376f278085ad5411bc7edbee` です。

| 境界 | UTC時刻 | 意味 |
|---|---|---|
| source data cutoff | 2026-08-01 00:00 | 登録時に利用可能だったBTC/ETH共通の完全日終端 |
| stable baseline cutoff | 2026-07-28 22:59 | 最長detect-to-exit 73時間と1分のbar-gap許容を引いた、後からイベントが補完されない封印境界 |
| evaluation start | 2026-10-01 00:00 | 将来観測の開始（含む） |
| evaluation end | 2027-10-01 00:00 | 将来観測の終了（含まない） |
| follow-up end | 2027-10-04 01:00 | 最後の対象イベントの決済を待つ最終時刻 |

stable baseline cutoff以降、evaluation startより前のイベントはquarantineです。収益集計には
含めませんが、同一戦略内の重複ポジションを除くgreedy purgeの状態には反映します。これにより、
登録時にはまだ決済足がなくCSVに現れなかった終端近傍のイベントが後日補完されても、封印済みの
履歴改変とは誤判定しません。

## 固定した統計規則

- 対象窓は `detect_ts in [2026-10-01, 2027-10-01)` の完全な12 UTC暦月。
- 主アウトカムは往復コスト控除後の `net_bp`。
- `entry_ts`, `exit_ts`, `detect_ts` 順のhalf-open interval greedy purgeで重複保有を除外。
- 30イベントかつ6 active month以上を適格条件とする。
- UTC月をクラスタとするbootstrap 2,000回、片側の正収益p値を使用。
- primary全体にBenjamini–Yekutieli補正を適用し、正の平均かつBY q <= 0.10を確認基準とする。
- shadowは効果量・区間・片側p値を最終時点だけ参考表示するが、q値と`pass`は付与しない。

follow-up endと必要データ被覆が揃うまでは、イベント数、active month数、状態だけを出力し、
平均、信頼区間、p値、q値を空欄にします。現在の状態は `not_started` です。primaryが0件なので、
最終時点にも確認的検定は実行されず、全体状態は `no_confirmatory_hypotheses` になります。

## 封印とfail-closed条件

登録簿はcanonical JSONで保存し、`integrity` 自身を除くcanonical payloadのSHA-256を本体と
`.sha256` sidecarの両方に記録します。
候補ごとに、安定したbaseline prefixの時刻・収益セマンティックhashとpurge状態を固定します。
評価時には次を満たさなければ統計を開示しません。

- 登録簿、本評価器、頑健性分析コードのhashが一致する。
- source生成スクリプトのhashが登録値と一致する。
- 登録されたevent CSVのhashがsource `run_manifest.json` と一致する。
- klineはfollow-up end、候補が必要とするOI/fundingはevaluation endまで被覆する。
- kline/OI/fundingの連続tailがstable baseline cutoff以前から続く。
- baselineイベントとpurge状態が登録時から変わっていない。
- 将来のdetect-to-exit lagが登録最大値+1分を超えない。

未知の追加CSVは探索対象にせず、登録簿に明記されたsource pathだけを読みます。hashは偶発的な
変更を検出する仕組みであり、単独では第三者に対する改ざん不能性を与えません。この登録では
外部アンカー名 `prospective-validation-2026-10-01-v1` を登録簿内に事前固定し、登録簿・sidecar・
research manifestを含むcommitへ同名の公開annotated Git tagを付けます。commit IDやmanifest hashを
登録簿へ自己参照させず、tag側から固定済みファイルへ到達させる設計です。

## コマンド

登録済み内容の検証:

```bash
cd /home/o9oem/workspace/crypto/analytics
python3 backtests/prospective_validation.py verify
```

公開タグが宣言どおりのremoteに存在し、そのtag内の登録簿・sidecar・research manifestが現在の4ファイルと
byte単位で一致することまで検証:

```bash
python3 backtests/prospective_validation.py verify --require-anchor
```

現在状態を新しい出力ディレクトリへ評価（既存・非空ディレクトリは拒否）:

```bash
python3 backtests/prospective_validation.py evaluate \
  --output-dir results/prospective_validation/evaluations/YYYY-MM-DD
```

### 観測中のsource再生成は禁止

`liq_reversion.py` と `crowding_signals.py` はevent CSVに個別収益を、summaryに平均やt値を出力します。
したがって、**2026-10-01から2027-10-04 01:00 UTCまでは、将来データを含めてこれらを再実行しては
いけません**。評価器が統計列を隠していても、source CSVを直接集計すれば盲検が破れるためです。
両sourceスクリプトはregistry本体・sidecarを検証し、片方または両方が欠落しても実行を拒否します。
この期間にevaluation start以降を含む実行をfail closedで拒否します。市場DBへの収集・ロードだけを継続し、sourceの再生成はfollow-up end到達後に
一度だけ行います。このガードは誤操作防止であり、OSレベルのアクセス制御ではありません。コードや
registryを改変した場合はGit履歴とSHA-256検証で監査します。

follow-up end到達後、未完成の現在足を除くBTC/ETH共通終端までsourceを再生成します。

```bash
python3 backtests/liq_reversion.py --end-ts latest
python3 backtests/crowding_signals.py --end-ts latest
```

現在の登録候補はすべて `liq_reversion` なので、確認対象だけなら前者で足ります。新しい実験を
作る場合の `create` は既存登録簿を上書きしません。別pathを明示し、観測開始前に作成します。

```bash
python3 backtests/prospective_validation.py create \
  --registry results/prospective_validation/new_registry.json \
  --start 2028-01-01T00:00:00Z
```

## 出力

- `registry.json`: 候補、train根拠、全境界、baseline、規則、コード/input provenance。
- `registry.json.sha256`: canonical registryのSHA-256。
- `event_audit.csv`: baseline/quarantine/scoreの件数とpurge後件数。
- `primary_results.csv`: primaryの固定schema。現在は候補0件のためheaderのみ。
- `shadow_results.csv`: shadowの状態・件数。完了前は収益統計が空欄。
- `evaluation_manifest.json`: lifecycle、被覆不足、候補数、統計開示有無、出力hash。

この検証はイベントスタディの将来再現性を測るものであり、板の深さ、約定失敗、サイズ別impact、
借入・funding等を含む実運用PnLの保証ではありません。
