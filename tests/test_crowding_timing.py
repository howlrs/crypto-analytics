import unittest

import numpy as np

from backtests.crowding_signals import asof_backward_index, forward_return


class CrowdingTimingTests(unittest.TestCase):
    def test_forward_return_enters_at_next_bar_open(self) -> None:
        ts = np.array([1_000, 2_000, 3_000, 4_000], dtype=np.int64)
        opens = np.array([10.0, 20.0, 30.0, 40.0])

        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(
            ts, opens, np.array([2_000], dtype=np.int64), 1_000
        )

        np.testing.assert_array_equal(valid, [True])
        np.testing.assert_array_equal(entry_ts, [3_000])
        np.testing.assert_allclose(entry_px, [30.0])
        np.testing.assert_array_equal(exit_ts, [4_000])
        np.testing.assert_allclose(exit_px, [40.0])
        np.testing.assert_allclose(ret, [40.0 / 30.0 - 1.0])

    def test_forward_return_holds_from_actual_entry_and_handles_gap(self) -> None:
        ts = np.array([1_000, 2_000, 5_000, 9_000], dtype=np.int64)
        opens = np.array([10.0, 20.0, 50.0, 90.0])

        ret, entry_px, entry_ts, exit_px, exit_ts, valid = forward_return(
            ts, opens, np.array([1_500], dtype=np.int64), 3_000
        )

        # Entry is 2_000; its exact 3_000ms holding target is 5_000, not
        # 4_500 relative to the original decision timestamp.
        np.testing.assert_array_equal(valid, [True])
        np.testing.assert_array_equal(entry_ts, [2_000])
        np.testing.assert_array_equal(exit_ts, [5_000])
        np.testing.assert_allclose(entry_px, [20.0])
        np.testing.assert_allclose(exit_px, [50.0])
        np.testing.assert_allclose(ret, [1.5])

    def test_oi_backward_asof_never_selects_future_snapshot(self) -> None:
        oi_ts = np.array([1_000, 4_000, 8_000], dtype=np.int64)
        funding_ts = np.array([500, 1_000, 3_999, 4_000, 7_999, 8_001], dtype=np.int64)

        idx = asof_backward_index(oi_ts, funding_ts)

        np.testing.assert_array_equal(idx, [-1, 0, 0, 1, 1, 2])
        matched = idx >= 0
        self.assertTrue(np.all(oi_ts[idx[matched]] <= funding_ts[matched]))


if __name__ == "__main__":
    unittest.main()
