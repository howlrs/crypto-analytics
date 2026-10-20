"""Close-time sampling, carry accounting and split handling for the xyz Japan perp diagnostics."""
import unittest

import numpy as np
import pandas as pd

from backtests.hl_japan_carry import (
    CLOSE_OFFSET, DAY, HOUR, ROOT, Config, daily_panel, dn_summary, funding_phases, liquidation_room,
    outside_repo, price_at_close, window_sum,
)

D0 = int(np.datetime64("2026-07-06", "D").astype(np.int64))  # a Monday


def candles(times, prices):
    prices = np.asarray(prices, float)
    return pd.DataFrame({"t": np.asarray(times, np.int64), "o": prices, "h": prices * 1.01, "l": prices * 0.99,
                         "c": prices, "v": np.ones(len(prices))})


def hourly_funding(start_ms, stop_ms, rate):
    t = np.arange(start_ms - start_ms % HOUR + HOUR, stop_ms + 1, HOUR, dtype=np.int64)
    return pd.DataFrame({"time": t, "rate": np.full(len(t), rate), "premium": np.zeros(len(t))})


def market(days, perp_prices, fx_prices):
    """15m candles at each close and hourly candles whose close is the same price."""
    ts = days * DAY + CLOSE_OFFSET
    perp = {"15m": candles(ts, perp_prices), "1h": candles(ts - 30 * 60_000, perp_prices)}
    fx = {"15m": candles(ts, fx_prices), "1h": candles(ts - 30 * 60_000, fx_prices)}
    return perp, fx


class SamplingTest(unittest.TestCase):
    def test_exact_quarter_hour_wins_and_hourly_close_is_the_fallback(self):
        days = np.array([D0, D0 + 1, D0 + 2])
        ts = days * DAY + CLOSE_OFFSET
        m15 = candles([ts[1]], [101.0])
        h1 = candles(ts - 30 * 60_000, [90.0, 91.0, 92.0])  # opens at 06:00, close ~07:00
        price, source, proxy = price_at_close(days, m15, h1)
        np.testing.assert_allclose(price, [90.0, 101.0, 92.0])
        self.assertEqual(list(source), ["1h_close_0700", "15m_0630", "1h_close_0700"])
        np.testing.assert_allclose(proxy, [90.0, 91.0, 92.0])
        price, source, _ = price_at_close(np.array([D0 + 9]), m15, h1)
        self.assertTrue(np.isnan(price[0]))
        self.assertEqual(source[0], "missing")

    def test_window_sum_is_left_open_right_closed(self):
        t = np.array([10, 20, 30], np.int64)
        np.testing.assert_allclose(window_sum(t, np.array([1.0, 2.0, 4.0]), np.array([10, 0]), np.array([30, 10])),
                                   [6.0, 1.0])


class CarryTest(unittest.TestCase):
    def test_stock_dn_is_minus_basis_change_plus_funding(self):
        days = np.array([D0, D0 + 1, D0 + 2])
        fair_jpy = np.array([15000.0, 15300.0, 14800.0])
        fx = np.array([150.0, 152.0, 149.0])
        b = np.array([0.001, 0.003, -0.002])
        perp, fxc = market(days, fair_jpy / fx * np.exp(b), fx)
        funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, 1e-5)
        fx_funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, -2e-5)
        panel = daily_panel("xyz:KIOXIA", days, fair_jpy, funding, fx_funding, perp, fxc)
        np.testing.assert_allclose(panel.basis, b, atol=1e-12)
        self.assertEqual(list(panel.valid), [False, True, True])
        np.testing.assert_allclose(panel.funding_short.iloc[1:], [24e-5, 24e-5])
        np.testing.assert_allclose(panel.dn.iloc[1:], -np.diff(b) + 24e-5, atol=1e-12)
        # FX moves do not touch a stock hedge in USD accounting.
        np.testing.assert_allclose(panel.dn_unhedged.iloc[1:], panel.dn.iloc[1:])

    def test_quanto_leaves_fx_open_unless_hedged_with_long_jpy(self):
        days = np.array([D0, D0 + 1])
        index = np.array([60000.0, 60600.0])
        fx = np.array([150.0, 153.0])
        perp, fxc = market(days, index, fx)  # zero basis
        funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, 0.0)
        fx_funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, -1e-5)  # longs receive
        panel = daily_panel("xyz:JP225", days, index, funding, fx_funding, perp, fxc)
        self.assertAlmostEqual(panel.dn_unhedged.iloc[1], -np.log(153 / 150))
        self.assertAlmostEqual(panel.dn.iloc[1], 24e-5)

    def test_split_day_with_mixed_share_bases_is_excluded(self):
        days = np.array([D0, D0 + 1, D0 + 2, D0 + 3])
        fair_jpy = np.array([30000.0, 30300.0, 10100.0, 10200.0])  # TSE ex-date on day 2
        perp_jpy = np.array([30000.0, 10100.0, 10100.0, 10200.0])  # perp rebased a day early
        perp, fxc = market(days, perp_jpy / 150, np.full(4, 150.0))
        funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, 0.0)
        panel = daily_panel("xyz:KIOXIA", days, fair_jpy, funding, funding, perp, fxc)
        self.assertEqual(list(panel.scale_mismatch), [False, True, False, False])
        self.assertEqual(list(panel.valid), [False, False, False, True])

    def test_missing_funding_hours_invalidate_the_day(self):
        days = np.array([D0, D0 + 1])
        perp, fxc = market(days, [100.0, 100.0], [150.0, 150.0])
        funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, 1e-5).iloc[::2]
        panel = daily_panel("xyz:KIOXIA", days, np.array([15000.0, 15000.0]), funding, funding, perp, fxc)
        self.assertFalse(panel.valid.iloc[1])

    def test_weekend_hold_collects_weekend_funding(self):
        days = np.array([D0 + 4, D0 + 7])  # Friday -> Monday
        perp, fxc = market(days, [100.0, 100.0], [150.0, 150.0])
        funding = hourly_funding(days[0] * DAY, days[-1] * DAY + DAY, 1e-5)
        panel = daily_panel("xyz:KIOXIA", days, np.array([15000.0, 15000.0]), funding, funding, perp, fxc)
        self.assertAlmostEqual(panel.funding_short.iloc[1], 72e-5)


class SummaryTest(unittest.TestCase):
    def test_phases_split_at_launch_window(self):
        start = D0 * DAY
        funding = hourly_funding(start - HOUR, start + 40 * DAY, 1e-5)
        funding.loc[funding.time >= start + 28 * DAY, "rate"] = 2e-5
        rows = {r["phase"]: r for r in funding_phases(funding, 28)}
        self.assertAlmostEqual(rows["launch"]["annualised_mean"], 1e-5 * 24 * 365)
        self.assertAlmostEqual(rows["post_launch"]["annualised_mean"], 2e-5 * 24 * 365)
        self.assertEqual(rows["launch"]["hours"] + rows["post_launch"]["hours"], rows["all"]["hours"])

    def test_dn_summary_costs_and_drawdown(self):
        frame = pd.DataFrame({"date": ["a", "b", "c", "d"], "valid": [False, True, True, True],
                              "dn": [np.nan, 0.01, -0.02, 0.005], "funding_short": [0, 0.001, 0.001, 0.001]})
        s = dn_summary(frame, Config())
        self.assertEqual(s["days"], 3)
        self.assertAlmostEqual(s["max_drawdown"], -0.02)
        self.assertAlmostEqual(s["annualised_return_one_round_trip"],
                               (-0.005 - Config().round_trip) / 3 * 245)

    def test_liquidation_room_and_limits(self):
        self.assertAlmostEqual(liquidation_room(4, 10), 0.2)
        with self.assertRaises(ValueError):
            Config(leverages=(5.0,)).validate()
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "results" / "x")


if __name__ == "__main__":
    unittest.main()
