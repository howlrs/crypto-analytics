"""Tests for prospective end boundaries and liquidation-run manifests."""
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtests import liq_reversion as lr


class LiquidationManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "market.db"
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, open REAL, close REAL)")
        con.execute("CREATE TABLE oi_metrics (venue TEXT, symbol TEXT, ts INTEGER, open_interest REAL)")
        rows = []
        for symbol, timestamps in {
            "BTCUSDT": [lr.TS_FULL_START, lr.TS_FULL_START + 60_000, lr.TS_FULL_START + 120_000],
            "ETHUSDT": [lr.TS_FULL_START, lr.TS_FULL_START + 60_000],
        }.items():
            rows.extend((lr.VENUE, lr.MARKET, symbol, ts, 100.0, 101.0) for ts in timestamps)
        con.executemany("INSERT INTO klines VALUES (?, ?, ?, ?, ?, ?)", rows)
        con.executemany(
            "INSERT INTO oi_metrics VALUES (?, ?, ?, ?)",
            [
                (lr.VENUE, "BTCUSDT", lr.TS_OI_END, 10.0),
                (lr.VENUE, "BTCUSDT", lr.TS_OI_END + 300_000, 11.0),
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_argument_preserves_fixed_end_constant(self):
        args = lr.parse_args([])
        self.assertEqual(args.end_ts, str(lr.TS_FULL_END))
        self.assertEqual(lr.parse_end_ts(args.end_ts), lr.TS_FULL_END)

    def test_latest_uses_minimum_of_symbol_maxima(self):
        expected = lr.TS_FULL_START + 60_000
        self.assertEqual(
            lr.resolve_latest_kline_end(
                db_path=self.db_path,
                symbols=["BTCUSDT", "ETHUSDT"],
                now_ms=lr.TS_FULL_START + 120_000,
            ),
            expected,
        )
        self.assertEqual(lr.parse_end_ts("latest", db_path=self.db_path), expected)

    def test_end_parser_rejects_invalid_and_start_boundary(self):
        with self.assertRaises(ValueError):
            lr.parse_end_ts("not-a-timestamp", db_path=self.db_path)
        with self.assertRaises(ValueError):
            lr.parse_end_ts(lr.TS_FULL_START, db_path=self.db_path)

    def test_kline_loader_honors_inclusive_end_boundary(self):
        end_ts = lr.TS_FULL_START + 60_000
        frame = lr.load_klines("BTCUSDT", end_ts=end_ts, db_path=self.db_path)
        self.assertEqual(frame["ts"].tolist(), [lr.TS_FULL_START, end_ts])

    def test_oi_loader_preserves_default_cap_but_extends_for_future_runs(self):
        legacy = lr.load_oi("BTCUSDT", end_ts=lr.TS_FULL_END, db_path=self.db_path)
        self.assertEqual(legacy["ts"].tolist(), [lr.TS_OI_END])
        frame = lr.load_oi("BTCUSDT", end_ts=lr.TS_OI_END + 300_000, db_path=self.db_path)
        self.assertEqual(frame["ts"].tolist(), [lr.TS_OI_END, lr.TS_OI_END + 300_000])

    def test_manifest_reports_individual_and_common_coverage(self):
        btc = pd.DataFrame({"ts": [1000, 61_000], "open": [1.0, 2.0]})
        eth = pd.DataFrame({"ts": [1000, 61_000], "open": [3.0, 4.0]})
        oi = pd.DataFrame({"ts": [5000], "open_interest": [10.0]})
        manifest = lr.build_run_manifest(
            requested_end="latest",
            analysis_end_ts=61_000,
            kline_frames={"BTCUSDT": btc, "ETHUSDT": eth},
            oi_frames={"BTCUSDT": oi, "ETHUSDT": pd.DataFrame(columns=["ts", "open_interest"])},
            summary_count=324,
            event_file_count=324,
            event_file_sha256={"events_example.csv": "a" * 64},
            script_path=lr.__file__,
        )
        self.assertEqual(manifest["coverage_end_exclusive_ts"], 121_000)
        self.assertEqual(manifest["kline_coverage"]["BTCUSDT"]["coverage_end_exclusive_ts"], 121_000)
        self.assertEqual(manifest["oi_coverage"]["BTCUSDT"]["coverage_end_exclusive_ts"], 305_000)
        self.assertIsNone(manifest["oi_coverage"]["ETHUSDT"]["max_ts"])
        self.assertEqual(manifest["output_counts"], {"summary_rows": 324, "event_csv_files": 324})
        self.assertEqual(manifest["kline_coverage"]["BTCUSDT"]["tail_contiguous_start_ts"], 1000)
        self.assertEqual(manifest["event_file_sha256"]["events_example.csv"], "a" * 64)
        self.assertEqual(len(manifest["script_sha256"]), 64)
        self.assertIn("non-confirmatory", manifest["statistical_warning"])

    def _sealed_registry(self, directory):
        path = Path(directory) / "registry.json"
        registry = {
            "evaluation_start": "2026-10-01T00:00:00Z",
            "followup_end": "2027-10-04T01:00:00Z",
            "integrity": {},
        }
        digest = hashlib.sha256(lr._canonical_registry_bytes(registry)).hexdigest()
        registry["integrity"]["registry_sha256"] = digest
        path.write_bytes(lr._canonical_registry_bytes(registry, include_integrity=True))
        path.with_name("registry.json.sha256").write_text(digest + "\n", encoding="ascii")
        return path

    def test_prospective_seal_allows_historical_and_post_followup_ranges(self):
        path = self._sealed_registry(self.temp_dir.name)
        lr.enforce_prospective_seal(
            1_790_812_799_999, registry_path=path,
            now=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        lr.enforce_prospective_seal(
            1_790_812_800_000, registry_path=path,
            now=datetime(2027, 10, 4, 1, tzinfo=timezone.utc),
        )

    def test_prospective_seal_rejects_active_and_tampered_registries(self):
        missing = Path(self.temp_dir.name) / "missing.json"
        with self.assertRaisesRegex(RuntimeError, "registry or its SHA-256 sidecar is missing"):
            lr.enforce_prospective_seal(
                1, registry_path=missing, now=datetime(2026, 10, 1, tzinfo=timezone.utc)
            )
        path = self._sealed_registry(self.temp_dir.name)
        with self.assertRaisesRegex(RuntimeError, "sealed prospective validation is active"):
            lr.enforce_prospective_seal(
                1_790_812_800_000, registry_path=path,
                now=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
        path.with_name("registry.json.sha256").write_text("0" * 64 + "\n", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            lr.enforce_prospective_seal(
                1, registry_path=path, now=datetime(2026, 10, 1, tzinfo=timezone.utc)
            )


if __name__ == "__main__":
    unittest.main()
