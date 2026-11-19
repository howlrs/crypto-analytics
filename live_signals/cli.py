"""Command-line entry point for offline early-warning diagnostics."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

from . import early_warning
from .io import InputError, load_archive, load_normalized_json, load_reference_json, serializable, sha256_file


def parse_iso8601_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be ISO-8601 with an explicit timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include an explicit timezone")
    return int(parsed.timestamp() * 1000)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate an offline descriptive early-warning diagnostic.")
    parser.add_argument("input", nargs="?", help="normalized JSON containing spot and optional perp series")
    parser.add_argument("--archive", help="single public_snapshots.zip with adjacent sources.json")
    parser.add_argument("--coin", choices=("BTC", "ETH", "HYPE"), help="coin to read from --archive")
    parser.add_argument("--as-of", required=True, type=parse_iso8601_ms, metavar="ISO8601")
    parser.add_argument("--generated-at", type=parse_iso8601_ms, metavar="ISO8601")
    parser.add_argument("--mode", choices=("observed", "market_time_replay"), default="observed")
    parser.add_argument("--reference-json", help="reference levels JSON")
    parser.add_argument("--output", required=True, help="new output JSON path; existing files are never replaced")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if bool(args.input) == bool(args.archive):
        parser.error("provide exactly one of input or --archive")
    if args.archive and not args.coin:
        parser.error("--coin is required with --archive")
    if args.coin and not args.archive:
        parser.error("--coin requires --archive")
    if args.mode == "observed" and args.generated_at is not None:
        parser.error("--generated-at is replay-only; observed diagnostics use the current UTC clock")
    try:
        if args.archive:
            spot, perp, source_sha256 = load_archive(args.archive, args.coin)
            manifest_sha256 = sha256_file(Path(args.archive).with_name("sources.json"))
        else:
            spot, perp = load_normalized_json(args.input)
            source_sha256 = sha256_file(args.input)
            manifest_sha256 = None
        reference = load_reference_json(args.reference_json) if args.reference_json else None
        reference_sha256 = sha256_file(args.reference_json) if args.reference_json else None
        generated_at_ms = args.generated_at if args.generated_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        result = early_warning.evaluate(spot, perp, as_of_ms=args.as_of, generated_at_ms=generated_at_ms,
                                        mode=args.mode, reference=reference)
        output = {"as_of_ms": args.as_of, "generated_at_ms": generated_at_ms, "mode": args.mode,
                  "source_sha256": source_sha256, "manifest_sha256": manifest_sha256,
                  "reference_sha256": reference_sha256, "diagnostic": serializable(result)}
        # Serialize before opening the destination: non-JSON engine output must
        # not leave an empty or partial report behind.
        encoded = json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        with Path(args.output).open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    except (InputError, OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
