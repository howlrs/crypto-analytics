#!/usr/bin/env python3
"""Offline descriptive metrics for a collector snapshot.

Usage: metrics.py SNAPSHOT [--earlier-full HOURLY_SNAPSHOT] [--output PATH]
The optional earlier directory supplies only data deliberately omitted by a
``collect.py --refresh`` run (hour candles and funding histories).
"""
import argparse, datetime as dt, hashlib, json, math, statistics as st
from pathlib import Path
from zipfile import ZipFile
import sys

sys.path.insert(0, '/home/o9oem/workspace/crypto/analytics')
from live_signals.io import load_archive, InputError

MIN=60_000; HOUR=3_600_000
COINS=('BTC','ETH','HYPE')
def iso(t): return dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).isoformat().replace('+00:00','Z')
def read(p): return json.loads(p.read_text())
def finite(x): return isinstance(x,(int,float)) and math.isfinite(x)

class Inputs:
 def __init__(self, current, earlier=None):
  self.current=Path(current); self.earlier=Path(earlier) if earlier else None
  self.manifests={p:read(p/'sources.json') for p in (self.current, self.earlier) if p}
  self.verified={}
  for base,m in self.manifests.items():
   for r in m.get('requests',[]):
    if r.get('status')=='ok' and r.get('name') and r.get('sha256'):
     self.verified.setdefault((base,r['name']),[]).append(r)
 def raw(self, name, *, prefer_current=True):
  bases=([self.current,self.earlier] if prefer_current else [self.earlier,self.current])
  for base in bases:
   if not base: continue
   p=base/'raw'/(name+'.json')
   if p.exists():
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    rs=[r for r in self.verified.get((base,name),[]) if r['sha256']==digest]
    if not rs: raise ValueError(f'unverified raw input {p}')
    return read(p), max(rs,key=lambda r:r.get('finished_ms',0)), base
  raise FileNotFoundError(name)

def bars_raw(obj, venue, step):
 if venue=='binance':
  out=[{'t':int(x[0]),'o':float(x[1]),'h':float(x[2]),'l':float(x[3]),'c':float(x[4]),'q':float(x[7]),'b':float(x[10])} for x in obj]
 else:
  out=[{'t':int(x[0]),'o':float(x[1]),'h':float(x[2]),'l':float(x[3]),'c':float(x[4]),'q':float(x[6]),'b':None} for x in obj['result']['list']]
 return sorted(out,key=lambda x:x['t'])
def audit(rows, step, end=None):
 ok=bool(rows) and all(finite(x[k]) and x[k]>0 for x in rows for k in ('o','h','l','c'))
 return {'count':len(rows),'unique_timestamps':len({x['t'] for x in rows})==len(rows),'contiguous':all(b['t']-a['t']==step for a,b in zip(rows,rows[1:])),
  'ohlc_finite_consistent':ok and all(x['l']<=min(x['o'],x['c'])<=max(x['o'],x['c'])<=x['h'] for x in rows),
  'first_utc':iso(rows[0]['t']) if rows else None,'last_open_utc':iso(rows[-1]['t']) if rows else None,
  'ends_at_expected_closed_bar': (rows[-1]['t']+step==end) if rows and end else None}
def exact_return(rows,n):
 part=rows[-(n+1):]
 if len(part)!=n+1 or any(b['t']-a['t']!=MIN for a,b in zip(part,part[1:])): return None
 return (part[-1]['c']/part[0]['c']-1)*100
def flow(rows,n):
 part=rows[-n:]
 if len(part)!=n or any(b['t']-a['t']!=MIN for a,b in zip(part,part[1:])): return None
 q=sum(x['q'] for x in part); b=None if any(x['b'] is None for x in part) else sum(x['b'] for x in part)
 return {'quote_volume':q,'taker_buy_quote':b,'signed_taker_quote':None if b is None else 2*b-q,'taker_imbalance_pct':None if b is None or not q else (2*b/q-1)*100}
def volume_ratio(rows):
 if len(rows)<30 or any(b['t']-a['t']!=MIN for a,b in zip(rows[-30:],rows[-29:])): return None
 a=sum(x['q'] for x in rows[-15:]); b=sum(x['q'] for x in rows[-30:-15]); return {'recent15_quote_volume':a,'prior_matched15_quote_volume':b,'ratio':a/b if b else None}
def atr5(rows):
 # complete 5m aggregates aligned to UTC boundaries
 groups={}
 for x in rows: groups.setdefault(x['t']//(5*MIN)*5*MIN,[]).append(x)
 agg=[]
 for t,p in sorted(groups.items()):
  if len(p)==5 and all(b['t']-a['t']==MIN for a,b in zip(p,p[1:])): agg.append({'t':t,'h':max(x['h'] for x in p),'l':min(x['l'] for x in p),'c':p[-1]['c']})
 if len(agg)<15: return None
 p=agg[-15:]; trs=[max(x['h']-x['l'],abs(x['h']-prev['c']),abs(x['l']-prev['c'])) for prev,x in zip(p,p[1:])]
 return {'atr14':sum(trs)/14,'atr14_pct_of_close':sum(trs)/14/p[-1]['c']*100,'bar_end_utc':iso(p[-1]['t']+5*MIN)}
def hourly_metrics(rows, anchor):
 out={'audit':audit(rows,HOUR,anchor),'source_hour_end_exclusive_utc':iso(anchor),'close':rows[-1]['c']}
 for h in (24,48,168):
  p=rows[-(h+1):]
  out[f'{h}h'] = None if len(p)!=h+1 or any(b['t']-a['t']!=HOUR for a,b in zip(p,p[1:])) else {'return_pct':(p[-1]['c']/p[0]['c']-1)*100,'high':max(x['h'] for x in p[1:]),'low':min(x['l'] for x in p[1:])}
 lr=[math.log(b['c']/a['c']) for a,b in zip(rows[-721:-1],rows[-720:]) if b['t']-a['t']==HOUR]
 out['hourly_log_return_sd_last30d']=st.pstdev(lr) if len(lr)==720 else None
 out['risk_scale_pct']={str(h):(out['hourly_log_return_sd_last30d']*math.sqrt(h)*100 if out['hourly_log_return_sd_last30d'] is not None else None) for h in (4,12,24,48)}
 return out
def oi_metric(raw, anchor):
 rows=sorted(({'t':int(x['timestamp']),'q':float(x['sumOpenInterest']),'v':float(x['sumOpenInterestValue'])} for x in raw),key=lambda x:x['t'])
 rows=[x for x in rows if x['t']<=anchor]
 z=rows[-1]; out={'latest_timestamp_utc':iso(z['t']),'lag_minutes':(anchor-z['t'])/MIN,'quantity':z['q'],'value_usd':z['v'],'audit':{'count':len(rows),'unique_timestamps':len({x['t'] for x in rows})==len(rows),'contiguous_5m':all(b['t']-a['t']==5*MIN for a,b in zip(rows,rows[1:]))},'changes':{}}
 for h in (0.25,1,4,24):
  target=anchor-int(h*HOUR); prior=[x for x in rows if x['t']<=target]
  if prior:
   x=prior[-1]; out['changes'][f'{h:g}h']={'prior_timestamp_utc':iso(x['t']),'prior_lag_minutes_from_target':(target-x['t'])/MIN,'quantity_pct':(z['q']/x['q']-1)*100,'value_pct':(z['v']/x['v']-1)*100}
 return out
def book(obj, bybit=False):
 b,a=(obj['result']['b'],obj['result']['a']) if bybit else (obj['bids'],obj['asks'])
 b=[(float(x[0]),float(x[1])) for x in b];a=[(float(x[0]),float(x[1])) for x in a]; mid=(b[0][0]+a[0][0])/2
 return {'midpoint':mid,'spread_bp':(a[0][0]-b[0][0])/mid*10000,'best_bid':b[0][0],'best_ask':a[0][0],'levels':{'bids':len(b),'asks':len(a)}}
def funding(raw,premium,hlraw,ctx, anchor):
 rs=sorted((int(x['fundingTime']),float(x['fundingRate'])) for x in raw); ints=[(b[0]-a[0])/HOUR for a,b in zip(rs,rs[1:])]
 last=rs[-1]; hl=sorted((int(x['time']),float(x['fundingRate'])) for x in hlraw)[-1]
 return {'binance_realized_last_rate_pct':last[1]*100,'binance_realized_last_utc':iso(last[0]),'binance_inferred_interval_hours_median':st.median(ints) if ints else None,'binance_current_premium_lastFundingRate_pct':float(premium['lastFundingRate'])*100,'binance_premium_timestamp_utc':iso(int(premium['time'])),'hyperliquid_realized_last_hourly_rate_pct':hl[1]*100,'hyperliquid_realized_last_utc':iso(hl[0]),'hyperliquid_context_current_hourly_rate_pct':float(ctx['funding'])*100,'constant_rate_holding_cost_scenario_pct':{str(h):float(ctx['funding'])*100*h for h in (4,12,24,48)},'caveat':'Current/premium rates and constant-rate scenarios are indicators, not funding forecasts.'}

def main():
 ap=argparse.ArgumentParser();ap.add_argument('snapshot',type=Path);ap.add_argument('--earlier-full',type=Path);ap.add_argument('--output',type=Path);args=ap.parse_args()
 out=args.output or args.snapshot/'metrics.json'
 if out.exists(): raise FileExistsError(f'refusing to overwrite {out}')
 inp=Inputs(args.snapshot,args.earlier_full); man=inp.manifests[args.snapshot]; minute_end=int(man['completed_minute_end_exclusive']); hour_end=int(man['hour_end_exclusive'])
 hourly_base=args.earlier_full or args.snapshot
 hourly_data_end=int(inp.manifests[hourly_base]['hour_end_exclusive'])
 result={'generated_from':str(args.snapshot),'minute_end_exclusive_utc':iso(minute_end),'snapshot_hour_end_exclusive_utc':iso(hour_end),'hourly_data_end_exclusive_utc':iso(hourly_data_end),'assets':{},'limitations':['Descriptive current-market metrics, not forecasts or trade instructions.','HYPE spot (Bybit candles) lacks taker-buy quote data; it is kept null.','Support/resistance are recent observed high/low ranges, not validated trading levels.']}
 for coin in COINS:
  spot,perp,_=load_archive(args.snapshot/'public_snapshots.zip',coin)
  sb=[{'t':x.ts,'o':x.open,'h':x.high,'l':x.low,'c':x.close,'q':x.quote_volume,'b':x.taker_buy_quote} for x in spot.bars if x.ts+MIN<=minute_end]
  pb=[{'t':x.ts,'o':x.open,'h':x.high,'l':x.low,'c':x.close,'q':x.quote_volume,'b':x.taker_buy_quote} for x in perp.bars if x.ts+MIN<=minute_end]
  hv,_r,hour_base=inp.raw(f'{coin}_spot_1h',prefer_current=False); hour_anchor=int(inp.manifests[hour_base]['hour_end_exclusive']); hrs=bars_raw(hv,'bybit' if coin=='HYPE' else 'binance',HOUR); hrs=[x for x in hrs if x['t']+HOUR<=hour_anchor]
  oi,_r,_=inp.raw(f'{coin}_oi_5m'); prem,_r,_=inp.raw(f'{coin}_premium'); fund,_r,_=inp.raw(f'{coin}_funding',prefer_current=False); hlf,_r,_=inp.raw(f'{coin}_hl_funding',prefer_current=False); by,_r,_=inp.raw(f'{coin}_bybit_ticker'); ctxraw,_r,_=inp.raw('hl_contexts'); meta,contexts=ctxraw; context=contexts[[x['name'] for x in meta['universe']].index(coin)]
  spbook,_r,_=inp.raw(f'{coin}_spot_book'); ppbook,_r,_=inp.raw(f'{coin}_perp_book')
  levels={}
  for label,n,series,source_end in [('4h',240,sb,minute_end),('24h',24,hrs,hour_anchor),('7d',168,hrs,hour_anchor)]:
   p=series[-n:] if len(series)>=n else []
   levels[label]=None if not p else {'low_support_proxy':min(x['l'] for x in p),'high_resistance_proxy':max(x['h'] for x in p),'distance_to_low_pct':(sb[-1]['c']/min(x['l'] for x in p)-1)*100,'distance_to_high_pct':(sb[-1]['c']/max(x['h'] for x in p)-1)*100,'source':'minute' if series is sb else 'hourly','source_range_end_utc':iso(source_end)}
  result['assets'][coin]={'spot_close':sb[-1]['c'],'perp_close':pb[-1]['c'],'minute_audit':{'spot':audit(sb,MIN,minute_end),'perp':audit(pb,MIN,minute_end)},'minute_returns_pct':{'spot':{str(n):exact_return(sb,n) for n in (15,60,240,720)},'perp':{str(n):exact_return(pb,n) for n in (15,60,240,720)}},'flow':{'spot':{str(n):flow(sb,n) for n in (15,60,240,720)},'perp':{str(n):flow(pb,n) for n in (15,60,240,720)},'spot_volume_15_vs_prior':volume_ratio(sb),'perp_volume_15_vs_prior':volume_ratio(pb)},'spot_hourly':hourly_metrics(hrs,hour_anchor),'atr14_5m':{'spot':atr5(sb),'perp':atr5(pb)},'observed_range_levels':levels,'binance_oi':oi_metric(oi,minute_end),'books':{'spot':book(spbook,coin=='HYPE'),'perp':book(ppbook)},'book_receipts_utc':{'spot':iso(int(inp.raw(f'{coin}_spot_book')[1]['finished_ms'])),'perp':iso(int(inp.raw(f'{coin}_perp_book')[1]['finished_ms']))},'funding':funding(fund,prem,hlf,context,minute_end),'crossvenue':{'bybit_linear_last_price':float(by['result']['list'][0]['lastPrice']),'bybit_linear_funding_rate_pct':float(by['result']['list'][0]['fundingRate'])*100,'hyperliquid_mark_price':float(context['markPx']),'hyperliquid_funding_hourly_pct':float(context['funding'])*100}}
 out.write_text(json.dumps(result,indent=2,allow_nan=False)); print(out)
if __name__=='__main__': main()
