"""Registers for the observe -> parameterize -> hypothesize loop.

Three plain files hold the state that the rest of the repository otherwise keeps in prose:

* ``parameters.json``  - every constant an analysis relies on, with whether it was measured here,
  copied from a venue document, assumed, or is still unknown.
* ``hypotheses.json``  - every strategy idea, who is expected to pay for it, and its verdict.
* ``annotations.jsonl`` - an append-only timeline of interventions (deployments, registrations,
  decisions) so that a later change in an observed series can be read against what was done.

This module only reads and validates those files, appends annotations, and prints a review packet.
It fetches nothing and never edits research outputs. Standard library only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent
PARAMETERS = DATA_DIR / "parameters.json"
HYPOTHESES = DATA_DIR / "hypotheses.json"
ANNOTATIONS = DATA_DIR / "annotations.jsonl"

PARAM_STATUS = ("measured", "documented", "assumed", "unknown")
PARAM_GROUPS = ("cost", "funding", "margin", "liquidation", "latency", "liquidity", "data", "policy", "loop")
HYPOTHESIS_STATUS = ("unverified", "testing", "registered_forward", "adopted_on_hold", "adopted",
                     "rejected", "descriptive_only", "retired")
ANNOTATION_KINDS = ("decision", "deploy", "stop", "registration", "data", "rule_change", "incident",
                    "review", "note")

PARAM_ID = re.compile(r"^[a-z0-9]+(\.[a-z0-9_]+)+$")
HYPOTHESIS_ID = re.compile(r"^H-\d{3}$")
# References that point outside the working tree and are therefore not checked for existence.
EXTERNAL_PREFIXES = ("knowledge:", "output_log:", "tag:", "pr:", "issue:", "http://", "https://", "/")
FUTURE_TOLERANCE = timedelta(minutes=5)
DEFAULT_STALE_DAYS = 90


class RegistryError(ValueError):
    """A register file cannot be read as the documented format."""


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO 8601 UTC timestamp written with a trailing ``Z``."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(_nonempty_str(item) for item in value)


def load_json_list(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{path.name}: {exc}") from exc
    entries = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise RegistryError(f"{path.name}: expected an object with a list under '{key}'")
    return entries


def load_annotations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistryError(f"{path.name}:{number}: {exc}") from exc
        if not isinstance(item, dict):
            raise RegistryError(f"{path.name}:{number}: expected a JSON object")
        entries.append(item)
    return entries


def _reference_errors(label: str, references: Iterable[str], root: Path) -> list[str]:
    errors = []
    for reference in references:
        if reference.startswith(EXTERNAL_PREFIXES):
            continue
        relative = re.split(r"[#\s]", reference, maxsplit=1)[0]
        if not (root / relative).exists():
            errors.append(f"{label}: referenced path does not exist: {relative}")
    return errors


def validate_parameters(entries: Sequence[dict[str, Any]], root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        identifier = entry.get("id")
        label = f"parameter[{index}] {identifier!r}"
        if not isinstance(identifier, str) or not PARAM_ID.fullmatch(identifier):
            errors.append(f"{label}: id must look like 'group.name' in lower case")
        elif identifier in seen:
            errors.append(f"{label}: duplicate id")
        else:
            seen.add(identifier)
        if entry.get("group") not in PARAM_GROUPS:
            errors.append(f"{label}: group must be one of {PARAM_GROUPS}")
        if not _nonempty_str(entry.get("name")):
            errors.append(f"{label}: name is required")
        status = entry.get("status")
        if status not in PARAM_STATUS:
            errors.append(f"{label}: status must be one of {PARAM_STATUS}")
            continue
        if status == "unknown":
            # An unknown value stays unknown: no placeholder number, and a way to measure it.
            if entry.get("value") is not None:
                errors.append(f"{label}: an unknown parameter must not carry a value")
            if not _nonempty_str(entry.get("how_to_measure")):
                errors.append(f"{label}: an unknown parameter needs how_to_measure")
        else:
            if entry.get("value") is None:
                errors.append(f"{label}: value is required unless status is 'unknown'")
            if _parse_date(entry.get("as_of")) is None:
                errors.append(f"{label}: as_of must be YYYY-MM-DD unless status is 'unknown'")
            if not _nonempty_str(entry.get("source")):
                errors.append(f"{label}: source is required unless status is 'unknown'")
        used_by = entry.get("used_by", [])
        if not _str_list(used_by):
            errors.append(f"{label}: used_by must be a list of paths")
        else:
            errors.extend(_reference_errors(label, used_by, root))
    return errors


def validate_hypotheses(entries: Sequence[dict[str, Any]], parameter_ids: set[str],
                        root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        identifier = entry.get("id")
        label = f"hypothesis[{index}] {identifier!r}"
        if not isinstance(identifier, str) or not HYPOTHESIS_ID.fullmatch(identifier):
            errors.append(f"{label}: id must look like 'H-001'")
        elif identifier in seen:
            errors.append(f"{label}: duplicate id")
        else:
            seen.add(identifier)
        for field in ("title", "payer", "verdict", "kill"):
            if not _nonempty_str(entry.get(field)):
                errors.append(f"{label}: {field} is required")
        if entry.get("status") not in HYPOTHESIS_STATUS:
            errors.append(f"{label}: status must be one of {HYPOTHESIS_STATUS}")
        if _parse_date(entry.get("updated")) is None:
            errors.append(f"{label}: updated must be YYYY-MM-DD")
        for field in ("observation", "evidence"):
            if not _str_list(entry.get(field)) or not entry.get(field):
                errors.append(f"{label}: {field} must be a non-empty list of strings")
        parameters = entry.get("parameters", [])
        if not _str_list(parameters):
            errors.append(f"{label}: parameters must be a list of parameter ids")
        else:
            for parameter in parameters:
                if parameter not in parameter_ids:
                    errors.append(f"{label}: unknown parameter id: {parameter}")
        if _str_list(entry.get("evidence")):
            errors.extend(_reference_errors(label, entry["evidence"], root))
    return errors


def validate_annotations(entries: Sequence[dict[str, Any]], known_refs: set[str]) -> list[str]:
    errors: list[str] = []
    for index, entry in enumerate(entries):
        label = f"annotation[{index}]"
        timestamp = parse_ts(entry.get("ts"))
        recorded = parse_ts(entry.get("recorded_at"))
        if timestamp is None:
            errors.append(f"{label}: ts must be ISO 8601 UTC ending in 'Z'")
        if recorded is None:
            errors.append(f"{label}: recorded_at must be ISO 8601 UTC ending in 'Z'")
        if timestamp and recorded and timestamp > recorded + FUTURE_TOLERANCE:
            errors.append(f"{label}: ts is later than recorded_at (an event cannot be logged before it happens)")
        if entry.get("kind") not in ANNOTATION_KINDS:
            errors.append(f"{label}: kind must be one of {ANNOTATION_KINDS}")
        if not _nonempty_str(entry.get("title")):
            errors.append(f"{label}: title is required")
        refs = entry.get("refs", [])
        if not _str_list(refs):
            errors.append(f"{label}: refs must be a list of strings")
        else:
            for ref in refs:
                if (PARAM_ID.fullmatch(ref) or HYPOTHESIS_ID.fullmatch(ref)) and ref not in known_refs:
                    errors.append(f"{label}: unknown reference: {ref}")
    return errors


def load_all(data_dir: Path = DATA_DIR) -> tuple[list[dict], list[dict], list[dict]]:
    return (load_json_list(data_dir / PARAMETERS.name, "parameters"),
            load_json_list(data_dir / HYPOTHESES.name, "hypotheses"),
            load_annotations(data_dir / ANNOTATIONS.name))


def check(data_dir: Path = DATA_DIR, root: Path = ROOT) -> list[str]:
    """Return every problem found in the three register files (empty when consistent)."""
    try:
        parameters, hypotheses, annotations = load_all(data_dir)
    except RegistryError as exc:
        return [str(exc)]
    parameter_ids = {entry["id"] for entry in parameters if isinstance(entry.get("id"), str)}
    hypothesis_ids = {entry["id"] for entry in hypotheses if isinstance(entry.get("id"), str)}
    return (validate_parameters(parameters, root)
            + validate_hypotheses(hypotheses, parameter_ids, root)
            + validate_annotations(annotations, parameter_ids | hypothesis_ids))


def append_annotation(path: Path, *, kind: str, title: str, ts: str | None = None,
                      refs: Sequence[str] = (), note: str | None = None,
                      known_refs: set[str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Append one event. Earlier lines are never rewritten."""
    current = now or datetime.now(timezone.utc)
    entry: dict[str, Any] = {"ts": ts or _format_ts(current), "kind": kind, "title": title,
                             "refs": list(refs)}
    if note:
        entry["note"] = note
    entry["recorded_at"] = _format_ts(current)
    errors = validate_annotations([entry], known_refs if known_refs is not None else set(entry["refs"]))
    if errors:
        raise RegistryError("; ".join(errors))
    existing = path.read_bytes() if path.exists() else b""
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    with path.open("ab") as handle:
        handle.write(separator + json.dumps(entry, ensure_ascii=False, sort_keys=False).encode("utf-8") + b"\n")
    return entry


def build_review(parameters: Sequence[dict], hypotheses: Sequence[dict], annotations: Sequence[dict],
                 *, as_of: date, stale_days: int = DEFAULT_STALE_DAYS) -> dict[str, Any]:
    """Collect what a periodic review has to look at. Pure function of the three registers."""
    stale_before = as_of - timedelta(days=stale_days)
    stale = []
    for entry in parameters:
        recorded = _parse_date(entry.get("as_of"))
        if entry.get("status") in ("measured", "documented") and recorded and recorded < stale_before:
            stale.append({"id": entry["id"], "as_of": entry["as_of"], "status": entry["status"],
                          "age_days": (as_of - recorded).days})
    reviews = [parse_ts(item.get("ts")) for item in annotations if item.get("kind") == "review"]
    reviews = [item for item in reviews if item is not None]
    last_review = max(reviews) if reviews else None
    since = [item for item in annotations
             if last_review is None or (parse_ts(item.get("ts")) or last_review) > last_review]
    open_status = ("unverified", "testing", "registered_forward", "adopted_on_hold")
    return {
        "as_of": as_of.isoformat(),
        "stale_days": stale_days,
        "parameter_counts": dict(Counter(entry.get("status") for entry in parameters)),
        "unknown_parameters": [{"id": entry["id"], "name": entry.get("name"),
                                "how_to_measure": entry.get("how_to_measure")}
                               for entry in parameters if entry.get("status") == "unknown"],
        "assumed_parameters": [{"id": entry["id"], "value": entry.get("value"), "unit": entry.get("unit"),
                                "used_by": entry.get("used_by", [])}
                               for entry in parameters if entry.get("status") == "assumed"],
        "stale_parameters": sorted(stale, key=lambda item: item["as_of"]),
        "hypothesis_counts": dict(Counter(entry.get("status") for entry in hypotheses)),
        "open_hypotheses": [{"id": entry["id"], "status": entry["status"], "title": entry.get("title"),
                             "next_check": entry.get("next_check")}
                            for entry in hypotheses if entry.get("status") in open_status],
        "last_review_ts": _format_ts(last_review) if last_review else None,
        "annotations_since_last_review": sorted(since, key=lambda item: str(item.get("ts"))),
    }


def render_review(review: dict[str, Any]) -> str:
    lines = [f"# Review packet ({review['as_of']})", ""]
    counts = ", ".join(f"{key} {value}" for key, value in sorted(review["parameter_counts"].items()))
    lines += [f"## Parameters ({counts})", ""]
    lines.append("### Unknown: measure these first")
    lines += [f"- {item['id']}: {item['name']} / how: {item['how_to_measure']}"
              for item in review["unknown_parameters"]] or ["- (none)"]
    lines += ["", "### Assumed: replace with a measurement when one exists"]
    lines += [f"- {item['id']} = {_show(item['value'])} {item['unit'] or ''}".rstrip()
              + (f" / used by {', '.join(item['used_by'])}" if item["used_by"] else "")
              for item in review["assumed_parameters"]] or ["- (none)"]
    lines += ["", f"### Older than {review['stale_days']} days: confirm they still hold"]
    lines += [f"- {item['id']} ({item['status']}, as of {item['as_of']}, {item['age_days']} days)"
              for item in review["stale_parameters"]] or ["- (none)"]
    counts = ", ".join(f"{key} {value}" for key, value in sorted(review["hypothesis_counts"].items()))
    lines += ["", f"## Hypotheses ({counts})", "", "### Open"]
    lines += [f"- {item['id']} [{item['status']}] {item['title']}"
              + (f" / next: {item['next_check']}" if item.get("next_check") else "")
              for item in review["open_hypotheses"]] or ["- (none)"]
    lines += ["", f"## Annotations since the last review ({review['last_review_ts'] or 'no review recorded'})", ""]
    lines += [f"- {item.get('ts')} [{item.get('kind')}] {item.get('title')}"
              for item in review["annotations_since_last_review"]] or ["- (none)"]
    return "\n".join(lines) + "\n"


def _show(value: Any) -> str:
    """Render a register value on one line; structured values as JSON rather than Python repr."""
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def _table(rows: Sequence[Sequence[Any]], header: Sequence[str]) -> str:
    cells = [list(map(str, header))] + [[_show(cell) for cell in row] for row in rows]
    return "\n".join("\t".join(row) for row in cells) + "\n"


def _split(value: str | None) -> list[str]:
    return [item for item in (value or "").split(",") if item]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m research_loop.registry", description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="directory holding the three register files")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate the three files and their cross references")
    params = commands.add_parser("params", help="list parameters")
    params.add_argument("--status", help="comma-separated: " + ",".join(PARAM_STATUS))
    params.add_argument("--group", help="comma-separated: " + ",".join(PARAM_GROUPS))
    hypotheses = commands.add_parser("hypotheses", help="list hypotheses")
    hypotheses.add_argument("--status", help="comma-separated: " + ",".join(HYPOTHESIS_STATUS))
    listing = commands.add_parser("annotations", help="list the timeline")
    listing.add_argument("--since", help="YYYY-MM-DD (UTC)")
    annotate = commands.add_parser("annotate", help="append one event to the timeline")
    annotate.add_argument("--kind", required=True, choices=ANNOTATION_KINDS)
    annotate.add_argument("--title", required=True)
    annotate.add_argument("--ts", help="when it happened, ISO 8601 UTC ending in 'Z' (default: now)")
    annotate.add_argument("--refs", help="comma-separated parameter ids, hypothesis ids or paths")
    annotate.add_argument("--note")
    review = commands.add_parser("review", help="print what a periodic review has to look at")
    review.add_argument("--as-of", help="YYYY-MM-DD (default: today, UTC)")
    review.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS)
    review.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir

    try:
        if args.command == "check":
            errors = check(data_dir)
            for error in errors:
                print(error, file=sys.stderr)
            print(f"{len(errors)} problem(s)")
            return 1 if errors else 0
        parameters, hypothesis_entries, annotations = load_all(data_dir)
        if args.command == "params":
            status, group = _split(args.status), _split(args.group)
            rows = [(entry["id"], entry.get("status"), entry.get("value"), entry.get("unit"),
                     entry.get("as_of"), entry.get("name"))
                    for entry in parameters
                    if (not status or entry.get("status") in status) and (not group or entry.get("group") in group)]
            sys.stdout.write(_table(rows, ("id", "status", "value", "unit", "as_of", "name")))
        elif args.command == "hypotheses":
            status = _split(args.status)
            rows = [(entry["id"], entry.get("status"), entry.get("updated"), entry.get("title"))
                    for entry in hypothesis_entries if not status or entry.get("status") in status]
            sys.stdout.write(_table(rows, ("id", "status", "updated", "title")))
        elif args.command == "annotations":
            since = _parse_date(args.since) if args.since else None
            if args.since and since is None:
                raise RegistryError("--since must be YYYY-MM-DD")
            rows = [(entry.get("ts"), entry.get("kind"), entry.get("title"), ",".join(entry.get("refs", [])))
                    for entry in sorted(annotations, key=lambda item: str(item.get("ts")))
                    if since is None or str(entry.get("ts", ""))[:10] >= since.isoformat()]
            sys.stdout.write(_table(rows, ("ts", "kind", "title", "refs")))
        elif args.command == "annotate":
            known = ({entry["id"] for entry in parameters} | {entry["id"] for entry in hypothesis_entries})
            entry = append_annotation(data_dir / ANNOTATIONS.name, kind=args.kind, title=args.title, ts=args.ts,
                                      refs=_split(args.refs), note=args.note, known_refs=known)
            print(json.dumps(entry, ensure_ascii=False))
        elif args.command == "review":
            as_of = _parse_date(args.as_of) if args.as_of else datetime.now(timezone.utc).date()
            if as_of is None:
                raise RegistryError("--as-of must be YYYY-MM-DD")
            packet = build_review(parameters, hypothesis_entries, annotations, as_of=as_of,
                                  stale_days=args.stale_days)
            sys.stdout.write(json.dumps(packet, ensure_ascii=False, indent=2) + "\n" if args.json
                             else render_review(packet))
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
