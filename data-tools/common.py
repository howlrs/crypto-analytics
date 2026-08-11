"""Shared helpers for market data fetch scripts. Stdlib only."""
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) crypto-analytics-data-tools/1.0"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, timeout=30, retries=3, backoff=1.0):
    """Return (status_code, bytes) or (status_code, None) on 404. Raises after retries exhausted for other errors."""
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            last_exc = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"GET failed after {retries} attempts: {url} ({last_exc})")


def http_post_json(url, payload, timeout=30, retries=3, backoff=1.0):
    data = json.dumps(payload).encode("utf-8")
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            last_exc = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last_exc = e
        if attempt < retries - 1:
            time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"POST failed after {retries} attempts: {url} ({last_exc})")


def http_get_json(url, timeout=30, retries=3, backoff=1.0):
    status, body = http_get(url, timeout=timeout, retries=retries, backoff=backoff)
    if status == 404 or body is None:
        return status, None
    return status, json.loads(body)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_checksum(data: bytes, checksum_text: str) -> bool:
    """Binance CHECKSUM file format: '<hex sha256>  <filename>'"""
    expected = checksum_text.strip().split()[0].lower()
    actual = sha256_hex(data)
    return expected == actual


def extract_single_csv_from_zip(zip_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError("empty zip")
        # usually exactly one csv inside
        csv_name = names[0]
        return zf.read(csv_name)


def month_range(start_year, start_month, end_year, end_month):
    """Yield (year, month) inclusive."""
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def day_range(start_date, end_date):
    """start_date/end_date: datetime.date. Yield each date inclusive."""
    from datetime import timedelta
    d = start_date
    while d <= end_date:
        yield d
        d += timedelta(days=1)
