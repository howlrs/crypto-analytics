"""Clock, split handling, the walk-forward residual and the decision rule for the crypto-linked stocks test."""
import unittest

import numpy as np
import pandas as pd

from backtests.japan_crypto_equities import (
    Config, adjusted_log_close, basket_signal, evaluate, primary_family, standardized_residuals, tse_clock,
)

HOUR = 3_600_000


class ClockTest(unittest.TestCase):
    def test_the_close_moves_to_1530_on_2024_11_05(self):
        clock = tse_clock(pd.Series(["2024-11-01", "2024-11-05"]))
        self.assertEqual(clock.close_ms[0] - clock.open_ms[0], 6 * HOUR)
        self.assertEqual(clock.close_ms[1] - clock.open_ms[1], 6 * HOUR + HOUR // 2)
        self.assertEqual(clock.next_close_ms[0], clock.close_ms[1])
        self.assertEqual(clock.prev_close_ms[1], clock.close_ms[0])

    def test_the_open_is_midnight_utc(self):
        self.assertEqual(tse_clock(pd.Series(["2026-11-06"])).open_ms[0] % (24 * HOUR), 0)


class SplitTest(unittest.TestCase):
    def test_a_split_is_not_a_return(self):
        bars = pd.DataFrame({"date": ["2024-01-04", "2024-01-05", "2024-01-09", "2024-01-10"],
                             "close": [3000.0, 2900.0, 1000.0, 1010.0], "adj_factor": [1.0, 1.0, 1 / 3, 1.0]})
        r = adjusted_log_close(bars).diff().to_numpy()
        self.assertAlmostEqual(r[2], np.log(1000 / (2900 / 3)))  # +3.4%, not -66%
        self.assertLess(abs(r[2]), 0.05)

    def test_a_day_without_trades_carries_the_price(self):
        bars = pd.DataFrame({"date": ["a", "b", "c"], "close": [100.0, np.nan, 110.0], "adj_factor": [1.0, 1.0, 1.0]})
        self.assertAlmostEqual(adjusted_log_close(bars).diff().sum(), np.log(1.1))


class ResidualTest(unittest.TestCase):
    def test_the_fit_never_uses_today(self):
        rng = np.random.default_rng(1)
        x = rng.normal(size=200)
        y = 2 * x + rng.normal(scale=0.5, size=200)
        base = standardized_residuals(y, x, 60, 40)
        changed = y.copy()
        changed[150] += 100
        moved = standardized_residuals(changed, x, 60, 40)
        np.testing.assert_allclose(base[:150], moved[:150], equal_nan=True)  # the past does not see it
        self.assertGreater(moved[150], base[150] + 50)  # it arrives as today's surprise

    def test_a_perfect_loading_leaves_only_noise(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=300)
        y = 3 * x + rng.normal(scale=0.1, size=300)
        z = standardized_residuals(y, x, 60, 40)[100:]
        self.assertLess(abs(np.corrcoef(z, x[100:])[0, 1]), 0.2)
        self.assertLess(abs(np.std(z) - 1), 0.2)

    def test_days_before_a_stock_starts_stay_out(self):
        dates = pd.Series([f"2024-{m:02d}-{d:02d}" for m in range(1, 8) for d in range(1, 22)])
        rng = np.random.default_rng(3)
        panel = pd.DataFrame({"date": dates, "btc_window": rng.normal(size=len(dates)), "topix_ret": rng.normal(size=len(dates))})
        closes = {"a": pd.Series(np.cumsum(rng.normal(size=len(dates))), index=dates)}
        z = basket_signal(panel, closes, {"a": "2024-04-01"}, Config())
        start = int((dates < "2024-04-01").sum())
        self.assertTrue(np.isnan(z[:start + 40]).all())  # 40 post-start returns are needed for the first fit
        self.assertTrue(np.isfinite(z[start + 41:]).all())


class DecisionTest(unittest.TestCase):
    def panel(self, loading, n=800, seed=4):
        rng = np.random.default_rng(seed)
        z = rng.normal(size=n)
        dates = pd.date_range("2022-01-03", periods=n, freq="B").strftime("%Y-%m-%d")
        return pd.DataFrame({"date": dates, "z": z, "y": loading * z + rng.normal(scale=0.02, size=n)})

    def test_a_planted_effect_is_found_and_noise_is_not(self):
        rows = [evaluate(self.panel(0.004), "z", "y", Config()), evaluate(self.panel(0.0, seed=5), "z", "y", Config())]
        out = primary_family(rows, Config())
        self.assertTrue(out.supports[0])
        self.assertFalse(out.supports[1])

    def test_the_wrong_sign_is_not_supported(self):
        out = primary_family([evaluate(self.panel(-0.004), "z", "y", Config())], Config())
        self.assertGreater(out.p_one_sided[0], 0.9)
        self.assertFalse(out.supports[0])

    def test_an_effect_in_one_half_only_is_not_supported(self):
        p = self.panel(0.0)
        half = len(p) // 2
        p.loc[half:, "y"] = 0.012 * p.z[half:] + np.random.default_rng(6).normal(scale=0.02, size=len(p) - half)
        p.loc[:half - 1, "y"] = -0.002 * p.z[:half] + np.random.default_rng(7).normal(scale=0.02, size=half)
        row = evaluate(p, "z", "y", Config())
        self.assertLess(row["slope_early"], 0)
        self.assertFalse(primary_family([row], Config()).supports[0])

    def test_a_hold_that_ends_in_a_shock_month_is_left_out_of_the_calm_slope(self):
        p = self.panel(0.0, n=900)
        boundary = p.index[p.date.str[:7] == "2022-05"][0] - 1  # the last day before the shock month
        p.loc[boundary, ["z", "y"]] = [8.0, 0.5]
        row = evaluate(p, "z", "y", Config())
        calm = evaluate(p.drop(index=boundary).reset_index(drop=True), "z", "y", Config())["slope_calm"]
        self.assertAlmostEqual(row["slope_calm"], calm, places=3)

    def test_a_missing_day_is_flat_and_is_not_traded_through(self):
        p = self.panel(0.0, n=300)
        p["z"] = 2.0 + 0.01 * p.z  # always a full position
        p["y"] = 1e-9 * p.z  # no return, so the P&L is the cost
        p.loc[150, "z"] = np.nan
        row = evaluate(p, "z", "y", Config())
        self.assertAlmostEqual(row["rule_mean_daily"] * 300, -3 * Config().cost_bp / 10_000, delta=1e-5)  # in, out at the gap, in again

    def test_too_few_days_are_reported_not_tested(self):
        row = evaluate(self.panel(0.0, n=50), "z", "y", Config())
        self.assertEqual(row["days"], 50)
        self.assertNotIn("slope", row)


if __name__ == "__main__":
    unittest.main()
