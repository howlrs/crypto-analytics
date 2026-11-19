"""Offline causality and failure-closed coverage for descriptive early warnings."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from live_signals.early_warning import (
    MINUTE_MS, MarketSeries, MinuteBar, ReferenceLevels, WarningConfig, evaluate,
)


def series(*, count=120, market="spot", buy=50.0):
    return MarketSeries("fixture", market, "BTC", [
        MinuteBar(i * MINUTE_MS, 100, 101, 99, 100, 100, buy,
                  (i + 1) * MINUTE_MS + 500) for i in range(count)])


def fixed(support=98, resistance=102, known_at=0):
    return ReferenceLevels("fixture", "BTC", support, resistance, known_at)


class EarlyWarningTests(unittest.TestCase):
    def run_snapshot(self, spot=None, clock_ms=None, **kwargs):
        kwargs.setdefault("as_of_ms", 120 * MINUTE_MS + 1000)
        kwargs.setdefault("generated_at_ms", kwargs["as_of_ms"])
        kwargs.setdefault("reference", fixed())
        # Explicit clock simulation for historical synthetic fixtures; the
        # production engine reads its wall clock, never a CLI override.
        with patch("live_signals.early_warning.time.time_ns", return_value=(
                kwargs["generated_at_ms"] if clock_ms is None else clock_ms) * 1_000_000):
            return evaluate(spot or series(), **kwargs)

    def names(self, result):
        return {item["name"] for item in result["flags"]}

    def test_sell_resilience_is_watch_not_breakout(self):
        result = self.run_snapshot(series(buy=30))
        self.assertEqual(result["state"], "early_watch")
        self.assertIn("sell_resilience", self.names(result))
        self.assertFalse(result["confirmation"]["price_confirmed"])
        self.assertTrue(result["usable_for_live"])

    def test_perp_receipts_after_candle_end_before_decision_are_usable(self):
        result = self.run_snapshot(perp=series(market="perp", buy=20))
        self.assertTrue(result["sources"]["perp"]["synchronized"])
        self.assertEqual(result["state"], "early_watch")

    def test_future_candles_cannot_change_past_result(self):
        original = series(count=150)
        mutated = replace(original, bars=[replace(bar, close=float("nan"), low=-1)
                          if bar.ts >= 120 * MINUTE_MS else bar for bar in original.bars])
        self.assertEqual(self.run_snapshot(original), self.run_snapshot(mutated))

    def test_in_progress_candle_is_ignored(self):
        result = self.run_snapshot(series(count=121), as_of_ms=120 * MINUTE_MS + 30_000)
        self.assertEqual(result["sources"]["spot"]["last_complete_end_ms"], 120 * MINUTE_MS)

    def test_later_receipt_not_available_historically(self):
        original = series()
        delayed = replace(original, bars=[replace(bar, received_ts=150 * MINUTE_MS) for bar in original.bars])
        observed = self.run_snapshot(delayed)
        replay = self.run_snapshot(delayed, mode="market_time_replay")
        self.assertEqual(observed["state"], "insufficient_data")
        self.assertEqual(replay["state"], "neutral")
        self.assertFalse(replay["usable_for_live"])
        self.assertEqual(replay["publishable_state"], "historical_replay")

    def test_missing_receipt_not_available_in_observed_mode(self):
        data = series()
        data = replace(data, bars=[replace(bar, received_ts=None) for bar in data.bars])
        self.assertEqual(self.run_snapshot(data)["state"], "insufficient_data")

    def test_generation_time_staleness_blocks_live_publication(self):
        result = self.run_snapshot(series(buy=20), generated_at_ms=123 * MINUTE_MS)
        self.assertEqual(result["state"], "early_watch")
        self.assertEqual(result["publishable_state"], "stale_data")
        self.assertFalse(result["usable_for_live"])

    def test_backdated_generation_cannot_bypass_actual_clock(self):
        result = self.run_snapshot(clock_ms=200 * MINUTE_MS)
        self.assertLess(result["freshness"]["price_age_seconds"], 120)
        self.assertFalse(result["usable_for_live"])
        self.assertEqual(result["publishable_state"], "stale_data")

    def test_future_declared_time_cannot_enable_live_publication(self):
        result = self.run_snapshot(clock_ms=119 * MINUTE_MS)
        self.assertFalse(result["usable_for_live"])
        self.assertEqual(result["publishable_state"], "invalid_clock")

    def test_reference_expiring_between_decision_and_publication(self):
        result = self.run_snapshot(reference=replace(fixed(), expires_at=120 * MINUTE_MS + 30_000),
                                   generated_at_ms=121 * MINUTE_MS)
        self.assertTrue(result["freshness"]["within_limit"])
        self.assertFalse(result["usable_for_live"])
        self.assertEqual(result["publishable_state"], "expired_reference")

    def test_gap_fails_closed(self):
        data = series()
        data = replace(data, bars=[bar for bar in data.bars if bar.ts != 100 * MINUTE_MS])
        self.assertEqual(self.run_snapshot(data)["state"], "insufficient_data")

    def test_stale_perp_cannot_supply_flow_flag(self):
        result = self.run_snapshot(perp=series(count=118, market="perp", buy=20))
        self.assertFalse(result["sources"]["perp"]["synchronized"])
        self.assertNotIn("perp", result["metrics"])
        self.assertEqual(result["state"], "neutral")

    def test_unknown_taker_flow_is_not_zero(self):
        data = series(buy=None)
        result = self.run_snapshot(data)
        self.assertIsNone(result["metrics"]["spot"]["short"]["taker_imbalance"])
        self.assertEqual(result["metrics"]["spot"]["short"]["flow_status"], "unknown")
        self.assertNotIn("sell_resilience", self.names(result))

    def test_three_support_reclaim_closes(self):
        data = series()
        bars = list(data.bars)
        bars[-4] = replace(bars[-4], low=97, close=97)
        result = self.run_snapshot(replace(data, bars=bars))
        self.assertIn("support_reclaimed", self.names(result))
        bars[-2] = replace(bars[-2], low=97, close=97)
        self.assertNotIn("support_reclaimed", self.names(self.run_snapshot(replace(data, bars=bars))))

    def test_reclaim_before_level_known_is_not_counted(self):
        data = series()
        bars = list(data.bars)
        bars[-4] = replace(bars[-4], low=97, close=97)
        result = self.run_snapshot(replace(data, bars=bars), reference=fixed(known_at=117 * MINUTE_MS))
        self.assertNotIn("support_reclaimed", self.names(result))

    def test_support_loss_overrides_positive_perp(self):
        result = self.run_snapshot(perp=series(market="perp", buy=20), reference=fixed(support=100.5))
        self.assertEqual(result["state"], "support_lost")

    def breakout_series(self, volume=200):
        data = series()
        return replace(data, bars=[replace(bar, open=103, high=104, low=102.5, close=103,
                                          quote_volume=volume, taker_buy_quote=volume / 2)
                                  if bar.ts >= 110 * MINUTE_MS else bar for bar in data.bars])

    def test_two_completed_blocks_and_volume_evidence(self):
        result = self.run_snapshot(self.breakout_series())
        self.assertEqual(result["state"], "price_breakout_confirmed")
        self.assertTrue(result["confirmation"]["volume_confirmed"])
        self.assertEqual(len(result["confirmation"]["bars"]), 2)

    def test_one_completed_block_does_not_confirm(self):
        result = self.run_snapshot(self.breakout_series(), as_of_ms=115 * MINUTE_MS + 1000)
        self.assertFalse(result["confirmation"]["price_confirmed"])

    def test_wick_only_does_not_confirm(self):
        data = series()
        data = replace(data, bars=[replace(bar, high=110) for bar in data.bars])
        self.assertFalse(self.run_snapshot(data)["confirmation"]["price_confirmed"])

    def test_price_confirmation_without_volume_not_hidden(self):
        result = self.run_snapshot(self.breakout_series(volume=100))
        self.assertEqual(result["state"], "price_breakout_confirmed")
        self.assertFalse(result["confirmation"]["volume_confirmed"])

    def test_unknown_volume_not_confirmed(self):
        data = self.breakout_series()
        data = replace(data, bars=[replace(bar, quote_volume=None, taker_buy_quote=None) for bar in data.bars])
        result = self.run_snapshot(data)
        self.assertFalse(result["confirmation"]["volume_confirmed"])
        self.assertIsNone(result["confirmation"]["bars"][-1]["volume_ratio"])

    def test_price_returning_below_confirmed_resistance_is_faded(self):
        data = self.breakout_series()
        extra = MinuteBar(120 * MINUTE_MS, 103, 104, 100, 100, 100, 50, 121 * MINUTE_MS)
        result = self.run_snapshot(replace(data, bars=[*data.bars, extra]), as_of_ms=121 * MINUTE_MS)
        self.assertEqual(result["state"], "breakout_faded")

    def test_reference_known_after_closes_cannot_confirm(self):
        result = self.run_snapshot(self.breakout_series(), reference=fixed(known_at=118 * MINUTE_MS))
        self.assertFalse(result["confirmation"]["price_confirmed"])

    def test_rolling_reference_excludes_confirmation_blocks(self):
        result = self.run_snapshot(self.breakout_series(), reference=None, mode="market_time_replay")
        self.assertEqual(result["reference"]["resistance"], 101)
        self.assertEqual(result["reference"]["known_at"], 110 * MINUTE_MS)
        self.assertTrue(result["confirmation"]["price_confirmed"])

    def test_rolling_reference_does_not_backdate_bulk_receipt(self):
        data = self.breakout_series()
        data = replace(data, bars=[replace(bar, received_ts=120 * MINUTE_MS + 500) for bar in data.bars])
        result = self.run_snapshot(data, reference=None)
        self.assertEqual(result["reference"]["known_at"], 120 * MINUTE_MS + 500)
        self.assertFalse(result["confirmation"]["price_confirmed"])

    def test_compression_near_resistance(self):
        data = series()
        data = replace(data, bars=[replace(bar, high=100.2, low=99.8) if bar.ts >= 105 * MINUTE_MS
                                   else bar for bar in data.bars])
        result = self.run_snapshot(data, reference=fixed(resistance=100.5))
        self.assertIn("compression_near_resistance", self.names(result))
        self.assertAlmostEqual(result["metrics"]["setup"]["compression_ratio"], .2)

    def test_invalid_inputs_rejected(self):
        for kwargs in ({"mode": "live"}, {"generated_at_ms": 1},
                       {"reference": replace(fixed(), venue="different")},
                       {"reference": fixed(known_at=200 * MINUTE_MS)},
                       {"reference": replace(fixed(), expires_at=120 * MINUTE_MS)},
                       {"perp": replace(series(market="perp"), symbol="ETH")},
                       {"config": WarningConfig(short_minutes=17)},
                       {"config": WarningConfig(pressure_imbalance=float("nan"))}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_snapshot(**kwargs)

    def test_invalid_bar_values_rejected(self):
        for changes in ({"close": float("nan")}, {"low": 105}, {"quote_volume": -1},
                        {"taker_buy_quote": 101}, {"received_ts": 0}):
            data = series()
            data = replace(data, bars=[*data.bars[:-1], replace(data.bars[-1], **changes)])
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_snapshot(data)

    def test_duplicate_bars_rejected(self):
        data = series()
        with self.assertRaises(ValueError):
            self.run_snapshot(replace(data, bars=[*data.bars, data.bars[-1]]))

    def test_flat_rolling_reference_fails_closed(self):
        data = series()
        data = replace(data, bars=[replace(bar, high=100, low=100) for bar in data.bars])
        self.assertEqual(self.run_snapshot(data, reference=None)["state"], "insufficient_data")

    def test_configuration_hash_reproducible_and_sensitive(self):
        a = self.run_snapshot()
        b = self.run_snapshot(config=WarningConfig(pressure_imbalance=.2))
        self.assertEqual(a["config_sha256"], self.run_snapshot()["config_sha256"])
        self.assertNotEqual(a["config_sha256"], b["config_sha256"])


if __name__ == "__main__":
    unittest.main()
