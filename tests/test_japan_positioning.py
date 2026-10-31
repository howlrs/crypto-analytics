"""Publication timing, signal construction and the decision rule for the Japan positioning test."""
import unittest

import numpy as np
import pandas as pd

from backtests.japan_positioning import (
    ROOT, Config, decision_days, latest_known, margin_ratio, newey_west_slope, nth_business_day_after,
    outside_repo, rule_pnl, short_ratio_daily, short_ratio_signal, test_signals, touches, trailing_z,
)

BUSINESS = np.array(["2024-08-01", "2024-08-02", "2024-08-05", "2024-08-06", "2024-08-07", "2024-08-08",
                     "2024-08-09", "2024-08-13", "2024-08-14", "2024-08-15", "2024-08-16"])


class TimingTest(unittest.TestCase):
    def test_decisions_are_last_business_day_of_each_iso_week(self):
        frame = decision_days(pd.Series(BUSINESS))
        self.assertEqual(list(frame.date), ["2024-08-02", "2024-08-09", "2024-08-16"])
        self.assertEqual(list(frame.next_date.fillna("")), ["2024-08-09", "2024-08-16", ""])

    def test_latest_known_needs_publication_strictly_before_and_freshness(self):
        decisions = pd.Series(["2024-08-09", "2024-08-16", "2024-09-20"])
        available = pd.Series(["2024-08-08", "2024-08-16", "2024-08-15"])
        reference = pd.Series(["2024-08-02", "2024-08-09", "2024-08-01"])
        values = pd.Series([1.0, 2.0, 3.0])
        out = latest_known(decisions, available, reference, values, 14)
        # 08-16: the 08-09 week is published that day, so only the 08-02 week counts (an older
        # reference published later never replaces a newer one); 09-20: everything is stale.
        np.testing.assert_allclose(out[:2], [1.0, 1.0])
        self.assertTrue(np.isnan(out[2]))

    def test_nth_business_day_after_skips_holidays(self):
        out = nth_business_day_after(pd.Series(["2024-08-09", "2024-08-02"]), BUSINESS, 2)
        self.assertEqual(list(out), ["2024-08-14", "2024-08-06"])  # 08-12 is a holiday


class SignalTest(unittest.TestCase):
    def test_margin_ratio_values_at_reference_close_and_respects_pubdate(self):
        margin = pd.DataFrame({"date": ["2024-08-09", "2024-08-09", "2024-08-09", "2024-08-16"],
                               "code": ["A", "B", "C", "A"], "pub_date": [None, None, None, "2024-08-21"],
                               "long_shares": [100.0, 10.0, 5.0, 100.0], "short_shares": [50.0, 10.0, 5.0, 25.0]})
        closes = pd.DataFrame({"date": ["2024-08-09", "2024-08-09", "2024-08-16"], "code": ["A", "B", "A"],
                               "close": [10.0, 100.0, 10.0]})
        signal, weekly = margin_ratio(margin, closes, np.r_[BUSINESS, ["2024-08-19", "2024-08-20", "2024-08-21"]], 2)
        self.assertAlmostEqual(signal.value.iloc[0], np.log((1000 + 1000) / (500 + 1000)))  # C has no price
        self.assertEqual(int(weekly.codes_priced.iloc[0]), 2)
        self.assertAlmostEqual(weekly.long_shares_unpriced_share.iloc[0], 5 / 115)
        self.assertEqual(signal.available.iloc[0], "2024-08-14")
        self.assertEqual(signal.available.iloc[1], "2024-08-21")  # PubDate later than the inferred 08-20
        signal, _ = margin_ratio(margin, closes, BUSINESS, 2)
        self.assertTrue(pd.isna(signal.available.iloc[1]))  # beyond the calendar: never treated as published

    def test_short_ratio_excludes_total_bucket_and_needs_full_prior_window(self):
        short = pd.DataFrame({"date": ["2024-08-05"] * 3, "s33": ["0050", "3050", "9999"],
                              "sell_ex_short_va": [60.0, 40.0, 1000.0], "short_with_res_va": [30.0, 10.0, 0.0],
                              "short_no_res_va": [10.0, 0.0, 0.0]})
        daily = short_ratio_daily(short)
        self.assertAlmostEqual(daily.ratio.iloc[0], 50 / 150)
        series = pd.DataFrame({"date": BUSINESS[:6], "ratio": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9], "sectors": 33})
        out = short_ratio_signal(pd.Series(["2024-08-08", "2024-08-07"]), series, BUSINESS, 5)
        self.assertAlmostEqual(out[0], 0.3)  # 08-01..08-07, not the decision day itself
        self.assertTrue(np.isnan(out[1]))  # only four prior days exist

    def test_trailing_z_uses_only_the_past(self):
        x = np.r_[np.arange(10.0), 100.0]
        z = trailing_z(x, 10, 10)
        past = np.arange(10.0)
        self.assertTrue(np.all(np.isnan(z[:10])))
        self.assertAlmostEqual(z[10], (100 - past.mean()) / past.std(ddof=1))
        x2 = x.copy()
        x2[10] = 0.0
        np.testing.assert_allclose(trailing_z(x2, 10, 10)[:10], z[:10], equal_nan=True)


class StatisticsTest(unittest.TestCase):
    def test_newey_west_slope_matches_ols_and_white_se(self):
        rng = np.random.default_rng(0)
        x = rng.normal(size=200)
        y = 0.3 * x + rng.normal(size=200)
        beta, se = newey_west_slope(y, x, 0)
        X = np.column_stack([np.ones(200), x])
        b = np.linalg.lstsq(X, y, rcond=None)[0]
        u = y - X @ b
        inv = np.linalg.inv(X.T @ X)
        white = inv @ (X * u[:, None]).T @ (X * u[:, None]) @ inv
        self.assertAlmostEqual(beta, b[1])
        self.assertAlmostEqual(se, np.sqrt(white[1, 1]))

    def test_rule_pnl_pays_turnover_and_goes_flat_without_signal(self):
        pnl = rule_pnl(np.array([2.0, np.nan, -0.5]), np.array([0.01, 0.02, -0.04]), -1, 3.0)
        np.testing.assert_allclose(pnl, [-0.01 - 3e-4, 0.0 - 3e-4, -0.02 - 1.5e-4])  # sign -1, z<0: long

    def test_decision_rule_on_planted_effects(self):
        rng = np.random.default_rng(1)
        n = 470
        dates = pd.date_range("2017-10-06", periods=n, freq="7D").strftime("%Y-%m-%d")
        z = rng.normal(size=(n, 3))
        y = 0.004 * z[:, 0] - 0.004 * z[:, 1] + 0.02 * rng.normal(size=n)  # S1 +, S2 -, S3 none
        panel = pd.DataFrame({"date": dates, "next_date": np.r_[dates[1:], [None]], "nikkei_ret": y,
                              "z_s1_foreign_flow": z[:, 0], "z_s2_margin_ratio": z[:, 1], "z_s3_short_ratio": z[:, 2]})
        out = test_signals(panel, "nikkei_ret", Config()).set_index("signal")
        self.assertTrue(out.loc["s1_foreign_flow", "supports"])
        self.assertTrue(out.loc["s2_margin_ratio", "supports"])
        self.assertFalse(out.loc["s3_short_ratio", "supports"])
        flipped = panel.assign(nikkei_ret=-y)
        out = test_signals(flipped, "nikkei_ret", Config()).set_index("signal")
        self.assertFalse(out.supports.any())  # the opposite sign is never credited
        self.assertGreater(out.loc["s1_foreign_flow", "p_one_sided"], 0.99)

    def test_shock_month_flags_and_limits(self):
        frame = pd.DataFrame({"date": ["2024-07-26", "2024-08-30", "2024-09-06"],
                              "next_date": ["2024-08-02", "2024-09-06", None]})
        self.assertEqual(list(touches(frame, ("2024-08",))), [True, True, False])
        with self.assertRaises(ValueError):
            Config(z_min_finite=60).validate()
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "x")


if __name__ == "__main__":
    unittest.main()
