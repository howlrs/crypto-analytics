"""The H1 monitor without J-Quants: the calendar, and the proxy against the ledger it stands in for."""
import json
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
