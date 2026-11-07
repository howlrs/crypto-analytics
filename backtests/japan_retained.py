#!/usr/bin/env python3
"""The store of J-Quants aggregates that are meant to outlive the data.

After a cancellation the licence requires deleting the acquired data and every derivative that can
restore it; aggregates that cannot restore it may stay if they are not published (docs/jquants-standard.md
section 9). data-tools/purge_jquants.sh deletes the whole J-Quants directory, so an aggregate worth keeping
has to live elsewhere: RETAINED_DIR, outside the repository, the purge and the GCS backup.

Everything written here passes check_aggregate. It does not prove a table is harmless, it refuses the
shapes that could carry the data itself: dated rows, repeated entities, statistics over a few observations.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
JQ_DIR = Path("/mnt/e/Datas/market/jquants")
RETAINED_DIR = Path("/mnt/e/Datas/market/japan_retained")
MIN_OBSERVATIONS = 60
MAX_SUMMARY_LIST = 200
MAX_SUMMARY_BYTES = 256_000
DATE_TEXT = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def retained_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    for forbidden, why in ((ROOT, "the public repository"), (JQ_DIR.resolve(), "the directory the purge deletes")):
        if resolved == forbidden or forbidden in resolved.parents:
            raise ValueError(f"retained aggregates cannot live in {why}")
    return resolved


def check_aggregate(frame: pd.DataFrame, key: list[str], observations: str | None = None) -> None:
    """Raise unless the table has one row per entity, no dated cells and enough observations behind each row."""
    if frame.empty:
        raise ValueError("nothing to retain")
    if frame.duplicated(subset=key).any():
        raise ValueError(f"more than one row per {key}: a repeated entity can carry a series")
    for column in frame.columns:
        values = frame[column]
        if pd.api.types.is_datetime64_any_dtype(values):
            raise ValueError(f"{column} is a date column")
        if not pd.api.types.is_numeric_dtype(values) and values.dropna().astype(str).str.match(DATE_TEXT).any():
            raise ValueError(f"{column} holds dates")
    if observations is not None and (frame[observations] < MIN_OBSERVATIONS).any():
        raise ValueError(f"a row rests on fewer than {MIN_OBSERVATIONS} observations")


def _short_lists(value) -> bool:
    if isinstance(value, dict):
        return all(_short_lists(x) for x in value.values())
    if isinstance(value, list):
        return len(value) <= MAX_SUMMARY_LIST and all(_short_lists(x) for x in value)
    return True


def _plain(value):
    if isinstance(value, (np.integer, np.floating, np.bool_)):
        return value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def write_table(frame: pd.DataFrame, name: str, key: list[str], observations: str | None = None,
                directory: Path = RETAINED_DIR) -> Path:
    check_aggregate(frame, key, observations)
    target = retained_path(directory) / f"{name}.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(target, index=False, float_format="%.6g")
    return target


def write_summary(payload: dict, name: str, directory: Path = RETAINED_DIR) -> Path:
    """A conclusion and its test statistics. A long list would be a series, so it is refused."""
    if not _short_lists(payload):
        raise ValueError(f"a list longer than {MAX_SUMMARY_LIST} looks like a series")
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True, default=_plain)
    if len(text) > MAX_SUMMARY_BYTES:
        raise ValueError("a summary this long is not a summary")
    target = retained_path(directory) / "analyses" / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n", encoding="utf-8")
    return target
