"""Publication timing, point-in-time membership and the estimators of the margin crowding test."""
import sqlite3
import unittest

import numpy as np
import pandas as pd

from backtests.japan_margin_crowding import (
    Config, adjusted_log_prices, book_alpha, book_return, by_year, cross_section_slope, decile_spread, load_wide, membership,
    ols_hac, primary_family, split_masks, weekly_cross_sections, weekly_schedule,
)

BUSINESS = pd.bdate_range("2024-07-01", "2024-08-30").strftime("%Y-%m-%d").to_numpy()


class ScheduleTest(unittest.TestCase):
    def references(self, dates, published=None):
        return pd.DataFrame({"date": dates, "pub_date": published if published is not None else [None] * len(dates)})

    def test_a_friday_balance_is_traded_at_wednesdays_close(self):
        out = weekly_schedule(BUSINESS, self.references(["2024-08-02", "2024-08-09"]), 3)
        self.assertEqual(list(out.decision), ["2024-08-07", "2024-08-14"])  # Tuesday 16:30 JST is after the close
        self.assertEqual(out.next_decision[0], "2024-08-14")
        self.assertTrue(pd.isna(out.next_decision[1]))

    def test_a_late_publication_delays_the_decision(self):
        out = weekly_schedule(BUSINESS, self.references(["2024-08-02"], ["2024-08-08"]), 3)
        self.assertEqual(out.decision[0], "2024-08-09")  # first close after the published day

    def test_mid_week_rows_of_the_daily_era_are_skipped(self):
        out = weekly_schedule(BUSINESS, self.references(["2024-08-05", "2024-08-06", "2024-08-09"]), 3)
        self.assertEqual(list(out.reference), ["2024-08-09"])

    def test_the_decision_is_never_on_or_before_the_published_day(self):
        with self.assertRaises(ValueError):
            Config(publication_lag=2).validate()


class PriceTest(unittest.TestCase):
    def test_wide_prices_ignore_splits_and_carry_gaps(self):
        close = pd.DataFrame({"a": [100.0, 25.0, np.nan, 26.0], "b": [10.0, 11.0, 12.0, 13.0]})
        adj = pd.DataFrame({"a": [1.0, 0.25, 1.0, 1.0], "b": [1.0, 1.0, 1.0, 1.0]})
        log_price = adjusted_log_prices(close, adj)
        self.assertAlmostEqual(log_price.a[1] - log_price.a[0], 0.0)
        self.assertAlmostEqual(log_price.a[2] - log_price.a[1], 0.0)
        self.assertAlmostEqual(log_price.a[3] - log_price.a[2], np.log(26 / 25))


class UnitTest(unittest.TestCase):
    def test_market_cap_reaches_yen(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE equities_daily (date TEXT, code TEXT, close REAL, adj_factor REAL, market_cap REAL, turnover REAL)")
        conn.execute("INSERT INTO equities_daily VALUES ('2024-01-04', '00010', 1000, 1, 12000000, 5000000000)")
        wide = load_wide(conn)
        self.assertEqual(wide["market_cap"].iloc[0, 0], 12e12)  # 12 million "millions" is 12 trillion yen
        self.assertEqual(wide["turnover"].iloc[0, 0], 5e9)
        self.assertGreater(wide["market_cap"].iloc[0, 0], Config().min_market_cap)


class MembershipTest(unittest.TestCase):
    def test_the_record_in_force_on_the_day_is_used(self):
        master = pd.DataFrame({"code": ["a", "a", "b"], "valid_from": ["2020-01-01", "2022-04-04", "2020-01-01"],
                               "valid_to": ["2022-04-03", None, "2021-01-01"], "mkt_nm": ["東証一部", "プライム", "マザーズ"]})
        codes = pd.Index(["a", "b", "c"])
        early, late = membership(master, "2021-06-01", codes), membership(master, "2023-06-01", codes)
        self.assertEqual(early.mkt_nm["a"], "東証一部")
        self.assertEqual(late.mkt_nm["a"], "プライム")
        self.assertTrue(pd.isna(late.mkt_nm["b"]))  # delisted names leave, not stay on their last record
        self.assertTrue(pd.isna(early.mkt_nm["c"]))


class EstimatorTest(unittest.TestCase):
    config = Config(min_stocks=50)

    def test_the_slope_recovers_a_planted_effect_through_controls_and_sectors(self):
        rng = np.random.default_rng(1)
        n = 4000
        control = rng.normal(size=n)
        signal = 0.8 * control + rng.normal(size=n)  # the raw signal is confounded by the control
        sectors = rng.integers(0, 33, size=n).astype(str)
        effect = pd.Series(sectors).map({str(i): i / 100 for i in range(33)}).to_numpy()
        y = -0.01 * (signal - signal.mean()) / signal.std() + 0.03 * control + effect + rng.normal(scale=0.03, size=n)
        got = cross_section_slope(y, signal, [control], sectors, self.config)
        self.assertAlmostEqual(got, -0.01, delta=0.0015)

    def test_a_thin_week_gives_nothing(self):
        x = np.arange(10.0)
        self.assertTrue(np.isnan(cross_section_slope(x, x, [x], np.array(["a"] * 10), self.config)))

    def test_decile_spread_is_high_minus_low(self):
        signal = np.arange(100.0)
        y = np.where(signal >= 90, 0.02, np.where(signal < 10, 0.05, 0.0))
        self.assertAlmostEqual(decile_spread(y, signal, self.config), -0.03)

    def test_a_book_is_weighted_by_its_balance_and_skips_missing_returns(self):
        self.assertAlmostEqual(book_return(np.array([0.1, -0.1, np.nan]), np.array([3.0, 1.0, 5.0])), 0.05)

    def test_hac_matches_ols_coefficients_and_widens_with_autocorrelation(self):
        rng = np.random.default_rng(2)
        e = np.convolve(rng.normal(size=2003), np.ones(4), "valid")
        X = np.ones((len(e), 1))
        beta, se0 = ols_hac(e, X, 0)
        _, se8 = ols_hac(e, X, 8)
        self.assertAlmostEqual(beta[0], e.mean())
        self.assertGreater(se8[0], 1.5 * se0[0])

    def test_alpha_is_separated_from_market_and_size(self):
        rng = np.random.default_rng(3)
        n = 500
        market, size = rng.normal(0.002, 0.02, n), rng.normal(0.0, 0.01, n)
        book = -0.001 + 1.3 * market + 0.8 * size + rng.normal(scale=0.002, size=n)
        got = book_alpha(pd.DataFrame({"long_book": book, "market": market, "equal": market + size}), "long_book", self.config)
        self.assertAlmostEqual(got["alpha_weekly"], -0.001, delta=0.0004)
        self.assertAlmostEqual(got["beta_market"], 1.3, delta=0.03)
        self.assertAlmostEqual(got["beta_size"], 0.8, delta=0.06)


class CrossSectionTest(unittest.TestCase):
    """One synthetic market through the whole weekly step."""
    stocks, days = 320, 300

    def market(self):
        rng = np.random.default_rng(7)
        index = pd.bdate_range("2021-01-04", periods=self.days).strftime("%Y-%m-%d")
        codes = [f"{i:05d}" for i in range(self.stocks)]
        close = pd.DataFrame(1000 * np.exp(np.cumsum(rng.normal(0, 0.01, (self.days, self.stocks)), axis=0)), index=index, columns=codes)
        wide = {"close": close, "adj_factor": pd.DataFrame(1.0, index=index, columns=codes),
                "market_cap": close * 1e7, "turnover": pd.DataFrame(1e8, index=index, columns=codes)}
        reference = index[280]
        shares = pd.Series(rng.uniform(1e4, 1e6, self.stocks), index=codes)
        margin = pd.DataFrame({"date": reference, "code": codes, "long_shares": shares.to_numpy(), "short_shares": 1e4})
        master = pd.DataFrame({"code": codes, "valid_from": "2020-01-01", "valid_to": None, "s33": [str(i % 5) for i in range(self.stocks)],
                               "mkt_nm": "プライム", "mrgn_nm": "貸借", "prod_cat": "011"})
        schedule = pd.DataFrame({"reference": [reference], "decision": [index[283]], "next_decision": [index[288]]})
        topix = pd.Series(2000.0, index=index)
        return wide, margin, master, schedule, topix

    def test_the_return_runs_from_the_decision_close_to_the_next(self):
        wide, margin, master, schedule, topix = self.market()
        one = wide["close"].copy()
        out = weekly_cross_sections(wide, margin, master, schedule, topix, Config()).iloc[0]
        close = wide["close"]
        decision, following = schedule.decision[0], schedule.next_decision[0]
        expected = (close.loc[following] / close.loc[decision] - 1).mean()
        self.assertEqual(int(out.stocks), self.stocks)
        self.assertAlmostEqual(out.equal, expected)
        wide["close"].loc[schedule.reference[0], one.columns[0]] *= 3  # an earlier price cannot change the return
        self.assertAlmostEqual(weekly_cross_sections(wide, margin, master, schedule, topix, Config()).iloc[0].equal, expected, places=9)

    def test_a_name_without_a_trade_at_the_decision_stays_out_of_the_books(self):
        wide, margin, master, schedule, topix = self.market()
        base = weekly_cross_sections(wide, margin, master, schedule, topix, Config()).iloc[0]
        halted = wide["close"].columns[3]
        wide["close"].loc[schedule.decision[0], halted] = np.nan  # its return would start from a stale close
        wide["close"].loc[schedule.next_decision[0]:, halted] *= 5
        out = weekly_cross_sections(wide, margin, master, schedule, topix, Config()).iloc[0]
        self.assertEqual(int(out.stocks), self.stocks - 1)
        self.assertLess(abs(out.long_book - base.long_book), 0.02)

    def test_the_stock_count_says_who_reached_the_regression(self):
        wide, margin, master, schedule, topix = self.market()
        day = wide["close"].index[150]  # listed too recently for the momentum control
        wide["close"].iloc[:150, :20] = np.nan
        wide["market_cap"].iloc[:150, :20] = np.nan
        out = weekly_cross_sections(wide, margin, master, schedule, topix, Config()).iloc[0]
        self.assertEqual(int(out.stocks), self.stocks)
        self.assertEqual(int(out.regressed_stocks), self.stocks - 20)
        self.assertEqual(day, wide["close"].index[150])

    def test_by_year_reports_the_excess_of_the_long_book(self):
        weeks = pd.DataFrame({"decision": ["2024-01-10", "2024-01-17"], "slope_level": [0.0, 0.0], "slope_change": [0.0, 0.0],
                              "decile_level": [0.0, 0.0], "long_book": [0.03, 0.01], "market": [0.02, 0.0]})
        self.assertAlmostEqual(by_year(weeks).long_minus_market[0], 0.02)


class DecisionTest(unittest.TestCase):
    def weeks(self, level, seed=4, n=500):
        rng = np.random.default_rng(seed)
        market = rng.normal(0.002, 0.02, n)
        size = rng.normal(0, 0.01, n)
        decisions = pd.Series(pd.date_range("2017-01-04", periods=n, freq="W-WED").strftime("%Y-%m-%d"))
        return pd.DataFrame({"decision": decisions, "next_decision": decisions.shift(-1), "market": market, "equal": market + size, "long_book": market + rng.normal(level / 2, 0.003, n),
                             "slope_level": rng.normal(level, 0.002, n), "slope_change": rng.normal(level, 0.002, n)})

    def test_consistent_negative_estimates_are_supported(self):
        self.assertTrue(primary_family(self.weeks(-0.001), Config()).supports.all())

    def test_noise_is_not_supported(self):
        self.assertFalse(primary_family(self.weeks(0.0), Config()).supports.any())

    def test_a_hold_that_ends_in_a_shock_month_is_not_calm(self):
        w = pd.DataFrame({"decision": ["2024-07-24", "2024-07-31", "2024-08-07", "2024-08-28", "2024-09-04"],
                          "next_decision": ["2024-07-31", "2024-08-07", "2024-08-14", "2024-09-04", "2024-09-11"]})
        self.assertEqual(split_masks(w)["calm"].tolist(), [True, False, False, False, True])

    def test_a_sign_flip_between_the_halves_fails_even_when_the_average_passes(self):
        w = self.weeks(-0.001)
        early = w.decision < "2022-01-01"
        w.loc[early, "slope_level"] = np.random.default_rng(5).normal(0.0004, 0.002, early.sum())
        w.loc[~early, "slope_level"] = np.random.default_rng(6).normal(-0.0024, 0.002, (~early).sum())
        out = primary_family(w, Config()).set_index("test")
        self.assertLess(out.estimate["D2_balance_level"], 0)
        self.assertFalse(out.supports["D2_balance_level"])


if __name__ == "__main__":
    unittest.main()
