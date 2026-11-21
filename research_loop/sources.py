"""Copies of public venue endpoints and pages, for the periodic review to check the registers against.

The review agents have no network. This module fetches a fixed list (``sources.json``) and writes each
response as a text file that an agent can cite by line: JSON re-indented to one field per line, a page
reduced to its visible text.

* Only the hosts named in the catalogue are contacted, over HTTPS and IPv4, through the
  interface-bound transport of ``orderbook.collect`` (the same one the order-book collector uses).
* A redirect is not followed, and a host that answers 403, 418 or 429 is left alone for the rest of the
  run. Either is recorded as a failed source; no file is written for it.
* Requests carry no account, key or cookie. Nothing here reads or writes the registers.

Standard library only.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Sequence

CATALOGUE = Path(__file__).resolve().parent / "sources.json"
MANIFEST = "manifest.json"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64)"   # generic on purpose: the request says nothing about its origin
MAX_BYTES = 4_000_000
MIN_PAGE_CHARS = 500          # less visible text than this means the page is drawn by a script
BACK_OFF_STATUS = (403, 418, 429)
SOURCE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
KINDS = {"json": ".json", "page": ".txt"}

Opener = Callable[[str, str, str, dict[str, Any] | None], tuple[int, bytes, str | None]]


class SourceError(RuntimeError):
    """The catalogue is not usable, or the protected route was lost."""


def load_catalogue(path: Path = CATALOGUE) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceError(f"{path}: the catalogue must be an object")
    return value


def validate_catalogue(catalogue: dict[str, Any], parameter_ids: Iterable[str]) -> list[str]:
    """Every problem in the catalogue (empty when usable)."""
    errors: list[str] = []
    hosts = catalogue.get("hosts")
    if not isinstance(hosts, list) or not all(isinstance(host, str) and HOST.fullmatch(host) for host in hosts):
        return ["hosts must be a list of plain host names"]
    sources = catalogue.get("sources")
    if not isinstance(sources, list) or not sources:
        return ["sources must be a non-empty list"]
    known = set(parameter_ids)
    seen: set[str] = set()
    for index, source in enumerate(sources):
        identifier = source.get("id") if isinstance(source, dict) else None
        label = f"source[{index}] {identifier!r}"
        if not isinstance(source, dict):
            errors.append(f"{label}: must be an object")
            continue
        if not isinstance(identifier, str) or not SOURCE_ID.fullmatch(identifier):
            errors.append(f"{label}: id must be lower-case words joined by hyphens")
        elif identifier in seen:
            errors.append(f"{label}: duplicate id")
        else:
            seen.add(identifier)
        if source.get("kind") not in KINDS:
            errors.append(f"{label}: kind must be one of {sorted(KINDS)}")
        if source.get("host") not in hosts:
            errors.append(f"{label}: host is not in the catalogue's hosts")
        path = source.get("path")
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"\s", path):
            errors.append(f"{label}: path must start with / and contain no white space")
        method, body = source.get("method"), source.get("body")
        if method not in ("GET", "POST"):
            errors.append(f"{label}: method must be GET or POST")
        if (method == "POST") != isinstance(body, dict):
            errors.append(f"{label}: a POST needs a JSON object as body, and a GET takes none")
        parameters = source.get("parameters")
        if not isinstance(parameters, list) or not parameters:
            errors.append(f"{label}: parameters must name at least one register id")
        else:
            errors += [f"{label}: unknown parameter id {item!r}" for item in parameters if item not in known]
        if not isinstance(source.get("shows"), str) or not source["shows"].strip():
            errors.append(f"{label}: shows must say what the response contains")
    return errors


class _VisibleText(HTMLParser):
    """Text a reader would see, one block per line; the cells of a table row share a line."""

    SKIPPED = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skipping = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _close_cell(self) -> None:
        if self._row is not None and self._cell is not None:
            self._row.append(" ".join(self._cell))
        self._cell = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIPPED:
            self._skipping += 1
        elif tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._close_cell()
            self._cell = []

    def _close_row(self) -> None:
        self._close_cell()
        if self._row and any(self._row):
            self.lines.append(" | ".join(self._row))
        self._row = None

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "table"):
            self._close_row()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if self._skipping or not text:
            return
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.lines.append(text)


def page_text(markup: str) -> str:
    parser = _VisibleText()
    parser.feed(markup)
    parser.close()
    parser._close_row()
    return "\n".join(parser.lines) + "\n"


def render(kind: str, raw: bytes) -> str:
    """The response as the text file an agent reads. Raises ValueError when it is not usable."""
    text = raw.decode("utf-8", "replace")
    if kind == "json":
        return json.dumps(json.loads(text), ensure_ascii=False, indent=1) + "\n"
    rendered = page_text(text)
    if len(rendered) < MIN_PAGE_CHARS:
        raise ValueError(f"the page has {len(rendered)} characters of visible text; it is probably drawn by a script")
    return rendered


def pinned_opener(transport: Any, timeout: float = 25.0) -> Opener:
    """Requests over ``orderbook.collect.Transport``: resolved and routed through its interface only."""
    from orderbook import collect  # the collector's transport; imported here so the checks above need no socket code

    def open_(method: str, host: str, path: str, body: dict[str, Any] | None) -> tuple[int, bytes, str | None]:
        try:
            address = transport.resolve(host)
            connection = collect._PinnedHTTPSConnection(host, address, make_socket=transport.make_socket,
                                                        timeout=timeout, context=transport.context)
        except collect.TransportError as exc:
            raise SourceError(f"protected route unavailable for {host}: {exc}") from exc
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9",
                       "Accept-Language": "en"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(MAX_BYTES + 1), response.getheader("Location")
        except OSError as exc:
            if transport.check_path():
                raise SourceError(f"protected route lost during the request to {host}") from exc
            raise
        finally:
            connection.close()

    return open_


def open_transport(source_interface: str | None, dns_server: str | None, allow_default_route: bool = False) -> Any:
    """The collector's transport, bound to ``source_interface`` unless the default route is allowed explicitly."""
    from orderbook import collect

    try:
        return collect.Transport(source_interface=source_interface, dns_server=dns_server,
                                 allow_default_route=allow_default_route,
                                 system_dns=allow_default_route and not dns_server, timeout=25.0)
    except (ValueError, collect.TransportError) as exc:
        raise SourceError(f"protected route unavailable: {exc}") from exc


def snapshot(catalogue: dict[str, Any], opener: Opener, directory: Path,
             now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> list[dict[str, Any]]:
    """Fetch every source once, write the usable ones under ``directory``, and return the manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    resting: set[str] = set()
    for source in catalogue["sources"]:
        entry: dict[str, Any] = {"id": source["id"], "url": f"https://{source['host']}{source['path']}",
                                 "method": source["method"], "parameters": list(source["parameters"]),
                                 "shows": source["shows"]}
        if "body" in source:
            entry["body"] = source["body"]
        manifest.append(entry)
        if source["host"] in resting:
            entry["error"] = "skipped: the host refused an earlier request in this run"
            continue
        entry["fetched_at"] = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            status, raw, location = opener(source["method"], source["host"], source["path"], source.get("body"))
        except SourceError:
            raise
        except (OSError, RuntimeError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            continue
        entry["status"] = status
        if status in BACK_OFF_STATUS:
            resting.add(source["host"])
        if status != 200:
            entry["error"] = f"HTTP {status}" + (f", redirect to {location} not followed" if location else "")
            continue
        if len(raw) > MAX_BYTES:
            entry["error"] = f"response exceeds {MAX_BYTES} bytes"
            continue
        try:
            text = render(source["kind"], raw)
        except ValueError as exc:
            entry["error"] = str(exc)
            continue
        name = source["id"] + KINDS[source["kind"]]
        (directory / name).write_text(text, encoding="utf-8")
        entry.update(file=name, lines=text.count("\n"), sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    (directory / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


def parameter_files(manifest: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """For each register id, the snapshot files that were actually written for it."""
    files: dict[str, list[str]] = {}
    for entry in manifest:
        if entry.get("file"):
            for identifier in entry["parameters"]:
                files.setdefault(identifier, []).append(entry["file"])
    return files


def altered(directory: Path, manifest: Sequence[dict[str, Any]]) -> list[str]:
    """Snapshot files that are missing, changed since they were written, or were not written by the fetch."""
    expected = {entry["file"]: entry["sha256"] for entry in manifest if entry.get("file")}
    problems: list[str] = []
    for name, digest in sorted(expected.items()):
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name}: changed")
    present = {path.name for path in directory.iterdir()} if directory.is_dir() else set()
    problems += [f"{name}: not written by the fetch" for name in sorted(present - set(expected) - {MANIFEST})]
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    from research_loop import registry

    parser = argparse.ArgumentParser(prog="python3 -m research_loop.sources", description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="directory to write the copies into")
    parser.add_argument("--source-interface", help="bind every request to this interface (the VPN)")
    parser.add_argument("--dns-server", help="resolver reached through that interface")
    parser.add_argument("--allow-default-route", action="store_true", help="fetch without binding to an interface")
    args = parser.parse_args(argv)
    try:
        catalogue = load_catalogue()
        errors = validate_catalogue(catalogue, (entry["id"] for entry in
                                                registry.load_json_list(registry.PARAMETERS, "parameters")))
        if errors:
            raise SourceError("; ".join(errors[:5]))
        transport = open_transport(args.source_interface, args.dns_server, args.allow_default_route)
        manifest = snapshot(catalogue, pinned_opener(transport), args.out)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for entry in manifest:
        print(f"{entry['id']}: " + (f"{entry['file']} ({entry['lines']} lines)" if entry.get("file")
                                    else f"not written: {entry.get('error')}"))
    return 0 if all(entry.get("file") for entry in manifest) else 1


if __name__ == "__main__":
    raise SystemExit(main())
