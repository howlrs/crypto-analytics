"""Timing invariants for close-observed liquidation-reversion executions."""
import unittest

import numpy as np

from backtests.liq_reversion import ONE_WAY_COST_BP, entry_exit_prices, net_return_bp


MINUTE = 60_000


class EntryExitPricesTimingTests(unittest.TestCase):
    def test_linear_short_return_uses_entry_notional(self) -> None:
        entry = np.array([100.0, 100.0])
        exit_ = np.array([90.0, 110.0])

        result = net_return_bp(entry, exit_, "short")

        np.testing.assert_allclose(
            result,
            [1_000.0 - 2 * ONE_WAY_COST_BP, -1_000.0 - 2 * ONE_WAY_COST_BP],
        )

    def test_return_rejects_unknown_direction(self) -> None:
        with self.assertRaisesRegex(ValueError, "direction"):
            net_return_bp(np.array([100.0]), np.array([90.0]), "sideways")

    def test_zero_delay_enters_at_next_bar_open_after_close_decision(self) -> None:
        ts = np.array([0, MINUTE, 2 * MINUTE, 3 * MINUTE], dtype=np.int64)
        open_px = np.array([100.0, 101.0, 102.0, 103.0])

        decision, entry_ts, entry_px, exit_ts, exit_px, valid = entry_exit_prices(
            ts, open_px, np.array([0]), entry_delay_min=0, exit_hold_h=0,
        )

        np.testing.assert_array_equal(decision, [MINUTE])
        np.testing.assert_array_equal(entry_ts, [MINUTE])
        np.testing.assert_allclose(entry_px, [101.0])
        np.testing.assert_array_equal(exit_ts, [MINUTE])
        np.testing.assert_allclose(exit_px, [101.0])
        np.testing.assert_array_equal(valid, [True])

    def test_delay_and_hold_are_measured_from_decision_and_actual_entry(self) -> None:
        ts = np.arange(0, 130 * MINUTE, MINUTE, dtype=np.int64)
        open_px = np.arange(len(ts), dtype=np.float64) + 100.0

        decision, entry_ts, entry_px, exit_ts, exit_px, valid = entry_exit_prices(
            ts, open_px, np.array([10]), entry_delay_min=30, exit_hold_h=1,
        )

        self.assertTrue(valid[0])
        self.assertEqual(decision[0], 11 * MINUTE)
        self.assertEqual(entry_ts[0], 41 * MINUTE)
        self.assertEqual(exit_ts[0], 101 * MINUTE)
        self.assertEqual(entry_px[0], 141.0)
        self.assertEqual(exit_px[0], 201.0)

    def test_gaps_use_first_available_bar_at_or_after_target(self) -> None:
        ts = np.array([0, MINUTE, 4 * MINUTE, 66 * MINUTE], dtype=np.int64)
        open_px = np.array([10.0, 11.0, 14.0, 76.0])

        decision, entry_ts, entry_px, exit_ts, exit_px, valid = entry_exit_prices(
            ts, open_px, np.array([0]), entry_delay_min=2, exit_hold_h=1,
        )

        self.assertTrue(valid[0])
        self.assertEqual(decision[0], MINUTE)
        self.assertEqual(entry_ts[0], 4 * MINUTE)
        self.assertEqual(exit_ts[0], 66 * MINUTE)
        self.assertEqual(entry_px[0], 14.0)
        self.assertEqual(exit_px[0], 76.0)

    def test_execution_prices_cannot_come_from_decision_or_earlier_bar(self) -> None:
        ts = np.array([0, MINUTE, 2 * MINUTE], dtype=np.int64)
        # Deliberately unique values make use of the event bar's price detectable.
        open_px = np.array([999.0, 101.0, 102.0])

        decision, entry_ts, entry_px, _, _, valid = entry_exit_prices(
            ts, open_px, np.array([0]), entry_delay_min=0, exit_hold_h=0,
        )

        self.assertTrue(valid[0])
        self.assertGreaterEqual(entry_ts[0], decision[0])
        self.assertNotEqual(entry_px[0], open_px[0])
        self.assertEqual(entry_px[0], open_px[1])


if __name__ == "__main__":
    unittest.main()
