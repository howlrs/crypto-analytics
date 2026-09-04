"""Unit tests for the causal market-regime analysis helpers."""

from __future__ import annotations

import sqlite3
import unittest

import numpy as np
import pandas as pd

from backtests.market_regimes import (
    DAY_MS,
    RegimeConfig,
    benjamini_hochberg,
    bootstrap_regime_means,
    build_regime_frame,
    calendar_block_indices,
    cross_asset_dependence,
    load_daily_close,
    max_drawdown,
    newey_west_slope,
    transition_table,
)


class MarketRegimesTest(unittest.TestCase):
    def test_load_daily_close_uses_last_bar_in_each_utc_day(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, close REAL)"
        )
        day_zero = int(pd.Timestamp("2025-01-01", tz="UTC").timestamp() * 1000)
        conn.executemany(
            "INSERT INTO klines VALUES (?, ?, ?, ?, ?)",
            [
                ("binance", "perp", "BTCUSDT", day_zero + 1_000, 100.0),
                ("binance", "perp", "BTCUSDT", day_zero + DAY_MS - 60_000, 101.0),
                ("binance", "perp", "BTCUSDT", day_zero + 2 * DAY_MS - 60_000, 102.0),
                ("binance", "perp", "BTCUSDT", day_zero + 2 * DAY_MS + 10_000, 103.0),
                ("binance", "perp", "ETHUSDT", day_zero + DAY_MS - 60_000, 999.0),
            ],
        )

        close = load_daily_close(conn, "binance", "perp", "BTCUSDT")

        expected_dates = pd.to_datetime([day_zero, day_zero + DAY_MS], unit="ms", utc=True)
        expected_dates.name = "date"
        pd.testing.assert_index_equal(close.index, expected_dates)
        np.testing.assert_allclose(close.to_numpy(), [101.0, 102.0])

    def test_build_regime_frame_is_causal_before_future_change(self) -> None:
        dates = pd.date_range("2025-01-01", periods=30, freq="D", tz="UTC")
        returns = 0.002 + 0.01 * np.sin(np.arange(29))
        close = pd.Series(100.0 * np.r_[1.0, np.cumprod(1.0 + returns)], index=dates)
        changed = close.copy()
        cutoff = dates[20]
        changed.loc[dates[21:]] *= 3.0
        config = RegimeConfig(
            trend_days=3,
            vol_days=3,
            threshold_lookback_days=12,
            threshold_min_history_days=10,
            bootstrap_samples=10,
            min_observations=3,
        )

        original = build_regime_frame(close, "BTCUSDT", config)
        revised = build_regime_frame(changed, "BTCUSDT", config)
        causal_columns = [
            "date",
            "symbol",
            "close",
            "return_1d",
            "trend_return",
            "realized_vol_annualized",
            "causal_vol_threshold",
            "regime",
        ]
        original_before_cutoff = original.loc[original["date"] <= cutoff, causal_columns]
        revised_before_cutoff = revised.loc[revised["date"] <= cutoff, causal_columns]

        pd.testing.assert_frame_equal(original_before_cutoff, revised_before_cutoff)

    def test_benjamini_hochberg_matches_known_adjustment(self) -> None:
        adjusted = benjamini_hochberg([0.01, 0.04, 0.03, 0.20, np.nan])

        np.testing.assert_allclose(adjusted[:4], [0.04, 0.05333333333333334, 0.05333333333333334, 0.20])
        self.assertTrue(np.isnan(adjusted[4]))

    def test_block_bootstrap_is_seeded_and_detects_regime_signal(self) -> None:
        labels = ["signal", "control"]
        regimes = np.array(labels * 60, dtype=object)
        residual = np.tile([-0.002, -0.001, 0.001, 0.002], 30)
        returns = residual + np.where(regimes == "signal", 0.015, 0.0)

        first = bootstrap_regime_means(
            regimes, returns, labels, samples=1_000, block_size=7, rng=np.random.default_rng(42)
        )
        second = bootstrap_regime_means(
            regimes, returns, labels, samples=1_000, block_size=7, rng=np.random.default_rng(42)
        )

        self.assertEqual(first, second)
        self.assertLess(first["signal"]["p_value"], 0.01)
        self.assertGreater(first["signal"]["ci_low"], 0.01)

    def test_calendar_blocks_preserve_contiguous_segments(self) -> None:
        dates = pd.to_datetime(
            [
                "2025-01-01",
                "2025-01-02",
                "2025-01-03",
                "2025-01-04",
                "2025-01-05",
                "2025-01-06",
                "2025-02-01",
                "2025-02-02",
                "2025-02-03",
                "2025-02-04",
                "2025-02-05",
                "2025-02-06",
            ],
            utc=True,
        )

        sampled = calendar_block_indices(dates, block_size=3, rng=np.random.default_rng(9))

        segment_by_position = np.array([0] * 6 + [1] * 6)
        for block in sampled.reshape(-1, 3):
            self.assertEqual(len(set(segment_by_position[block])), 1)
            local = block % 6
            np.testing.assert_array_equal((np.diff(local) % 6), [1, 1])

    def test_transition_table_does_not_bridge_missing_calendar_days(self) -> None:
        frame = pd.DataFrame(
            {
                "symbol": ["BTCUSDT"] * 4,
                "date": pd.to_datetime(
                    ["2025-01-01", "2025-01-02", "2025-01-04", "2025-01-05"], utc=True
                ),
                "regime": ["uptrend_low_vol", "uptrend_high_vol", "downtrend_low_vol", "downtrend_low_vol"],
            }
        )

        transitions = transition_table(frame)
        observed = {
            (row.from_regime, row.to_regime, row.count)
            for row in transitions.itertuples(index=False)
        }

        self.assertEqual(
            observed,
            {
                ("uptrend_low_vol", "uptrend_high_vol", 1),
                ("downtrend_low_vol", "downtrend_low_vol", 1),
            },
        )

    def test_newey_west_slope_recovers_linear_relationship(self) -> None:
        x = np.arange(100, dtype=float)
        y = 1.5 + 2.0 * x + 0.25 * np.sin(x)

        slope, standard_error, p_value = newey_west_slope(x, y, max_lag=5)

        self.assertAlmostEqual(slope, np.polyfit(x, y, 1)[0], places=12)
        self.assertGreater(standard_error, 0.0)
        self.assertLess(p_value, 1e-20)

    def test_cross_asset_returns_share_endpoints_and_use_prior_regime(self) -> None:
        dates = pd.date_range("2025-01-01", periods=8, freq="D", tz="UTC")
        close_panel = pd.DataFrame(
            {
                "BTC": [100, 102, 101, 103, 104, 108, 107, 110],
                "SP500": [np.nan, 200, 201, np.nan, np.nan, 198, 202, 204],
            },
            index=dates,
        )
        regime_frame = pd.DataFrame(
            {
                "date": dates,
                "symbol": "BTC",
                "regime": "uptrend_low_vol",
            }
        )
        config = RegimeConfig(
            threshold_min_history_days=10,
            bootstrap_samples=10,
            min_observations=3,
        )

        result = cross_asset_dependence(close_panel, regime_frame, ["BTC"], config)
        conditional = result[result["regime"] == "uptrend_low_vol"].iloc[0]

        self.assertEqual(conditional["n_intervals"], 4)
        self.assertEqual(conditional["regime_information_lag_calendar_days"], 1)
        self.assertEqual(conditional["median_return_horizon_calendar_days"], 1)
        self.assertEqual(conditional["max_return_horizon_calendar_days"], 3)

    def test_max_drawdown_is_zero_for_monotonic_gain(self) -> None:
        self.assertEqual(max_drawdown([0.01, 0.02, 0.03]), 0.0)


if __name__ == "__main__":
    unittest.main()
