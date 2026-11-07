"""The H1 monitor without J-Quants: the calendar, and the proxy against the ledger it stands in for."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests import hl_japan_carry as carry
from backtests import japan_perp_monitor as monitor

HOUR = 3_600_000
DAY = 24 * HOUR
START = pd.Timestamp("2026-11-02", tz="UTC").value // 10**6


def write_market(base: Path, coin: str, price: float, rate: float, hours: int = 24 * 12, spike_hour: int | None = None) -> None:
    stem = carry.stem(coin)
    (base / f"{stem}-funding.json").write_text(json.dumps(
        [{"coin": coin, "fundingRate": str(rate), "premium": "0", "time": START + h * HOUR + 40} for h in range(hours)]))
    for interval, step, count in (("15m", HOUR // 4, hours * 4), ("1h", HOUR, hours)):
        rows = []
        for i in range(count):
            t = START + i * step
            high = price * (1.4 if spike_hour is not None and spike_hour * HOUR <= i * step < (spike_hour + 1) * HOUR else 1.0)
            rows.append({"t": t, "T": t + step - 1, "s": coin, "i": interval, "o": str(price), "c": str(price), "h": str(high), "l": str(price), "v": "1", "n": 1})
        (base / f"{stem}-candles-{interval}.json").write_text(json.dumps(rows))


class CalendarTest(unittest.TestCase):
    def test_weekends_and_holidays_are_skipped(self):
        self.assertEqual(monitor.tse_days("2026-11-02", "2026-11-05"), ["2026-11-02", "2026-11-04", "2026-11-05"])
        self.assertEqual(monitor.tse_days("2026-12-30", "2027-01-04"), ["2026-12-30", "2027-01-04"])


class StatusTest(unittest.TestCase):
    def test_status_runs_on_hyperliquid_files_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for coin, price, rate in ((carry.FX, 150.0, 0.0), ("xyz:SOFTBANK", 10.0, 0.0001), ("xyz:KIOXIA", 20.0, 0.0)):
                write_market(base, coin, price, rate)
            out = monitor.status(monitor.REGISTRY, base, base / "store")
            soft = out["instruments"]["xyz:SOFTBANK"]
            self.assertEqual(soft["liquidations_on_proxy"], 0)
            self.assertEqual(soft["last_day"], "2026-11-13")
            self.assertAlmostEqual(soft["funding_received_sum"], 0.0001 * 11 * 24)  # eleven days of hourly funding
            self.assertTrue((base / "store/h1_monitor/status.json").exists())

    def test_the_annual_rate_counts_only_days_it_could_measure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for coin, price, rate in ((carry.FX, 150.0, 0.0), ("xyz:SOFTBANK", 10.0, 0.0001), ("xyz:KIOXIA", 20.0, 0.0)):
                write_market(base, coin, price, rate)
            path = base / f"{carry.stem('xyz:SOFTBANK')}-funding.json"
            rows = json.loads(path.read_text())
            del rows[100]  # one missing settlement makes that day unmeasured
            path.write_text(json.dumps(rows))
            soft = monitor.status(monitor.REGISTRY, base, base / "store")["instruments"]["xyz:SOFTBANK"]
            self.assertEqual(soft["days_with_gaps"], 1)
            measured = soft["tse_days"] - soft["days_with_gaps"]
            self.assertAlmostEqual(soft["funding_on_capital_annual"], soft["funding_on_capital_sum"] / measured * carry.TSE_DAYS_PER_YEAR)


class CalibrateTest(unittest.TestCase):
    """The comparison with the official ledger, on a stand-in database with the two tables it reads."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        for coin, price in ((carry.FX, 150.0), ("xyz:SOFTBANK", 10.0), ("xyz:KIOXIA", 20.0)):
            write_market(self.base, coin, price, 0.0001)
        self.days = monitor.tse_days("2026-11-02", "2026-11-13")

    def database(self, softbank_basis: np.ndarray) -> Path:
        path = self.base / "stand-in.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE trading_calendar (date TEXT, hol_div TEXT)")
            conn.execute("CREATE TABLE equities_daily (date TEXT, code TEXT, close REAL)")
            conn.executemany("INSERT INTO trading_calendar VALUES (?, '1')", [(d,) for d in self.days])
            for i, day in enumerate(self.days):
                conn.execute("INSERT INTO equities_daily VALUES (?, '285A0', ?)", (day, 20.0 * 150))
                value = 10.0 * 150 * np.exp(-softbank_basis[i])  # a perp above fair means a lower TSE close
                conn.execute("INSERT INTO equities_daily VALUES (?, '99840', ?)", (day, None if np.isnan(value) else value))
        conn.close()
        return path

    def run_calibration(self, basis: np.ndarray) -> dict:
        return monitor.calibrate(monitor.REGISTRY, self.database(basis), self.base, self.base / "store", first="2026-11-02")

    def test_a_tse_close_equal_to_the_perp_leaves_no_error(self):
        out = self.run_calibration(np.zeros(len(self.days)))["instruments"]["xyz:KIOXIA"]
        self.assertEqual(out["days"], len(self.days) - 1)  # the first day only opens the position
        self.assertAlmostEqual(out["cumulative_error_on_capital"], 0.0)
        self.assertTrue(out["same_liquidations"])

    def test_the_error_is_the_basis_change_on_capital(self):
        basis = np.zeros(len(self.days))
        basis[-1] = 0.006  # the position closes with the perp 0.6% above the TSE price
        out = self.run_calibration(basis)["instruments"]["xyz:SOFTBANK"]
        self.assertAlmostEqual(out["cumulative_error_on_capital"], -0.006 / (1 + 1 / 3))

    def test_the_correlation_skips_pairs_across_a_gap(self):
        basis = np.array([0.0, 0.001, 0.004, -0.002, np.nan, 0.003, 0.002, -0.001, 0.005])
        out = self.run_calibration(basis)["instruments"]["xyz:SOFTBANK"]
        self.assertEqual(out["days"], 6)  # the first day, the gap and the day after it have no daily change
        pairs = [(1, 2), (2, 3), (6, 7), (7, 8)]  # never (3, 5) or (3, 6), which are not neighbouring days
        x, y = zip(*[(basis[a], basis[b]) for a, b in pairs])
        self.assertAlmostEqual(out["basis_day_to_day_correlation"], float(np.corrcoef(x, y)[0, 1]))


class ProxyTest(unittest.TestCase):
    spec = {"leverage": 3.0}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name)
        write_market(self.base, carry.FX, 150.0, 0.0)

    def run_ledger(self, fair=None):
        days = monitor.tse_days("2026-11-02", "2026-11-09")
        return monitor.ledger("xyz:SOFTBANK", self.spec, days, fair, "2026-11-02", self.base), days

    def test_the_proxy_income_is_the_funding_and_the_basis_is_zero(self):
        write_market(self.base, "xyz:SOFTBANK", 10.0, 0.0001)
        led, _ = self.run_ledger()
        self.assertTrue(led.valid.all())
        np.testing.assert_allclose(led.basis, 0.0, atol=1e-12)
        np.testing.assert_allclose(led.pnl_notional, led.funding_short)
        self.assertGreater(led.funding_short.iloc[0], 0)

    def test_the_proxy_equals_the_official_ledger_when_the_tse_close_matches_the_perp(self):
        write_market(self.base, "xyz:SOFTBANK", 10.0, 0.0001)
        proxy, days = self.run_ledger()
        official, _ = self.run_ledger(np.full(len(days), 10.0 * 150.0))
        np.testing.assert_allclose(proxy.roc, official.roc)

    def test_a_basis_change_is_the_only_gap_to_the_official_ledger(self):
        write_market(self.base, "xyz:SOFTBANK", 10.0, 0.0)
        proxy, days = self.run_ledger()
        fair = np.full(len(days), 1500.0)
        fair[2] = 1500.0 * np.exp(-0.01)  # the perp stood 1% above fair at that close
        official, _ = self.run_ledger(fair)
        self.assertAlmostEqual(official.roc.sum() - proxy.roc.sum(), 0.0, places=9)  # the term telescopes back to zero
        self.assertGreater(official.roc.abs().max(), 0.005)

    def test_the_liquidation_check_needs_no_tse_price(self):
        write_market(self.base, "xyz:SOFTBANK", 10.0, 0.0, spike_hour=24 * 3 + 1)
        led, _ = self.run_ledger()
        self.assertAlmostEqual(led.rise.max(), 0.4)  # the room at 3x is 28.3%
        self.assertEqual(int(led.liquidated.sum()), 1)
        write_market(self.base, "xyz:SOFTBANK", 10.0, 0.0)
        self.assertFalse(self.run_ledger()[0].liquidated.any())


if __name__ == "__main__":
    unittest.main()
