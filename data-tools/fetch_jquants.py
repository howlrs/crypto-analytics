#!/usr/bin/env python3
"""Mirror every J-Quants V2 bulk dataset the plan serves and load all columns into a local SQLite.

J-Quants data is licensed for private use and this repository is public, so raw files,
the derived database and analysis outputs live under --data-dir (default
/mnt/e/Datas/market/jquants). Paths inside the repository are refused.

  sync   list bulk files, download new or changed ones, verify size, gzip and SHA-256 (network)
  build  rebuild jquants.db from the verified raw files, one file at a time (offline, no API key)

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
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from operator import itemgetter
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import log, now_iso

API_BASE = "https://api.jquants.com/v2"
DOWNLOAD_HOST_SUFFIX = ".r2.cloudflarestorage.com"  # where bulk/get pointed as of 2026-10-20
REPO = Path(__file__).resolve().parents[1]
DEFAULT_DIR = Path("/mnt/e/Datas/market/jquants")
KEY_NAMES = ("JQUANTS_API_KEY", "JQUANTS_APIKEY")  # official CLI name first, legacy .env name second
DATASETS = {
    "topix": "/indices/bars/daily/topix",
    "options225": "/derivatives/bars/daily/options/225",
    "calendar": "/markets/calendar",
    # Standard plan, checked 2026-10-20 (futures, dividends, breakdown and AM bars return 403)
    "equities_daily": "/equities/bars/daily",
    "equities_master": "/equities/master",
    "indices_daily": "/indices/bars/daily",
    "margin_weekly": "/markets/margin-interest",
    "margin_alert": "/markets/margin-alert",
    "short_ratio": "/markets/short-ratio",
    "short_positions": "/markets/short-sale-report",
    "investor_types": "/equities/investor-types",
    "fins_summary": "/fins/summary",  # disclosure dates and times: earnings-jump risk for the xyz stock perps
    "equities_valuation": "/equities/valuation",  # daily EPS/BPS/ROE/PER/PBR per stock (found 2026-10-31)
    "fins_earnings_date": "/fins/earnings-date",  # scheduled earnings dates with their publication dates
}
PERIOD = re.compile(r"_(\d{6}|\d{8})\.csv\.gz$")
SCHEMA = """
CREATE TABLE source_files (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, dataset TEXT NOT NULL,
  last_modified TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE n225_option_atm (date TEXT NOT NULL, sqd TEXT NOT NULL, ltd TEXT, contract_month TEXT,
  days_to_sq INTEGER NOT NULL, underlying REAL NOT NULL, strike REAL NOT NULL, put_iv REAL NOT NULL,
  call_iv REAL NOT NULL, atm_iv REAL NOT NULL, put_settle REAL NOT NULL, call_settle REAL NOT NULL,
  put_oi REAL, call_oi REAL, put_volume REAL, call_volume REAL, base_vol REAL, source_id INTEGER NOT NULL,
  PRIMARY KEY (date, sqd)) WITHOUT ROWID;
CREATE TABLE build_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE build_quality (table_name TEXT NOT NULL, column_name TEXT NOT NULL, token TEXT NOT NULL,
  rows INTEGER NOT NULL, PRIMARY KEY (table_name, column_name, token)) WITHOUT ROWID;
"""
ATM_COLUMNS = ("date", "sqd", "ltd", "contract_month", "days_to_sq", "underlying", "strike", "put_iv", "call_iv",
               "atm_iv", "put_settle", "call_settle", "put_oi", "call_oi", "put_volume", "call_volume", "base_vol")
# Column kinds. Raw values are text and '' is NULL; SQLite column affinity converts numbers. After the
# load, values a numeric, boolean or time column could not take are counted in build_quality and set to
# NULL, so nothing is dropped silently.
#   T text   R real   I integer   B 'true'/'false' -> 1/0   H time (HH:MM until 2018-03 becomes HH:MM:SS)
#   F margin-alert reasons: the dict literal becomes one 0/1 column per known reason, other keys as JSON
SQL_TYPES = {"T": "TEXT", "R": "REAL", "I": "INTEGER", "B": "INTEGER", "H": "TEXT"}
ALERT_REASONS = (("Restricted", "restricted"), ("DailyPublication", "daily_publication"),
                 ("Monitoring", "monitoring"), ("RestrictedByJSF", "restricted_by_jsf"),
                 ("PrecautionByJSF", "precaution_by_jsf"), ("UnclearOrSecOnAlert", "unclear_or_sec_on_alert"))
_INVESTOR_GROUPS = ("Prop", "Brk", "Tot", "Ind", "Frgn", "SecCo", "InvTr", "BusCo", "OthCo", "InsCo", "Bank",
                    "TrstBnk", "OthFin")


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class Table:
    """One loaded dataset: (source, target, kind) columns, the date column a newer file replaces, and
    a primary key (None keeps exact duplicate rows). A table without secondary indexes is WITHOUT
    ROWID; one with them stays a rowid table, because with a WITHOUT ROWID table SQLite plans
    joins such as japan_positioning's (equities_daily JOIN a temp date table) as a scan of the
    secondary index with a lookup per row, about 10M random reads. A staged table is loaded into a
    scratch database and compressed afterwards (the daily master snapshots)."""

    def __init__(self, name: str, date: str, columns, key: tuple | None, indexes: tuple = (), stage: bool = False):
        self.name, self.date, self.columns, self.key = name, date, tuple(columns), key
        self.indexes, self.stage = indexes, stage
        self.targets = []
        for _, target, kind in self.columns:
            if kind == "F":
                self.targets += [(t, "INTEGER") for _, t in ALERT_REASONS] + [(f"{target}_extra", "TEXT")]
            else:
                self.targets.append((target, SQL_TYPES[kind]))
        names = [t for t, _ in self.targets]
        if len(set(names)) != len(names) or date not in names or any(k not in names for k in key or ()):
            raise ValueError(f"{name}: inconsistent column definition")

    def kinds(self, wanted: str) -> list[str]:
        return [t for _, t, k in self.columns if k in wanted]

    def schema(self) -> str:
        required = {self.date, *(self.key or ())}
        cols = [f"{t} {typ}{' NOT NULL' if t in required else ''}" for t, typ in self.targets]
        if not self.stage:
            cols.append("source_id INTEGER NOT NULL")
        prefix = "stage." if self.stage else ""
        if self.key:
            return (f"CREATE TABLE {prefix}{self.name} ({', '.join(cols)}, PRIMARY KEY ({', '.join(self.key)}))"
                    f"{'' if self.indexes else ' WITHOUT ROWID'};")
        return f"CREATE TABLE {prefix}{self.name} ({', '.join(cols)});"


def _cols(kind: str, *sources: str, rename: dict | None = None) -> list:
    return [(s, (rename or {}).get(s, _snake(s)), kind) for s in sources]


_FINS = ("DiscDate DiscTime Code DiscNo DocType CurPerType CurPerSt CurPerEn CurFYSt CurFYEn NxtFYSt NxtFYEn Sales OP "
         "OdP NP EPS DEPS TA Eq EqAR BPS CFO CFI CFF CashEq Div1Q Div2Q Div3Q DivFY DivAnn DivUnit DivTotalAnn "
         "PayoutRatioAnn FDiv1Q FDiv2Q FDiv3Q FDivFY FDivAnn FDivUnit FDivTotalAnn FPayoutRatioAnn NxFDiv1Q NxFDiv2Q "
         "NxFDiv3Q NxFDivFY NxFDivAnn NxFDivUnit NxFPayoutRatioAnn FSales2Q FOP2Q FOdP2Q FNP2Q FEPS2Q NxFSales2Q "
         "NxFOP2Q NxFOdP2Q NxFNp2Q NxFEPS2Q FSales FOP FOdP FNP FEPS NxFSales NxFOP NxFOdP NxFNp NxFEPS MatChgSub "
         "SigChgInC ChgByASRev ChgNoASRev ChgAcEst RetroRst ShOutFY TrShFY AvgSh NCSales NCOP NCOdP NCNP NCEPS NCTA "
         "NCEq NCEqAR NCBPS FNCSales2Q FNCOP2Q FNCOdP2Q FNCNP2Q FNCEPS2Q NxFNCSales2Q NxFNCOP2Q NxFNCOdP2Q NxFNCNP2Q "
         "NxFNCEPS2Q FNCSales FNCOP FNCOdP FNCNP FNCEPS NxFNCSales NxFNCOP NxFNCOdP NxFNCNP NxFNCEPS ShEq NCShEq ROE "
         "NCROE").split()
_FINS_TEXT = {"DiscDate", "Code", "DiscNo", "DocType", "CurPerType", "CurPerSt", "CurPerEn", "CurFYSt", "CurFYEn",
              "NxtFYSt", "NxtFYEn"}
_FINS_BOOL = {"MatChgSub", "SigChgInC", "ChgByASRev", "ChgNoASRev", "ChgAcEst", "RetroRst"}
_FINS_RENAME = {"CurPerSt": "cur_per_start", "CurPerEn": "cur_per_end", "CurFYSt": "cur_fy_start",
                "CurFYEn": "cur_fy_end", "NxtFYSt": "nxt_fy_start", "NxtFYEn": "nxt_fy_end"}
_OHLC = {"O": "open", "H": "high", "L": "low", "C": "close", "Vo": "volume", "Va": "turnover"}

# Every mirrored dataset is loaded with all of its columns. Existing table and column names are kept
# for the analyses in backtests/; new columns use the snake_case of the J-Quants V2 name.
TABLES = {
    "calendar": Table("trading_calendar", "date", _cols("T", "Date", "HolDiv"), ("date",)),
    "topix": Table("topix_daily", "date", _cols("T", "Date") + _cols("R", "O", "H", "L", "C", rename=_OHLC),
                   ("date",)),
    "options225": Table("n225_options", "date", _cols("T", "Date", "Code") + _cols(
        "R", "O", "H", "L", "C", "EO", "EH", "EL", "EC", "AO", "AH", "AL", "AC", "Vo", "OI", "Va",
        rename={**_OHLC, "OI": "open_interest"}) + _cols("T", "CM", rename={"CM": "contract_month"})
        + _cols("R", "Strike", "VoOA") + _cols("T", "EmMrgnTrgDiv", "PCDiv", "LTD", "SQD", rename={"PCDiv": "pc_div"})
        + _cols("R", "Settle", "Theo", "BaseVol", "UnderPx", "IV", "IR"),
        ("date", "code", "em_mrgn_trg_div")),  # an emergency-margin day (001) repeats each contract beside 002
    "equities_daily": Table("equities_daily", "date", _cols("T", "Date", "Code") + _cols(
        "R", "O", "H", "L", "C", rename=_OHLC) + _cols("I", "UL", "LL", rename={"UL": "upper_limit", "LL": "lower_limit"})
        + _cols("R", "Vo", "Va", "AdjFactor", "MktCap", rename={**_OHLC, "MktCap": "market_cap"})
        + _cols("T", "ExRT"), ("date", "code"), indexes=(("code", "date"),)),
    "equities_master": Table("equities_master_stage", "date", _cols(
        "T", "Date", "Code", "CoName", "CoNameEn", "S17", "S17Nm", "S33", "S33Nm", "ScaleCat", "Mkt", "MktNm",
        "Mrgn", "MrgnNm", "ProdCat"), ("date", "code"), stage=True),
    "equities_valuation": Table("equities_valuation", "date", _cols("T", "Date", "Code") + _cols(
        "R", "EPS", "FwdEPS", "BPS", "ROE", "FwdROE", "PER", "FwdPER", "PBR", "MktCap", rename={"MktCap": "market_cap"}),
        ("date", "code"), indexes=(("code", "date"),)),
    "indices_daily": Table("indices_daily", "date", _cols("T", "Date", "Code") + _cols("R", "O", "H", "L", "C",
                                                                                      rename=_OHLC), ("date", "code")),
    "margin_weekly": Table("margin_weekly", "date", _cols("T", "Date", "Code", "PubDate", "IssType") + _cols(
        "R", "LongVol", "ShrtVol", "LongNegVol", "ShrtNegVol", "LongStdVol", "ShrtStdVol", "LongVal", "ShrtVal",
        "LongNegVal", "ShrtNegVal", "LongStdVal", "ShrtStdVal",
        rename={f"{side}{part}{unit}": f"{'long' if side == 'Long' else 'short'}{'_' + part.lower() if part else ''}_"
                f"{'shares' if unit == 'Vol' else 'value'}" for side in ("Long", "Shrt") for part in ("", "Neg", "Std")
                for unit in ("Vol", "Val")}), ("date", "code"), indexes=(("code", "date"),)),
    "margin_alert": Table("margin_alert", "pub_date", _cols("T", "PubDate", "Code", "AppDate")
                          + [("PubReason", "pub_reason", "F")] + _cols(
        "R", "ShrtOut", "ShrtOutChg", "ShrtOutRatio", "LongOut", "LongOutChg", "LongOutRatio", "SLRatio", "ShrtNegOut",
        "ShrtNegOutChg", "ShrtStdOut", "ShrtStdOutChg", "LongNegOut", "LongNegOutChg", "LongStdOut", "LongStdOutChg",
        rename={"SLRatio": "sl_ratio"})
        + _cols("T", "TSEMrgnRegCls", rename={"TSEMrgnRegCls": "tse_margin_reg_cls"}),
        ("pub_date", "code", "app_date"), indexes=(("code", "pub_date"),)),
    "short_ratio": Table("short_ratio", "date", _cols("T", "Date", "S33") + _cols(
        "R", "SellExShortVa", "ShrtWithResVa", "ShrtNoResVa",
        rename={"ShrtWithResVa": "short_with_res_va", "ShrtNoResVa": "short_no_res_va"}), ("date", "s33")),
    "short_positions": Table("short_positions", "disc_date", _cols(
        "T", "DiscDate", "CalcDate", "Code", "SSName", "SSAddr", "DICName", "DICAddr", "FundName",
        rename={"SSName": "ss_name", "SSAddr": "ss_addr", "DICName": "dic_name", "DICAddr": "dic_addr"})
        + _cols("R", "ShrtPosToSO", "ShrtPosShares", "ShrtPosUnits", rename={"ShrtPosToSO": "shrt_pos_to_so"})
        + _cols("T", "PrevRptDate") + _cols("R", "PrevRptRatio") + _cols("T", "Notes"),
        None, indexes=(("code", "calc_date"),)),  # reports can repeat verbatim within a file; all are kept
    "investor_types": Table("investor_types", "en_date", _cols("T", "PubDate", "StDate", "EnDate", "Section")
                            + _cols("R", *(f"{g}{k}" for g in _INVESTOR_GROUPS for k in ("Sell", "Buy", "Tot", "Bal"))),
                            ("en_date", "section", "pub_date")),  # a week can be republished with a later PubDate
    "fins_summary": Table("fins_summary", "disc_date", [
        (s, _FINS_RENAME.get(s, _snake(s)), "H" if s == "DiscTime" else "T" if s in _FINS_TEXT
         else "B" if s in _FINS_BOOL else "R") for s in _FINS], ("code", "disc_no"), indexes=(("disc_date",),)),
    "fins_earnings_date": Table("fins_earnings_date", "pub_date", _cols(
        "T", "PubDate", "SchDate", "FQName", "FYE", "Code", "CoName", "CoNameEn", rename={"FQName": "fq_name"}),
        None, indexes=(("code", "sch_date"), ("sch_date",))),  # SchDate is empty while a date is undecided
}


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
        # The netloc must be the bare host: no userinfo and no port.
        if not split or split.scheme != "https" or not split.hostname or split.netloc.lower() != split.hostname:
            raise JQuantsError("bulk/get returned an unsafe download URL")
        if not split.hostname.endswith(DOWNLOAD_HOST_SUFFIX):
            raise JQuantsError(f"bulk/get returned an unexpected download host {split.hostname}; "
                               "check J-Quants and update DOWNLOAD_HOST_SUFFIX")
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
                outside_repo(path)  # a symlink below raw/ must not lead into the repository
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
                    outside_repo(old)
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


def _csv_table(data: bytes) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(gzip.decompress(data).decode("utf-8"), newline=""))
    header = next(reader, [])
    return header, list(reader)


def _modified(entry: dict) -> datetime:
    value = datetime.fromisoformat(entry["last_modified"].replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


_REASON = re.compile(r"'(\w+)': '([^']*)'")


def _reasons(value: str) -> tuple:
    """Margin-alert PubReason dict literal -> one value per ALERT_REASONS key, then the rest (JSON).
    A value that does not look like the dict literal is kept verbatim in the extra column."""
    if not value:
        return (None,) * (len(ALERT_REASONS) + 1)
    found = dict(_REASON.findall(value))
    if not found:
        return (None,) * len(ALERT_REASONS) + (value,)
    flags = tuple(found.pop(source, None) for source, _ in ALERT_REASONS)
    return flags + (json.dumps(found, sort_keys=True, ensure_ascii=False) if found else None,)


def _chunks(values: list, size: int = 500):
    for i in range(0, len(values), size):
        yield values[i:i + size]


class _Loader:
    """Streams one verified file at a time into its table; a newer file replaces every date it holds."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.loaded = conn, defaultdict(set)

    def _delete_dates(self, table: str, column: str, dates: list[str]) -> None:
        for part in _chunks(dates):
            self.conn.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' * len(part))})", part)

    def load(self, spec: Table, header: list[str], rows: list[list[str]], source_id: int, label: str,
             also: tuple[tuple[str, str], ...] = ()) -> tuple[int, list[str]]:
        """Returns (dates overridden, unloaded header columns). `also` lists (table, date column)
        pairs derived from the same file, replaced together with it."""
        position = {name: i for i, name in enumerate(header)}
        missing = [s for s, _, _ in spec.columns if s not in position]
        if missing:
            raise JQuantsError(f"{label}: missing columns {missing}; check the schema")
        extra = sorted(set(header) - {s for s, _, _ in spec.columns})
        idx = [position[s] for s, _, _ in spec.columns]
        date_i = position[next(s for s, t, _ in spec.columns if t == spec.date)]
        dates = set(map(itemgetter(date_i), rows))
        if "" in dates:
            raise JQuantsError(f"{label}: row without date or key")
        overlap = sorted(dates & self.loaded[spec.name])
        prefix = "stage." if spec.stage else ""
        if overlap:
            self._delete_dates(prefix + spec.name, spec.date, overlap)
            for table, column in also:
                self._delete_dates(table, column, overlap)
        self.loaded[spec.name] |= dates
        if any(k == "F" for _, _, k in spec.columns):
            values = [tuple(x for i, (_, _, k) in zip(idx, spec.columns)
                            for x in (_reasons(r[i]) if k == "F" else (r[i],))) for r in rows]
        else:
            values = list(map(itemgetter(*idx), rows))
        replace = bool(spec.key) and spec.date not in spec.key
        if replace:  # the key can move to another date; within one file it must still be unique
            key_i = [i for i, t in enumerate(t for t, _ in spec.targets) if t in spec.key]
            keys = list(map(itemgetter(*key_i), values))
            if len(set(keys)) != len(keys):
                raise JQuantsError(f"{label}: duplicate {spec.name} row")
        marks = ", ".join(["NULLIF(?, '')"] * len(spec.targets) + ([] if spec.stage else [str(int(source_id))]))
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        try:
            self.conn.executemany(f"{verb} INTO {prefix}{spec.name} VALUES ({marks})", values)
        except sqlite3.IntegrityError as exc:
            what = "row without date or key" if "NOT NULL" in str(exc) else f"duplicate {spec.name} row"
            raise JQuantsError(f"{label}: {what}") from None
        return len(overlap), extra


def _clean(conn: sqlite3.Connection, spec: Table, quality: list) -> None:
    """Count and null the values a typed column could not take; normalise booleans and times."""
    name = ("stage." if spec.stage else "") + spec.name
    numeric = [t for t, typ in spec.targets if typ in ("REAL", "INTEGER") and t not in spec.kinds("B")]
    checks = numeric + spec.kinds("B") + spec.kinds("H")
    if not checks:
        return
    for column in spec.kinds("B"):
        conn.execute(f"UPDATE {name} SET {column} = CASE {column} WHEN 'true' THEN 1 WHEN 'false' THEN 0 "
                     f"ELSE {column} END WHERE typeof({column}) = 'text'")
    for column in spec.kinds("H"):
        conn.execute(f"UPDATE {name} SET {column} = {column} || ':00' WHERE {column} GLOB '[0-2][0-9]:[0-5][0-9]'")
    bad = {c: (f"typeof({c}) = 'text'" if c not in spec.kinds("H")
               else f"{c} NOT GLOB '[0-2][0-9]:[0-5][0-9]:[0-5][0-9]'") for c in checks}
    counts = conn.execute(f"SELECT {', '.join(f'COALESCE(SUM({w}), 0)' for w in bad.values())} FROM {name}").fetchone()
    for (column, where), total in zip(bad.items(), counts):
        if not total:
            continue
        top = conn.execute(f"SELECT {column}, COUNT(*) FROM {name} WHERE {where} GROUP BY {column} "
                           "ORDER BY COUNT(*) DESC, 1 LIMIT 20").fetchall()
        quality += [(spec.name, column, str(token), n) for token, n in top]
        if total > sum(n for _, n in top):
            quality.append((spec.name, column, "<other>", total - sum(n for _, n in top)))
        conn.execute(f"UPDATE {name} SET {column} = NULL WHERE {where}")


def _finish_master(conn: sqlite3.Connection, spec: Table) -> dict:
    """Compress the daily master snapshots into validity intervals: a new interval starts whenever a
    code's attributes change or it is missing from a snapshot. The equities_master view rebuilds
    every (date, code) snapshot row exactly."""
    attrs = [t for t, _ in spec.targets if t not in ("date", "code")]
    cols = ", ".join(attrs)
    statements = [
        "CREATE TABLE equities_master_dates (date TEXT PRIMARY KEY) WITHOUT ROWID",
        f"INSERT INTO equities_master_dates SELECT DISTINCT date FROM stage.{spec.name} ORDER BY date",
        f"CREATE TABLE equities_master_history (code TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
        f"{', '.join(f'{a} TEXT' for a in attrs)}, snapshots INTEGER NOT NULL, PRIMARY KEY (code, valid_from)) WITHOUT ROWID",
        f"WITH d AS (SELECT date, ROW_NUMBER() OVER (ORDER BY date) AS i FROM equities_master_dates), "
        f"s AS (SELECT m.*, d.i - ROW_NUMBER() OVER (PARTITION BY m.code, {', '.join(f'm.{a}' for a in attrs)} "
        f"ORDER BY d.i) AS g FROM stage.{spec.name} m JOIN d USING (date)) "
        f"INSERT INTO equities_master_history SELECT code, MIN(date), MAX(date), {cols}, COUNT(*) FROM s "
        f"GROUP BY code, {cols}, g",
        f"CREATE VIEW equities_master AS SELECT d.date, h.code, {', '.join(f'h.{a}' for a in attrs)} "
        "FROM equities_master_dates d JOIN equities_master_history h ON d.date BETWEEN h.valid_from AND h.valid_to",
    ]
    for statement in statements:  # not executescript: it would commit the open build transaction
        conn.execute(statement)
    staged = conn.execute(f"SELECT COUNT(*) FROM stage.{spec.name}").fetchone()[0]
    kept, covered = conn.execute("SELECT COUNT(*), COALESCE(SUM(snapshots), 0) FROM equities_master_history").fetchone()
    if covered != staged:
        raise JQuantsError(f"master compression lost rows: {covered} != {staged}")
    return {"equities_master_snapshot_rows": staged, "equities_master_history_rows": kept}


def build(data_dir: Path, work_dir: Path | None = None) -> dict:
    """Rebuild jquants.db with every column of every mirrored dataset, one file at a time.
    A file with a newer LastModified replaces every date it contains. work_dir holds the database
    while it is written (random writes are slow on a Windows drive under WSL); the finished file is
    copied next to the old one and swapped in atomically."""
    root = outside_repo(data_dir)
    work = outside_repo(work_dir) if work_dir else root
    work.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(root / "raw" / "manifest.json")
    files = sorted(manifest["files"].values(), key=lambda x: (_modified(x), x["key"]))
    unknown = sorted({f["dataset"] for f in files} - set(TABLES))
    if unknown:
        raise JQuantsError(f"no table definition for datasets {unknown}")
    stats = {"files": len(files), "overridden_dates": 0, "inconsistent_option_groups": 0,
             "option_dates_without_atm": 0}
    unloaded: dict[str, list[str]] = {}
    quality: list[tuple] = []
    tmp, stage = work / f".jquants.db.tmp-{os.getpid()}", work / f".jquants-stage.tmp-{os.getpid()}"
    swap = root / f".jquants.db.swap-{os.getpid()}"
    for path in (tmp, stage, swap):
        path.unlink(missing_ok=True)
    try:
        conn = sqlite3.connect(tmp, isolation_level=None)
        try:
            conn.execute("ATTACH DATABASE ? AS stage", (str(stage),))
            for pragma in ("main.journal_mode=OFF", "stage.journal_mode=OFF", "main.synchronous=OFF",
                           "stage.synchronous=OFF", "cache_size=-1048576", "temp_store=MEMORY"):
                conn.execute(f"PRAGMA {pragma}")
            conn.executescript(SCHEMA)
            for spec in TABLES.values():
                conn.execute(spec.schema())
                if spec.key is None or spec.key[0] != spec.date:  # date deletes need an index during the load
                    conn.execute(f"CREATE INDEX {'stage.' if spec.stage else ''}{spec.name}_{spec.date} "
                                 f"ON {spec.name} ({spec.date})")
            loader = _Loader(conn)
            conn.execute("BEGIN")
            for source_id, entry in enumerate(files, 1):
                path = root / "raw" / entry["key"]
                data = path.read_bytes() if path.exists() else None
                if data is None or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise JQuantsError(f"{entry['key']}: raw file missing or SHA-256 mismatch; run sync")
                conn.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?)", (
                    source_id, entry["key"], entry["dataset"], entry["last_modified"], entry["size"], entry["sha256"]))
                header, rows = _csv_table(data)
                if not rows:
                    continue
                spec = TABLES[entry["dataset"]]
                also = (("n225_option_atm", "date"),) if entry["dataset"] == "options225" else ()
                overridden, extra = loader.load(spec, header, rows, source_id, entry["key"], also)
                stats["overridden_dates"] += overridden
                if extra:
                    unloaded[entry["dataset"]] = sorted(set(unloaded.get(entry["dataset"], [])) | set(extra))
                if entry["dataset"] == "options225":
                    atm, bad = atm_by_expiry([dict(zip(header, r)) for r in rows], entry["key"])
                    if not atm:
                        raise JQuantsError(f"{entry['key']}: option rows present but no usable ATM pair; check the schema")
                    stats["inconsistent_option_groups"] += bad
                    day = header.index("Date")
                    stats["option_dates_without_atm"] += len({r[day] for r in rows} - {r["date"] for r in atm})
                    conn.executemany(f"INSERT INTO n225_option_atm VALUES ({','.join('?' * (len(ATM_COLUMNS) + 1))})",
                                     [tuple(r[c] for c in ATM_COLUMNS) + (source_id,) for r in atm])
            for spec in TABLES.values():
                _clean(conn, spec, quality)
            stats["topix_without_close"] = conn.execute(
                "DELETE FROM topix_daily WHERE close IS NULL OR close <= 0").rowcount
            stats.update(_finish_master(conn, TABLES["equities_master"]))
            conn.execute("COMMIT")
            conn.execute("DETACH DATABASE stage")
            for spec in TABLES.values():
                for cols in spec.indexes:
                    if spec.key is None or cols != (spec.date,) or spec.key[0] == spec.date:
                        conn.execute(f"CREATE INDEX IF NOT EXISTS {spec.name}_{'_'.join(cols)} "
                                     f"ON {spec.name} ({', '.join(cols)})")
            for spec in TABLES.values():
                if not spec.stage:
                    stats[f"{spec.name}_rows"] = conn.execute(f"SELECT COUNT(*) FROM {spec.name}").fetchone()[0]
            stats.update({"topix_rows": stats["topix_daily_rows"], "calendar_rows": stats["trading_calendar_rows"],
                          "option_atm_rows": conn.execute("SELECT COUNT(*) FROM n225_option_atm").fetchone()[0],
                          "unloaded_columns": unloaded, "quality_issues": len(quality), "built_at": now_iso()})
            conn.executemany("INSERT INTO build_quality VALUES (?,?,?,?)", quality)
            # No ANALYZE: without statistics the planner keeps the plans the analyses were written against.
            conn.executemany("INSERT INTO build_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in stats.items()])
        finally:
            conn.close()
        if work == root:
            os.replace(tmp, root / "jquants.db")
        else:
            shutil.copyfile(tmp, swap)
            os.replace(swap, root / "jquants.db")
    finally:
        for path in (tmp, stage, swap):
            path.unlink(missing_ok=True)
    for name, columns in unloaded.items():
        log(f"warning: {name} has columns without a table definition: {columns}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("sync", "build"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="build: write the database here first (a Linux filesystem is much faster than /mnt)")
    parser.add_argument("--datasets", default=",".join(DATASETS))
    parser.add_argument("--start", default="2016-10", help="first month (YYYY-MM) to mirror")
    parser.add_argument("--end", default=None, help="last month (YYYY-MM); default: everything listed")
    args = parser.parse_args(argv)
    try:
        outside_repo(args.data_dir)
        if args.command == "build":
            log(f"build: {json.dumps(build(args.data_dir, args.work_dir), sort_keys=True)}")
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
