import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from backtests.position_ev import (
    CUTOFF_EXCLUSIVE_TS, HOUR_MS, MINUTE_MS, FundingSeries, PositionPlan, build_grid, compute_features,
    by_year, decision_indices, delta_bootstrap, evaluate_decision, main, non_overlapping, parse_condition,
    parse_time,
)


def grid_from(rows, base_ts=0):
    """rows: (open, high, low, close); ``None`` leaves the minute missing."""
    records = [(base_ts + i * MINUTE_MS, *row) for i, row in enumerate(rows) if row is not None]
    return build_grid(pd.DataFrame(records, columns=["ts", "open", "high", "low", "close"]))


def flat(n, px=100.0):
    return [(px, px, px, px)] * n


def plan(**overrides):
    values = dict(venue="binance", market="perp", symbol="BTCUSDT", side="long", horizon_min=3,
                  taker_fee_bp=5.0, maker_fee_bp=2.0, slippage_bp=2.0, end_ms=10**13)
    values.update(overrides)
    return PositionPlan(**values)


def minute_funding(first, last, rate=0.0001):
    ts = [k * MINUTE_MS + 2 for k in range(first, last + 1)]
    return FundingSeries.from_frame(pd.DataFrame({"ts": ts, "rate": rate, "interval_hours": 1 / 60}))


class OutcomeTests(unittest.TestCase):
    def test_time_exit_charges_costs_and_funding_held_over_entry_to_exit(self):
        grid = grid_from(flat(4) + [(101, 101, 101, 101), (101, 101, 101, 101)])
        got = evaluate_decision(grid, minute_funding(-1, 6), plan(), 1, limit=False)
        # Entry open at minute 1, exit open at minute 4: settlements at minutes 1, 2, 3 (+2ms).
        self.assertEqual(got["exit_kind_pess"], "time")
        self.assertEqual(got["settlements_pess"], 3)
        self.assertAlmostEqual(got["gross_pess_bp"], 100.0)
        self.assertAlmostEqual(got["funding_pess_bp"], 3.0)
        self.assertAlmostEqual(got["net_pess_bp"], 100.0 - 14.0 - 3.0)
        self.assertFalse(got["ambiguous"])

    def test_short_pays_negative_funding_sign_and_stops_at_level(self):
        rows = [(100, 101, 99, 100), (100, 102.5, 99.5, 101)] + flat(4)
        got = evaluate_decision(grid_from(rows), None, plan(side="short", horizon_min=5, stop_bp=200), 0, limit=False)
        self.assertEqual(got["exit_kind_pess"], "stop")
        self.assertAlmostEqual(got["gross_pess_bp"], -200.0)
        self.assertAlmostEqual(got["net_pess_bp"], -214.0)
        self.assertEqual(got["exit_ts_pess"], 2 * MINUTE_MS)  # intrabar exit: end of bar 1
        funded = evaluate_decision(grid_from(flat(6)), minute_funding(-1, 8), plan(side="short", horizon_min=2), 0,
                                   limit=False)
        self.assertAlmostEqual(funded["funding_pess_bp"], -2.0)  # shorts receive positive funding

    def test_gap_through_stop_fills_at_open_not_stop(self):
        rows = [(100, 100, 100, 100), (95, 96, 94, 95)] + flat(4, 95)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=5, stop_bp=200), 0, limit=False)
        self.assertEqual(got["exit_kind_pess"], "stop")
        self.assertAlmostEqual(got["gross_pess_bp"], -500.0)
        self.assertEqual(got["exit_ts_pess"], MINUTE_MS)

    def test_take_gap_fills_at_resting_limit_with_maker_fee(self):
        rows = [(100, 100, 100, 100), (105, 106, 104, 105)] + flat(4, 105)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=5, take_bp=300), 0, limit=False)
        self.assertEqual(got["exit_kind_pess"], "take")
        self.assertAlmostEqual(got["gross_pess_bp"], 300.0)
        self.assertAlmostEqual(got["net_pess_bp"], 300.0 - 7.0 - 2.0)

    def test_bar_spanning_both_barriers_is_bracketed(self):
        rows = [(100, 100, 100, 100), (100, 104, 97, 100)] + flat(4)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=5, stop_bp=200, take_bp=300), 0, limit=False)
        self.assertEqual((got["exit_kind_pess"], got["exit_kind_opt"]), ("stop", "take"))
        self.assertAlmostEqual(got["net_pess_bp"], -214.0)
        self.assertAlmostEqual(got["net_opt_bp"], 291.0)
        self.assertTrue(got["ambiguous"])

    def test_exit_bar_high_low_close_and_later_bars_do_not_matter(self):
        base = [(100, 100, 100, 100)] * 3
        one = evaluate_decision(grid_from(base + [(101, 101, 101, 101)]), None, plan(stop_bp=500, take_bp=500), 0,
                                limit=False)
        two = evaluate_decision(grid_from(base + [(101, 999, 1, 50), (1, 999, 1, 1)]), None,
                                plan(stop_bp=500, take_bp=500), 0, limit=False)
        for key in ("exit_kind_pess", "net_pess_bp", "net_opt_bp", "path_mae_bp", "path_mfe_bp"):
            self.assertEqual(one[key], two[key])


class LimitEntryTests(unittest.TestCase):
    def test_fill_bar_take_is_uncertain_but_later_path_is_certain(self):
        rows = [(100, 104, 98.9, 99.5)] + flat(7, 99.5)
        got = evaluate_decision(grid_from(rows), None,
                                plan(horizon_min=4, stop_bp=300, take_bp=300, entry_offset_bp=100,
                                     entry_window_min=3), 0, limit=True)
        self.assertAlmostEqual(got["entry_px"], 99.0)
        self.assertEqual(got["exit_kind_pess"], "time")
        self.assertEqual(got["exit_kind_opt"], "take")
        self.assertAlmostEqual(got["net_pess_bp"], (99.5 / 99 - 1) * 10_000 - 2.0 - 7.0)
        self.assertAlmostEqual(got["net_opt_bp"], 300.0 - 4.0)
        self.assertTrue(got["ambiguous"])
        self.assertEqual(got["path_mfe_bp"], max(0.0, (99.5 / 99 - 1) * 10_000))  # fill-bar high excluded

    def test_fill_bar_stop_is_certain(self):
        rows = [(100, 100, 95, 96)] + flat(7, 96)
        got = evaluate_decision(grid_from(rows), None,
                                plan(horizon_min=4, stop_bp=300, take_bp=300, entry_offset_bp=100,
                                     entry_window_min=3), 0, limit=True)
        self.assertEqual((got["exit_kind_pess"], got["exit_kind_opt"]), ("stop", "stop"))
        self.assertAlmostEqual(got["net_pess_bp"], -300.0 - 2.0 - 7.0)

    def test_touching_without_trading_through_is_unfilled(self):
        rows = [(100, 100, 99, 99.5)] * 3 + flat(5, 99.5)
        p = plan(horizon_min=2, entry_offset_bp=100, entry_window_min=3)
        self.assertEqual(evaluate_decision(grid_from(rows), None, p, 0, limit=True)["status"], "unfilled")
        filled = evaluate_decision(grid_from(rows), None, plan(horizon_min=2, entry_offset_bp=100, entry_window_min=3,
                                                                fill_through_bp=0.0), 0, limit=True)
        self.assertEqual(filled["status"], "complete")

    def test_missing_window_bar_before_fill_is_unknown(self):
        rows = [(100, 100, 100, 100), None, (100, 100, 98, 98)] + flat(5, 98)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=2, entry_offset_bp=100, entry_window_min=3),
                                0, limit=True)
        self.assertEqual((got["status"], got["reason"]), ("unknown", "missing_bar_in_plan_window"))

    def test_short_limit_fills_above_the_decision_open(self):
        rows = [(100, 101.2, 99.8, 101)] + flat(7, 100.5)
        got = evaluate_decision(grid_from(rows), None,
                                plan(side="short", horizon_min=4, stop_bp=300, take_bp=300, entry_offset_bp=100,
                                     entry_window_min=3), 0, limit=True)
        self.assertAlmostEqual(got["entry_px"], 101.0)
        self.assertEqual((got["exit_kind_pess"], got["exit_kind_opt"]), ("time", "time"))
        self.assertAlmostEqual(got["net_pess_bp"], -(100.5 / 101 - 1) * 10_000 - 2.0 - 7.0)


class RiskAndCoverageTests(unittest.TestCase):
    def test_liquidation_precedes_a_wider_stop(self):
        rows = [(100, 100, 100, 100), (90, 90, 74, 80)] + flat(4, 80)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=5, stop_bp=3000, leverage=4.0), 0, limit=False)
        self.assertEqual(got["exit_kind_pess"], "liquidation")
        self.assertAlmostEqual(got["net_pess_bp"], -2500.0 - 7.0)
        self.assertEqual(got["equity_pess_pct"], -100.0)

    def test_missing_path_bar_and_funding_gap_are_unknown_not_zero(self):
        rows = [(100, 100, 100, 100), None] + flat(4)
        self.assertEqual(evaluate_decision(grid_from(rows), None, plan(), 0, limit=False)["reason"],
                         "missing_bar_in_plan_window")
        gappy = FundingSeries.from_frame(pd.DataFrame({
            "ts": [-MINUTE_MS + 2, 2, 3 * MINUTE_MS + 2, 4 * MINUTE_MS + 2, 5 * MINUTE_MS + 2],
            "rate": 0.0001, "interval_hours": 1 / 60}))
        got = evaluate_decision(grid_from(flat(6)), gappy, plan(), 0, limit=False)
        self.assertEqual((got["status"], got["reason"]), ("unknown", "funding_gap"))
        short_series = minute_funding(-1, 1)
        got = evaluate_decision(grid_from(flat(6)), short_series, plan(), 0, limit=False)
        self.assertEqual(got["reason"], "funding_out_of_range")

    def test_gap_beyond_stop_and_liquidation_is_a_liquidation(self):
        rows = [(100, 100, 100, 100), (85, 86, 84, 85)] + flat(4, 85)
        got = evaluate_decision(grid_from(rows), None, plan(horizon_min=5, stop_bp=900, leverage=10.0), 0,
                                limit=False)
        self.assertEqual(got["exit_kind_pess"], "liquidation")
        self.assertAlmostEqual(got["net_pess_bp"], -1000.0 - 7.0)

    def test_funding_coverage_does_not_depend_on_how_the_trade_exits(self):
        ts = [k * MINUTE_MS + 2 for k in range(-1, 11) if k not in (3, 4, 5)]
        gappy = FundingSeries.from_frame(pd.DataFrame({"ts": ts, "rate": 0.0001, "interval_hours": 1 / 60}))
        early_stop = [(100, 100, 100, 100), (95, 96, 94, 95)] + flat(5, 95)
        liquidated = [(100, 100, 100, 100), (90, 90, 74, 80)] + flat(5, 80)
        for rows, p in ((flat(7), plan(horizon_min=5)), (early_stop, plan(horizon_min=5, stop_bp=200)),
                        (liquidated, plan(horizon_min=5, leverage=4.0))):
            got = evaluate_decision(grid_from(rows), gappy, p, 0, limit=False)
            self.assertEqual((got["status"], got["reason"]), ("unknown", "funding_gap"))

    def test_funding_tail_is_covered_only_until_the_next_settlement_could_fall_due(self):
        series = FundingSeries.from_frame(pd.DataFrame({"ts": [0, 8 * HOUR_MS], "rate": 0.0001,
                                                        "interval_hours": 8.0}))
        self.assertEqual(series.coverage(9 * HOUR_MS, 16 * HOUR_MS), "")
        self.assertEqual(series.coverage(9 * HOUR_MS, 16 * HOUR_MS + 1), "funding_out_of_range")
        self.assertEqual(list(series.held(0, 16 * HOUR_MS)), [0, 1])

    def test_plan_validation_rejects_research_boundary_and_bad_risk(self):
        with self.assertRaisesRegex(ValueError, "research boundary"):
            plan(end_ms=CUTOFF_EXCLUSIVE_TS + MINUTE_MS).validate()
        with self.assertRaisesRegex(ValueError, "spot"):
            plan(market="spot", side="short", end_ms=CUTOFF_EXCLUSIVE_TS).validate()
        with self.assertRaisesRegex(ValueError, "maintenance"):
            plan(leverage=20, maintenance_margin_rate=0.06, end_ms=CUTOFF_EXCLUSIVE_TS).validate()
        with self.assertRaisesRegex(ValueError, "window"):
            plan(entry_offset_bp=50, end_ms=CUTOFF_EXCLUSIVE_TS).validate()


class FeatureAndGridTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 46_000)))
        opens = np.r_[100, close[:-1]]
        self.rows = [(o, max(o, c) * 1.0005, min(o, c) * 0.9995, c) for o, c in zip(opens, close)]

    def test_features_ignore_the_decision_bar_and_everything_after(self):
        decision = np.array([44_000])
        before = compute_features(grid_from(self.rows), decision, None)
        changed = self.rows[:44_000] + [(1e6, 2e6, 1, 5e5)] * (len(self.rows) - 44_000)
        after = compute_features(grid_from(changed), decision, None)
        pd.testing.assert_frame_equal(before, after)
        self.assertTrue(before.drop(columns="funding_8h_bp").notna().all(axis=None))

    def test_funding_feature_uses_only_settled_rates(self):
        series = FundingSeries.from_frame(pd.DataFrame({"ts": [0, 8 * HOUR_MS + 2], "rate": [0.0001, 0.0009],
                                                        "interval_hours": 8.0}))
        got = series.latest_rate_8h_bp(np.array([8 * HOUR_MS, 8 * HOUR_MS + 2, 30 * HOUR_MS]))
        self.assertAlmostEqual(got[0], 1.0)
        self.assertAlmostEqual(got[1], 9.0)
        self.assertTrue(np.isnan(got[2]))  # stale beyond 1.5 intervals
        hourly = FundingSeries.from_frame(pd.DataFrame({"ts": [0], "rate": [0.0000125], "interval_hours": [1.0]}))
        self.assertAlmostEqual(hourly.latest_rate_8h_bp(np.array([1]))[0], 1.0)

    def test_decision_grid_keeps_latest_possible_exit_before_end(self):
        grid = grid_from(self.rows)
        end = grid.ts(45_000)
        p = plan(horizon_min=600, entry_offset_bp=50, entry_window_min=120, end_ms=end, start_ms=MINUTE_MS)
        idx = decision_indices(grid, p)
        latest_exit = grid.ts(int(idx[-1])) + p.max_exit_offset_min * MINUTE_MS
        self.assertLess(latest_exit, end)
        self.assertGreaterEqual(latest_exit + 60 * MINUTE_MS, end - MINUTE_MS)
        self.assertTrue(((grid.ts(0) + idx * MINUTE_MS) % HOUR_MS == 0).all())

    def test_time_parser_does_not_read_years_as_milliseconds(self):
        self.assertEqual(parse_time("2023"), int(pd.Timestamp("2023-01-01", tz="UTC").value // 1_000_000))
        self.assertEqual(parse_time("1700000000000"), 1_700_000_000_000)
        self.assertEqual(parse_time("2026-08-01T09:00:00+09:00"), CUTOFF_EXCLUSIVE_TS)

    def test_non_overlap_and_yearly_tables(self):
        ledger = pd.DataFrame({
            "decision_ts": [0, 1, 2, 3], "entry_ts": [0, 10, 20, 30], "exit_ts_pess": [25, 30, 40, 50],
            "month": ["2024-12", "2024-12", "2025-01", "2025-01"],
            "status": ["complete", "complete", "unfilled", "complete"],
            "net_pess_bp": [10.0, -5.0, np.nan, 20.0], "decision_net_pess_bp": [10.0, -5.0, 0.0, 20.0],
            "decision_net_opt_bp": [10.0, -5.0, 0.0, 20.0], "exit_kind_pess": ["take", "stop", None, "time"],
            "features_known": True, "matched": [True, False, True, True]})
        kept = non_overlapping(ledger[ledger.status.eq("complete")])
        self.assertEqual(list(kept.entry_ts), [0, 30])
        yearly = by_year(ledger, [parse_condition("ret_4h_bp>0")]).set_index(["subset", "year"])
        self.assertAlmostEqual(yearly.loc[("baseline", "2025"), "ev_per_decision_pess_bp"], 10.0)
        self.assertEqual(yearly.loc[("conditional", "2024"), "trades"], 1)

    def test_condition_parser(self):
        self.assertEqual(parse_condition(" ret_24h_bp >= -2.5e2").value, -250.0)
        for bad in ("ret_24h_bp=1", "unknown>1", "ret_24h_bp>nan"):
            with self.assertRaises(ValueError):
                parse_condition(bad)

    def test_delta_bootstrap_of_identical_subset_is_zero(self):
        frame = pd.DataFrame({"month": np.repeat(["2025-01", "2025-02", "2025-03"], 4),
                              "v": np.arange(12, dtype=float)})
        lo, hi, dropped = delta_bootstrap(frame, frame, "v", 200, 1)
        self.assertEqual((lo, hi, dropped), (0.0, 0.0, 0))


class EndToEndTests(unittest.TestCase):
    def _db(self, root: Path) -> Path:
        db = root / "market.db"
        base = 1_700_000_000_000 - 1_700_000_000_000 % (8 * HOUR_MS)
        minutes = np.arange(3 * 24 * 60)
        px = 100 + 2 * np.sin(minutes / 180)
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE klines (venue, market, symbol, ts, open, high, low, close)")
            con.execute("CREATE TABLE funding (venue, symbol, ts, rate, interval_hours)")
            for market in ("perp", "spot"):
                con.executemany(f"INSERT INTO klines VALUES ('binance','{market}','BTCUSDT',?,?,?,?,?)",
                                [(int(base + m * MINUTE_MS), p, p * 1.001, p * 0.999, p)
                                 for m, p in zip(minutes, px)])
            con.executemany("INSERT INTO funding VALUES ('binance','BTCUSDT',?,?,8.0)",
                            [(int(base + k * 8 * HOUR_MS + 2), 0.0001) for k in range(-1, 11)])
        return db, base

    def test_cli_writes_reproducible_outputs_and_keeps_db_read_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, base = self._db(root)
            before = db.stat().st_mtime_ns
            out = root / "out"
            args = ["--db", str(db), "--output-dir", str(out), "--venue", "binance", "--symbol", "BTCUSDT",
                    "--side", "long", "--horizon-hours", "2", "--stop-bp", "150", "--take-bp", "150",
                    "--start", str(base + 24 * HOUR_MS), "--where", "ret_4h_bp>0", "--bootstrap-samples", "50"]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual(db.stat().st_mtime_ns, before)
            summary = pd.read_csv(out / "position_summary.csv")
            self.assertEqual(list(summary.subset), ["baseline", "conditional", "conditional_minus_baseline"])
            ledger = pd.read_csv(out / "position_ledger.csv")
            self.assertTrue(ledger.status.eq("complete").all())
            self.assertTrue((ledger.settlements_pess >= 0).all())
            self.assertEqual(int(summary.trades.iloc[0]), len(ledger))
            manifest = json.loads((out / "position_manifest.json").read_text())
            self.assertEqual(manifest["conditions"], ["ret_4h_bp>0.0"])
            self.assertEqual(manifest["plan"]["funding_venue"], "binance")
            self.assertEqual(set(manifest["output_sha256"]), {"position_ledger.csv", "position_summary.csv",
                                                              "position_by_year.csv",
                                                              "position_feature_quantiles.csv"})
            with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
                main(args)  # non-empty output directory
            with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
                main(args[:3] + [str(root / "late")] + args[4:] + ["--end", "2026-08-02"])

    def test_spot_plan_has_no_funding_and_rejects_shorts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db, base = self._db(root)
            args = ["--db", str(db), "--venue", "binance", "--market", "spot", "--symbol", "BTCUSDT",
                    "--horizon-hours", "2", "--start", str(base + 24 * HOUR_MS), "--bootstrap-samples", "20"]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args + ["--side", "long", "--output-dir", str(root / "spot")]), 0)
            manifest = json.loads((root / "spot" / "position_manifest.json").read_text())
            self.assertIsNone(manifest["plan"]["funding_venue"])
            ledger = pd.read_csv(root / "spot" / "position_ledger.csv")
            self.assertTrue(ledger.funding_pess_bp.eq(0).all())
            with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
                main(args + ["--side", "short", "--output-dir", str(root / "short")])


if __name__ == "__main__":
    unittest.main()
