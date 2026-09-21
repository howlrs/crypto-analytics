import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.execution_sensitivity import (CUTOFF_EXCLUSIVE_TS, DEFAULT_SCENARIOS, Scenario,
                                              evaluate_scenario, load_events, run)


def quotes(values, *, start=0):
    return pd.Series(values, index=np.arange(start, start + len(values) * 60_000, 60_000, dtype=np.int64))


class ExecutionSensitivityTests(unittest.TestCase):
    def setUp(self):
        self.events = pd.DataFrame({"event_id": ["x"], "source_file": ["BTCUSDT.csv"], "symbol": ["BTCUSDT"],
                                    "detect_ts": [3_600_000], "entry_ts": [3_600_000], "exit_ts": [10_800_000],
                                    "source_net_bp": [86.0]})
        self.quotes = {"BTCUSDT": quotes(np.full(181, 100_000.0))}

    def test_baseline_reproduces_source_net_without_double_counting(self):
        ledger = evaluate_scenario(self.events, self.quotes, DEFAULT_SCENARIOS[0])
        self.assertEqual(ledger.execution_status.iloc[0], "complete")
        self.assertAlmostEqual(ledger.scenario_net_bp.iloc[0], 86.0)
        self.assertAlmostEqual(ledger.source_implied_gross_bp.iloc[0], 100.0)

    def test_prior_volume_is_causal_and_actual_bar_is_only_audit(self):
        altered = self.quotes["BTCUSDT"].copy()
        altered.loc[3_600_000] = 1.0
        a = evaluate_scenario(self.events, self.quotes, DEFAULT_SCENARIOS[1])
        b = evaluate_scenario(self.events, {"BTCUSDT": altered}, DEFAULT_SCENARIOS[1])
        self.assertAlmostEqual(a.scenario_net_bp.iloc[0], b.scenario_net_bp.iloc[0])
        self.assertNotEqual(a.entry_actual_participation.iloc[0], b.entry_actual_participation.iloc[0])

    def test_costs_are_monotonic_with_notional_and_impact(self):
        low = evaluate_scenario(self.events, self.quotes, Scenario("low", 1_000, 1, 5, 2, 1))
        high = evaluate_scenario(self.events, self.quotes, Scenario("high", 2_000, 1, 5, 2, 2))
        self.assertLess(high.scenario_net_bp.iloc[0], low.scenario_net_bp.iloc[0])

    def test_entry_skip_and_exit_failure_remain_in_ledger(self):
        missing_entry = self.quotes["BTCUSDT"].drop(0)
        skipped = evaluate_scenario(self.events, {"BTCUSDT": missing_entry}, DEFAULT_SCENARIOS[0])
        self.assertEqual(len(skipped), 1); self.assertEqual(skipped.execution_status.iloc[0], "skipped_entry")
        missing_exit = self.quotes["BTCUSDT"].drop(7_260_000)
        incomplete = evaluate_scenario(self.events, {"BTCUSDT": missing_exit}, DEFAULT_SCENARIOS[0])
        self.assertEqual(len(incomplete), 1); self.assertEqual(incomplete.execution_status.iloc[0], "incomplete_exit")
        self.assertTrue(np.isnan(incomplete.scenario_net_bp.iloc[0]))

    def test_known_exit_capacity_breach_keeps_hypothetical_cost_and_net(self):
        scenario = Scenario("breach", 50_000, .1, 5, 2, 10)
        volume = np.full(181, 1_000_000.0)
        volume[120:180] = 100_000.0  # exit's prior 60 completed minutes only
        ledger = evaluate_scenario(self.events, {"BTCUSDT": quotes(volume)}, scenario)
        self.assertEqual(ledger.execution_status.iloc[0], "exit_capacity_breach")
        self.assertTrue(ledger.hypothetical_priced.iloc[0])
        self.assertTrue(np.isfinite(ledger.scenario_net_bp.iloc[0]))

    def test_fee_slippage_and_impact_are_separate_and_future_bars_do_not_matter(self):
        scenario = Scenario("split", 1_000, 1, 3, 4, 5)
        original = evaluate_scenario(self.events, self.quotes, scenario)
        future = self.quotes["BTCUSDT"].copy()
        future.loc[20_000_000] = 1.0
        later = evaluate_scenario(self.events, {"BTCUSDT": future}, scenario)
        self.assertEqual(original.entry_fee_bp.iloc[0], 3)
        self.assertEqual(original.entry_slippage_bp.iloc[0], 4)
        self.assertGreater(original.entry_impact_bp.iloc[0], 0)
        self.assertAlmostEqual(original.scenario_net_bp.iloc[0], later.scenario_net_bp.iloc[0])

    def test_strict_cutoff_rejects_late_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "BTCUSDT_events.csv"
            pd.DataFrame({"detect_ts": [1], "entry_ts": [2], "exit_ts": [CUTOFF_EXCLUSIVE_TS], "net_ret_bp": [1]}).to_csv(path, index=False)
            with self.assertRaises(ValueError): load_events([path])

    def test_bounded_sqlite_smoke_and_common_set_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root / "market.db"; events = root / "BTCUSDT_events.csv"; out = root / "out"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, quote_volume REAL)")
            rows = [("binance", "perp", "BTCUSDT", i * 60_000, 100_000.0) for i in range(181)]
            con.executemany("INSERT INTO klines VALUES (?, ?, ?, ?, ?)", rows); con.commit(); con.close()
            pd.DataFrame({"detect_ts": [3_600_000], "entry_ts": [3_600_000], "exit_ts": [10_800_000], "net_ret_bp": [10.0]}).to_csv(events, index=False)
            result = run([str(events)], db, out)
            self.assertEqual(result["ledger_rows"], len(DEFAULT_SCENARIOS))
            summary = pd.read_csv(out / "scenario_summary.csv")
            self.assertEqual(summary.loc[0, "net_result_label"], "full_hypothetical_all_targets")
            self.assertTrue((out / "common_set_deltas.csv").exists())

    def test_empty_csv_and_custom_scenario_are_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "BTCUSDT_events.csv"
            pd.DataFrame(columns=["detect_ts", "entry_ts", "exit_ts", "net_ret_bp"]).to_csv(path, index=False)
            loaded = load_events([path])
            self.assertTrue(loaded.empty)
            with self.assertRaises(ValueError):
                Scenario("bad", float("nan"), .1, 5, 2, 0).validate()

    def test_same_basename_from_two_directories_has_distinct_event_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = root / "a"; second = root / "b"; first.mkdir(); second.mkdir()
            for directory in (first, second):
                pd.DataFrame({"detect_ts": [1], "entry_ts": [2], "exit_ts": [3], "net_ret_bp": [1]}).to_csv(directory / "BTCUSDT_events.csv", index=False)
            loaded = load_events([first / "BTCUSDT_events.csv", second / "BTCUSDT_events.csv"])
            self.assertEqual(loaded.source_file.nunique(), 1)
            self.assertEqual(loaded.event_id.nunique(), 2)

    def test_manifest_records_db_stability_and_output_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root / "market.db"; events = root / "BTCUSDT_events.csv"; out = root / "out"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, quote_volume REAL)")
            con.executemany("INSERT INTO klines VALUES (?, ?, ?, ?, ?)", [("binance", "perp", "BTCUSDT", i * 60_000, 100_000.) for i in range(181)])
            con.commit(); con.close()
            pd.DataFrame({"detect_ts": [3_600_000], "entry_ts": [3_600_000], "exit_ts": [10_800_000], "net_ret_bp": [1.]}).to_csv(events, index=False)
            run([str(events)], db, out)
            import json
            manifest = json.loads((out / "run_manifest.json").read_text())
            self.assertEqual(manifest["database"]["before"], manifest["database"]["after"])
            self.assertIn("execution_ledger.csv", manifest["outputs_sha256"])
            self.assertIn("BTCUSDT", manifest["quote_volume_sha256"])


if __name__ == "__main__":
    unittest.main()
