"""Reaction-session timing, split-adjusted moves and the leverage rule for earnings jumps."""
import unittest

import numpy as np
import pandas as pd

from backtests.japan_earnings_risk import (
    ROOT, Config, choose_leverage, daily_moves, earnings_days, liquidation_room, outside_repo, reaction_session,
)

BUSINESS = np.array(["2024-10-31", "2024-11-01", "2024-11-05", "2024-11-06", "2026-10-30", "2026-11-02"])


class ReactionSessionTest(unittest.TestCase):
    def test_disclosure_before_the_close_reacts_the_same_day(self):
        self.assertEqual(reaction_session("2024-11-01", "14:59:00", BUSINESS), "2024-11-01")
        self.assertEqual(reaction_session("2024-11-05", "15:15:00", BUSINESS), "2024-11-05")

    def test_close_time_moved_from_15_00_to_15_30(self):
        self.assertEqual(reaction_session("2024-11-01", "15:15:00", BUSINESS), "2024-11-05")
        self.assertEqual(reaction_session("2024-11-05", "15:30:00", BUSINESS), "2024-11-06")

    def test_evening_and_weekend_disclosures_react_next_session(self):
        self.assertEqual(reaction_session("2026-10-30", "18:45:00", BUSINESS), "2026-11-02")
        self.assertEqual(reaction_session("2026-10-31", "10:00:00", BUSINESS), "2026-11-02")
        self.assertEqual(reaction_session("2026-10-30", None, BUSINESS), "2026-11-02")
        self.assertIsNone(reaction_session("2026-11-02", "16:00:00", BUSINESS))


class MovesTest(unittest.TestCase):
    def test_split_day_is_measured_against_the_adjusted_previous_close(self):
        bars = pd.DataFrame({"date": ["2026-09-25", "2026-09-28", "2026-09-29"], "high": [3100.0, 1100.0, 1210.0],
                             "close": [3000.0, 1050.0, 1100.0], "low": [2900.0, 990.0, 1040.0],
                             "adj_factor": [1.0, 1 / 3, 1.0]})
        m = daily_moves(bars)
        self.assertEqual(list(m.date), ["2026-09-28", "2026-09-29"])
        np.testing.assert_allclose(m.ret_close, [0.05, 1100 / 1050 - 1])
        np.testing.assert_allclose(m.rise_high, [0.10, 1210 / 1050 - 1])

    def test_only_scheduled_statements_count_once_per_reaction_day(self):
        disc = pd.DataFrame({"disc_date": ["2026-10-30", "2026-10-30", "2026-10-30"],
                             "disc_time": ["18:45:00", "18:45:00", "19:00:00"],
                             "doc_type": ["2QFinancialStatements_Consolidated_IFRS", "EarnForecastRevision",
                                          "2QFinancialStatements_NonConsolidated_JP"]})
        e = earnings_days(disc, BUSINESS)
        self.assertEqual(list(e.reaction), ["2026-11-02"])
        self.assertEqual(e.disc_time.iloc[0], "18:45:00")


class LeverageTest(unittest.TestCase):
    def test_largest_leverage_whose_room_covers_the_buffered_worst_rise(self):
        cfg = Config()
        self.assertAlmostEqual(liquidation_room(3.0), 1 / 3 - 0.05)
        self.assertEqual(choose_leverage(0.20, cfg), 3.0)  # 1.25 x 20% = 25% <= 28.3%
        self.assertEqual(choose_leverage(0.32, cfg), 2.0)  # 40% <= 45%
        self.assertEqual(choose_leverage(0.15, cfg), 4.0)
        self.assertIsNone(choose_leverage(0.55, cfg))

    def test_config_and_output_guards(self):
        with self.assertRaises(ValueError):
            Config(leverages=(5.0,)).validate()
        with self.assertRaises(ValueError):
            Config(buffer=0.9).validate()
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "results/x")


if __name__ == "__main__":
    unittest.main()
