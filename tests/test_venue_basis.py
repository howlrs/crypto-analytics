"""Hand-calculated accounting and causal alignment for cross-venue diagnostics."""
import unittest

import numpy as np
import pandas as pd

from backtests.venue_basis import (
    CUTOFF, HOUR, MINUTE, Config, build_panel, event_ledger, funding_leg, prices, summarize,
)


def bars(values, start=0):
    return pd.DataFrame({"ts": np.arange(len(values)) * MINUTE + start,
                         "open": values, "close": values})


def funding(rate=0.001):
    return pd.DataFrame({"ts": [0, 8 * HOUR, 16 * HOUR], "rate": [rate] * 3,
                         "interval_hours": [8.] * 3})


class VenueBasisTest(unittest.TestCase):
    def setUp(self):
        self.config = Config(threshold_bp=50, horizons_hours=(1,), binance_fee_bp=0,
                             bybit_fee_bp=0, slippage_bp=0)

    def test_signal_close_and_strict_later_entry(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        panel, _ = build_panel(b, y, b, self.config)
        ledger = event_ledger(panel, b, y, funding(), funding(), self.config)
        self.assertEqual(panel.signal_ts.iloc[0], MINUTE)
        self.assertEqual(ledger.entry_ts.iloc[0], 2 * MINUTE)
        self.assertEqual(ledger.exit_ts.iloc[0], 62 * MINUTE)
        self.assertEqual(ledger.status.iloc[0], "complete")
        self.assertEqual(ledger.net_pnl.iloc[0], 0)

    def test_convergence_quantity_denominator_and_sign(self):
        for sign in (1, -1):
            b, y = bars([100.] * 70), bars([100. + 2 * sign] * 70)
            y.loc[y.ts >= 62 * MINUTE, ["open", "close"]] = 100.
            panel, _ = build_panel(b, y, b, self.config)
            row = event_ledger(panel, b, y, funding(), funding(), self.config).iloc[0]
            self.assertEqual(row.binance_direction, sign)
            self.assertEqual(row.bybit_direction, -sign)
            self.assertEqual(row.price_pnl, 2.)
            self.assertAlmostEqual(row.net_ret_on_gross_notional_bp, 20_000 / (200 + 2 * sign))

    def test_divergence_loses_and_costs_reduce_pnl(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        y.loc[y.ts >= 62 * MINUTE, ["open", "close"]] = 104.
        config = Config(threshold_bp=50, horizons_hours=(1,), quantity=3,
                        binance_fee_bp=5, bybit_fee_bp=10, slippage_bp=2)
        panel, _ = build_panel(b, y, b, config)
        row = event_ledger(panel, b, y, funding(), funding(), config).iloc[0]
        self.assertEqual(row.price_pnl, -6.)
        self.assertEqual(row.entry_gross_notional, 606.)
        self.assertAlmostEqual(row.cost, 3 * (200 * 7 + 206 * 12) / 10_000)
        self.assertLess(row.net_pnl, row.price_pnl)

    def test_funding_boundary_sign_proxy_and_missing(self):
        pf = prices(bars([100.] * (16 * 60 + 1)))
        # Entry settlement is included, exit settlement is excluded.
        pnl, state, count = funding_leg(funding(), pf, 8 * HOUR, 9 * HOUR, 1, 2, 8)
        self.assertAlmostEqual(pnl, -.2)
        self.assertEqual(count, 1)
        self.assertIn("proxy", state)
        pnl, _, count = funding_leg(funding(), pf, 7 * HOUR, 8 * HOUR, -1, 2, 8)
        self.assertEqual(pnl, 0)
        self.assertEqual(count, 0)
        pnl, _, _ = funding_leg(funding(), pf, 8 * HOUR, 9 * HOUR, -1, 2, 8)
        self.assertAlmostEqual(pnl, .2)
        pnl, state, _ = funding_leg(funding().drop(index=1), pf, 8 * HOUR, 9 * HOUR, 1, 2, 8)
        self.assertTrue(np.isnan(pnl))
        self.assertIn("missing", state)
        pf.loc[8 * HOUR, "open"] = np.nan
        pnl, state, _ = funding_leg(funding(), pf, 8 * HOUR, 9 * HOUR, 1, 2, 8)
        self.assertTrue(np.isnan(pnl))
        self.assertEqual(state, "settlement_price_missing")

    def test_future_signal_invariance_and_open_only_entry(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        panel, _ = build_panel(b, y, b, self.config)
        changed = y.copy()
        changed.loc[changed.ts >= 20 * MINUTE, "close"] = 200.
        later, _ = build_panel(b, changed, b, self.config)
        signal_columns = ["ts", "signal_ts", "basis_bp", "extreme", "episode_start"]
        pd.testing.assert_frame_equal(panel.loc[panel.ts < 20 * MINUTE, signal_columns],
                                      later.loc[later.ts < 20 * MINUTE, signal_columns])
        # An invalid *future close* in the entry bar cannot change its open selection.
        y.loc[y.ts == 2 * MINUTE, "close"] = np.nan
        altered_panel, _ = build_panel(b, y, b, self.config)
        row = event_ledger(altered_panel, b, y, funding(), funding(), self.config).iloc[0]
        self.assertEqual(row.entry_ts, 2 * MINUTE)

    def test_funding_timestamp_jitter_keeps_actual_boundary(self):
        pf = prices(bars([100.] * (16 * 60 + 1)))
        shifted = funding()
        shifted.ts += 2
        pnl, state, count = funding_leg(shifted, pf, 8 * HOUR, 9 * HOUR, 1, 2, 8)
        self.assertAlmostEqual(pnl, -.2)
        self.assertEqual(count, 1)
        pnl, _, count = funding_leg(shifted, pf, 7 * HOUR, 8 * HOUR, 1, 2, 8)
        self.assertEqual((pnl, count), (0., 0))
        shifted.ts += 2_000
        pnl, state, _ = funding_leg(shifted, pf, 8 * HOUR, 9 * HOUR, 1, 2, 8)
        self.assertTrue(np.isnan(pnl))

    def test_asynchronous_gaps_no_stale_price_and_wait_reason(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        y = y[~y.ts.isin([2 * MINUTE, 3 * MINUTE])]
        panel, coverage = build_panel(b, y, b, self.config)
        self.assertEqual(coverage["missing_or_invalid_bybit_close"], 2)
        self.assertNotIn(2 * MINUTE, panel.ts.to_list())
        row = event_ledger(panel, b, y, funding(), funding(), self.config).iloc[0]
        self.assertEqual(row.status, "entry_missing_or_wait_exceeded")
        self.assertTrue(np.isnan(row.entry_ts))

    def test_empty_panels_incomplete_bar_and_historical_guard(self):
        b, y = bars([100.]), bars([102.])
        panel, coverage = build_panel(b, y, b, self.config, end_ts=30_000)
        self.assertTrue(panel.empty)
        self.assertEqual(coverage["excluded_incomplete_closes"], 1)
        ledger = event_ledger(panel, b, y, funding(), funding(), self.config)
        self.assertEqual(summarize(panel, ledger)["episodes"], 0)
        with self.assertRaises(ValueError):
            build_panel(b, y, b, self.config, CUTOFF + 1)

    def test_missing_funding_never_becomes_known_zero(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        panel, _ = build_panel(b, y, b, self.config)
        ledger = event_ledger(panel, b, y, funding().iloc[:0], funding(), self.config)
        self.assertEqual(ledger.status.iloc[0], "funding_unknown")
        self.assertTrue(np.isnan(ledger.net_pnl.iloc[0]))
        self.assertEqual(summarize(panel, ledger)["events"][0]["complete_events"], 0)

    def test_spot_reference_missing_is_audited_without_substitution(self):
        b, y = bars([100.] * 70), bars([102.] * 70)
        panel, coverage = build_panel(b, y, b.iloc[:0], self.config)
        self.assertEqual(coverage["missing_spot_reference"], 70)
        self.assertTrue(panel.binance_spot_basis_bp.isna().all())
        self.assertTrue(panel.basis_bp.notna().all())

    def test_configured_horizon_in_panel_and_summary(self):
        b, y = bars([100.] * 200), bars([102.] * 200)
        config = Config(horizons_hours=(2,))
        panel, _ = build_panel(b, y, b, config)
        self.assertIn("basis_change_2h_bp", panel)
        ledger = event_ledger(panel, b, y, funding(), funding(), config)
        result = summarize(panel, ledger)
        self.assertIn("basis_change_2h_bp_quantiles", result)
        self.assertNotIn("basis_change_1h_bp_quantiles", result)


if __name__ == "__main__":
    unittest.main()
