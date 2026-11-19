"""Turn an ``orderbook.analyze`` output directory into register values.

Reads ``book_metrics.csv`` (per-snapshot rows) and ``book_summary.csv`` (per-stream quantiles) and prints the
numbers that belong in ``parameters.json``: REST fetch latency by venue and market-order cost by notional.
It measures nothing itself and writes no file. Standard library only.

Latency here is ``received - requested`` of one REST call from the collecting machine; it depends on where the
collector ran. ``exchange_lag_ms`` is ``received - exchange timestamp`` and also contains any clock offset.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

QUANTILES = (("p10", 0.10), ("p50", 0.50), ("p90", 0.90), ("p99", 0.99))
NOTIONALS = ("10k", "100k", "1m")


class InputError(ValueError):
    """The analysis directory is not what ``orderbook.analyze`` writes."""


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an ascending sequence (the numpy default)."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[lower])
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower))


def _finite(text: str | None) -> float | None:
    if text is None or text == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def describe(values: Iterable[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"n": 0}
    return {"n": len(ordered), **{name: round(quantile(ordered, q), 1) for name, q in QUANTILES}}


def _iso(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rows(path: Path, required: Sequence[str]) -> Iterable[dict[str, str]]:
    if not path.is_file():
        raise InputError(f"missing {path.name} in {path.parent}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise InputError(f"{path.name}: missing columns {missing}")
        yield from reader


def summarize(analysis_dir: Path) -> dict[str, Any]:
    """Collect latency by venue and market-order cost by stream from one analysis output."""
    latency: dict[str, list[float]] = defaultdict(list)
    exchange_lag: dict[str, list[float]] = defaultdict(list)
    rows = 0
    for row in _rows(analysis_dir / "book_metrics.csv", ("venue", "latency_ms", "exchange_lag_ms")):
        rows += 1
        value = _finite(row["latency_ms"])
        if value is not None:
            latency[row["venue"]].append(value)
        value = _finite(row["exchange_lag_ms"])
        if value is not None:
            exchange_lag[row["venue"]].append(value)

    impact: dict[str, Any] = {}
    spread: dict[str, Any] = {}
    first_ms: float | None = None
    last_ms: float | None = None
    required = ["stream", "snapshots", "first_ms", "last_ms", "spread_bp_p50", "spread_bp_p90"]
    for row in _rows(analysis_dir / "book_summary.csv", required):
        stream = row["stream"]
        start, end = _finite(row["first_ms"]), _finite(row["last_ms"])
        first_ms = start if first_ms is None or (start is not None and start < first_ms) else first_ms
        last_ms = end if last_ms is None or (end is not None and end > last_ms) else last_ms
        spread[stream] = {"p50": _finite(row["spread_bp_p50"]), "p90": _finite(row["spread_bp_p90"])}
        per_notional = {}
        for notional in NOTIONALS:
            entry = {}
            for side in ("buy", "sell"):
                # A missing quantile means the visible book was too thin at that quantile; it stays null.
                entry[side] = {"p50": _finite(row.get(f"{side}_impact_bp_{notional}_p50")),
                               "p90": _finite(row.get(f"{side}_impact_bp_{notional}_p90")),
                               "known_share": _finite(row.get(f"{side}_impact_known_share_{notional}"))}
            per_notional[notional] = entry
        impact[stream] = {"snapshots": int(float(row["snapshots"])), **per_notional}

    manifest = analysis_dir / "book_manifest.json"
    return {
        "analysis_dir": str(analysis_dir),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None,
        "first": _iso(first_ms),
        "last": _iso(last_ms),
        "snapshot_rows": rows,
        "latency_rest_book_ms": {venue: describe(values) for venue, values in sorted(latency.items())},
        "exchange_lag_ms": {venue: describe(values) for venue, values in sorted(exchange_lag.items())},
        "spread_bp": dict(sorted(spread.items())),
        "impact_bp": dict(sorted(impact.items())),
    }


def _round(value: Any, digits: int = 2) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {key: _round(item, digits) for key, item in value.items()}
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m research_loop.orderbook_params",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("--analysis-dir", type=Path, required=True,
                        help="output directory of python3 -m orderbook.analyze")
    args = parser.parse_args(argv)
    try:
        summary = summarize(args.analysis_dir)
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(_round(summary), ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
