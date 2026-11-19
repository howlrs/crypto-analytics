from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from research_loop import orderbook_params

SUMMARY_COLUMNS = ["stream", "snapshots", "first_ms", "last_ms", "spread_bp_p50", "spread_bp_p90"] + [
    f"{side}_impact_{kind}_{notional}{suffix}"
    for notional in ("10k", "100k", "1m") for side in ("buy", "sell")
    for kind, suffix in (("bp", "_p50"), ("bp", "_p90"), ("known_share", ""))]


def _write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _analysis(directory: Path) -> None:
    metrics = [{"venue": "alpha", "latency_ms": str(value), "exchange_lag_ms": str(value + 5)}
               for value in range(1, 102)]          # 1..101 -> p50 51, p90 91, p10 11, p99 100
    metrics += [{"venue": "beta", "latency_ms": "200", "exchange_lag_ms": ""},
                {"venue": "beta", "latency_ms": "", "exchange_lag_ms": "nan"}]
    _write(directory / "book_metrics.csv", ["venue", "latency_ms", "exchange_lag_ms"], metrics)
    row = {name: "" for name in SUMMARY_COLUMNS}
    row.update({"stream": "alpha:perp:X", "snapshots": "101", "first_ms": "1700000000000",
                "last_ms": "1700003600000", "spread_bp_p50": "0.5", "spread_bp_p90": "1.25",
                "buy_impact_bp_10k_p50": "0.4", "buy_impact_bp_10k_p90": "0.9", "buy_impact_known_share_10k": "1.0",
                "sell_impact_bp_10k_p50": "0.45", "sell_impact_bp_10k_p90": "1.1", "sell_impact_known_share_10k": "1.0",
                # At 1m the p90 is unknown: the visible book was too thin there, and it must stay unknown.
                "buy_impact_bp_1m_p50": "6.0", "buy_impact_bp_1m_p90": "", "buy_impact_known_share_1m": "0.7"})
    _write(directory / "book_summary.csv", SUMMARY_COLUMNS, [row])
    (directory / "book_manifest.json").write_text("{}", encoding="utf-8")


class QuantileTest(unittest.TestCase):
    def test_linear_interpolation_matches_hand_values(self) -> None:
        self.assertEqual(orderbook_params.quantile([10.0], 0.9), 10.0)
        self.assertEqual(orderbook_params.quantile([0.0, 10.0], 0.5), 5.0)
        self.assertAlmostEqual(orderbook_params.quantile([1.0, 2.0, 3.0, 4.0], 0.9), 3.7)
        with self.assertRaises(ValueError):
            orderbook_params.quantile([], 0.5)


class SummarizeTest(unittest.TestCase):
    def test_latency_by_venue_and_cost_by_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            _analysis(directory)
            summary = orderbook_params.summarize(directory)
        self.assertEqual(summary["snapshot_rows"], 103)
        self.assertEqual(summary["latency_rest_book_ms"]["alpha"],
                         {"n": 101, "p10": 11.0, "p50": 51.0, "p90": 91.0, "p99": 100.0})
        # Blank and non-finite cells are not measurements; they are dropped, not counted as zero.
        self.assertEqual(summary["latency_rest_book_ms"]["beta"]["n"], 1)
        self.assertEqual(summary["exchange_lag_ms"]["alpha"]["p50"], 56.0)
        self.assertNotIn("beta", summary["exchange_lag_ms"])
        self.assertEqual(summary["first"], "2023-11-14T22:13:20Z")
        self.assertEqual(summary["last"], "2023-11-14T23:13:20Z")
        cost = summary["impact_bp"]["alpha:perp:X"]
        self.assertEqual(cost["snapshots"], 101)
        self.assertEqual(cost["10k"]["buy"], {"p50": 0.4, "p90": 0.9, "known_share": 1.0})
        self.assertEqual(cost["1m"]["buy"], {"p50": 6.0, "p90": None, "known_share": 0.7})
        self.assertEqual(cost["100k"]["sell"], {"p50": None, "p90": None, "known_share": None})
        self.assertEqual(summary["spread_bp"]["alpha:perp:X"], {"p50": 0.5, "p90": 1.25})
        self.assertEqual(len(summary["manifest_sha256"]), 64)

    def test_missing_file_or_column_is_an_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaises(orderbook_params.InputError):
                orderbook_params.summarize(directory)
            _write(directory / "book_metrics.csv", ["venue", "latency_ms"], [])
            with self.assertRaises(orderbook_params.InputError):
                orderbook_params.summarize(directory)

    def test_command_line_prints_json_and_reports_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            _analysis(directory)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(orderbook_params.main(["--analysis-dir", temp]), 0)
            self.assertEqual(json.loads(out.getvalue())["latency_rest_book_ms"]["alpha"]["p50"], 51.0)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(orderbook_params.main(["--analysis-dir", "/nonexistent-analysis-dir"]), 2)
        self.assertIn("missing book_metrics.csv", err.getvalue())


if __name__ == "__main__":
    unittest.main()
