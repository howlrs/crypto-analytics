"""Session returns and the card of one synthetic stock."""
import unittest

import numpy as np
import pandas as pd

from backtests import japan_earnings_risk as earnings
from backtests.japan_risk_cards import MIN_DAYS, card, session_returns


def bars(n=400, seed=1):
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    frame = pd.DataFrame({"date": pd.bdate_range("2020-01-06", periods=n).strftime("%Y-%m-%d"), "close": close})
    frame["open"] = frame.close.shift().fillna(frame.close) * 1.001
    frame["high"] = frame[["open", "close"]].max(axis=1) * 1.002
    frame["low"] = frame[["open", "close"]].min(axis=1) * 0.998
    return frame.assign(adj_factor=1.0, turnover=1e9, upper_limit=0, lower_limit=0)


class SessionTest(unittest.TestCase):
    def test_the_two_legs_add_up_to_the_close_change(self):
        b = bars(30)
        s = session_returns(b)
        np.testing.assert_allclose((s.overnight + s.intraday)[1:], np.log(b.close).diff()[1:])

    def test_a_split_is_not_an_overnight_gap(self):
        b = pd.DataFrame({"date": ["a", "b"], "open": [100.0, 25.5], "close": [100.0, 26.0], "adj_factor": [1.0, 0.25]})
        self.assertAlmostEqual(session_returns(b).overnight[1], np.log(25.5 / 25))


class CardTest(unittest.TestCase):
    def setUp(self):
        self.config = earnings.Config()
        self.bars = bars()
        self.topix = pd.Series(np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.008, len(self.bars)))) * 2000, index=self.bars.date)

    def test_a_jump_sets_the_worst_rise_and_the_leverage_rule(self):
        b = self.bars.copy()
        b.loc[200, "high"] = b.close[199] * 1.25
        out = card(b, self.topix, set(), self.config)
        self.assertAlmostEqual(out["worst_rise_from_prev_close"], 0.25)
        self.assertEqual(out["rule_leverage_short"], 2.0)  # 1.25 x 25% = 31%, room at 3x is 28%, at 2x 45%

    def test_no_rule_leverage_when_nothing_fits(self):
        b = self.bars.copy()
        b.loc[200, "high"] = b.close[199] * 1.6
        self.assertTrue(np.isnan(card(b, self.topix, set(), self.config)["rule_leverage_short"]))

    def test_earnings_days_are_separated(self):
        b = self.bars.copy()
        day = b.date[200]
        b.loc[200, ["close", "high"]] = [b.close[199] * 1.1, b.close[199] * 1.1]
        out = card(b, self.topix, {day}, self.config)
        self.assertEqual(out["earnings_days"], 1)
        self.assertGreater(out["earnings_mean_abs_change"], 5 * out["other_days_mean_abs_change"])

    def test_a_card_has_no_dated_value_and_a_short_history_gets_none(self):
        self.assertTrue(all(isinstance(v, (int, float)) for v in card(self.bars, self.topix, set(), self.config).values()))
        self.assertIsNone(card(self.bars.head(MIN_DAYS - 1), self.topix, set(), self.config))


if __name__ == "__main__":
    unittest.main()
