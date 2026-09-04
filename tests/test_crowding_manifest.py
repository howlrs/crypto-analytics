import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtests import crowding_signals as cs
from backtests.crowding_signals import (
    MARKET,
    SYMBOLS,
    TS_FULL_END,
    VENUE,
    build_run_manifest,
    frame_coverage,
    parse_end_ts,
    resolve_analysis_end_ts,
    resolve_latest_end_ts,
)


class CrowdingManifestTests(unittest.TestCase):
    def test_parse_end_ts_accepts_integer_and_latest(self) -> None:
        self.assertEqual(parse_end_ts(str(TS_FULL_END)), TS_FULL_END)
        self.assertEqual(parse_end_ts("latest"), "latest")
        with self.assertRaises(Exception):
            parse_end_ts("not-a-timestamp")

    def test_latest_resolution_uses_the_lower_symbol_maximum(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER)")
        conn.executemany(
            "INSERT INTO klines VALUES (?, ?, ?, ?)",
            [
                (VENUE, MARKET, SYMBOLS[0], 100),
                (VENUE, MARKET, SYMBOLS[0], 300),
                (VENUE, MARKET, SYMBOLS[1], 200),
                (VENUE, MARKET, SYMBOLS[1], 250),
            ],
        )
        self.assertEqual(resolve_analysis_end_ts(conn, "latest"), 250)
        self.assertEqual(resolve_analysis_end_ts(conn, 123), 123)

    def test_latest_resolution_excludes_the_open_minute(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER)")
        base = TS_FULL_END
        rows = [
            (VENUE, MARKET, symbol, timestamp)
            for symbol in SYMBOLS
            for timestamp in (base, base + 60_000, base + 120_000)
        ]
        conn.executemany("INSERT INTO klines VALUES (?, ?, ?, ?)", rows)
        self.assertEqual(resolve_latest_end_ts(conn, now_ms=base + 120_000), base + 60_000)

    def test_manifest_has_default_and_per_source_coverage(self) -> None:
        kline_coverage = frame_coverage(pd.DataFrame({"ts": [1_000, 2_000]}), 60_000)
        source_coverage = {
            symbol: {
                "kline": kline_coverage,
                "funding": {"min_ts": 500, "max_ts": 1_000, "count": 2, "coverage_end_exclusive_ts": 28_800_000},
                "oi": {"min_ts": 600, "max_ts": 900, "count": 2, "coverage_end_exclusive_ts": 300_900},
            }
            for symbol in SYMBOLS
        }
        kline_data = {symbol: {"coverage": kline_coverage} for symbol in SYMBOLS}
        manifest = build_run_manifest(
            requested_end=TS_FULL_END,
            analysis_end_ts=TS_FULL_END,
            kline_data=kline_data,
            source_coverage=source_coverage,
            source_event_file_count=7,
            event_file_sha256={"A2_contrarian_events_BTCUSDT_24h.csv": "b" * 64},
            generated_at_utc="2026-09-04T00:00:00Z",
        )
        self.assertEqual(manifest["requested_end"], TS_FULL_END)
        self.assertEqual(manifest["analysis_end_ts"], TS_FULL_END)
        self.assertEqual(manifest["coverage_end_exclusive_ts"], 62_000)
        self.assertEqual(manifest["sources"][SYMBOLS[0]]["kline"]["coverage_end_exclusive_ts"], 62_000)
        self.assertEqual(manifest["source_event_file_count"], 7)
        self.assertEqual(manifest["event_file_sha256"]["A2_contrarian_events_BTCUSDT_24h.csv"], "b" * 64)
        self.assertIn("non-confirmatory", manifest["statistical_warning"])

    def _sealed_registry(self, directory):
        path = Path(directory) / "registry.json"
        registry = {
            "evaluation_start": "2026-10-01T00:00:00Z",
            "followup_end": "2027-10-04T01:00:00Z",
            "integrity": {},
        }
        digest = hashlib.sha256(cs._canonical_registry_bytes(registry)).hexdigest()
        registry["integrity"]["registry_sha256"] = digest
        path.write_bytes(cs._canonical_registry_bytes(registry, include_integrity=True))
        path.with_name("registry.json.sha256").write_text(digest + "\n", encoding="ascii")
        return path

    def test_prospective_seal_boundaries_and_tamper_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with self.assertRaisesRegex(RuntimeError, "registry or its SHA-256 sidecar is missing"):
                cs.enforce_prospective_seal(
                    1, registry_path=missing, now=datetime(2026, 10, 1, tzinfo=timezone.utc)
                )
            path = self._sealed_registry(directory)
            cs.enforce_prospective_seal(
                1_790_812_799_999, registry_path=path,
                now=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
            with self.assertRaisesRegex(RuntimeError, "sealed prospective validation is active"):
                cs.enforce_prospective_seal(
                    1_790_812_800_000, registry_path=path,
                    now=datetime(2026, 10, 1, tzinfo=timezone.utc),
                )
            cs.enforce_prospective_seal(
                1_790_812_800_000, registry_path=path,
                now=datetime(2027, 10, 4, 1, tzinfo=timezone.utc),
            )
            path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "integrity verification"):
                cs.enforce_prospective_seal(
                    1, registry_path=path, now=datetime(2026, 10, 1, tzinfo=timezone.utc)
                )

    def test_inferential_dataframe_is_labeled_exploratory(self) -> None:
        labeled = cs.mark_exploratory_inference(pd.DataFrame({"t_stat": [1.0], "p_value": [0.1]}))
        self.assertEqual(labeled["inference_scope"].tolist(), ["exploratory_uncorrected"])

    def test_funding_tail_allows_clock_jitter_but_detects_missing_interval(self) -> None:
        interval = 8 * 3_600_000
        jittered = pd.DataFrame({"ts": [1_000, 1_000 + interval + 10, 1_000 + 2 * interval]})
        complete = frame_coverage(
            jittered, interval, maximum_expected_interval_ms=interval,
            gap_tolerance_ms=60_000,
        )
        self.assertEqual(complete["unexpected_gap_count"], 0)
        self.assertEqual(complete["tail_contiguous_start_ts"], 1_000)
        missing = frame_coverage(
            pd.DataFrame({"ts": [1_000, 1_000 + 2 * interval]}), interval,
            maximum_expected_interval_ms=interval, gap_tolerance_ms=60_000,
        )
        self.assertEqual(missing["unexpected_gap_count"], 1)
        self.assertEqual(missing["tail_contiguous_start_ts"], 1_000 + 2 * interval)


if __name__ == "__main__":
    unittest.main()
