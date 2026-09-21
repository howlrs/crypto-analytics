import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stderr
from io import StringIO

import numpy as np
import pandas as pd

from backtests.event_paths import MINUTE_MS, SUMMARY_COLUMNS, analyze_path, audit_file, direction_for, main, write_outputs


def bars(rows):
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])


class EventPathTests(unittest.TestCase):
    def _db_with_simple_path(self, root: Path) -> Path:
        db = root / "market.db"
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS klines (venue, market, symbol, ts, open, high, low, close)")
            con.executemany(
                "INSERT INTO klines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [("binance", "perp", "BTCUSDT", 0, 100, 101, 99, 100),
                 ("binance", "perp", "BTCUSDT", MINUTE_MS, 101, 101, 101, 101)],
            )
        return db

    def test_short_excursions_use_linear_original_notional_math(self):
        data = bars([[0, 100, 110, 94, 105], [MINUTE_MS, 100, 100, 100, 100]])
        got = analyze_path(data, entry_ts=0, exit_ts=MINUTE_MS, direction="short", stop_bp=2_000, take_bp=2_000)
        self.assertAlmostEqual(got["mae_bp"], -1_000.0)
        self.assertAlmostEqual(got["mfe_bp"], 600.0)
        self.assertEqual(got["underwater_minutes"], 1)

    def test_terminal_exit_open_can_set_extrema_and_gap_barrier(self):
        data = bars([[0, 100, 102, 99, 101], [MINUTE_MS, 85, 1, 999, 500]])
        got = analyze_path(data, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=1_000, take_bp=1_000)
        self.assertAlmostEqual(got["mae_bp"], -1_500.0)
        self.assertEqual(got["mae_ts"], MINUTE_MS)
        self.assertEqual(got["first_barrier_hit"], "stop_open")
        self.assertEqual(got["barrier_ts"], MINUTE_MS)

    def test_baseline_clips_mae_to_zero(self):
        data = bars([[0, 100, 105, 100, 102], [MINUTE_MS, 103, 1, 999, 10]])
        got = analyze_path(data, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=5_000, take_bp=5_000)
        self.assertEqual(got["mae_bp"], 0.0)
        self.assertEqual(got["mae_ts"], 0)

    def test_exit_hlc_and_post_exit_bars_cannot_change_metrics(self):
        normal = bars([[0, 100, 103, 98, 99], [MINUTE_MS, 101, 101, 101, 101]])
        noisy = bars([[0, 100, 103, 98, 99], [MINUTE_MS, 101, 1_000_000, 0.001, -999], [2 * MINUTE_MS, 1, 1_000_000, 0.001, 1]])
        first = analyze_path(normal, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=5_000, take_bp=5_000)
        second = analyze_path(noisy, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=5_000, take_bp=5_000)
        for key in ("mae_bp", "mae_ts", "mfe_bp", "mfe_ts", "first_barrier_hit", "barrier_ts"):
            if pd.isna(first[key]):
                self.assertTrue(pd.isna(second[key]))
            else:
                self.assertEqual(first[key], second[key])

    def test_prior_unambiguous_hit_is_retained_before_terminal_gap(self):
        data = bars([[0, 100, 100, 100, 100], [MINUTE_MS, 100, 111, 99, 100], [2 * MINUTE_MS, 80, 1, 999, 1]])
        got = analyze_path(data, entry_ts=0, exit_ts=2 * MINUTE_MS, direction="long", stop_bp=1_000, take_bp=1_000)
        self.assertEqual(got["first_barrier_hit"], "take_intrabar")
        self.assertEqual(got["barrier_ts"], MINUTE_MS)

    def test_same_minute_two_barriers_is_explicitly_ambiguous(self):
        data = bars([[0, 100, 100, 100, 100], [MINUTE_MS, 100, 111, 89, 100], [2 * MINUTE_MS, 100, 1, 999, 1]])
        got = analyze_path(data, entry_ts=0, exit_ts=2 * MINUTE_MS, direction="long", stop_bp=1_000, take_bp=1_000)
        self.assertEqual(got["first_barrier_hit"], "ambiguous")
        self.assertEqual(got["ambiguous_bar_ts"], MINUTE_MS)

    def test_missing_and_bad_ohlc_paths_are_rejected(self):
        missing = bars([[0, 100, 100, 100, 100], [2 * MINUTE_MS, 100, 100, 100, 100]])
        self.assertEqual(analyze_path(missing, entry_ts=0, exit_ts=2 * MINUTE_MS, direction="long", stop_bp=100, take_bp=100)["reason"], "missing_path_bar")
        inf = bars([[0, 100, np.inf, 99, 100], [MINUTE_MS, 100, 1, 999, 1]])
        self.assertEqual(analyze_path(inf, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=100, take_bp=100)["reason"], "invalid_ohlc")
        inverted = bars([[0, 100, 99, 101, 100], [MINUTE_MS, 100, 1, 999, 1]])
        self.assertEqual(analyze_path(inverted, entry_ts=0, exit_ts=MINUTE_MS, direction="long", stop_bp=100, take_bp=100)["reason"], "invalid_ohlc_order")

    def test_summary_retains_an_all_rejected_strategy_and_output_is_not_overwritten(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_outputs([{"source_file": "x.csv", "strategy": "all_bad", "symbol": "BTCUSDT", "direction": "long",
                            "row_number": 0, "path_complete": False, "reason": "missing_path_bar"}], [], root / "report",
                          inputs=[], db=Path(__file__), stop_bp=100, take_bp=100)
            summary = pd.read_csv(root / "report" / "event_path_summary.csv")
            self.assertEqual(summary.loc[0, "valid_path_count"], 0)
            self.assertEqual(summary.loc[0, "missing_path_count"], 1)
            source = root / "events_btc_long_x.csv"
            source.write_text("entry_ts,exit_ts,net_bp\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                main(["--output-dir", str(root / "report"), "--event-file", str(source)])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(["--output-dir", str(root / "another"), "--event-file", str(source), "--stop-bp", "nan"])

    def test_header_only_summary_matches_populated_schema(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_outputs([], [], root, inputs=[], db=Path(__file__), stop_bp=100, take_bp=100)
            self.assertEqual(pd.read_csv(root / "event_path_summary.csv").columns.tolist(), SUMMARY_COLUMNS)

    def test_crowding_direction_mapping(self):
        self.assertEqual(direction_for(Path("A2_contrarian_events_BTCUSDT_24h.csv"), pd.Series({"leg": "top"})), "short")
        self.assertEqual(direction_for(Path("A3_momentum_events_ETHUSDT_24h.csv"), pd.Series({"leg": "bottom"})), "short")

    def test_b2_row_horizons_are_separate_audit_and_summary_strategies(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "B2_deleverage_events_BTCUSDT_neg3pct.csv"
            pd.DataFrame({"entry_ts": [0, 0], "exit_ts": [MINUTE_MS, MINUTE_MS],
                          "horizon": ["24h", "72h"], "net_bp": [86.0, 86.0]}).to_csv(source, index=False)
            ledger, rejected = audit_file(source, self._db_with_simple_path(root), 100, 100)
            self.assertEqual(rejected, [])
            self.assertEqual({row["strategy"] for row in ledger},
                             {"B2_deleverage_BTCUSDT_neg3pct_24h", "B2_deleverage_BTCUSDT_neg3pct_72h"})
            write_outputs(ledger, rejected, root / "report", inputs=[], db=Path(__file__), stop_bp=100, take_bp=100)
            summary = pd.read_csv(root / "report" / "event_path_summary.csv")
            self.assertEqual(set(summary.strategy), {row["strategy"] for row in ledger})
            self.assertTrue((summary.events == 1).all())

    def test_audit_rejects_bad_source_net_per_row_without_aborting_valid_rows(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "events_btc_long_x.csv"
            pd.DataFrame({"entry_ts": [0] * 5, "exit_ts": [MINUTE_MS] * 5,
                          "net_bp": ["", "nan", "inf", "not-a-number", "86"]}).to_csv(source, index=False)
            ledger, rejected = audit_file(source, self._db_with_simple_path(root), 100, 100)
            self.assertEqual(len(ledger), 5)
            self.assertEqual(sum(row["path_complete"] for row in ledger), 1)
            self.assertEqual({row["reason"] for row in rejected}, {"missing_source_net", "invalid_source_net"})
            accepted = next(row for row in ledger if row["path_complete"])
            self.assertEqual(accepted["source_net_bp"], "86")

    def test_audit_rejects_missing_net_column_and_invalid_optional_source_price(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            no_net = root / "events_btc_long_missing.csv"
            pd.DataFrame({"entry_ts": [0], "exit_ts": [MINUTE_MS]}).to_csv(no_net, index=False)
            ledger, rejected = audit_file(no_net, self._db_with_simple_path(root), 100, 100)
            self.assertEqual(ledger[0]["reason"], "missing_source_net")
            self.assertEqual(rejected, ledger)

            bad_price = root / "events_btc_long_bad-price.csv"
            pd.DataFrame({"entry_ts": [0], "exit_ts": [MINUTE_MS], "net_bp": [86],
                          "entry_px": ["not-a-price"]}).to_csv(bad_price, index=False)
            ledger, rejected = audit_file(bad_price, self._db_with_simple_path(root), 100, 100)
            self.assertEqual(ledger[0]["reason"], "invalid_source_entry_price")
            self.assertEqual(rejected, ledger)


if __name__ == "__main__":
    unittest.main()
