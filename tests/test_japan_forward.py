"""Registry sealing, liquidation accounting and the descriptive exit rule of the Japan forward tracking."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.japan_forward import (
    CLOSE_OFFSET, DAY, ROOT, RULES, funding_exit, hac_mean_test, instrument_ledger, load_registry, max_rise,
    outside_repo, register, sidecar,
)

BEFORE = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
D0 = int(np.datetime64("2026-11-02", "D").astype(np.int64))


def candles(times, highs):
    highs = np.asarray(highs, float)
    return pd.DataFrame({"t": np.asarray(times, np.int64), "o": highs, "h": highs, "l": highs, "c": highs,
                         "v": np.ones(len(highs))})


def window(dates, prev_dates, dn, valid, perp_prev=100.0, fair=(1.0, 1.0), funding=0.0):
    n = len(dates)
    return pd.DataFrame({"date": dates, "prev_date": prev_dates, "perp": np.full(n, perp_prev),
                         "perp_prev": np.full(n, perp_prev), "fair_usd": np.full(n, fair[1]),
                         "fair_usd_prev": np.full(n, fair[0]), "valid": valid, "dn": dn,
                         "funding_short": np.full(n, funding)})


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.design = self.dir / "summary.json"
        self.design.write_text("{}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_once_before_the_first_close_and_verify(self):
        path = self.dir / "registry.json"
        reg = register(path, self.design, BEFORE)
        self.assertEqual(reg["registered_at"], "2026-10-31T00:00:00Z")
        self.assertEqual(load_registry(path)["integrity"], reg["integrity"])
        with self.assertRaises(FileExistsError):
            register(path, self.design, BEFORE)
        late = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "precede"):
            register(self.dir / "late.json", self.design, late)

    def test_edits_and_sidecar_mismatch_are_refused(self):
        path = self.dir / "registry.json"
        register(path, self.design, BEFORE)
        original = path.read_text()
        path.write_text(original.replace('"leverage": 3.0', '"leverage": 4.0'))
        with self.assertRaisesRegex(ValueError, "integrity"):
            load_registry(path)
        path.write_text(original)
        sidecar(path).write_text("0" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            load_registry(path)

    def test_rules_in_the_registry_must_match_the_script(self):
        path = self.dir / "registry.json"
        register(path, self.design, BEFORE)
        RULES["h1_carry"]["criteria"]["max_liquidations"] = 1
        try:
            with self.assertRaisesRegex(ValueError, "rules differ"):
                load_registry(path)
        finally:
            RULES["h1_carry"]["criteria"]["max_liquidations"] = 0

    def test_outputs_are_refused_inside_the_repository(self):
        with self.assertRaises(ValueError):
            outside_repo(ROOT / "results/japan_forward")


class StatisticsTest(unittest.TestCase):
    def test_hac_mean_test_direction(self):
        rng = np.random.default_rng(0)
        t, p = hac_mean_test(0.001 + 0.001 * rng.standard_normal(250), 5)
        self.assertGreater(t, 5)
        self.assertLess(p, 1e-6)
        t, p = hac_mean_test(np.full(10, 0.01), 5)
        self.assertTrue(np.isnan(t) and np.isnan(p))


class LedgerTest(unittest.TestCase):
    def test_rise_uses_15m_when_covered_else_hourly(self):
        a, b = D0 * DAY + CLOSE_OFFSET, (D0 + 1) * DAY + CLOSE_OFFSET
        m15 = candles(np.arange(a, b, 900_000), np.full(96, 100.0))
        m15.loc[50, "h"] = 112.0
        h1 = candles(np.arange(a - 30 * 60_000, b, 3_600_000), np.full(25, 130.0))
        np.testing.assert_allclose(max_rise(np.array([a]), np.array([b]), np.array([100.0]), m15, h1), [0.12])
        sparse = m15.iloc[::2]  # half the quarter hours missing
        np.testing.assert_allclose(max_rise(np.array([a]), np.array([b]), np.array([100.0]), sparse, h1), [0.30])

    def test_liquidation_loses_the_margin_and_costs_survive_unmeasured_days(self):
        dates = ["2026-11-04", "2026-11-05", "2026-11-06"]
        w = window(dates, ["2026-11-02", "2026-11-04", "2026-11-05"], dn=[0.001, np.nan, 0.002],
                   valid=[True, False, True], fair=(1.0, np.exp(0.10)))
        ts = [(int(np.datetime64(d, "D").astype(np.int64)) * DAY + CLOSE_OFFSET) for d in ["2026-11-02"] + dates]
        times = np.concatenate([np.arange(ts[i], ts[i + 1], 900_000) for i in range(3)])
        highs = np.full(len(times), 100.0)
        highs[np.searchsorted(times, ts[2]) + 3] = 130.0  # day 3: +30% >= 1/3 - 0.05
        led = instrument_ledger(w, candles(times, highs), candles([], []), 3.0, {"2026-11-05"}, 0.0004)
        self.assertEqual(list(led.liquidated), [False, False, True])
        np.testing.assert_allclose(led.pnl_notional.iloc[2], 0.10 - 1 / 3)
        np.testing.assert_allclose(led.cost_notional, [0.0, 0.0004, 0.0004])
        self.assertEqual(list(led.measured), [True, False, True])
        np.testing.assert_allclose(led.roc_net.iloc[0], 0.001 / (4 / 3))

    def test_funding_exit_decides_at_the_previous_close(self):
        hist = pd.Series([1e-4, 1e-4, 1e-4])
        w = window(["d1", "d2", "d3", "d4"], ["d0", "d1", "d2", "d3"], dn=[0.0] * 4, valid=[True] * 4)
        w["funding_short"] = [-5e-4, 1e-4, 6e-4, 0.0]
        w["roc_net"] = [0.01, 0.01, 0.01, 0.01]
        out = funding_exit(hist, w, 3, 0.001)
        # trailing sums at closes d0..d3: +3e-4, -3e-4, -3e-4, +2e-4 -> held over d1, flat d2-d3, back in for d4
        np.testing.assert_allclose(out, [0.01 - 0.001, -0.001, 0.0, 0.01 - 0.001 - 0.001])


if __name__ == "__main__":
    unittest.main()
