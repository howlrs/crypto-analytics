from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from research_loop import registry


def _parameter(**overrides) -> dict:
    entry = {"id": "cost.example_bp", "group": "cost", "name": "example", "value": 5.0, "unit": "bp",
             "status": "assumed", "as_of": "2026-01-01", "source": "synthetic", "used_by": []}
    entry.update(overrides)
    return entry


def _hypothesis(**overrides) -> dict:
    entry = {"id": "H-001", "title": "example", "payer": "someone", "observation": ["a series"],
             "parameters": ["cost.example_bp"], "status": "unverified", "verdict": "none yet",
             "kill": "a stop rule", "evidence": ["knowledge:synthetic"], "updated": "2026-01-01"}
    entry.update(overrides)
    return entry


def _write_registers(directory: Path, parameters: list[dict], hypotheses: list[dict]) -> None:
    (directory / "parameters.json").write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
    (directory / "hypotheses.json").write_text(json.dumps({"hypotheses": hypotheses}), encoding="utf-8")


class RepositoryRegistersTest(unittest.TestCase):
    def test_committed_registers_are_consistent(self) -> None:
        self.assertEqual(registry.check(), [])

    def test_every_unknown_parameter_says_how_to_measure_it(self) -> None:
        parameters = registry.load_json_list(registry.PARAMETERS, "parameters")
        unknown = [entry for entry in parameters if entry["status"] == "unknown"]
        self.assertTrue(unknown, "the register should list what is still unmeasured")
        for entry in unknown:
            self.assertIsNone(entry["value"], entry["id"])
            self.assertTrue(entry["how_to_measure"].strip(), entry["id"])

    def test_every_hypothesis_names_a_payer_and_a_stop_rule(self) -> None:
        for entry in registry.load_json_list(registry.HYPOTHESES, "hypotheses"):
            self.assertTrue(entry["payer"].strip(), entry["id"])
            self.assertTrue(entry["kill"].strip(), entry["id"])


class ParameterValidationTest(unittest.TestCase):
    def test_valid_entry_has_no_errors(self) -> None:
        self.assertEqual(registry.validate_parameters([_parameter()]), [])

    def test_unknown_must_not_carry_a_placeholder_value(self) -> None:
        errors = registry.validate_parameters([_parameter(status="unknown", value=0.0, how_to_measure="measure it")])
        self.assertTrue(any("must not carry a value" in error for error in errors), errors)

    def test_unknown_needs_a_measurement_method_but_no_date(self) -> None:
        entry = _parameter(status="unknown", value=None)
        del entry["as_of"], entry["source"]
        errors = registry.validate_parameters([entry])
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("how_to_measure", errors[0])
        entry["how_to_measure"] = "record exchange and receive timestamps"
        self.assertEqual(registry.validate_parameters([entry]), [])

    def test_known_value_needs_date_source_and_value(self) -> None:
        errors = registry.validate_parameters([_parameter(as_of="2026-13-01", source=" ", value=None)])
        self.assertEqual(len(errors), 3, errors)

    def test_duplicate_ids_bad_status_and_missing_usage_path_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "exists.py").write_text("", encoding="utf-8")
            errors = registry.validate_parameters(
                [_parameter(used_by=["exists.py"]), _parameter(used_by=["missing.py"]),
                 _parameter(id="cost.other", status="guessed")], root)
        self.assertTrue(any("duplicate id" in error for error in errors), errors)
        self.assertTrue(any("missing.py" in error for error in errors), errors)
        self.assertTrue(any("status must be one of" in error for error in errors), errors)
        self.assertFalse(any("exists.py" in error for error in errors), errors)


class HypothesisValidationTest(unittest.TestCase):
    def test_valid_entry_has_no_errors(self) -> None:
        self.assertEqual(registry.validate_hypotheses([_hypothesis()], {"cost.example_bp"}), [])

    def test_missing_payer_unknown_parameter_and_missing_evidence_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            errors = registry.validate_hypotheses(
                [_hypothesis(payer="", parameters=["cost.absent"], evidence=["docs/absent.md"])],
                {"cost.example_bp"}, Path(temp))
        self.assertTrue(any("payer is required" in error for error in errors), errors)
        self.assertTrue(any("unknown parameter id: cost.absent" in error for error in errors), errors)
        self.assertTrue(any("docs/absent.md" in error for error in errors), errors)

    def test_external_references_are_not_checked_on_disk(self) -> None:
        entry = _hypothesis(evidence=["knowledge:abc", "tag:v1", "https://example.invalid/x", "/outside/file"])
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(registry.validate_hypotheses([entry], {"cost.example_bp"}, Path(temp)), [])

    def test_evidence_path_may_carry_a_section_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "doc.md").write_text("", encoding="utf-8")
            entry = _hypothesis(evidence=["doc.md#results", "doc.md section 3"])
            self.assertEqual(registry.validate_hypotheses([entry], {"cost.example_bp"}, root), [])


class AnnotationTest(unittest.TestCase):
    NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

    def test_append_keeps_earlier_lines_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "annotations.jsonl"
            registry.append_annotation(path, kind="deploy", title="first", ts="2026-01-01T00:00:00Z", now=self.NOW)
            before = path.read_bytes()
            entry = registry.append_annotation(path, kind="review", title="second", refs=["H-001"],
                                               known_refs={"H-001"}, now=self.NOW)
            after = path.read_bytes()
            self.assertTrue(after.startswith(before))
            self.assertEqual(entry["ts"], "2026-01-10T12:00:00Z")
            self.assertEqual([item["title"] for item in registry.load_annotations(path)], ["first", "second"])

    def test_an_event_cannot_be_logged_before_it_happens(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "annotations.jsonl"
            with self.assertRaises(registry.RegistryError):
                registry.append_annotation(path, kind="deploy", title="future", ts="2026-01-10T12:06:00Z", now=self.NOW)
            self.assertFalse(path.exists())
            registry.append_annotation(path, kind="deploy", title="clock skew", ts="2026-01-10T12:04:00Z", now=self.NOW)

    def test_unknown_register_reference_and_bad_timestamp_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "annotations.jsonl"
            with self.assertRaises(registry.RegistryError):
                registry.append_annotation(path, kind="note", title="x", refs=["H-999"], known_refs={"H-001"},
                                           now=self.NOW)
            with self.assertRaises(registry.RegistryError):
                registry.append_annotation(path, kind="note", title="x", ts="2026-01-01 00:00", now=self.NOW)
            # A path is a free-form reference and is not resolved against the registers.
            registry.append_annotation(path, kind="note", title="x", refs=["docs/some.md"], known_refs=set(),
                                       now=self.NOW)


class ReviewTest(unittest.TestCase):
    def test_packet_lists_unknown_assumed_stale_and_events_since_the_last_review(self) -> None:
        parameters = [
            _parameter(id="cost.assumed_bp"),
            _parameter(id="latency.unknown_ms", group="latency", status="unknown", value=None,
                       how_to_measure="record timestamps"),
            _parameter(id="margin.old", group="margin", status="documented", as_of="2026-01-01"),
            _parameter(id="margin.fresh", group="margin", status="measured", as_of="2026-05-20"),
            # An assumption does not go stale by age; it is listed until a measurement replaces it.
            _parameter(id="cost.old_assumption_bp", as_of="2020-01-01"),
        ]
        hypotheses = [_hypothesis(), _hypothesis(id="H-002", status="rejected"),
                      _hypothesis(id="H-003", status="registered_forward", next_check="2027-01-01")]
        annotations = [
            {"ts": "2026-03-01T00:00:00Z", "kind": "deploy", "title": "before"},
            {"ts": "2026-04-01T00:00:00Z", "kind": "review", "title": "review"},
            {"ts": "2026-05-01T00:00:00Z", "kind": "stop", "title": "after"},
        ]
        packet = registry.build_review(parameters, hypotheses, annotations, as_of=date(2026, 6, 1), stale_days=90)
        self.assertEqual([item["id"] for item in packet["unknown_parameters"]], ["latency.unknown_ms"])
        self.assertEqual([item["id"] for item in packet["assumed_parameters"]],
                         ["cost.assumed_bp", "cost.old_assumption_bp"])
        self.assertEqual([(item["id"], item["age_days"]) for item in packet["stale_parameters"]], [("margin.old", 151)])
        self.assertEqual([item["id"] for item in packet["open_hypotheses"]], ["H-001", "H-003"])
        self.assertEqual(packet["last_review_ts"], "2026-04-01T00:00:00Z")
        self.assertEqual([item["title"] for item in packet["annotations_since_last_review"]], ["after"])
        text = registry.render_review(packet)
        self.assertIn("latency.unknown_ms", text)
        self.assertIn("H-003 [registered_forward]", text)
        self.assertIn("next: 2027-01-01", text)

    def test_without_a_recorded_review_every_event_is_listed(self) -> None:
        annotations = [{"ts": "2026-03-01T00:00:00Z", "kind": "deploy", "title": "only"}]
        packet = registry.build_review([], [], annotations, as_of=date(2026, 6, 1))
        self.assertIsNone(packet["last_review_ts"])
        self.assertEqual(len(packet["annotations_since_last_review"]), 1)
        self.assertIn("no review recorded", registry.render_review(packet))


class CommandLineTest(unittest.TestCase):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = registry.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_fails_with_a_message_when_a_register_is_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            _write_registers(directory, [_parameter()], [_hypothesis(parameters=["cost.absent"])])
            code, out, err = self._run("--data-dir", temp, "check")
            self.assertEqual(code, 1)
            self.assertIn("unknown parameter id: cost.absent", err)
            self.assertIn("1 problem(s)", out)

    def test_annotate_then_list_and_review_use_the_given_directory_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            _write_registers(directory, [_parameter()], [_hypothesis()])
            repository_before = registry.ANNOTATIONS.read_bytes()
            code, out, _ = self._run("--data-dir", temp, "annotate", "--kind", "deploy", "--title", "started",
                                     "--ts", "2026-01-01T00:00:00Z", "--refs", "H-001,cost.example_bp")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["refs"], ["H-001", "cost.example_bp"])
            code, out, _ = self._run("--data-dir", temp, "annotations", "--since", "2026-01-01")
            self.assertEqual(code, 0)
            self.assertIn("started", out)
            code, out, _ = self._run("--data-dir", temp, "review", "--as-of", "2026-06-01", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["assumed_parameters"][0]["id"], "cost.example_bp")
            code, _, err = self._run("--data-dir", temp, "annotate", "--kind", "deploy", "--title", "bad ref",
                                     "--refs", "H-777")
            self.assertEqual(code, 2)
            self.assertIn("unknown reference: H-777", err)
            self.assertEqual(registry.ANNOTATIONS.read_bytes(), repository_before)

    def test_listing_filters_by_status(self) -> None:
        code, out, _ = self._run("params", "--status", "unknown")
        self.assertEqual(code, 0)
        rows = out.strip().splitlines()[1:]
        self.assertTrue(rows)
        self.assertTrue(all(row.split("\t")[1] == "unknown" for row in rows))
        code, out, _ = self._run("hypotheses", "--status", "rejected")
        self.assertEqual(code, 0)
        self.assertTrue(all(row.split("\t")[1] == "rejected" for row in out.strip().splitlines()[1:]))


if __name__ == "__main__":
    unittest.main()
