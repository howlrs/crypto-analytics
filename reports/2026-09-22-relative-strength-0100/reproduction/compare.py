"""Offline fixed-baseline comparison; no network or orders."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Inputs, bars_raw, iso

ROOT = Path('/home/o9oem/workspace/crypto/analytics')
BASE = ROOT / 'reports/2026-09-21-long-overheat-2013'
MIN = 60000
OLD_HIGHS = {'BTC': 82100, 'ETH': 2707.70, 'HYPE': 94.57}
CANCEL = {'BTC': 83700, 'ETH': 2700, 'HYPE': 94.40}
LIMITS = {'BTC': [84150, 83950, 83750], 'ETH': [2712, 2708, 2704],
          'HYPE': [94.95, 94.85, 94.62, 94.55]}
PLAN = json.loads((ROOT/'reports/2026-09-21-long-limit-plan/data.json').read_text())['generated_at_ms']

def read(path):
    return json.loads(path.read_text())

def aggregates(rows, step):
    groups = {}
    for row in rows:
        groups.setdefault(row['t']//step*step, []).append(row)
    out = []
    for t, part in sorted(groups.items()):
        if len(part) != step//MIN or part[0]['t'] != t:
            continue
        assert all(b['t']-a['t']==MIN for a,b in zip(part,part[1:]))
        out.append({'t':t,'end':t+step,'o':part[0]['o'],'c':part[-1]['c'],
                    'h':max(x['h'] for x in part),'l':min(x['l'] for x in part)})
    return out

def run(snapshot):
    inp = Inputs(snapshot)
    manifest = inp.manifests[snapshot]
    end = manifest['completed_minute_end_exclusive']
    old_manifest = read(BASE/'sources.json')
    old_end = old_manifest['completed_minute_end_exclusive']
    old_metrics = read(BASE/'metrics.json')
    current_metrics = read(snapshot/'metrics.json')
    def old_raw(name):
        with ZipFile(BASE/'public_snapshots.zip') as archive:
            data = archive.read(name+'.json')
        digest = hashlib.sha256(data).hexdigest()
        assert any(r.get('name')==name and r.get('sha256')==digest and r.get('status')=='ok'
                   for r in old_manifest['requests'])
        return json.loads(data)
    def context(raw, coin):
        return raw[1][[x['name'] for x in raw[0]['universe']].index(coin)]
    old_contexts = old_raw('hl_contexts')
    new_contexts, _, _ = inp.raw('hl_contexts')
    result = {'current_end_utc':iso(end),'baseline_end_utc':iso(old_end),
              'elapsed_minutes':(end-old_end)/MIN,'prior_plan_published_utc':iso(PLAN),
              'baseline_metrics_sha256':hashlib.sha256((BASE/'metrics.json').read_bytes()).hexdigest(),
              'assets':{},'relative_ratio_change_pct':{}}
    for coin in ('BTC','ETH','HYPE'):
        raw, _, _ = inp.raw(coin+'_spot_1m')
        rows = bars_raw(raw, 'bybit' if coin=='HYPE' else 'binance', MIN)
        rows = [x for x in rows if x['t']+MIN<=end]
        old_close = old_metrics['assets'][coin]['spot_close']
        old_bar = [x for x in rows if x['t']+MIN==old_end]
        assert len(old_bar)==1 and old_bar[0]['c']==old_close, (coin,'baseline candle mismatch')
        post = [x for x in rows if old_end<=x['t']<end]
        assert len(post)==(end-old_end)//MIN
        high = max(post,key=lambda x:x['h']); low=min(post,key=lambda x:x['l'])
        current = rows[-1]['c']
        q15 = aggregates(rows,15*MIN)
        cancels = [x for x in q15 if x['t']>=PLAN and x['c']<CANCEL[coin]]
        complete_after_plan = [x for x in rows if x['t']>=PLAN]
        touches = {}
        for limit in LIMITS[coin]:
            hit=[x for x in complete_after_plan if x['l']<=limit]
            touches[str(limit)] = {'first_bar_open_utc':iso(hit[0]['t']) if hit else None,
                                  'meaning':'price touched/passed only; not evidence of order or fill'}
        oi=current_metrics['assets'][coin]['binance_oi']
        old_oi=old_metrics['assets'][coin]['binance_oi']
        old_hl = context(old_contexts,coin); new_hl = context(new_contexts,coin)
        old_bybit = old_raw(coin+'_bybit_ticker')['result']['list'][0]
        new_bybit = inp.raw(coin+'_bybit_ticker')[0]['result']['list'][0]
        funding_rows = inp.raw(coin+'_hl_funding')[0]
        payments = [x for x in funding_rows if old_manifest['completed_ms']<int(x['time'])<=manifest['completed_ms']]
        result['assets'][coin]={
            'previous_close':old_close,'current_close':current,
            'return_since_baseline_pct':(current/old_close-1)*100,
            'post_baseline_high':high['h'],'post_baseline_high_bar_utc':iso(high['t']),
            'post_baseline_low':low['l'],'post_baseline_low_bar_utc':iso(low['t']),
            'min_excursion_from_baseline_pct':(low['l']/old_close-1)*100,
            'max_excursion_from_baseline_pct':(high['h']/old_close-1)*100,
            'current_from_post_baseline_high_pct':(current/high['h']-1)*100,
            'old_breakout_level':OLD_HIGHS[coin],
            'current_vs_old_breakout_pct':(current/OLD_HIGHS[coin]-1)*100,
            'prior_plan_cancel_level':CANCEL[coin],
            'first_15m_close_below_cancel':None if not cancels else {
                'end_utc':iso(cancels[0]['end']),'close':cancels[0]['c']},
            'latest_15m_close':q15[-1],
            'prior_plan_limit_touches':touches,
            'post_plan_observed_low':min(x['l'] for x in complete_after_plan),
            'binance_oi_baseline_comparison':{
                'previous_timestamp_utc':old_oi['latest_timestamp_utc'],
                'current_timestamp_utc':oi['latest_timestamp_utc'],
                'quantity_change_pct':(oi['quantity']/old_oi['quantity']-1)*100,
                'value_change_pct':(oi['value_usd']/old_oi['value_usd']-1)*100},
            'hyperliquid_oi_quantity_change_pct':(float(new_hl['openInterest'])/float(old_hl['openInterest'])-1)*100,
            'bybit_oi_quantity_change_pct':(float(new_bybit['openInterest'])/float(old_bybit['openInterest'])-1)*100,
            'hyperliquid_realized_since_previous_report':{
                'count':len(payments),'sum_rate_pct_constant_notional_approximation':sum(float(x['fundingRate']) for x in payments)*100,
                'payments':[{'utc':iso(int(x['time'])),'rate_pct':float(x['fundingRate'])*100} for x in sorted(payments,key=lambda x:int(x['time']))]},
            'recent_20_complete_15m_bars':q15[-20:]}
    for numerator,denominator in [('ETH','BTC'),('HYPE','BTC'),('HYPE','ETH')]:
        a=result['assets'][numerator]; b=result['assets'][denominator]
        result['relative_ratio_change_pct'][numerator+'/'+denominator]=(
            (a['current_close']/b['current_close'])/(a['previous_close']/b['previous_close'])-1)*100
    with (snapshot/'comparison.json').open('x') as out:
        json.dump(result,out,indent=2,allow_nan=False)
    print(json.dumps(result,indent=2,allow_nan=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('snapshot',type=Path)
    run(p.parse_args().snapshot)
