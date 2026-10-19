"""Causal timing, IV construction and statistics for the Japan cross-market diagnostics."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.japan_crossmarket import (
    CUTOFF, DAY, HOUR, MINUTE, ROOT, Config, RealizedVariance, asset_panel, before_cutoff, benjamini_hochberg,
    clark_west, cluster_bootstrap_diff, day_number, decluster, event_study, expanding_tail, forecast_tests, iv30,
    in_sample_fit, japan_panel, oos_forecasts, oos_r2, outside_repo, overlay, path_outcomes, qlike, validate_bars,
)


def minute_bars(start_ms, closes):
    closes = np.asarray(closes, float)
    return validate_bars(pd.DataFrame({"ts": start_ms + np.arange(len(closes)) * MINUTE, "open": closes,
                                       "high": closes * 1.001, "low": closes * 0.999, "close": closes}))


def random_walk(start_ms, minutes, seed=0):
    rng = np.random.default_rng(seed)
    return minute_bars(start_ms, 100 * np.exp(np.cumsum(rng.normal(0, 0.001, minutes))))


class IvTest(unittest.TestCase):
    def test_total_variance_interpolation_and_short_expiry_exclusion(self):
        atm = pd.DataFrame({"date": ["2024-01-04"] * 3, "days_to_sq": [5, 10, 38], "atm_iv": [0.9, 0.2, 0.3]})
        row = iv30(atm, Config()).iloc[0]
        w = (30 - 10) / (38 - 10)
        expected = np.sqrt(((1 - w) * 0.04 * 10 + w * 0.09 * 38) / 30)
        self.assertAlmostEqual(row.jiv, expected)
        self.assertEqual(row.jiv_method, "interpolated")
        atm.loc[0, "atm_iv"] = 5.0  # expiries inside the 7-day exclusion never matter
        self.assertAlmostEqual(iv30(atm, Config()).jiv.iloc[0], expected)

    def test_extrapolates_after_roll_and_uses_lone_expiry(self):
        atm = pd.DataFrame({"date": ["2024-08-05"] * 3, "days_to_sq": [4, 39, 67], "atm_iv": [1.4, 0.52, 0.4]})
        row = iv30(atm, Config()).iloc[0]
        w = (30 - 39) / (67 - 39)
        self.assertAlmostEqual(row.jiv, np.sqrt(((1 - w) * 0.52 ** 2 * 39 + w * 0.4 ** 2 * 67) / 30))
        self.assertEqual((row.jiv_method, row.jiv_near_days, row.jiv_far_days), ("extrapolated", 39, 67))
        lone = iv30(atm.iloc[:2], Config()).iloc[0]
        self.assertEqual((lone.jiv, lone.jiv_method), (0.52, "single"))

    def test_japan_inputs_stop_at_research_boundary(self):
        frame = pd.DataFrame({"date": ["2026-07-31", "2026-08-01", "2026-10-19"]})
        self.assertEqual(list(before_cutoff(frame).date), ["2026-07-31"])


class JapanPanelTest(unittest.TestCase):
    def test_expanding_threshold_uses_strictly_earlier_values(self):
        values = np.r_[np.arange(1.0, 301.0), np.nan, 0.0, 400.0]
        flags, thresholds = expanding_tail(values, 0.05, True, 250)
        self.assertFalse(flags[:250].any())
        self.assertTrue(np.isnan(thresholds[300]))
        self.assertTrue(flags[301])
        self.assertAlmostEqual(thresholds[301], np.quantile(np.arange(1.0, 301.0), 0.05))
        self.assertFalse(flags[302])

    def test_vix_is_previous_us_date_and_gaps_are_not_bridged(self):
        topix = pd.DataFrame({"date": ["2024-01-04", "2024-01-05", "2024-01-15"], "close": [100.0, 101.0, 99.0]})
        jiv = pd.DataFrame({"date": topix.date, "jiv": [0.2, 0.22, 0.25]})
        vix = pd.DataFrame({"date": ["2024-01-03", "2024-01-04", "2024-01-05"], "vix": [13.0, 14.0, 15.0]})
        frame = japan_panel(topix, jiv, vix, Config())
        self.assertEqual(frame.vix.iloc[0], 13.0)  # same-date VIX is not yet closed at T
        self.assertEqual(frame.vix.iloc[1], 14.0)
        self.assertTrue(np.isnan(frame.vix.iloc[2]))  # newest VIX is 10 days old
        self.assertAlmostEqual(frame.topix_ret.iloc[1], np.log(101 / 100))
        self.assertTrue(np.isnan(frame.topix_ret.iloc[2]) and np.isnan(frame.jiv_chg.iloc[2]))
        self.assertEqual(frame.decision_ts.iloc[0], day_number(["2024-01-04"])[0] * DAY + 19 * 3_600_000)


class RealizedVolTest(unittest.TestCase):
    def test_hand_calculated_window_and_missing_bar(self):
        closes = np.full(20, 100.0)
        closes[9], closes[14] = 101.0, 99.0  # 5-minute closes at minutes 10 and 15
        bars = minute_bars(0, closes)
        rv = RealizedVariance(bars)
        expected = np.log(101 / 100) ** 2 + np.log(99 / 101) ** 2
        vol = rv.vol([5 * MINUTE], [15 * MINUTE])[0]
        self.assertAlmostEqual(vol ** 2 * (10 * MINUTE) / (365 * DAY), expected)
        self.assertTrue(np.isnan(rv.vol([0], [15 * MINUTE])[0]))  # no close at minute 0
        gapped = RealizedVariance(bars.drop(index=9))
        self.assertTrue(np.isnan(gapped.vol([5 * MINUTE], [15 * MINUTE])[0]))

    def test_features_ignore_future_bars_and_targets_ignore_past_bars(self):
        start = day_number(["2024-01-01"])[0] * DAY
        bars = random_walk(start, 40 * 1440)
        japan = japan_panel(pd.DataFrame({"date": ["2024-02-05"], "close": [1.0]}),
                            pd.DataFrame({"date": ["2024-02-05"], "jiv": [0.2]}),
                            pd.DataFrame({"date": ["2024-02-02"], "vix": [14.0]}), Config(event_min_history=1))
        t = japan.decision_ts.iloc[0]
        base = asset_panel(japan, bars, Config())
        future, past = bars.copy(), bars.copy()
        future.loc[future.ts >= t, ["open", "high", "low", "close"]] *= 3
        past.loc[past.ts < t - MINUTE, ["open", "high", "low", "close"]] *= 0.5
        features = ["log_rv1", "log_rv7", "log_rv30"]
        targets = ["target_1d", "target_3d", "fwd_ret_3d", "fwd_low_3d"]
        pd.testing.assert_frame_equal(base[features], asset_panel(japan, future, Config())[features])
        pd.testing.assert_frame_equal(base[targets], asset_panel(japan, past, Config())[targets])
        self.assertTrue(np.isfinite(base[features + targets].to_numpy()).all())


class TargetBoundaryTest(unittest.TestCase):
    def test_targets_crossing_the_cutoff_are_missing(self):
        start = CUTOFF - 35 * DAY
        bars = random_walk(start, 35 * 1440)
        dates = ["2026-07-30", "2026-07-31"]
        japan = japan_panel(pd.DataFrame({"date": dates, "close": [1.0, 1.0]}),
                            pd.DataFrame({"date": dates, "jiv": [0.2, 0.2]}),
                            pd.DataFrame({"date": ["2026-07-29"], "vix": [14.0]}), Config(event_min_history=1))
        out = asset_panel(japan, bars, Config())
        self.assertTrue(np.isfinite(out.target_1d.iloc[0]))  # 07-30 19:00 + 1d ends before the cutoff
        self.assertTrue(np.isnan(out.target_1d.iloc[1]) and out.target_3d.isna().all())
        self.assertTrue(out.fwd_ret_3d.isna().all())


class PathTest(unittest.TestCase):
    def test_entry_exit_extremes_and_cutoff(self):
        bars = minute_bars(0, [100.0, 102.0, 98.0, 105.0])
        out = path_outcomes(bars, np.array([0, 2 * MINUTE]), 2 * MINUTE)
        self.assertAlmostEqual(out.ret.iloc[0], np.log(98 / 100))
        self.assertAlmostEqual(out.low.iloc[0], 100 * 0.999 / 100 - 1)
        self.assertAlmostEqual(out.high.iloc[0], 102 * 1.001 / 100 - 1)
        self.assertTrue(np.isnan(out.ret.iloc[1]))  # exit bar missing
        late = minute_bars(CUTOFF - 3 * MINUTE, [1.0, 1.0, 1.0])
        inside, beyond = path_outcomes(late, np.array([CUTOFF - 3 * MINUTE, CUTOFF - 2 * MINUTE]), 2 * MINUTE).ret
        self.assertEqual(inside, 0.0)  # exit bar opens one minute before the cutoff
        self.assertTrue(np.isnan(beyond))


class ForecastTest(unittest.TestCase):
    def panel(self, n=400):
        rng = np.random.default_rng(1)
        x = rng.normal(size=(n, 5))
        dates = pd.date_range("2022-01-03", periods=n, freq="D").strftime("%Y-%m-%d")
        frame = pd.DataFrame(x, columns=["log_rv1", "log_rv7", "log_rv30", "log_vix", "log_jiv"])
        frame["date"], frame["decision_ts"] = dates, day_number(dates) * DAY
        frame["target_3d"] = frame.log_rv1 + 0.5 * frame.log_jiv + rng.normal(0, 0.1, n)
        return frame

    def test_training_rows_have_closed_targets(self):
        panel = self.panel()
        out = oos_forecasts(panel, 3, Config(oos_start="2022-10-01", min_train_rows=250))
        first = out.iloc[0]
        t = panel.decision_ts.to_numpy()
        self.assertEqual(first.n_train, int((t + 3 * DAY <= first.decision_ts).sum()))
        self.assertEqual(first.date, "2022-10-01")
        self.assertLess(np.mean((out.target - out.pred_MVJ) ** 2), np.mean((out.target - out.pred_MV) ** 2))
        closed = panel.target_3d[t + 3 * DAY <= first.decision_ts]
        self.assertAlmostEqual(first.pred_MEAN, closed.mean())
        fit = {row["term"]: row for row in in_sample_fit(panel, 3, Config())}
        self.assertAlmostEqual(fit["log_jiv"]["coef"], 0.5, delta=0.02)
        self.assertGreater(fit["log_jiv"]["hac_t"], 20)
        self.assertLess(abs(fit["log_vix"]["hac_t"]), 4)

    def test_loss_functions(self):
        y = np.log([0.5, 0.8])
        self.assertAlmostEqual(qlike(y, y, [0.0, 0.0]), 0.0)
        self.assertAlmostEqual(qlike([np.log(0.5)], [np.log(0.5) - np.log(2) / 2], [0.0]), 2 - np.log(2) - 1)
        self.assertAlmostEqual(oos_r2(np.array([1.0, 2.0]), np.array([0.0, 0.0]), np.array([1.0, 1.0])), 1 - 1 / 5)

    def test_family_bh_and_decision_rule(self):
        rng = np.random.default_rng(3)
        dates = pd.date_range("2023-01-02", "2026-07-30", freq="D").strftime("%Y-%m-%d")
        y = rng.normal(size=len(dates))

        def frame(big_error, shock_only=False):
            f = pd.DataFrame({"date": dates, "decision_ts": day_number(dates) * DAY, "target": y})
            noise = rng.normal(0, 1, len(dates))
            for m in ("M0", "MV", "MJ", "MEAN"):
                f[f"pred_{m}"], f[f"s2_{m}"] = y + noise, 1.0
            better = y + big_error * noise
            f["pred_MVJ"] = np.where(f.date.str.startswith("2024-08"), better, f.pred_MV) if shock_only else better
            f["s2_MVJ"] = 1.0
            return f
        tests, years = forecast_tests({("A", 1): frame(0.5), ("B", 1): frame(0.5, shock_only=True)}, Config())
        primary = tests.loc[tests.family == "primary"].set_index("asset")
        self.assertTrue(primary.supports_jiv["A"])
        self.assertFalse(primary.supports_jiv["B"])  # gains only inside the shock month
        self.assertAlmostEqual(primary.oos_r2_without_shock_month["B"], 0.0)
        self.assertEqual(primary.positive_years["A"], 4)
        secondary = tests.loc[tests.family == "secondary"]
        np.testing.assert_allclose(secondary.q_value, benjamini_hochberg(secondary.cw_p))
        self.assertEqual(set(years.year), {"2023", "2024", "2025", "2026"})

    def test_clark_west_and_bh(self):
        y = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertTrue(np.isnan(clark_west(y, y * 0, y * 0, 2)[0]))
        t, p = clark_west(y, np.zeros(6), y + np.array([.1, -.1, .1, -.1, .1, -.1]), 2)
        self.assertGreater(t, 0)
        self.assertLess(p, 0.5)
        np.testing.assert_allclose(benjamini_hochberg([0.01, 0.04, 0.03, 0.5, np.nan]),
                                   [0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5, np.nan])


class EventTest(unittest.TestCase):
    def test_decluster_and_bootstrap(self):
        ts = np.array([0, DAY, int(2.5 * DAY), 10 * DAY])
        flags = np.array([True, True, True, False])
        np.testing.assert_array_equal(decluster(ts, flags, 3 * DAY), [True, False, False, False])
        np.testing.assert_array_equal(decluster(ts, flags, DAY), [True, True, True, False])
        months = np.repeat([f"2024-{m:02d}" for m in range(1, 13)], 10)
        values = np.zeros(120)
        is_event = np.zeros(120, bool)
        is_event[::10] = True
        values[is_event] = 0.05
        low, high, p = cluster_bootstrap_diff(values, is_event, months, 500, 0)
        self.assertGreater(low, 0)
        self.assertLess(p, 0.01)
        self.assertEqual(cluster_bootstrap_diff(np.zeros(120), is_event, months, 500, 0)[2], 1.0)


class EventStudyTest(unittest.TestCase):
    def test_declustered_events_and_family_q(self):
        dates = pd.date_range("2022-01-03", periods=120, freq="D").strftime("%Y-%m-%d")
        panel = pd.DataFrame({"date": dates, "decision_ts": day_number(dates) * DAY + 19 * HOUR,
                              "topix_ret": 0.0, "jiv_chg": 0.0, "event_topix": False, "event_jiv": False})
        panel.loc[[10, 11, 50], "event_topix"] = True
        panel.loc[[20], "event_jiv"] = True
        for h in (1, 3):
            panel[f"fwd_ret_{h}d"] = np.where(panel.event_topix, 0.03, 0.0)
            panel[f"fwd_low_{h}d"] = -0.01
        summary, by_year, events = event_study({"BTCUSDT": panel}, Config(bootstrap=200))
        cell = summary.set_index(["horizon_days", "event"])
        self.assertEqual(cell.loc[(1, "event_topix"), "events"], 3)
        self.assertEqual(cell.loc[(3, "event_topix"), "events"], 2)  # day 11 overlaps day 10's window
        self.assertAlmostEqual(cell.loc[(3, "event_topix"), "diff"], 0.03 - 3 * 0.03 / 120)
        np.testing.assert_allclose(summary.q_value, benjamini_hochberg(summary.p_two_sided))
        self.assertEqual(len(events), 3 + 1 + 2 + 1)


class OverlayAndGuardTest(unittest.TestCase):
    def test_turnover_is_charged_on_every_change(self):
        bars = minute_bars(0, np.full(3 * DAY // MINUTE + 1, 100.0))
        forecasts = pd.DataFrame({"date": ["d0", "d1", "d2"], "decision_ts": [0, DAY, 2 * DAY]})
        for model in ("M0", "MV", "MVJ"):
            forecasts[f"pred_{model}"], forecasts[f"s2_{model}"] = np.log([0.5, 0.25, 0.5]), 0.0
        periods, _ = overlay(forecasts, bars, Config())
        cost = Config().one_way_cost
        np.testing.assert_allclose(periods.lev_M0, [1.0, 2.0])
        np.testing.assert_allclose(periods.net_M0, [-cost, -cost])

    def test_leverage_cap_and_costs(self):
        bars = minute_bars(0, np.r_[np.full(DAY // MINUTE, 100.0), np.full(DAY // MINUTE + 1, 110.0)])
        forecasts = pd.DataFrame({"date": ["d0", "d1"], "decision_ts": [0, DAY]})
        for model, vol in (("M0", 0.01), ("MV", 0.5), ("MVJ", 1.0)):
            forecasts[f"pred_{model}"], forecasts[f"s2_{model}"] = np.log(vol), 0.0
        periods, summary = overlay(forecasts, bars, Config())
        cost = Config().one_way_cost
        self.assertEqual(periods.lev_M0.iloc[0], 4.0)
        self.assertAlmostEqual(periods.net_M0.iloc[0], 4 * 0.1 - 4 * cost)
        self.assertAlmostEqual(periods.net_MVJ.iloc[0], 0.5 * 0.1 - 0.5 * cost)
        self.assertAlmostEqual(periods.net_constant_1x.iloc[0], 0.1 - cost)
        self.assertEqual({row["policy"] for row in summary}, {"constant_1x", "M0", "MV", "MVJ"})

    def test_repository_outputs_are_refused(self):
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "results" / "japan")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(outside_repo(Path(tmp)), Path(tmp).resolve())

    def test_config_caps_leverage(self):
        with self.assertRaises(ValueError):
            Config(max_leverage=5).validate()


if __name__ == "__main__":
    unittest.main()
