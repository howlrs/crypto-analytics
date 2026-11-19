from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import pandas as pd

from backtests import propagation_lag as pl

M = pl.MINUTE
T0 = 1_700_000_040_000  # a UTC minute boundary


def _frame(closes: list[float], opens: list[float] | None = None, start: int = T0) -> pd.DataFrame:
    opens = opens if opens is not None else [closes[0]] + closes[:-1]
    return pl.prices(pd.DataFrame({"ts": [start + i * M for i in range(len(closes))], "open": opens, "close": closes}))


def _config(**overrides) -> pl.Config:
    settings = dict(window_minutes=2, thresholds_bp=(100.0,), refractory_minutes=3, entry_delays_minutes=(0, 1),
                    horizons_minutes=(1, 2), primary_threshold_bp=100.0, primary_delay_minutes=1,
                    primary_horizon_minutes=2, bootstrap_samples=200, seed=7, min_events=2, min_months=1)
    settings.update(overrides)
    return pl.Config(**settings)


class DetectEventsTest(unittest.TestCase):
    def test_threshold_is_inclusive_and_decision_is_the_bar_close(self) -> None:
        # bar 2 closes exactly +100bp above bar 0; bar 3 would also qualify but is inside the refractory period
        leader = _frame([100.0, 100.0, 101.0, 101.5, 101.5, 101.5, 101.5])
        events = pl.detect_events(leader, _config(), 100.0, T0, T0 + 100 * M)
        self.assertEqual(events.bar_ts.tolist(), [T0 + 2 * M])
        self.assertEqual(events.decision_ts.tolist(), [T0 + 3 * M])
        self.assertEqual(events.direction.tolist(), [1])
        self.assertAlmostEqual(events.leader_window_bp.iloc[0], 100.0)

    def test_down_move_and_refractory_release(self) -> None:
        closes = [100.0, 100.0, 98.0] + [98.0] * 4 + [96.0] + [96.0] * 3
        events = pl.detect_events(_frame(closes), _config(), 100.0, T0, T0 + 100 * M)
        # first event decided at bar 2 close; bars 3 are blocked until decision + 3 minutes; bar 7 fires again
        self.assertEqual(events.direction.tolist(), [-1, -1])
        self.assertEqual(events.bar_ts.tolist(), [T0 + 2 * M, T0 + 7 * M])

    def test_later_prices_do_not_change_an_earlier_event(self) -> None:
        base = [100.0, 100.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0]
        changed = base[:3] + [50.0, 200.0, 1.0, 300.0, 5.0]
        first = pl.detect_events(_frame(base), _config(), 100.0, T0, T0 + 100 * M).iloc[0]
        second = pl.detect_events(_frame(changed), _config(), 100.0, T0, T0 + 100 * M).iloc[0]
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_a_missing_close_inside_the_window_prevents_an_event(self) -> None:
        raw = pd.DataFrame({"ts": [T0, T0 + 2 * M, T0 + 3 * M], "open": [100.0, 101.0, 101.0],
                            "close": [100.0, 101.0, 101.0]})  # bar 1 is absent
        events = pl.detect_events(pl.prices(raw), _config(), 100.0, T0, T0 + 100 * M)
        self.assertTrue(events.empty)

    def test_refractory_clock_does_not_depend_on_the_requested_start(self) -> None:
        closes = [100.0, 100.0, 101.0, 102.1, 102.1, 102.1, 102.1]
        leader = _frame(closes)
        everything = pl.detect_events(leader, _config(), 100.0, T0, T0 + 100 * M)
        later_start = pl.detect_events(leader, _config(), 100.0, T0 + 4 * M, T0 + 100 * M)
        self.assertEqual(everything.bar_ts.tolist(), [T0 + 2 * M])
        self.assertTrue(later_start.empty)  # bar 3 stays blocked by the event before the start


class OutcomesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.leader = _frame([100.0, 100.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0],
                             opens=[100.0, 100.0, 100.0, 101.0, 101.2, 101.4, 101.6, 101.8])
        self.follower_item = pl.Instrument("X", "v", "perp", "XUSDT", fee_bp=5.0, slippage_bp=2.0)
        self.follower = _frame([50.0, 50.0, 50.1, 50.2, 50.4, 50.6, 50.8, 51.0],
                               opens=[50.0, 50.0, 50.0, 50.0, 50.5, 51.0, 50.0, 50.0])
        self.events = pl.detect_events(self.leader, _config(), 100.0, T0, T0 + 100 * M)

    def test_entry_exit_cost_and_leader_control_are_hand_computable(self) -> None:
        ledger = pl.outcomes(self.events, self.leader, self.follower, self.follower_item, _config(), 100.0,
                             end_ts=T0 + 100 * M)
        row = ledger[(ledger.entry_delay_min == 0) & (ledger.horizon_min == 1)].iloc[0]
        self.assertEqual((row.entry_ts, row.exit_ts), (T0 + 3 * M, T0 + 4 * M))
        self.assertAlmostEqual(row.gross_bp, (50.5 / 50.0 - 1) * 10_000)          # +100bp
        self.assertAlmostEqual(row.net_bp, 100.0 - 14.0)
        self.assertAlmostEqual(row.leader_same_interval_bp, (101.2 / 101.0 - 1) * 10_000)
        self.assertAlmostEqual(row.follower_window_bp, (50.1 / 50.0 - 1) * 10_000)
        delayed = ledger[(ledger.entry_delay_min == 1) & (ledger.horizon_min == 2)].iloc[0]
        self.assertEqual((delayed.entry_ts, delayed.exit_ts), (T0 + 4 * M, T0 + 6 * M))
        self.assertAlmostEqual(delayed.gross_bp, (50.0 / 50.5 - 1) * 10_000)

    def test_down_event_signs_the_follower_return(self) -> None:
        leader = _frame([100.0, 100.0, 99.0, 99.0, 99.0, 99.0])
        follower = _frame([50.0] * 6, opens=[50.0, 50.0, 50.0, 50.0, 49.5, 49.5])
        events = pl.detect_events(leader, _config(), 100.0, T0, T0 + 100 * M)
        ledger = pl.outcomes(events, leader, follower, self.follower_item, _config(), 100.0, end_ts=T0 + 100 * M)
        row = ledger[(ledger.entry_delay_min == 0) & (ledger.horizon_min == 1)].iloc[0]
        self.assertAlmostEqual(row.gross_bp, 100.0)  # the follower fell 1% after a down event

    def test_missing_bar_and_boundary_are_kept_as_incomplete_rows(self) -> None:
        follower = self.follower.drop(index=T0 + 4 * M)
        ledger = pl.outcomes(self.events, self.leader, follower, self.follower_item, _config(), 100.0,
                             end_ts=T0 + 5 * M)
        reasons = ledger.set_index(["entry_delay_min", "horizon_min"]).reason.to_dict()
        self.assertEqual(reasons[(0, 1)], "missing_follower_bar")           # exit bar removed
        self.assertEqual(reasons[(0, 2)], "exit_at_or_after_boundary")      # exit == boundary
        self.assertEqual(reasons[(1, 1)], "exit_at_or_after_boundary")
        self.assertFalse(ledger.complete.any())
        self.assertTrue(ledger.net_bp.isna().all())

    def test_end_after_the_research_boundary_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pl.outcomes(self.events, self.leader, self.follower, self.follower_item, _config(), 100.0,
                        end_ts=pl.CUTOFF + M)


class SummaryTest(unittest.TestCase):
    def _ledger(self, nets: list[float], months: list[str]) -> pd.DataFrame:
        stamps = [int(pd.Timestamp(f"{month}-15", tz="UTC").timestamp() * 1000) + i * M
                  for i, month in enumerate(months)]
        return pd.DataFrame({"follower": "X", "threshold_bp": 100.0, "entry_delay_min": 1, "horizon_min": 2,
                             "decision_ts": stamps, "direction": [1, -1] * (len(nets) // 2) + [1] * (len(nets) % 2),
                             "gross_bp": [value + 14 for value in nets], "net_bp": nets,
                             "leader_same_interval_bp": 0.0, "follower_window_bp": 10.0, "leader_window_bp": 100.0,
                             "complete": True})

    def test_pooled_cell_is_primary_and_reproducible(self) -> None:
        ledger = self._ledger([10.0, 20.0, 30.0, 40.0], ["2025-01", "2025-01", "2025-02", "2025-02"])
        first = pl.summarize(ledger, _config())
        second = pl.summarize(ledger, _config())
        pd.testing.assert_frame_equal(first, second)
        pooled = first[first.direction == "both"].iloc[0]
        self.assertTrue(pooled.primary)
        self.assertEqual(pooled.complete_events, 4)
        self.assertAlmostEqual(pooled.mean_net_bp, 25.0)
        self.assertAlmostEqual(pooled.mean_gross_bp, 39.0)
        self.assertEqual(pooled.status, "evaluated")
        self.assertGreater(pooled.net_ci95_low_bp, 0)       # every month mean is positive
        self.assertLess(pooled.p_one_sided_net_positive, 0.05)
        self.assertFalse(first[first.direction != "both"].primary.any())

    def test_small_or_single_month_samples_are_not_evaluated(self) -> None:
        ledger = self._ledger([10.0, 20.0, 30.0, 40.0], ["2025-01"] * 4)
        summary = pl.summarize(ledger, _config(min_months=2))
        self.assertEqual(set(summary.status), {"insufficient_sample"})
        self.assertTrue(summary.p_one_sided_net_positive.isna().all())
        self.assertTrue(summary.bh_q_all_cells.isna().all())
        self.assertAlmostEqual(summary[summary.direction == "both"].mean_net_bp.iloc[0], 25.0)

    def test_bh_matches_hand_values(self) -> None:
        q = pl._bh(pd.Series([0.01, 0.04, 0.03, np.nan]))
        self.assertAlmostEqual(q.iloc[0], 0.03)
        self.assertAlmostEqual(q.iloc[1], 0.04)
        self.assertAlmostEqual(q.iloc[2], 0.04)
        self.assertTrue(np.isnan(q.iloc[3]))


class RunTest(unittest.TestCase):
    def _db(self, directory: Path) -> Path:
        db = directory / "market.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE klines (venue TEXT, market TEXT, symbol TEXT, ts INTEGER, open REAL, "
                         "high REAL, low REAL, close REAL)")
            start = int(pd.Timestamp("2025-01-10", tz="UTC").timestamp() * 1000)
            for i in range(180):
                leader_close = 100.0 if i < 100 else 102.0  # +200bp, clear of every threshold
                follower_open = 50.0 if i <= 101 else 50.0 + 0.01 * (i - 101)
                ts = start + i * M
                conn.execute("INSERT INTO klines VALUES ('binance','perp','BTCUSDT',?,?,?,?,?)",
                             (ts, leader_close, leader_close, leader_close, leader_close))
                conn.execute("INSERT INTO klines VALUES ('binance','perp','ETHUSDT',?,?,?,?,?)",
                             (ts, follower_open, follower_open, follower_open, follower_open))
        return db

    def test_run_writes_outputs_and_refuses_bad_periods_or_used_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            db = self._db(directory)
            start = int(pd.Timestamp("2025-01-10", tz="UTC").timestamp() * 1000)
            end = start + 180 * M
            followers = (pl.Instrument("ETH", "binance", "perp", "ETHUSDT"),)
            config = pl.Config(bootstrap_samples=50)
            result = pl.run(db, directory / "out", start, end, config, followers=followers)
            self.assertEqual(result["events_by_threshold"], {"50.0": 1, "100.0": 1, "150.0": 1})
            manifest = json.loads((directory / "out" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["outputs_sha256"]), {"events.csv", "outcomes.csv", "summary.csv"})
            self.assertEqual(manifest["research_boundary_ts"], pl.CUTOFF)
            ledger = pd.read_csv(directory / "out" / "outcomes.csv")
            row = ledger[(ledger.threshold_bp == 100.0) & (ledger.entry_delay_min == 1) & (ledger.horizon_min == 15)].iloc[0]
            # decision at bar 101 open; delay 1 -> entry at bar 102 (50.01), exit at bar 117 (50.16)
            self.assertAlmostEqual(row.gross_bp, (50.16 / 50.01 - 1) * 10_000, places=6)
            with self.assertRaises(ValueError):
                pl.run(db, directory / "out", start, end, config, followers=followers)       # not empty
            with self.assertRaises(ValueError):
                pl.run(db, directory / "late", start, pl.CUTOFF + M, config, followers=followers)

    def test_follower_without_history_before_an_event_is_not_scored_for_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            db = self._db(directory)
            start = int(pd.Timestamp("2025-01-10", tz="UTC").timestamp() * 1000)
            with sqlite3.connect(db) as conn:  # the follower's history begins after the leader's move
                conn.execute("DELETE FROM klines WHERE symbol='ETHUSDT' AND ts < ?", (start + 100 * M,))
            followers = (pl.Instrument("ETH", "binance", "perp", "ETHUSDT"),)
            pl.run(db, directory / "out", start, start + 180 * M, pl.Config(bootstrap_samples=50), followers=followers)
            self.assertEqual((directory / "out" / "outcomes.csv").read_text(encoding="utf-8").strip(), "")


if __name__ == "__main__":
    unittest.main()
