"""Task-local public market capture; HTTPS/DNS VPN-bound by localhost proxy.

Writes a NEW directory only. No API authentication or financial actions.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener
from zipfile import ZipFile, ZIP_DEFLATED

os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
OPENER = build_opener(ProxyHandler({'https': 'http://127.0.0.1:18765'}))
MINUTE, HOUR, DAY = 60000, 3600000, 86400000


def fetch(url, payload=None):
    if not url.startswith('https://'):
        raise ValueError('HTTPS only')
    req = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                  headers={'Content-Type': 'application/json', 'User-Agent': 'crypto-current-report/2.0'})
    with OPENER.open(req, timeout=20) as response:
        raw = response.read()
    obj = json.loads(raw)
    if isinstance(obj, dict) and (obj.get('retCode', 0) != 0 or obj.get('code', 0) < 0):
        raise ValueError(str(obj))
    return obj, raw


def collect(output, refresh=False):
    output.mkdir(parents=True, exist_ok=False)
    rawdir = output / 'raw'
    rawdir.mkdir()
    server, _ = fetch('https://api.binance.com/api/v3/time')
    now = int(server['serverTime'])
    end = now // MINUTE * MINUTE
    hour_end = now // HOUR * HOUR
    manifest = {'server_time_ms': now, 'local_clock_ms': time.time_ns() // 1000000,
                'completed_minute_end_exclusive': end, 'hour_end_exclusive': hour_end,
                'refresh': refresh,
                'transport': 'Allowlisted CONNECT proxy; IPv4 socket and DNS bound to verified ExpressVPN interface eth1; no direct fallback.',
                'collector_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'requests': []}
    jobs = []

    def get(name, host, path, **query):
        jobs.append((name, 'https://' + host + path + '?' + urlencode(query), None))

    for coin in ('BTC', 'ETH', 'HYPE'):
        symbol = coin + 'USDT'
        get(coin + '_perp_1m', 'fapi.binance.com', '/fapi/v1/klines', symbol=symbol, interval='1m', limit=1000, endTime=end-1)
        if coin == 'HYPE':
            get(coin + '_spot_1m', 'api.bybit.com', '/v5/market/kline', category='spot', symbol=symbol, interval='1', limit=1000, end=end-1)
            get(coin + '_spot_book', 'api.bybit.com', '/v5/market/orderbook', category='spot', symbol=symbol, limit=50)
        else:
            get(coin + '_spot_1m', 'api.binance.com', '/api/v3/klines', symbol=symbol, interval='1m', limit=1000, endTime=end-1)
            get(coin + '_spot_book', 'api.binance.com', '/api/v3/depth', symbol=symbol, limit=100)
        get(coin + '_perp_book', 'fapi.binance.com', '/fapi/v1/depth', symbol=symbol, limit=100)
        get(coin + '_oi_5m', 'fapi.binance.com', '/futures/data/openInterestHist', symbol=symbol, period='5m', limit=500, endTime=end)
        get(coin + '_premium', 'fapi.binance.com', '/fapi/v1/premiumIndex', symbol=symbol)
        if not refresh:
            get(coin + '_perp_1h', 'fapi.binance.com', '/fapi/v1/klines', symbol=symbol, interval='1h', limit=1000, endTime=hour_end-1)
            if coin == 'HYPE':
                get(coin + '_spot_1h', 'api.bybit.com', '/v5/market/kline', category='spot', symbol=symbol, interval='60', limit=1000, end=hour_end-1)
            else:
                get(coin + '_spot_1h', 'api.binance.com', '/api/v3/klines', symbol=symbol, interval='1h', limit=1000, endTime=hour_end-1)
            get(coin + '_oi_1h', 'fapi.binance.com', '/futures/data/openInterestHist', symbol=symbol, period='1h', limit=200, endTime=hour_end)
            get(coin + '_funding', 'fapi.binance.com', '/fapi/v1/fundingRate', symbol=symbol, startTime=now-14*DAY, endTime=now, limit=1000)
            get(coin + '_bybit_ticker', 'api.bybit.com', '/v5/market/tickers', category='linear', symbol=symbol)
            jobs.append((coin + '_hl_funding', 'https://api.hyperliquid.xyz/info', {'type':'fundingHistory','coin':coin,'startTime':now-DAY,'endTime':now}))
    jobs.append(('hl_contexts', 'https://api.hyperliquid.xyz/info', {'type':'metaAndAssetCtxs'}))

    def run(job, attempt=0):
        name, url, payload = job
        item = {'name': name, 'url': url, 'payload': payload, 'started_ms': time.time_ns()//1000000, 'attempt': attempt}
        try:
            _, body = fetch(url, payload)
            (rawdir / (name + '.json')).write_bytes(body)
            item.update(status='ok', sha256=hashlib.sha256(body).hexdigest(), bytes=len(body))
        except Exception as exc:
            item.update(status='error', error=str(exc))
        item['finished_ms'] = time.time_ns()//1000000
        print(name, item['status'], flush=True)
        return item

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, jobs))
    manifest['requests'].extend(results)
    for job, result in zip(jobs, results):
        if result['status'] == 'error':
            manifest['requests'].append(run(job, 1))
    manifest['completed_ms'] = time.time_ns()//1000000
    (output / 'sources.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    with ZipFile(output / 'public_snapshots.zip', 'x', compression=ZIP_DEFLATED) as archive:
        for path in sorted(rawdir.glob('*.json')):
            archive.write(path, path.name)
    print(json.dumps({'directory': str(output), 'end': end, 'completed_ms': manifest['completed_ms']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    collect(args.output, args.refresh)
