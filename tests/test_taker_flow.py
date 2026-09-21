import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.taker_flow import ANALYSIS_CUTOFF_MS, HOUR_MS, FlowConfig, build_features, causal_zscore, raw_input_coverage, run


def minute_klines(market, hour, quote, buy, *, bars=60):
    return pd.DataFrame({
        "market": market,
        "ts": [hour + i * 60_000 for i in range(bars)],
        "close": 100.0,
        "quote_volume": quote,
        "taker_buy_quote": buy,
    })


class TakerFlowTests(unittest.TestCase):
    def config(self, **kwargs):
        values = dict(start_ms=0, end_ms=10 * HOUR_MS, standardize_hours=3,
                      min_standardize_hours=2, oi_max_age_hours=2, funding_max_age_hours=2)
        values.update(kwargs)
        return FlowConfig(**values)

    def test_complete_windows_zero_and_partial_are_unclassified(self):
        klines = pd.concat([
            minute_klines("spot", 0, 10.0, 7.0), minute_klines("perp", 0, 20.0, 8.0),
            minute_klines("spot", HOUR_MS, 0.0, 0.0), minute_klines("perp", HOUR_MS, 20.0, 12.0),
            minute_klines("spot", 2 * HOUR_MS, 10.0, 5.0, bars=59), minute_klines("perp", 2 * HOUR_MS, 20.0, 10.0),
        ], ignore_index=True)
        features = build_features(klines, pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                                  pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config())
        self.assertEqual(features.loc[features.ts == HOUR_MS, "spot_flow_state"].item(), "classified")
        self.assertAlmostEqual(features.loc[features.ts == HOUR_MS, "spot_taker_imbalance"].item(), 0.4)
        self.assertEqual(features.loc[features.ts == 2 * HOUR_MS, "spot_flow_state"].item(), "unclassified_zero_volume")
        # The partial spot hour is never converted into a signal; the complete perp side remains diagnosable.
        self.assertEqual(features.loc[features.ts == 3 * HOUR_MS, "spot_flow_state"].item(), "unclassified_missing")

    def test_standardisation_excludes_current_and_future_values(self):
        base = pd.Series([1.0, 2.0, 3.0, 4.0])
        z = causal_zscore(base, window=3, min_history=2)
        self.assertTrue(np.isnan(z.iloc[0]))
        self.assertTrue(np.isnan(z.iloc[1]))
        self.assertAlmostEqual(z.iloc[2], 3.0)
        changed_future = base.copy()
        changed_future.iloc[3] = 1000.0
        np.testing.assert_allclose(causal_zscore(changed_future, 3, 2).iloc[:3], z.iloc[:3], equal_nan=True)

    def test_invalid_duplicate_and_time_gap_cannot_make_a_signal_or_history(self):
        # 60 rows with a duplicated minute are not a completed hour.
        duplicate = minute_klines("spot", 0, 10.0, 6.0)
        duplicate.loc[59, "ts"] = duplicate.loc[58, "ts"]
        invalid = minute_klines("spot", HOUR_MS, 10.0, 6.0)
        invalid.loc[0, "quote_volume"] = -1.0
        perps = pd.concat([minute_klines("perp", h * HOUR_MS, 10.0, 6.0) for h in (0, 1, 3, 4)], ignore_index=True)
        spots = pd.concat([duplicate, invalid, minute_klines("spot", 3 * HOUR_MS, 10.0, 6.0),
                           minute_klines("spot", 4 * HOUR_MS, 10.0, 6.0)], ignore_index=True)
        features = build_features(pd.concat([spots, perps], ignore_index=True),
                                  pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                                  pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config())
        self.assertEqual(features.loc[features.ts == HOUR_MS, "spot_flow_state"].item(), "unclassified_missing")
        self.assertTrue(np.isnan(features.loc[features.ts == 2 * HOUR_MS, "spot_taker_net_quote"].item()))
        # The absent 3h decision breaks rolling history; row at 4h starts a new causal segment.
        self.assertTrue(np.isnan(features.loc[features.ts == 4 * HOUR_MS, "spot_taker_imbalance_z"].item()))

    def test_future_asof_rows_do_not_change_prior_features(self):
        klines = pd.concat([minute_klines(m, h * HOUR_MS, 10.0, 6.0) for m in ("spot", "perp") for h in range(4)], ignore_index=True)
        oi = pd.DataFrame({"ts": [0, HOUR_MS], "open_interest": [10.0, 11.0], "oi_value": [100.0, 110.0]})
        funding = pd.DataFrame({"ts": [0], "rate": [0.0001], "interval_hours": [8.0]})
        before = build_features(klines, oi, funding, self.config())
        after = build_features(klines, pd.concat([oi, pd.DataFrame({"ts": [9 * HOUR_MS], "open_interest": [999.0], "oi_value": [999.0]})]),
                               pd.concat([funding, pd.DataFrame({"ts": [9 * HOUR_MS], "rate": [9.0], "interval_hours": [8.0]})]), self.config())
        pd.testing.assert_frame_equal(before, after)

    def test_diagnostic_state_and_summary_output(self):
        spot_buy = [0.0, 0.10, 0.50, 0.0]
        # Perp has enough variation for a valid z-score but is not a buy-dominant flow.
        perp_buy = [-0.10, 0.0, 0.01, 0.0]
        def flow(market, values):
            return pd.concat([minute_klines(market, h * HOUR_MS, 10.0, 5.0 + imbalance * 5.0)
                              for h, imbalance in enumerate(values)], ignore_index=True)
        oi = pd.DataFrame({"ts": [0, HOUR_MS, 2 * HOUR_MS], "open_interest": [100.0, 101.0, 104.0],
                           "oi_value": [1_000.0, 1_010.0, 1_040.0]})
        cfg = self.config(flow_z_threshold=1.0, imbalance_threshold=0.05, oi_increase_threshold=0.01)
        features = build_features(pd.concat([flow("spot", spot_buy), flow("perp", perp_buy)]), oi,
                                  pd.DataFrame({"ts": [0], "rate": [0.0], "interval_hours": [8.0]}), cfg)
        self.assertEqual(features.loc[features.ts == 3 * HOUR_MS, "flow_oi_state"].item(), "spot_buy_dominant_consistent")

    def test_empty_inputs_are_a_valid_empty_dataset(self):
        empty = build_features(pd.DataFrame(columns=["market", "ts", "close", "quote_volume", "taker_buy_quote"]),
                               pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                               pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config())
        self.assertTrue(empty.empty)
        self.assertIn("flow_oi_state", empty.columns)
        audit = raw_input_coverage(pd.DataFrame(columns=["market", "ts", "close", "quote_volume", "taker_buy_quote"]),
                                   self.config(end_ms=4 * HOUR_MS), empty)
        self.assertEqual(audit["all_missing_hours"], 3)

    def test_common_coverage_uses_shared_hours_not_outer_feature_union(self):
        spot = minute_klines("spot", 0, 10.0, 6.0)
        perp = minute_klines("perp", HOUR_MS, 10.0, 6.0)
        cfg = self.config(end_ms=4 * HOUR_MS)
        features = build_features(pd.concat([spot, perp]), pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                                  pd.DataFrame(columns=["ts", "rate", "interval_hours"]), cfg)
        audit = raw_input_coverage(pd.concat([spot, perp]), cfg, features)
        self.assertEqual(audit["common_valid_hours"], 0)
        self.assertIsNone(audit["common_valid_start_utc"])
        self.assertEqual(audit["one_sided_valid_hours"], 2)
        self.assertEqual(audit["all_missing_hours"], 1)

    def test_price_context_is_independent_of_flow_and_requires_valid_close(self):
        spot = minute_klines("spot", 0, 0.0, 0.0)
        perp = minute_klines("perp", 0, 10.0, 6.0)
        perp["close"] = 110.0
        spot["close"] = 100.0
        features = build_features(pd.concat([spot, perp]), pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                                  pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config())
        row = features.iloc[0]
        self.assertEqual(row.spot_flow_state, "unclassified_zero_volume")
        self.assertAlmostEqual(row.basis, 0.10)
        self.assertEqual(row.basis_state, "classified")
        spot["close"] = -1.0
        invalid = build_features(pd.concat([spot, perp]), pd.DataFrame(columns=["ts", "open_interest", "oi_value"]),
                                 pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config()).iloc[0]
        self.assertTrue(np.isnan(invalid.basis))
        self.assertEqual(invalid.spot_price_state, "unclassified_invalid_or_missing")

    def test_nonfinite_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            self.config(flow_z_threshold=float("nan")).validate()

    def test_asof_age_state_and_quantity_value_are_independent(self):
        klines = pd.concat([minute_klines(m, h * HOUR_MS, 10.0, 6.0) for m in ("spot", "perp") for h in range(4)], ignore_index=True)
        oi = pd.DataFrame({"ts": [0], "open_interest": [10.0], "oi_value": [0.0]})
        funding = pd.DataFrame({"ts": [0], "rate": [0.0001], "interval_hours": [8.0]})
        features = build_features(klines, oi, funding, self.config())
        at_one = features.loc[features.ts == HOUR_MS].iloc[0]
        at_three = features.loc[features.ts == 3 * HOUR_MS].iloc[0]
        self.assertEqual(at_one.oi_open_interest_state, "available")
        self.assertEqual(at_one.oi_oi_value_state, "unclassified_zero")
        self.assertEqual(at_three.oi_open_interest_state, "stale")
        self.assertEqual(at_three.funding_rate_state, "stale")
        self.assertEqual(at_three.oi_age_hours, 3.0)

    def test_oi_change_is_hand_calculated_and_invalid_oi_is_unclassified(self):
        klines = pd.concat([minute_klines(m, h * HOUR_MS, 10.0, 6.0) for m in ("spot", "perp") for h in range(3)], ignore_index=True)
        oi = pd.DataFrame({"ts": [0, 2 * HOUR_MS], "open_interest": [100.0, 110.0], "oi_value": [1_000.0, -1.0]})
        features = build_features(klines, oi, pd.DataFrame(columns=["ts", "rate", "interval_hours"]), self.config())
        row = features.loc[features.ts == 2 * HOUR_MS].iloc[0]
        self.assertAlmostEqual(row.oi_open_interest_change_1h, 0.10)
        self.assertEqual(row.oi_oi_value_state, "unclassified_invalid")

    def test_run_writes_manifest_and_rejects_post_cutoff(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "fixture.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, close REAL, quote_volume REAL, taker_buy_quote REAL);
                CREATE TABLE oi_metrics (venue TEXT, symbol TEXT, ts INTEGER, open_interest REAL, oi_value REAL);
                CREATE TABLE funding (venue TEXT, symbol TEXT, ts INTEGER, rate REAL, interval_hours REAL);
            """)
            rows = [("binance", market, "BTCUSDT", i * 60_000, 100.0, 10.0, 6.0) for market in ("spot", "perp") for i in range(60)]
            conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?,?)", rows)
            conn.execute("INSERT INTO oi_metrics VALUES (?,?,?,?,?)", ("binance", "BTCUSDT", 0, 1.0, 2.0))
            conn.execute("INSERT INTO funding VALUES (?,?,?,?,?)", ("binance", "BTCUSDT", 0, 0.0, 8.0))
            conn.commit(); conn.close()
            out = Path(temp) / "out"
            frame = run(db, out, FlowConfig(start_ms=0, end_ms=2 * HOUR_MS, standardize_hours=2, min_standardize_hours=2))
            self.assertEqual(len(frame), 1)
            manifest = json.loads((out / "run_manifest.json").read_text())
            self.assertEqual(manifest["coverage"]["hour_rows"], 1)
            self.assertTrue((out / "features.csv").is_file())
            self.assertTrue((out / "state_summary.csv").is_file())
            with self.assertRaises(FileExistsError):
                run(db, out, FlowConfig(start_ms=0, end_ms=2 * HOUR_MS, standardize_hours=2, min_standardize_hours=2))
            with self.assertRaises(ValueError):
                FlowConfig(end_ms=ANALYSIS_CUTOFF_MS + 1).validate()


if __name__ == "__main__":
    unittest.main()
