"""Render an immutable, timestamped descriptive report from verified snapshots."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import time

JST = timezone(timedelta(hours=9))
COINS = ('BTC', 'ETH', 'HYPE')
PRIOR = {'BTC': (81878.98, 82100), 'ETH': (2678.70, 2707.70), 'HYPE': (94.42, 94.57)}
NAMES = {'price_breakout_confirmed': '価格突破を確認', 'early_watch': '早期警戒',
         'neutral': '確認条件未成立', 'support_lost': '支持線割れ', 'breakout_faded': '突破後に失速'}


def iso(ms):
    return datetime.fromtimestamp(ms/1000, JST).isoformat(timespec='seconds')


def pct(value, digits=2, signed=True):
    if value is None:
        return '不明'
    return f'{value:+.{digits}f}%' if signed else f'{value:.{digits}f}%'


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '|' + '|'.join(['---']*len(headers)) + '|'] +
                     ['| ' + ' | '.join(map(str, row)) + ' |' for row in rows])


def main(full, latest, output):
    metrics = json.loads((latest/'metrics.json').read_text())
    warnings = json.loads((latest/'warnings.json').read_text())
    sources = json.loads((latest/'sources.json').read_text())
    generated = time.time_ns()//1000000
    end = sources['completed_minute_end_exclusive']
    age = (generated-end)/1000
    if age > 120:
        raise ValueError(f'Snapshot is {age:.1f}s old; refresh before generating current report')
    for coin in COINS:
        x = metrics['assets'][coin]
        for audit in [*x['minute_audit'].values(), x['spot_hourly']['audit']]:
            for key in ('unique_timestamps', 'contiguous', 'ohlc_finite_consistent', 'ends_at_expected_closed_bar'):
                if not audit[key]:
                    raise ValueError(f'{coin} audit failed: {key}')
        for diagnostic in warnings['coins'][coin].values():
            if not diagnostic['usable_for_live']:
                raise ValueError(f'{coin} warning is not currently publishable')
    a = metrics['assets']
    ranking = sorted(COINS, key=lambda c: a[c]['minute_returns_pct']['spot']['60'], reverse=True)
    blocks = [f'# BTC・ETH・HYPE 現況評価 — {iso(end)}',
              f'作成: {iso(generated)}。最新確定1分足の終了: **{iso(end)}**。生成時の足の年齢は{age:.1f}秒。'
              f'24時間・48時間・7日変化と30日変動率の時間足基準は **{metrics["hourly_data_end_exclusive_utc"]}（18:00 JST）**。'
              '時間足の完成待ちと、分足・気配の鮮度を区別する。',
              '## 結論',
              f'直近1時間の価格上昇順位は **{" → ".join(ranking)}**。7日間ではHYPEの強さが大きい。'
              'ただし、強い値動きと、現在価格から新しくロングする条件の良さは同じではない。'
              '今回は前回の高値水準を固定した突破判定と、最新の価格・現物/先物フロー・建玉数量・Fundingを組み合わせた。'
              '勝率を推定する機能ではなく、4–48時間の利益を保証する検証済みシグナルでもない。']

    rows = []
    for coin in COINS:
        d = warnings['coins'][coin]['original_high7d']
        x = a[coin]
        rows.append([coin, f'{x["spot_close"]:,.2f}', pct(x['minute_returns_pct']['spot']['15']),
                     pct(x['minute_returns_pct']['spot']['60']), pct(x['minute_returns_pct']['spot']['240']),
                     pct(x['minute_returns_pct']['spot']['720']), NAMES.get(d['state'], d['state'])])
    blocks += ['## 1. 現在の強さと突破確認',
               table(['銘柄','現物1分終値 USDT','過去15分','過去1時間','過去4時間','過去12時間','前回7日高値に対する状態'],rows),
               'BTC・ETHはBinance現物、HYPEはBybit現物。上記は過去の価格変化であり、同じ長さの将来リターンではない。',
               table(['18:00 JST基準の過去変化','BTC','ETH','HYPE'],
                     [[name,*[pct(a[c]['spot_hourly'][key]['return_pct']) for c in COINS]]
                      for name,key in [('24時間','24h'),('48時間','48h'),('7日','168h')]])]

    rows=[]
    for coin in COINS:
        d=warnings['coins'][coin]['original_high7d']
        confirmation=d['confirmation']
        rows.append([coin, f'{PRIOR[coin][1]:,.2f}',
                     ' / '.join(f'{b["close"]:,.2f}' for b in confirmation['bars']),
                     '成立' if confirmation['price_confirmed'] else '未成立',
                     '成立' if confirmation['volume_confirmed'] else '未成立',
                     ' / '.join('不明' if b['volume_ratio'] is None else f'{b["volume_ratio"]:.2f}倍' for b in confirmation['bars'])])
    ends=warnings['coins']['BTC']['original_high7d']['confirmation']['bars']
    blocks += [table(['銘柄','前回7日高値','完了5分足の終値2本','価格条件','出来高拡大条件','各足の出来高/先行12本中央値'],rows),
               f'確認対象の足の終了時刻: {iso(ends[0]["end_ms"])} / {iso(ends[1]["end_ms"])}。'
               '価格条件は連続2本の5分終値が水準を上回ること。出来高条件は少なくとも1本が先行12本中央値の1.5倍以上。'
               '最新1分終値が抵抗線以下へ戻った場合、現在の状態は価格条件より慎重に扱う。'
               'これらは今回の実装の固定条件で、予測性能が検証された売買ルールではない。']

    rows=[]
    for coin in COINS:
        x=a[coin]
        rows.append([coin,*[pct(x['flow'][market][window]['taker_imbalance_pct'])
                            for market,window in [('spot','15'),('spot','60'),('perp','15'),('perp','60')]],
                     f'{x["flow"]["spot_volume_15_vs_prior"]["ratio"]:.2f}倍',
                     pct(x['binance_oi']['changes']['1h']['quantity_pct']),
                     pct(x['binance_oi']['changes']['4h']['quantity_pct'])])
    blocks += ['## 2. 買いの裏付けと反証',
               table(['銘柄','現物15分偏り','現物1時間偏り','先物15分偏り','先物1時間偏り','現物15分出来高/直前15分','OI数量1時間','OI数量4時間'],rows),
               'taker偏り = 2×買い成行側quote約定額 / 全quote約定額 − 1。資金流入や新規ロング量ではない。'
               'HYPE現物の方向内訳は不明。先物の買いを現物の買いへ代入しない。'
               'この表の15分出来高比は直前15分との比較で、上の突破確認用5分出来高比とは基準が異なる。',
               table(['OIの時点・価格効果','BTC','ETH','HYPE'],[
                   ['最新観測UTC',*[a[c]['binance_oi']['latest_timestamp_utc'] for c in COINS]],
                   ['分足終端からの遅れ',*[f'{a[c]["binance_oi"]["lag_minutes"]:.0f}分' for c in COINS]],
                   ['OIドル評価額・1時間変化',*[pct(a[c]['binance_oi']['changes']['1h']['value_pct']) for c in COINS]]]),
               'OIドル評価額は価格上昇だけでも増えるため、強弱判断では数量変化を優先する。'
               '数量の増加・減少だけで、新規ロング、ショート新規、買い戻しを識別できない。']
    for coin in COINS:
        x=a[coin]; d=warnings['coins'][coin]['original_high7d']; rolling=warnings['coins'][coin]['rolling_context']
        flag_text=[]
        for flag in rolling['flags']:
            if flag['name']=='compression_near_resistance':
                flag_text.append(f'高値{rolling["reference"]["resistance"]:,.2f}付近で値幅縮小（直近15分幅/過去4本中央値={flag["ratio"]:.2f}倍）')
            elif flag['name']=='buy_pressure_near_resistance':
                flag_text.append(f'{flag["market"]}の買い偏りを伴う高値接近')
            elif flag['name']=='sell_resilience':
                flag_text.append(f'{flag["market"]}の売り優勢でも価格維持')
            else:
                flag_text.append(flag['name'])
        flags='、'.join(flag_text) or '追加条件なし（弱気判定という意味ではない）'
        if coin=='BTC':
            view='短期上昇と現物フローの一致を重視する。ただし急伸後の追い買いでは、押し目・最新高値の再突破・Fundingの再確認が必要。'
        elif coin=='ETH':
            view=('前回高値の価格確認は成立しており、前回の「回復待ち」からは改善。ただし定着継続を確認する。' if d['state']=='price_breakout_confirmed'
                  else '前回4時間高値の回復と、7日高値の突破確認を分ける。7日高値付近の買い圧力があっても、未確認の突破を成立扱いしない。')
        else:
            view='7日間の強さは支援材料だが、現物フロー不明・相対的に大きい変動・先物保有コストが制約。買い戻し主体と断定しない。'
        blocks.append(f'- **{coin}**: {view} 最新の自動水準による警戒: {flags}。')
    blocks += ['自動水準は今回取得した過去60分から毎回算出するため、固定した同一セットアップの継続記録とは異なる。'
               '過去の抵抗線突破には前回レポートで既知だった固定水準を使い、今取得した水準を当時から知っていたとは扱わない。',
               '## 3. 4–48時間保有のロングをどう評価するか',
               '以下は現在の条件に基づく定性的判断。新規ポジションを現在価格で検討する想定であり、既存建玉の決済指示ではない。'
               '「候補」は成行買い推奨ではなく、価格維持・現物フロー・保有コストを再確認する対象という意味。',
               table(['想定保持','BTC','ETH','HYPE'],[
                   ['4時間','優先観察候補。ただし急伸直後の追い買いは慎重。短期高値/押し目の維持を確認',
                    ('候補へ改善。7日高値の突破後、買いと定着が続くか確認' if warnings['coins']['ETH']['original_high7d']['state']=='price_breakout_confirmed' else '7日高値の確認待ち。未確認の突破を買い根拠にしない'),
                    '突破維持は支援材料。現物方向不明のまま高値を追わない'],
                   ['12時間','条件付き候補。途中の失速とFunding上昇で評価を更新','突破定着と買いの継続を確認してから。価格だけで評価しない','条件付き・慎重。BTCより大きい変動を想定し、コストを再確認'],
                   ['24時間','上昇構造は支援材料だが、現在の一時点から24時間固定保持は正当化できない','同左。BTCと同方向の市場リスクがある','7日間の強さだけでは不十分。Funding・現物フロー欠測が重い'],
                   ['48時間','現在の材料だけでは固定保持の根拠不足','現在の材料だけでは固定保持の根拠不足','特に慎重。変動と累積コストの影響が大きい']]),
               'ロング判断を強めるには、支持・抵抗水準の維持と、価格反応を伴う現物買いを確認する。'
               '新たな高値を追う場合は、事前に水準を固定して完了5分足2本・出来高1.5倍条件を確認する。'
               '支持線割れや、買いが増えても価格が上がらない状態は反証。エントリー・損切り・利確の統合戦略は未検証で、期待損益比は算出していない。',
               '## 4. 参考水準と変動リスク',
               table(['銘柄','前回4時間高値','前回7日高値','現在までの直近4時間安値','同高値','完了5分足ATR14'],[
                   [c,f'{PRIOR[c][0]:,.2f}',f'{PRIOR[c][1]:,.2f}',
                    f'{a[c]["observed_range_levels"]["4h"]["low_support_proxy"]:,.2f}',
                    f'{a[c]["observed_range_levels"]["4h"]["high_resistance_proxy"]:,.2f}',
                    f'{a[c]["atr14_5m"]["spot"]["atr14"]:,.2f} ({a[c]["atr14_5m"]["spot"]["atr14_pct_of_close"]:.2f}%)'] for c in COINS]),
               '価格水準は取引所別の観測値。前回高値を下抜ける場合は突破維持を再評価するが、遠い水準をそのまま損切りに使う指示ではない。'
               '直近4時間高安は観測レンジであり、機械的な売買水準ではない。',
               table(['過去30日の時間足標準偏差×√保持時間','BTC','ETH','HYPE'],[
                   [f'{h}時間',*[pct(a[c]['spot_hourly']['risk_scale_pct'][str(h)],signed=False) for c in COINS]] for h in (4,12,24,48)]),
               'この表は過去の変動幅の尺度。今後の予想レンジ・損失上限・68%信頼区間ではない。'
               '急変や連続した売買がある局面で√時間則を保証しない。安全なレバレッジはこの表から決められない。',
               '## 5. Funding・取引コスト',
               table(['銘柄','HL表示Funding/時間','4時間一定率仮定','12時間','24時間','48時間'],[
                   [c,pct(a[c]['funding']['hyperliquid_context_current_hourly_rate_pct'],digits=6),
                    *[pct(a[c]['funding']['constant_rate_holding_cost_scenario_pct'][str(h)],digits=4,signed=False) for h in (4,12,24,48)]] for c in COINS]),
               '正のFundingはロング側の支払い。HL表示率と建玉名目額が今後ずっと一定という仮定の費用例であり、将来の支払い予測ではない。'
               '価格リターンとは別に名目額へかかる。自己資金の現物買いには先物Fundingはかからない。'
               '[Hyperliquid公式Funding仕様](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)。',
               table(['銘柄','Binance直近確定Funding/回','実測中央値の間隔','premium API表示/回','HL直近確定/時間'],[
                   [c,pct(a[c]['funding']['binance_realized_last_rate_pct'],digits=5),
                    f'{a[c]["funding"]["binance_inferred_interval_hours_median"]:g}時間',
                    pct(a[c]['funding']['binance_current_premium_lastFundingRate_pct'],digits=5),
                    pct(a[c]['funding']['hyperliquid_realized_last_hourly_rate_pct'],digits=6)] for c in COINS]),
               '確定済みの支払率、APIの表示値、今後の費用仮定を分ける。間隔の異なる取引所の1回分の率を直接比較しない。'
               '実際の取引手数料・スリッページ・レバレッジ・口座条件は未指定のため、純期待利益は算出していない。',
               '## 6. 取得時刻・データ品質・限界',
               table(['銘柄','板のspot中間値','板の受信UTC','spotスプレッド'],[
                   [c,f'{a[c]["books"]["spot"]["midpoint"]:,.3f}',a[c]['book_receipts_utc']['spot'],
                    f'{a[c]["books"]["spot"]["spread_bp"]:.4f}bp'] for c in COINS]),
               '板の中間値は冒頭の確定1分終値とは時刻・定義が異なる。スプレッドだけで大口注文の執行可能性や出口の流動性を保証しない。',
               '- BTC/ETH Binance現物、HYPE Bybit現物、Binance先物・建玉、HL表示Fundingを今回更新。分足は全銘柄・両市場で1,000本の連続性と終了時刻を検証。\n'
               '- 補助的な時間足・確定Funding履歴・Bybit tickerは初回取得を利用し、その時刻をdata.jsonとsources.jsonに保存。現在の分足と同時刻の約定ではない。\n'
               '- 早期警戒は `observed` モード。実受信時刻・実時計の鮮度・価格水準の既知時刻を確認し、再生データを現在のシグナルとして扱っていない。\n'
               '- HYPE現物のtaker方向、最新のTWAP・口座別清算帯、ニュース/経済イベントの網羅的確認は今回は未実施。前回の短い観測を現在の証拠として再利用していない。\n'
               '- 既存の凍結研究・事前登録、原レポート、ユーザーの分析コードは変更していない。自動売買・継続監視は開始していない。\n'
               '- 当日の値動き後に作成した警戒ルールであり、誤警報率・取り逃し率・保有時間別の予測精度は未評価。',
               '## データと再現性',
               '[今回の数値・診断](data.json)、[最新取得manifest](latest/sources.json)、[最新の保存APIデータ](latest/public_snapshots.zip)、'
               '[時間足等の初回取得manifest](baseline/sources.json)、[判定器仕様](../../docs/early-warning.md)。'
               '取得先はBinance/Bybit/Hyperliquidの公開API。全通信は確認済みExpressVPNのIPv4・DNS固定経路を使用し、失敗時に通常経路へ切り替えていない。'
               '各応答の取得URL・時刻・SHA256をmanifestに残す。'
               '[Binance現物API](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)、'
               '[Bybitローソク足API](https://bybit-exchange.github.io/docs/v5/market/kline)、'
               '[Hyperliquid公開API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)。']
    output.mkdir(parents=True, exist_ok=False)
    for name, source in [('baseline',full),('latest',latest)]:
        destination=output/name; destination.mkdir()
        for filename in ('sources.json','public_snapshots.zip','metrics.json','warnings.json'):
            sourcefile=source/filename
            if name=='baseline' and filename=='metrics.json' and (source/'metrics-v2.json').exists():
                sourcefile=source/'metrics-v2.json'
            if sourcefile.exists(): shutil.copyfile(sourcefile,destination/filename)
    payload={'report_generated_at_ms':generated,'report_price_age_seconds':age,'metrics':metrics,'warnings':warnings,
             'baseline_manifest_sha256':hashlib.sha256((full/'sources.json').read_bytes()).hexdigest(),
             'latest_manifest_sha256':hashlib.sha256((latest/'sources.json').read_bytes()).hexdigest()}
    (output/'data.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False))
    (output/'report.md').write_text('\n\n'.join(blocks)+'\n')
    reproduction=output/'reproduction';reproduction.mkdir()
    for filename in ('collect.py','metrics.py','evaluate_warnings.py','build_report.py'):
        shutil.copyfile(Path(__file__).parent/filename,reproduction/filename)
    print(output/'report.md')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('full',type=Path);parser.add_argument('latest',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();main(args.full,args.latest,args.output)
