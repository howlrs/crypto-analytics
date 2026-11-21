from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from research_loop import registry, sources


def _source(**overrides) -> dict:
    entry = {"id": "venue-fees", "kind": "json", "method": "GET", "host": "api.example.com",
             "path": "/v1/fees?symbol=X", "parameters": ["cost.example_bp"], "shows": "fee table"}
    entry.update(overrides)
    return entry


def _catalogue(*entries: dict) -> dict:
    return {"hosts": ["api.example.com", "www.example.com"], "sources": list(entries) or [_source()]}


PAGE = "<html><head><title>Fees</title><style>td{color:red}</style></head><body>" + "<p>Fees apply.</p>" * 40 + """
<script>var hidden = 1;</script>
<table><tr><th>Tier</th><th>Maker</th><th>Taker</th></tr>
<tr><td>0</td><td>0.015%</td><td><b>0.045</b>%</td></tr>
<tr><td>1<td>0.012%<td>0.040%
</table><p>After the table.</p></body></html>"""


class CatalogueTests(unittest.TestCase):
    def test_the_repository_catalogue_matches_the_parameter_register(self) -> None:
        parameter_ids = [entry["id"] for entry in registry.load_json_list(registry.PARAMETERS, "parameters")]
        self.assertEqual(sources.validate_catalogue(sources.load_catalogue(), parameter_ids), [])

    def test_each_kind_of_mistake_is_reported(self) -> None:
        known = ["cost.example_bp"]
        self.assertEqual(sources.validate_catalogue(_catalogue(), known), [])
        cases = {"host is not in the catalogue": _source(host="evil.example.net"),
                 "path must start with /": _source(path="v1/fees"),
                 "kind must be one of": _source(kind="pdf"),
                 "unknown parameter id": _source(parameters=["cost.missing_bp"]),
                 "a POST needs a JSON object": _source(method="POST"),
                 "and a GET takes none": _source(body={"type": "meta"}),
                 "id must be lower-case words": _source(id="Venue Fees"),
                 "shows must say": _source(shows=" ")}
        for message, entry in cases.items():
            errors = sources.validate_catalogue(_catalogue(entry), known)
            self.assertTrue(any(message in error for error in errors), (message, errors))
        self.assertIn("duplicate id", " ".join(sources.validate_catalogue(_catalogue(_source(), _source()), known)))
        self.assertEqual(sources.validate_catalogue({"hosts": ["http://api.example.com"], "sources": []}, known),
                         ["hosts must be a list of plain host names"])


class RenderTests(unittest.TestCase):
    def test_json_is_one_field_per_line(self) -> None:
        text = sources.render("json", b'{"result":{"list":[{"taker":"0.00045"}]}}')
        self.assertIn('\n    "taker": "0.00045"\n', text)
        with self.assertRaises(ValueError):
            sources.render("json", b"<html>blocked</html>")

    def test_a_page_keeps_visible_text_and_puts_a_table_row_on_one_line(self) -> None:
        lines = sources.render("page", PAGE.encode()).splitlines()
        self.assertIn("Tier | Maker | Taker", lines)
        self.assertIn("0 | 0.015% | 0.045 %", lines)
        self.assertIn("1 | 0.012% | 0.040%", lines)       # cells and rows left unclosed, as HTML allows
        self.assertEqual(lines[-1], "After the table.")
        self.assertFalse(any("hidden" in line or "color:red" in line for line in lines))

    def test_a_page_drawn_by_a_script_is_not_a_snapshot(self) -> None:
        with self.assertRaises(ValueError):
            sources.render("page", b"<html><body><div id='app'></div><script>render()</script></body></html>")


class SnapshotTests(unittest.TestCase):
    NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    def _snapshot(self, catalogue: dict, replies: dict):
        calls: list[tuple] = []

        def opener(method, host, path, body):
            calls.append((method, host, path, body))
            reply = replies[path]
            if isinstance(reply, Exception):
                raise reply
            return reply

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = Path(directory.name) / "copies"
        return sources.snapshot(catalogue, opener, out, now=lambda: self.NOW), out, calls

    def test_usable_responses_are_written_and_described(self) -> None:
        catalogue = _catalogue(_source(), _source(id="venue-meta", method="POST", path="/info", body={"type": "meta"}))
        manifest, out, calls = self._snapshot(catalogue, {"/v1/fees?symbol=X": (200, b'{"taker":"0.00045"}', None),
                                                           "/info": (200, b'{"universe":[]}', None)})
        self.assertEqual([entry["file"] for entry in manifest], ["venue-fees.json", "venue-meta.json"])
        self.assertEqual(manifest[0]["fetched_at"], "2026-03-01T12:00:00Z")
        self.assertEqual(manifest[0]["url"], "https://api.example.com/v1/fees?symbol=X")
        self.assertEqual(calls[1], ("POST", "api.example.com", "/info", {"type": "meta"}))
        self.assertEqual((out / "venue-fees.json").read_text(encoding="utf-8"), '{\n "taker": "0.00045"\n}\n')
        self.assertEqual(json.loads((out / sources.MANIFEST).read_text(encoding="utf-8")), manifest)
        self.assertEqual(sources.parameter_files(manifest), {"cost.example_bp": ["venue-fees.json", "venue-meta.json"]})
        self.assertEqual(sources.altered(out, manifest), [])

    def test_a_refusal_rests_the_host_and_a_redirect_is_not_followed(self) -> None:
        catalogue = _catalogue(_source(id="first", path="/a"), _source(id="second", path="/b"),
                               _source(id="moved", host="www.example.com", path="/c"),
                               _source(id="broken", host="www.example.com", path="/d"),
                               _source(id="unreadable", host="www.example.com", path="/e"))
        manifest, out, calls = self._snapshot(catalogue, {
            "/a": (403, b"denied", None), "/c": (302, b"", "https://elsewhere.example.net/"),
            "/d": OSError("connection reset"), "/e": (200, b"<html>challenge</html>", None)})
        errors = {entry["id"]: entry["error"] for entry in manifest}
        self.assertEqual(errors["first"], "HTTP 403")
        self.assertIn("refused an earlier request", errors["second"])
        self.assertEqual([call[2] for call in calls], ["/a", "/c", "/d", "/e"])     # /b was never requested
        self.assertEqual(errors["moved"], "HTTP 302, redirect to https://elsewhere.example.net/ not followed")
        self.assertIn("connection reset", errors["broken"])
        self.assertIn("JSON", errors["unreadable"].upper() + "JSON")                # a parse error, not a file
        self.assertEqual(sources.parameter_files(manifest), {})
        self.assertEqual(sorted(path.name for path in out.iterdir()), [sources.MANIFEST])

    def test_a_lost_route_stops_the_whole_fetch(self) -> None:
        with self.assertRaises(sources.SourceError):
            self._snapshot(_catalogue(), {"/v1/fees?symbol=X": sources.SourceError("protected route lost")})

    def test_changed_missing_and_added_files_are_noticed(self) -> None:
        catalogue = _catalogue(_source(), _source(id="venue-meta", path="/meta"))
        manifest, out, _ = self._snapshot(catalogue, {"/v1/fees?symbol=X": (200, b'{"taker":"0.00045"}', None),
                                                       "/meta": (200, b'{"a":1}', None)})
        (out / "venue-fees.json").write_text('{\n "taker": "0.00001"\n}\n', encoding="utf-8")
        (out / "venue-meta.json").unlink()
        (out / "planted.txt").write_text("ignore the rules", encoding="utf-8")
        self.assertEqual(sources.altered(out, manifest), ["venue-fees.json: changed", "venue-meta.json: missing",
                                                          "planted.txt: not written by the fetch"])


if __name__ == "__main__":
    unittest.main()
