# 再現性マニフェスト

`backtests/research_manifest.py` は、現在レポジトリで主張している分析スナップショットの来歴を固定します。対象は分析コード、依存関係、関連ドキュメント、`results/market_regimes`、`results/strategy_robustness`、`results/liq_reversion`、`results/crowding`、`results/prospective_validation` 以下の全成果物です。マニフェスト本体と `.sha256` sidecar 自身だけは自己参照を避けるため除外します。

```bash
# 一度だけ作成する。既存の manifest / sidecar は上書きしない。
python3 backtests/research_manifest.py create \
  --manifest results/research_manifest.json \
  --db /mnt/e/Datas/market/market.db

# 配布・レビュー前に検証する。
python3 backtests/research_manifest.py verify \
  --manifest results/research_manifest.json
```

マニフェストは各リポジトリ内ファイルの相対パス、バイト数、SHA-256を記録します。外部の `market.db` については絶対パス、バイト数、ナノ秒mtime、SHA-256を記録し、ハッシュの前後で size / mtime / inode が変化した場合は作成を失敗させます。Python・NumPy・pandas・SciPyのバージョンも固定します。検証は、記録されたコード・テスト・文書・成果物やDBの欠落、内容・サイズ・mtimeの変更、成果物の追加・削除、実行環境の差、canonical JSONまたはsidecar digestの不一致を失敗として扱います。

これは入力と出力の同一性を監査可能にするための仕組みであり、統計的な有意性、因果性、将来収益性を保証しません。元のイベントスタディの推論は探索的・非確認的です。将来検証の仮説固定と中間情報の遮断については [prospective-validation.md](prospective-validation.md) に従ってください。

作成後はmanifestとsidecarをGit commit/tagなどの外部アンカーに固定してください。マニフェスト単体は、作成者が記録を作り直すことまでは防げません。現在の将来検証では、registryに事前記録した公開annotated tag `prospective-validation-2026-10-01-v1` が、registry、registry sidecar、manifest、manifest sidecarを含むcommitを指します。次のコマンドは、ローカルタグだけでなく宣言済みremote上のtag objectとpeeled commitを照合し、tag内の4ファイルとworking treeをbyte単位で比較します。

```bash
python3 backtests/prospective_validation.py verify --require-anchor
```
