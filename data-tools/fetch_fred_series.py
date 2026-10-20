#!/usr/bin/env python3
"""Download FRED daily series as `observation_date,<ID>` CSV under /mnt/e/Datas/market/indices/.

Same layout as the existing fred_*.csv files. An existing file is updated when the download only
appends rows; any other difference (a revision) needs --force. Blank or '.' values (market
holidays) are kept as blanks.
FRED's edge stalls urllib clients (2026-10-20), so the download uses the system curl binary.
"""
import argparse
import csv
import io
import math
import os
import re
import subprocess
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import log, sha256_hex

BASE = "/mnt/e/Datas/market/indices"
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def validate(series, body: bytes) -> int:
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    if not rows or rows[0] != ["observation_date", series]:
        raise ValueError(f"{series}: unexpected header {rows[:1]}")
    previous = None
    for day, value in rows[1:]:
        current = date.fromisoformat(day)
        if previous is not None and current <= previous:
            raise ValueError(f"{series}: dates are not strictly increasing at {day}")
        if value not in ("", ".") and not math.isfinite(float(value)):
            raise ValueError(f"{series}: non-finite value at {day}")
        previous = current
    if len(rows) < 2:
        raise ValueError(f"{series}: no observations")
    return len(rows) - 1


def extends(old: bytes, new: bytes) -> bool:
    """True when `new` keeps every existing line unchanged and only appends."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    return len(new_lines) >= len(old_lines) and new_lines[:len(old_lines)] == old_lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", required=True, help="comma-separated FRED IDs, e.g. VIXCLS")
    parser.add_argument("--out-dir", default=BASE)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    out_dir = os.path.realpath(args.out_dir)
    if os.path.commonpath([out_dir, REPO]) == REPO:  # series such as VIXCLS are third-party copyrighted
        raise SystemExit("--out-dir must be outside the public repository")
    failed = []
    for series in args.series.split(","):
        try:
            fetch(series, out_dir, args.force)
        except (ValueError, OSError) as exc:
            log(f"{series}: {exc}")
            failed.append(series)
    if failed:
        raise SystemExit(f"failed: {','.join(failed)}")


def fetch(series, out_dir, force):
    if not re.fullmatch(r"[A-Z0-9_]+", series):
        raise ValueError(f"invalid FRED series id: {series!r}")
    result = subprocess.run(["curl", "--fail", "-sS", "-m", "60", "--retry", "2", URL.format(series)],
                            capture_output=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"curl failed: {result.stderr.decode(errors='replace').strip()}")
    body = result.stdout
    count = validate(series, body)
    path = os.path.join(out_dir, f"fred_{series}.csv")
    if os.path.exists(path) and not force:
        with open(path, "rb") as handle:
            old = handle.read()
        if old == body:
            log(f"{series}: unchanged ({count} rows)")
            return
        if not extends(old, body):
            raise ValueError(f"{path} would change existing rows; rerun with --force")
    os.makedirs(out_dir, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    log(f"{series}: {count} rows -> {path} sha256={sha256_hex(body)}")


if __name__ == "__main__":
    main()
