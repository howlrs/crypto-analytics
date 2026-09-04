import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.strategy_robustness import (benjamini_hochberg, benjamini_yekutieli, cluster_bootstrap,
                                           embargo_split, greedy_purge, join_prior_day_regime, load_strategies,
                                           normalise_events, _adjust)


class StrategyRobustnessTests(unittest.TestCase):
    def test_overlap_purge_is_greedy(self):
        e = pd.DataFrame({"strategy": ["x"] * 3, "detect_ts": [1, 2, 3], "entry_ts": [1, 2, 5], "exit_ts": [4, 6, 7], "net_bp": [1, 2, 3]})
        kept, audit = greedy_purge(e)
        self.assertEqual(kept.entry_ts.tolist(), [1, 5]); self.assertEqual(int(audit.events_purged.iloc[0]), 1)

    def test_prior_day_regime_join(self):
        e = pd.DataFrame({"detect_ts": [pd.Timestamp("2025-01-02T12:00Z").value // 1_000_000], "entry_ts": [1], "exit_ts": [2], "net_bp": [1.]})
        daily = pd.DataFrame({"date": ["2025-01-01"], "symbol": ["BTCUSDT"], "regime": ["uptrend_low_vol"]})
        out = join_prior_day_regime(e, daily, "BTCUSDT")
        self.assertEqual(out.regime.iloc[0], "uptrend_low_vol"); self.assertEqual(out.trend.iloc[0], "uptrend")

    def test_cluster_bootstrap_is_deterministic_and_detects_positive_signal(self):
        x = np.repeat(50., 24); m = np.repeat(["2024-01", "2024-02"], 12)
        a = cluster_bootstrap(x, m, 300, 7); b = cluster_bootstrap(x, m, 300, 7)
        self.assertEqual(a, b); self.assertLess(a["p_positive"], .01); self.assertGreater(a["ci_low_bp"], 0)

    def test_cluster_bootstrap_rejects_nonpositive_draw_count_and_negative_mean(self):
        with self.assertRaises(ValueError):
            cluster_bootstrap([1.], ["2024-01"], 0, 7)
        negative = cluster_bootstrap(np.repeat(-50., 24), np.repeat(["2024-01", "2024-02"], 12), 300, 7)
        self.assertGreater(negative["p_positive"], .99)

    def test_by_is_no_less_conservative_than_bh(self):
        p = [.001, .03, .2, np.nan]
        self.assertTrue(np.all(benjamini_yekutieli(p)[:3] >= benjamini_hochberg(p)[:3]))

    def test_embargo_split(self):
        day = 86_400_000; start = pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000
        e = pd.DataFrame({"detect_ts": [start - 4*day, start - 1*day, start + 3*day, start + 365*day], "entry_ts": [start - 4*day, start-day, start+3*day, start+365*day], "exit_ts": [start-3*day, start+day, start+4*day, start+366*day], "net_bp": [1.]*4})
        train, test = embargo_split(e, 2025, 2*day, "2023-01-01")
        self.assertEqual(len(train), 1); self.assertEqual(len(test), 1)

    def test_embargo_boundary_is_strict_for_train_and_year_end(self):
        day = 86_400_000
        start = pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000
        end = pd.Timestamp("2026-01-01", tz="UTC").value // 1_000_000
        horizon = 2 * day
        e = pd.DataFrame({
            "detect_ts": [start - horizon - day, start - horizon - day, start + horizon, end - day],
            "entry_ts": [start - horizon - day, start - horizon - day, start + horizon, end - day],
            "exit_ts": [start - horizon, start - horizon - 1, start + horizon + day, end],
            "net_bp": [1.] * 4,
        })
        train, test = embargo_split(e, 2025, horizon, "2023-01-01")
        self.assertEqual(train.index.tolist(), [1])
        self.assertEqual(test.index.tolist(), [2])

    def test_empty_events_are_auditable(self):
        e = pd.DataFrame({"strategy": pd.Series(dtype=str), "detect_ts": pd.Series(dtype="int64"),
                          "entry_ts": pd.Series(dtype="int64"), "exit_ts": pd.Series(dtype="int64"), "net_bp": pd.Series(dtype=float)})
        kept, audit = greedy_purge(e, "empty")
        self.assertTrue(kept.empty); self.assertEqual(audit.strategy.iloc[0], "empty")

    def test_empty_source_stays_in_inventory_but_not_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            liq = root / "liq_reversion"
            liq.mkdir()
            pd.DataFrame({"variant": ["btc_long_ret-5_oinone_entry0_exit4h"]}).to_csv(liq / "summary.csv", index=False)
            pd.DataFrame(columns=["detect_ts", "entry_ts", "exit_ts", "net_ret_bp"]).to_csv(
                liq / "events_btc_long_ret-5_oinone_entry0_exit4h.csv", index=False)
            daily = pd.DataFrame({"date": ["2025-01-01"], "symbol": ["BTCUSDT"],
                                  "regime": ["uptrend_low_vol"]})
            strategies, inventory = load_strategies(root, daily)
            self.assertEqual(strategies, [])
            self.assertEqual(len(inventory), 1)
            self.assertEqual(int(inventory.events_kept.iloc[0]), 0)

    def test_selection_q_uses_train_not_test_p(self):
        base = pd.DataFrame({"fold_year": [2025, 2025], "family": ["a", "b"], "train_eligible": [True, True],
                             "test_eligible": [True, True], "train_p_positive": [.001, .9], "test_p_positive": [.9, .001],
                             "train_mean_bp": [2., 2.]})
        a = _adjust(base, "train_p_positive", "train_eligible", "train", ["fold_year"])
        b = _adjust(base.assign(test_p_positive=[.001, .9]), "train_p_positive", "train_eligible", "train", ["fold_year"])
        self.assertTrue(np.allclose(a.train_by_q, b.train_by_q, equal_nan=True))
        self.assertTrue(np.allclose(a.train_by_q, benjamini_yekutieli(base.train_p_positive)))
        self.assertTrue(np.allclose(a.train_family_by_q, base.train_p_positive))
        self.assertTrue((a.train_mean_bp.gt(0) & a.train_by_q.le(.1)).iloc[0])

    def test_timing_validation(self):
        bad = pd.DataFrame({"ts": [2], "entry_ts": [1], "exit_ts": [3], "net_bp": [1.]})
        with self.assertRaises(ValueError): normalise_events(bad, "bad", "family")

    def test_nonfinite_event_is_rejected_instead_of_silently_dropped(self):
        bad = pd.DataFrame({"ts": [1], "entry_ts": [2], "exit_ts": [3], "net_bp": [np.nan]})
        with self.assertRaises(ValueError):
            normalise_events(bad, "bad", "family")


if __name__ == "__main__": unittest.main()
