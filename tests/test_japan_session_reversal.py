"""Window timing, event definition and one-sided bootstrap for the held-out session-reversal check."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.japan_crossmarket import DAY, HOUR, day_number
from backtests.japan_session_reversal import Config, evaluate, load_csv_opens, topix_events, window_returns


def opens_for(dates, entry, exit_):
    days = day_number(dates)
    ts = np.r_[days * DAY + 8 * HOUR, days * DAY + 19 * HOUR]
    return pd.DataFrame({"ts": ts, "open": np.r_[entry, exit_]}).sort_values("ts").reset_index(drop=True)


class SessionReversalTest(unittest.TestCase):
    def test_window_uses_entry_and_exit_opens_only(self):
        events = topix_events(pd.DataFrame({"date": ["2018-02-05", "2018-02-06"], "close": [100.0, 95.0]}),
                              Config(event_min_history=1))
        opens = opens_for(["2018-02-05", "2018-02-06"], [100.0, 200.0], [101.0, 210.0])
        out = window_returns(events, opens, "2018-02-01", "2018-02-28", Config())
        np.testing.assert_allclose(out.window_ret, np.log([1.01, 1.05]))
        missing = window_returns(events, opens.iloc[1:], "2018-02-01", "2018-02-28", Config())
        self.assertEqual(list(missing.date), ["2018-02-06"])
        outside = window_returns(events, opens, "2018-02-06", "2018-02-06", Config())
        self.assertEqual(list(outside.date), ["2018-02-06"])

    def test_events_need_prior_history(self):
        closes = np.r_[np.linspace(100, 130, 300), 120.0]
        dates = pd.bdate_range("2016-10-03", periods=301).strftime("%Y-%m-%d")
        events = topix_events(pd.DataFrame({"date": dates, "close": closes}), Config())
        self.assertFalse(events.event.iloc[:251].any())
        self.assertTrue(events.event.iloc[-1])

    def test_one_sided_bootstrap_and_costs(self):
        dates = np.repeat([f"2019-{m:02d}-15" for m in range(1, 13)], 5)
        frame = pd.DataFrame({"date": dates, "window_ret": 0.0, "event": False})
        frame.loc[::5, ["window_ret", "event"]] = [0.02, True]
        result = evaluate(frame, Config(bootstrap=500), seed=1)
        self.assertEqual(result["events"], 12)
        self.assertAlmostEqual(result["diff"], 0.02 - 0.02 / 5)
        self.assertLess(result["p_greater"], 0.01)
        self.assertAlmostEqual(result["event_mean_after_round_trip"], 0.02 - 0.0014)
        frame.loc[frame.event, "window_ret"] = -0.02
        self.assertGreater(evaluate(frame, Config(bootstrap=500), seed=1)["p_greater"], 0.99)

    def test_csv_loader_normalises_microseconds_drops_off_grid_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.csv").write_text("1735689600000000,1.0\n1735689660000000,2.0\n1735689720799,3.0\n")
            frame, hashes, off_grid = load_csv_opens(Path(tmp))
            self.assertEqual(list(frame.ts), [1735689600000, 1735689660000])
            self.assertEqual((set(hashes), off_grid), ({"a.csv"}, 1))
            Path(tmp, "b.csv").write_text("1735689600000,1.0\n")
            with self.assertRaises(ValueError):
                load_csv_opens(Path(tmp))

    def test_periods_must_not_overlap(self):
        with self.assertRaises(ValueError):
            Config(holdout=("2017-08-17", "2021-06-30")).validate()


if __name__ == "__main__":
    unittest.main()
