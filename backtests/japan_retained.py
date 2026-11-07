#!/usr/bin/env python3
"""The store of J-Quants aggregates that are meant to outlive the data.

After a cancellation the licence requires deleting the acquired data and every derivative that can
restore it; aggregates that cannot restore it may stay if they are not published (docs/jquants-standard.md
section 9). data-tools/purge_jquants.sh deletes the whole J-Quants directory, so an aggregate worth keeping
has to live elsewhere: RETAINED_DIR, outside the repository, the purge and the GCS backup.

Everything written here passes a shape check, which catches accidents rather than a deliberate attempt to get
around it. It does not prove a file is harmless, it refuses the shapes that could carry the data itself. A
table has one row per stock code, a bounded width, no date in a cell or a column name, and a minimum of
observations behind every row. A summary holds a conclusion and its test statistics: it may name the bounds
of a few periods, but it has no long or date-keyed container and little text.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
RETAINED_DIR = Path("/mnt/e/Datas/market/japan_retained")
MIN_OBSERVATIONS = 60
MAX_COLUMNS = 40
MAX_ROWS = 5_000
MAX_TEXT = 120
MAX_SUMMARY_ITEMS = 200
MAX_SUMMARY_NUMBERS = 2_000
MAX_SUMMARY_DATES = 40
MAX_SUMMARY_TEXT = 500
MAX_SUMMARY_ALL_TEXT = 20_000
MAX_SUMMARY_BYTES = 256_000
DATE_TEXT = re.compile(r"(?<!\d)(?:(?:19|20)\d{2}[-/._年]\d{1,2}(?:[-/._月]\d{1,2})?|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:\d{2})?(?!\d)"
                       r"|\d{1,2}/\d{1,2}/(?:19|20)\d{2})")
DATE_NUMBERS = ((19_000_101, 21_001_231), (190_001, 210_012), (946_684_800_000, 4_102_444_800_000))  # YYYYMMDD, YYYYMM, epoch ms
TSE_CODE = re.compile(r"^[0-9][0-9A-Z]{3}[0-9]$")
NAME = re.compile(r"^[a-z0-9_]+$")


def retained_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    for forbidden, why in ((ROOT, "the public repository"), (JQ_DIR.resolve(), "the directory the purge deletes")):
        if resolved == forbidden or forbidden in resolved.parents:
            raise ValueError(f"retained aggregates cannot live in {why}")
    return resolved


def _checked_name(name: str) -> str:
    if not NAME.match(name):
        raise ValueError("a retained file name is lower-case letters, digits and underscores")
    return name


def check_aggregate(frame: pd.DataFrame, key: str, observations: str) -> None:
    """Raise unless the table has one row per entity and nothing shaped like the source series."""
    if frame.empty:
        raise ValueError("nothing to retain")
    if len(frame.columns) > MAX_COLUMNS or len(frame) > MAX_ROWS:
        raise ValueError("too wide or too long for an aggregate: it may be a pivot of the data")
    if frame[key].isna().any() or frame[key].duplicated().any():
        raise ValueError(f"more than one row per {key}: a repeated entity can carry a series")
    if not frame[key].astype(str).map(lambda x: bool(TSE_CODE.match(x))).all():
        raise ValueError(f"{key} must hold stock codes: row numbers would make any series pass")
    counts = pd.to_numeric(frame[observations], errors="coerce").replace([np.inf, -np.inf], np.nan)
    if observations == key or counts.isna().any() or (counts < MIN_OBSERVATIONS).any():
        raise ValueError(f"a row rests on fewer than {MIN_OBSERVATIONS} observations")
    for column in frame.columns:
        values = frame[column].dropna()
        if DATE_TEXT.search(str(column)) or len(str(column)) > MAX_TEXT:
            raise ValueError(f"{str(column)[:40]} is a date used as a column name, or a long one")
        if pd.api.types.is_datetime64_any_dtype(values):
            raise ValueError(f"{column} is a date column")
        if pd.api.types.is_numeric_dtype(values):
            numbers = values.astype(float)
            whole = numbers[np.isfinite(numbers) & (numbers == np.floor(numbers))]
            dated = np.logical_or.reduce([whole.between(*span) for span in DATE_NUMBERS]) if len(whole) else np.array([])
            if len(values) and dated.sum() * 2 >= len(values):  # half: a real measure lands in these ranges only by chance
                raise ValueError(f"{column} holds dates written as numbers")
            continue
        text = values.astype(str)
        if text.map(lambda x: bool(DATE_TEXT.search(x))).any():
            raise ValueError(f"{column} holds dates")
        if (text.str.len() > MAX_TEXT).any():
            raise ValueError(f"{column} holds long text, which can carry a series")


def _plain(value):
    """JSON types only; a number that is not finite becomes null."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _check_summary(value, seen: dict[str, int]) -> None:
    """Raise on anything shaped like a series; add up the numbers, dates and text seen on the way."""
    if isinstance(value, dict):
        if len(value) > MAX_SUMMARY_ITEMS or any(DATE_TEXT.search(k) or len(k) > MAX_TEXT for k in value):
            raise ValueError("a long or date-keyed mapping looks like a series")
        seen["text"] += sum(len(k) for k in value)
        for item in value.values():
            _check_summary(item, seen)
    elif isinstance(value, list):
        if len(value) > MAX_SUMMARY_ITEMS:
            raise ValueError(f"a list longer than {MAX_SUMMARY_ITEMS} looks like a series")
        for item in value:
            _check_summary(item, seen)
    elif isinstance(value, str):
        if len(value) > MAX_SUMMARY_TEXT:
            raise ValueError("long text can carry a series")
        seen["text"] += len(value)
        seen["dates"] += bool(DATE_TEXT.search(value))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seen["numbers"] += 1


def write_table(frame: pd.DataFrame, name: str, key: str, observations: str, directory: Path = RETAINED_DIR) -> Path:
    check_aggregate(frame, key, observations)
    target = retained_path(directory) / f"{_checked_name(name)}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, float_format="%.6g")
    return target


def write_summary(payload: dict, name: str, directory: Path = RETAINED_DIR) -> Path:
    """A conclusion and its test statistics, never a series."""
    plain = _plain(payload)
    seen = {"numbers": 0, "dates": 0, "text": 0}
    _check_summary(plain, seen)
    if seen["numbers"] > MAX_SUMMARY_NUMBERS or seen["dates"] > MAX_SUMMARY_DATES or seen["text"] > MAX_SUMMARY_ALL_TEXT:
        raise ValueError(f"too many numbers, dates or text for a summary: it may be a series ({seen})")
    text = json.dumps(plain, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False)
    if len(text) > MAX_SUMMARY_BYTES:
        raise ValueError("a summary this long is not a summary")
    target = retained_path(directory) / "analyses" / f"{_checked_name(name)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target
