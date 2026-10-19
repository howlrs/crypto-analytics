#!/usr/bin/env python3
"""Mirror the J-Quants V2 bulk files this repository uses and build a small local SQLite.

J-Quants data is licensed for private use and this repository is public, so raw files,
the derived database and analysis outputs live under --data-dir (default
/mnt/e/Datas/market/jquants). Paths inside the repository are refused.

  sync   list bulk files, download new or changed ones, verify size, gzip and SHA-256 (network)
  build  rebuild jquants.db from the verified raw files (offline, no API key)

The API key is read from $JQUANTS_API_KEY, else from the repository .env
(JQUANTS_API_KEY, or the legacy name JQUANTS_APIKEY). It is sent only to api.jquants.com,
redirects are never followed, and presigned download URLs are fetched without it.
Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import hashlib
import http.client
import io
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import log, now_iso

API_BASE = "https://api.jquants.com/v2"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_DIR = Path("/mnt/e/Datas/market/jquants")
KEY_NAMES = ("JQUANTS_API_KEY", "JQUANTS_APIKEY")  # official CLI name first, legacy .env name second
DATASETS = {
    "topix": "/indices/bars/daily/topix",
    "options225": "/derivatives/bars/daily/options/225",
    "calendar": "/markets/calendar",
}
PERIOD = re.compile(r"_(\d{6}|\d{8})\.csv\.gz$")
SCHEMA = """
CREATE TABLE source_files (key TEXT PRIMARY KEY, dataset TEXT NOT NULL, last_modified TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE topix_daily (date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL NOT NULL,
  source_key TEXT NOT NULL);
CREATE TABLE trading_calendar (date TEXT PRIMARY KEY, hol_div TEXT NOT NULL, source_key TEXT NOT NULL);
CREATE TABLE n225_option_atm (date TEXT NOT NULL, sqd TEXT NOT NULL, ltd TEXT, contract_month TEXT,
  days_to_sq INTEGER NOT NULL, underlying REAL NOT NULL, strike REAL NOT NULL, put_iv REAL NOT NULL,
  call_iv REAL NOT NULL, atm_iv REAL NOT NULL, put_settle REAL NOT NULL, call_settle REAL NOT NULL,
  put_oi REAL, call_oi REAL, put_volume REAL, call_volume REAL, base_vol REAL, source_key TEXT NOT NULL,
  PRIMARY KEY (date, sqd));
CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class JQuantsError(RuntimeError):
    """Messages never contain the API key or presigned URLs."""


def outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise ValueError("J-Quants data must stay outside the public repository")
    return resolved


def _checked_key(value: str, name: str) -> str:
    if not value or not all("!" <= ch <= "~" for ch in value):  # printable ASCII; never echo the value
        raise ValueError(f"{name} is empty or malformed")
    return value


def load_api_key(env_file: Path = REPO / ".env", environ=os.environ) -> str:
    for name in KEY_NAMES:
        if environ.get(name) is not None:
            return _checked_key(environ[name], name)
    try:
        lines = Path(env_file).read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        raise ValueError("J-Quants API key is not configured") from None
    found: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("export") and line[6:7].isspace():
            line = line[7:].lstrip()
        name, sep, raw = line.partition("=")
        name, raw = name.strip(), raw.strip()
        if sep and name in KEY_NAMES:  # the last assignment wins, as when the shell sources the file
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            found[name] = raw
    for name in KEY_NAMES:
        if name in found:
            return _checked_key(found[name], name)
    raise ValueError("J-Quants API key is not configured")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # following a redirect would forward x-api-key to another URL


def _retry_delay(error: HTTPError | None, attempt: int) -> float:
    value = error.headers.get("Retry-After") if error is not None and error.headers else None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, IndexError, OverflowError):
                delay = math.nan
        if math.isfinite(delay) and 0 <= delay <= 300:
            return delay
    return min(120.0, (30.0 if error is not None and error.code == 429 else 2.0) * 2 ** attempt)


class Client:
    """Paced V2 requests (Standard allows 120/min; default one per second)."""

    def __init__(self, api_key: str, opener=None, sleep=time.sleep, monotonic=time.monotonic,
                 min_interval: float = 1.0, retries: int = 3, timeout: float = 120.0):
        self._key = _checked_key(api_key, "API key")
        self._opener = opener or build_opener(_NoRedirect())
        self._sleep, self._monotonic = sleep, monotonic
        self._min_interval, self._retries, self._timeout = min_interval, retries, timeout
        self._last: float | None = None

    def _read(self, request: Request, what: str) -> bytes:
        for attempt in range(self._retries + 1):
            error = None
            try:
                if self._last is not None:
                    wait = self._min_interval - (self._monotonic() - self._last)
                    if wait > 0:
                        self._sleep(wait)
                self._last = self._monotonic()
                with self._opener.open(request, timeout=self._timeout) as response:
                    return response.read()
            except HTTPError as exc:
                if not (exc.code == 429 or 500 <= exc.code < 600) or attempt == self._retries:
                    raise JQuantsError(f"{what} failed with HTTP {exc.code}") from None
                error = exc
            except (URLError, TimeoutError, OSError, http.client.HTTPException):
                if attempt == self._retries:
                    raise JQuantsError(f"{what} failed with a network error") from None
            self._sleep(_retry_delay(error, attempt))
        raise AssertionError("unreachable")

    def api(self, path: str, query: dict[str, str]) -> dict:
        if not path.startswith("/") or "?" in path or "//" in path:
            raise ValueError("path must be an absolute V2 path")
        request = Request(f"{API_BASE}{path}?{urlencode(query)}",
                          headers={"x-api-key": self._key, "Accept": "application/json"})
        try:
            payload = json.loads(self._read(request, f"GET {path}"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise JQuantsError(f"GET {path} returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise JQuantsError(f"GET {path} returned a non-object")
        return payload

    def bulk_list(self, endpoint: str) -> list[dict]:
        query, items, seen = {"endpoint": endpoint}, [], set()
        while True:
            page = self.api("/bulk/list", query)
            data = page.get("data")
            if not isinstance(data, list):
                raise JQuantsError("bulk list has no data array")
            items.extend(_bulk_item(x, endpoint) for x in data)
            cursor = page.get("pagination_key")
            if cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise JQuantsError("invalid or repeating pagination_key")
            seen.add(cursor)
            query = {"endpoint": endpoint, "pagination_key": cursor}

    def download(self, key: str) -> bytes:
        url = self.api("/bulk/get", {"key": key}).get("url")
        split = urlsplit(url) if isinstance(url, str) else None
        if not split or split.scheme != "https" or not split.hostname or split.username or split.password:
            raise JQuantsError("bulk/get returned an unsafe download URL")
        return self._read(Request(url, headers={"Accept": "application/gzip"}), "bulk download")


def _bulk_item(value, endpoint: str) -> dict:
    if not isinstance(value, dict):
        raise JQuantsError("bulk list contains a non-object")
    key, modified, size = value.get("Key"), value.get("LastModified"), value.get("Size")
    if isinstance(size, float) and size.is_integer():
        size = int(size)
    prefix = endpoint.strip("/") + "/"
    if (not isinstance(key, str) or not key.startswith(prefix) or ".." in key.split("/")
            or not key.endswith(".csv.gz") or not isinstance(modified, str) or not modified
            or type(size) is not int or size < 0):
        raise JQuantsError("bulk list contains an unexpected entry")
    return {"key": key, "last_modified": modified, "size": size}


def in_months(key: str, start: str, end: str | None) -> bool:
    """Monthly (YYYYMM) and daily (YYYYMMDD) files are filtered by month; undated files always pass."""
    match = PERIOD.search(key)
    if not match:
        return True
    month = f"{match.group(1)[:4]}-{match.group(1)[4:6]}"
    return start <= month and (end is None or month <= end)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict:
    if not path.exists():
        return {"files": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise JQuantsError("raw manifest is malformed")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sync(client: Client, data_dir: Path, datasets: list[str], start: str, end: str | None) -> dict:
    raw = outside_repo(data_dir) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    counts = {"downloaded": 0, "skipped": 0, "superseded": 0}
    with open(raw / ".sync.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JQuantsError("another sync holds the lock; retry when it finishes") from None
        manifest = _read_manifest(raw / "manifest.json")
        for name in datasets:
            listed = [x for x in client.bulk_list(DATASETS[name]) if in_months(x["key"], start, end)]
            log(f"{name}: {len(listed)} bulk files in range")
            for item in sorted(listed, key=lambda x: x["key"]):
                path, entry = raw / item["key"], manifest["files"].get(item["key"])
                if (entry and entry["last_modified"] == item["last_modified"] and entry["size"] == item["size"]
                        and path.exists() and sha256_file(path) == entry["sha256"]):
                    counts["skipped"] += 1
                    continue
                data = client.download(item["key"])
                if len(data) != item["size"]:
                    raise JQuantsError(f"{item['key']}: size {len(data)} != listed {item['size']}")
                try:
                    gzip.decompress(data)
                except (OSError, EOFError):
                    raise JQuantsError(f"{item['key']}: not a complete gzip file") from None
                digest = hashlib.sha256(data).hexdigest()
                previous = sha256_file(path) if path.exists() else None
                if previous is not None and previous != digest:
                    # Corrections are kept, never silently overwritten, even without a manifest entry.
                    old = raw / "_superseded" / f"{item['key']}.{previous[:12]}"
                    old.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, old)
                    counts["superseded"] += 1
                _atomic_write(path, data)
                manifest["files"][item["key"]] = {"dataset": name, "endpoint": DATASETS[name], **item,
                                                  "sha256": digest, "fetched_at": now_iso()}
                _atomic_write(raw / "manifest.json", (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode())
                counts["downloaded"] += 1
                log(f"  {item['key']} ({len(data):,} B)")
    return counts


def _number(value: str) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def atm_by_expiry(rows, source_key: str) -> tuple[list[dict], int]:
    """One at-the-money row per (date, SQ date) from clearing-price rows (EmMrgnTrgDiv 002).

    ATM is the strike nearest the underlying where both the put (PCDiv 1) and the call
    (PCDiv 2) have a positive settlement price and IV; ties go to the lower strike.
    """
    groups: dict[tuple[str, str], dict[float, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        strike = _number(row.get("Strike"))
        if row.get("EmMrgnTrgDiv") != "002" or row.get("PCDiv") not in ("1", "2") or strike is None:
            continue
        legs = groups[(row["Date"], row["SQD"])][strike]
        if row["PCDiv"] in legs:
            raise ValueError(f"{source_key}: duplicate option row {row['Date']} {row.get('Code')}")
        legs[row["PCDiv"]] = row
    out, inconsistent = [], 0
    for (day, sqd), strikes in sorted(groups.items()):
        under = {_number(leg.get("UnderPx")) for legs in strikes.values() for leg in legs.values()}
        if len(under) != 1 or None in under or not min(under) > 0:
            inconsistent += 1
            continue
        underlying = under.pop()

        def usable(legs):
            return len(legs) == 2 and all((_number(legs[pc].get("Settle")) or 0) > 0
                                          and (_number(legs[pc].get("IV")) or 0) > 0 for pc in ("1", "2"))
        candidates = [k for k, legs in strikes.items() if usable(legs)]
        if not candidates:
            continue
        strike = min(candidates, key=lambda k: (abs(k - underlying), k))
        put, call = strikes[strike]["1"], strikes[strike]["2"]
        put_iv, call_iv = _number(put["IV"]) / 100, _number(call["IV"]) / 100
        base = _number(put.get("BaseVol"))
        out.append({
            "date": day, "sqd": sqd, "ltd": put.get("LTD") or None, "contract_month": put.get("CM") or None,
            "days_to_sq": (date.fromisoformat(sqd) - date.fromisoformat(day)).days,
            "underlying": underlying, "strike": strike, "put_iv": put_iv, "call_iv": call_iv,
            "atm_iv": (put_iv + call_iv) / 2, "put_settle": _number(put["Settle"]), "call_settle": _number(call["Settle"]),
            "put_oi": _number(put.get("OI")), "call_oi": _number(call.get("OI")),
            "put_volume": _number(put.get("Vo")), "call_volume": _number(call.get("Vo")),
            "base_vol": base / 100 if base is not None else None, "source_key": source_key,
        })
    return out, inconsistent


def _csv_rows(data: bytes):
    return csv.DictReader(io.StringIO(gzip.decompress(data).decode("utf-8"), newline=""))


def _modified(entry: dict) -> datetime:
    value = datetime.fromisoformat(entry["last_modified"].replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build(data_dir: Path) -> dict:
    """Rebuild jquants.db. A file with a newer LastModified replaces every date it contains."""
    root = outside_repo(data_dir)
    manifest = _read_manifest(root / "raw" / "manifest.json")
    files = sorted(manifest["files"].values(), key=lambda x: (_modified(x), x["key"]))
    topix, calendar, options = {}, {}, {}
    stats = {"files": len(files), "overridden_dates": 0, "inconsistent_option_groups": 0,
             "topix_without_close": 0, "option_dates_without_atm": 0}
    for entry in files:
        path = root / "raw" / entry["key"]
        data = path.read_bytes() if path.exists() else None
        if data is None or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise JQuantsError(f"{entry['key']}: raw file missing or SHA-256 mismatch; run sync")
        rows = list(_csv_rows(data))
        if entry["dataset"] == "topix":
            for row in rows:
                stats["overridden_dates"] += row["Date"] in topix
                close = _number(row.get("C"))
                if close is None or close <= 0:
                    topix.pop(row["Date"], None)
                    stats["topix_without_close"] += 1
                    continue
                topix[row["Date"]] = (row["Date"], _number(row.get("O")), _number(row.get("H")),
                                      _number(row.get("L")), close, entry["key"])
        elif entry["dataset"] == "calendar":
            for row in rows:
                calendar[row["Date"]] = (row["Date"], row["HolDiv"], entry["key"])
        elif entry["dataset"] == "options225":
            atm, bad = atm_by_expiry(rows, entry["key"])
            if rows and not atm:
                raise JQuantsError(f"{entry['key']}: option rows present but no usable ATM pair; check the schema")
            stats["inconsistent_option_groups"] += bad
            dates = {row["Date"] for row in rows}
            stats["option_dates_without_atm"] += len(dates - {r["date"] for r in atm})
            stale = {k for k in options if k[0] in dates}
            stats["overridden_dates"] += len({k[0] for k in stale})
            for k in stale:
                del options[k]
            options.update({(r["date"], r["sqd"]): r for r in atm})
    tmp = root / f".jquants.db.tmp-{os.getpid()}"
    tmp.unlink(missing_ok=True)
    try:
        conn = sqlite3.connect(tmp)
        try:
            conn.executescript(SCHEMA)
            conn.executemany("INSERT INTO source_files VALUES (?,?,?,?,?)",
                             [(f["key"], f["dataset"], f["last_modified"], f["size"], f["sha256"]) for f in files])
            conn.executemany("INSERT INTO topix_daily VALUES (?,?,?,?,?,?)", sorted(topix.values()))
            conn.executemany("INSERT INTO trading_calendar VALUES (?,?,?)", sorted(calendar.values()))
            columns = ("date", "sqd", "ltd", "contract_month", "days_to_sq", "underlying", "strike", "put_iv",
                       "call_iv", "atm_iv", "put_settle", "call_settle", "put_oi", "call_oi", "put_volume",
                       "call_volume", "base_vol", "source_key")
            conn.executemany(f"INSERT INTO n225_option_atm VALUES ({','.join('?' * len(columns))})",
                             [tuple(options[k][c] for c in columns) for k in sorted(options)])
            stats.update({"topix_rows": len(topix), "calendar_rows": len(calendar), "option_atm_rows": len(options),
                          "built_at": now_iso()})
            conn.executemany("INSERT INTO build_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in stats.items()])
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, root / "jquants.db")
    finally:
        tmp.unlink(missing_ok=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("sync", "build"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--datasets", default=",".join(DATASETS))
    parser.add_argument("--start", default="2016-10", help="first month (YYYY-MM) to mirror")
    parser.add_argument("--end", default=None, help="last month (YYYY-MM); default: everything listed")
    args = parser.parse_args(argv)
    try:
        outside_repo(args.data_dir)
        if args.command == "build":
            log(f"build: {json.dumps(build(args.data_dir), sort_keys=True)}")
            return 0
        datasets = [x.strip() for x in args.datasets.split(",") if x.strip()]
        if not datasets or any(x not in DATASETS for x in datasets):
            raise ValueError(f"datasets must be a subset of {sorted(DATASETS)}")
        for month in (args.start, args.end):
            if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                raise ValueError("months must be YYYY-MM")
        counts = sync(Client(load_api_key()), args.data_dir, datasets, args.start, args.end)
        log(f"sync: {json.dumps(counts, sort_keys=True)}")
        return 0
    except (ValueError, KeyError, OSError, csv.Error, sqlite3.Error, JQuantsError) as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
