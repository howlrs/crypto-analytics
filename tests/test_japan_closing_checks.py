"""The estimators behind the post-hoc checks, on data whose answer is known."""
import unittest

import numpy as np
import pandas as pd

from backtests import japan_closing_checks as checks
from backtests.japan_closing_checks import block_bootstrap, crypto_checks, permutation_p, serial_checks, stress_episodes, week_case
from backtests.japan_margin_crowding import Config


def persistent(n, rho, seed):
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = rho * x[i - 1] + rng.normal()
    return x


class BootstrapTest(unittest.TestCase):
    def test_a_block_sees_the_persistence_a_plain_resample_misses(self):
        x = persistent(2000, 0.75, 1)
        rng = np.random.default_rng(2)
        one, long = block_bootstrap(x, 1, 4000, rng).std(), block_bootstrap(x, 26, 4000, rng).std()
        self.assertGreater(long, 1.8 * one)  # the true ratio is sqrt((1 + rho) / (1 - rho)), about 2.6

    def test_each_mean_is_built_from_the_right_blocks(self):
        x = np.arange(10.0)
        means = block_bootstrap(x, 2, 2000, np.random.default_rng(3))
        self.assertTrue((means >= 0.5).all() and (means <= 8.5).all())
        self.assertAlmostEqual(means.mean(), x.mean(), delta=0.1)

    def test_a_series_has_n_observations_when_blocks_do_not_divide_n(self):
        x = np.random.default_rng(9).normal(size=11)
        got = block_bootstrap(x, 4, 3, np.random.default_rng(10))
        starts = np.random.default_rng(10).integers(0, 11, size=(3, 3))
        wrapped = np.r_[x, x[:3]]
        for draw in range(3):
            series = np.concatenate([wrapped[s:s + 4] for s in starts[draw]])[:11]
            self.assertAlmostEqual(got[draw], series.mean())

    def test_serial_checks_report_the_autocorrelation_and_every_block(self):
        out = serial_checks(persistent(1000, 0.5, 4), 500)
        self.assertAlmostEqual(out["autocorrelation"]["1"], 0.5, delta=0.1)
        self.assertEqual(sorted(out["blocks"], key=int), ["4", "8", "13", "26"])
        self.assertEqual(sorted(out["hac_t_by_lag"], key=int), ["0", "2", "4", "8", "12"])


class PermutationTest(unittest.TestCase):
    def test_a_real_link_is_rare_under_shuffling(self):
        rng = np.random.default_rng(5)
        z = rng.normal(size=400)
        self.assertLess(permutation_p(0.5 * z + rng.normal(size=400), z, 200, 1), 0.02)

    def test_a_week_case_keeps_the_signal_first_and_removes_sector_means(self):
        rng = np.random.default_rng(6)
        n = 300
        signal, control = rng.normal(size=n), rng.normal(size=n)
        sectors = np.array([str(i % 4) for i in range(n)], dtype=object)
        X, Y, groups = week_case(rng.normal(size=n), signal, [control], sectors, Config())
        self.assertEqual(X.shape, (n, 2))
        for g in range(4):
            self.assertAlmostEqual(X[groups == g, 0].mean(), 0.0)
            self.assertAlmostEqual(Y[groups == g].mean(), 0.0)
        self.assertGreater(np.corrcoef(X[:, 0], signal)[0, 1], 0.95)


    def test_a_shuffle_moves_only_the_signal_and_only_inside_a_sector(self):
        X = np.array([[1.0, 5.0], [2.0, 6.0], [3.0, 7.0], [4.0, 8.0]] * 10)
        Y = np.arange(40.0)
        groups = np.array([0, 0, 1, 1] * 10)
        seen = []
        real, checks.np.linalg.lstsq = checks.np.linalg.lstsq, lambda a, b, rcond=None: (seen.append(a.copy()) or real(a, b, rcond=None))
        checks._CASES[:] = [[(X, Y, groups)] * 40]
        try:
            self.assertEqual(len(checks._shuffled_t(1)), 1)
        finally:
            checks.np.linalg.lstsq = real
            checks._CASES[:] = []
        np.testing.assert_array_equal(X[:, 0], [1.0, 2.0, 3.0, 4.0] * 10)  # the case itself is not changed
        shuffled = seen[0]
        np.testing.assert_array_equal(shuffled[:, 1], X[:, 1])
        for g in (0, 1):
            np.testing.assert_array_equal(np.sort(shuffled[groups == g, 0]), np.sort(X[groups == g, 0]))
        self.assertFalse((shuffled[:, 0] == X[:, 0]).all())


class StressTest(unittest.TestCase):
    def test_close_weeks_are_one_episode(self):
        weeks = pd.DataFrame({"decision": ["2020-03-04", "2020-03-11", "2021-01-06", "2022-01-05"], "topix_5d": [-0.06, -0.05, -0.05, 0.01],
                              "slope_level": [0.01, 0.03, -0.01, 0.9]})
        out = stress_episodes(weeks, -0.04)
        self.assertEqual((out["weeks"], out["episodes"], out["positive_episodes"]), (3, 2, 1))
        self.assertAlmostEqual(out["episode_mean"], (0.02 - 0.01) / 2)
        self.assertAlmostEqual(out["weekly_mean_without_march_2020"], -0.01)


class CryptoTest(unittest.TestCase):
    def test_the_previous_window_ends_before_the_signal_window(self):
        rng = np.random.default_rng(7)
        n = 700
        btc = rng.normal(0, 0.02, n)  # btc[i] runs from close i to close i+1
        dates = pd.date_range("2024-07-01", periods=n).strftime("%Y-%m-%d")  # the signal can start before August
        window = np.r_[np.nan, btc[:-1]]  # the window that ends at close i
        z = pd.Series(window).shift(1).to_numpy() * 50  # the signal at i follows the window that ended at close i-1
        panel = pd.DataFrame({"date": dates, "next_close_ms": 0, "btc_window": window, "btc_next": btc, "eth_next": btc,
                              "z_treasury": z, "z_exchange": rng.normal(size=n)})
        out = crypto_checks(panel, 50)
        self.assertGreater(out["previous_window"]["z_treasury"], 10)
        self.assertLess(abs(out["same_window"]["z_treasury"]), 3)


if __name__ == "__main__":
    unittest.main()
