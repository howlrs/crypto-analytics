#!/usr/bin/env python3
"""The store of J-Quants aggregates that are meant to outlive the data.

After a cancellation the licence requires deleting the acquired data and every derivative that can
restore it; aggregates that cannot restore it may stay if they are not published (docs/jquants-standard.md
section 9). data-tools/purge_jquants.sh deletes the whole J-Quants directory, so an aggregate worth keeping
has to live elsewhere: RETAINED_DIR, outside the repository, the purge and the GCS backup.

Everything written here passes a shape check. It does not prove a file is harmless, it refuses the shapes
that could carry the data itself. A table has one row per entity, a bounded width, no date in a cell or a
column name, and a minimum of observations behind every row. A summary holds a conclusion and its test
statistics: it may name the bounds of a period, but it has no long or date-keyed container and no long text.
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
MAX_SUMMARY_TEXT = 500
MAX_SUMMARY_BYTES = 256_000
DATE_TEXT = re.compile(r"^(19|20)\d{2}[-/.]?\d{2}([-/.]?\d{2})?(\b|T)")
DATE_NUMBER = (19_000_101, 21_001_231)
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
    counts = pd.to_numeric(frame[observations], errors="coerce")
    if counts.isna().any() or (counts < MIN_OBSERVATIONS).any():
        raise ValueError(f"a row rests on fewer than {MIN_OBSERVATIONS} observations")
    for column in frame.columns:
        values = frame[column].dropna()
        if DATE_TEXT.match(str(column)):
            raise ValueError(f"{column} is a date used as a column name")
        if pd.api.types.is_datetime64_any_dtype(values):
            raise ValueError(f"{column} is a date column")
        if pd.api.types.is_numeric_dtype(values):
            whole = values.astype(float) == np.floor(values.astype(float))
            if len(values) and whole.all() and values.between(*DATE_NUMBER).all():
                raise ValueError(f"{column} holds dates written as numbers")
            continue
        text = values.astype(str)
        if text.str.match(DATE_TEXT).any():
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


def _check_summary(value) -> int:
    """Raise on anything shaped like a series; return how many numbers the value holds."""
    if isinstance(value, dict):
        if len(value) > MAX_SUMMARY_ITEMS or any(DATE_TEXT.match(k) for k in value):
            raise ValueError("a long or date-keyed mapping looks like a series")
        return sum(_check_summary(v) for v in value.values())
    if isinstance(value, list):
        if len(value) > MAX_SUMMARY_ITEMS:
            raise ValueError(f"a list longer than {MAX_SUMMARY_ITEMS} looks like a series")
        return sum(_check_summary(v) for v in value)
    if isinstance(value, str) and len(value) > MAX_SUMMARY_TEXT:
        raise ValueError("long text can carry a series")
    return int(isinstance(value, (int, float)) and not isinstance(value, bool))


def write_table(frame: pd.DataFrame, name: str, key: str, observations: str, directory: Path = RETAINED_DIR) -> Path:
    check_aggregate(frame, key, observations)
    target = retained_path(directory) / f"{_checked_name(name)}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, float_format="%.6g")
    return target


def write_summary(payload: dict, name: str, directory: Path = RETAINED_DIR) -> Path:
    """A conclusion and its test statistics, never a series."""
    plain = _plain(payload)
    if _check_summary(plain) > MAX_SUMMARY_NUMBERS:
        raise ValueError("too many numbers for a summary: it may be a series")
    text = json.dumps(plain, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False)
    if len(text) > MAX_SUMMARY_BYTES:
        raise ValueError("a summary this long is not a summary")
    target = retained_path(directory) / "analyses" / f"{_checked_name(name)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target
