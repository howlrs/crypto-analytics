"""The store of aggregates that outlive the J-Quants data must refuse anything shaped like the data."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.japan_retained import JQ_DIR, RETAINED_DIR, ROOT, check_aggregate, retained_path, write_summary, write_table


class CheckTest(unittest.TestCase):
    good = pd.DataFrame({"code": ["13210", "285A0"], "days": [2000, 900], "worst": [0.2, 0.3]})

    def refused(self, frame, message, observations="days"):
        with self.assertRaisesRegex(ValueError, message):
            check_aggregate(frame, "code", observations)

    def test_one_row_per_entity_passes(self):
        check_aggregate(self.good, "code", "days")

    def test_a_repeated_entity_is_refused(self):
        self.refused(pd.concat([self.good, self.good]), "more than one row")

    def test_dates_in_cells_are_refused_in_every_format(self):
        for value in ("2020-03-24", "2020-03", "2020/03/24", "20200324", "2020.03.24", "2020-03-24T06:30:00", "20160104_close",
                      "close_2016-01-04", "d2016-01-04", "2016_01_04", " 2016-01-04", "2016年1月4日", "04/01/2016", "202003"):
            self.refused(self.good.assign(worst_day=[value, None]), "holds dates")
        self.refused(self.good.assign(day=pd.to_datetime(["2020-03-24", "2025-04-08"])), "date column")

    def test_dates_written_as_numbers_are_refused(self):
        self.refused(self.good.assign(ymd=[20200324, 20250408]), "as numbers")
        self.refused(self.good.assign(ymd=[20200324, 0]), "as numbers")
        self.refused(self.good.assign(month=[202003, 202504]), "as numbers")
        self.refused(self.good.assign(worst_ms=[1585000000000, 1745000000000]), "as numbers")

    def test_a_pivot_with_dates_as_column_names_is_refused(self):
        for name in ("2020-03-24", "20160104_close", "close_2016-01-04", ("close", "2020-03-24"), "x" * 500):
            self.refused(pd.DataFrame({"code": ["13210", "285A0"], "days": [100, 100], name: [1.0, 2.0]}), "column name")
        many = pd.DataFrame(np.ones((2, 250)), columns=[f"p{i}" for i in range(250)]).assign(code=["13210", "285A0"], days=100)
        self.refused(many, "too wide")

    def test_a_row_number_is_not_an_entity(self):
        self.refused(pd.DataFrame({"code": range(100), "days": 100, "close": 1.5}), "stock codes")

    def test_a_series_in_a_text_cell_is_refused(self):
        self.refused(self.good.assign(blob=[json.dumps(list(range(100))), ""]), "long text")

    def test_every_row_needs_its_observations(self):
        self.refused(self.good.assign(days=[2000, 5]), "fewer than")
        self.refused(self.good.assign(days=[2000, np.nan]), "fewer than")
        self.refused(self.good.assign(days=[2000, np.inf]), "fewer than")
        with self.assertRaisesRegex(ValueError, "fewer than"):
            check_aggregate(self.good.assign(code=[13210, 20010]), "code", "code")
        with self.assertRaises(TypeError):
            check_aggregate(self.good, "code")  # the count cannot be left out

    def test_stock_codes_are_not_mistaken_for_dates(self):
        check_aggregate(self.good.assign(code=["20010", "19490"]), "code", "days")


class PathTest(unittest.TestCase):
    def test_the_repository_and_the_purged_directory_are_refused(self):
        for bad in (ROOT / "results/x", JQ_DIR / "results/x", JQ_DIR):
            with self.assertRaises(ValueError):
                retained_path(bad)

    def test_the_default_store_survives_the_purge(self):
        self.assertNotIn(JQ_DIR.resolve(), [RETAINED_DIR.resolve(), *RETAINED_DIR.resolve().parents])
        purge = (ROOT / "data-tools/purge_jquants.sh").read_text()
        self.assertIn('"$(basename "$resolved")" != "jquants"', purge)  # it can only delete a directory named jquants

    def test_a_name_cannot_leave_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../escaped", "a/b", "A.b"):
                with self.assertRaisesRegex(ValueError, "file name"):
                    write_table(CheckTest.good, bad, "code", "days", Path(tmp))
                with self.assertRaisesRegex(ValueError, "file name"):
                    write_summary({"a": 1}, bad, Path(tmp))


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)

    def test_tables_and_summaries_land_in_the_store(self):
        target = write_table(CheckTest.good, "cards", "code", "days", self.path)
        self.assertEqual(len(pd.read_csv(target)), 2)
        path = write_summary({"verdict": "x", "t": np.float64(1.5), "n": np.int64(3), "ok": np.bool_(True)}, "s", self.path)
        self.assertEqual(json.loads(path.read_text())["n"], 3)

    def test_a_number_that_is_not_finite_is_strict_json_null(self):
        path = write_summary({"bad": float("nan"), "worse": np.float64("inf")}, "s", self.path)
        loaded = json.loads(path.read_text(), parse_constant=self.fail)
        self.assertIsNone(loaded["bad"])
        self.assertIsNone(loaded["worse"])

    def test_series_are_refused_in_every_container(self):
        for payload, message in (({"v": list(range(500))}, "longer than"), ({"v": tuple(range(5000))}, "longer than"),
                                 ({f"p{i}": 1.0 for i in range(500)}, "long or date-keyed"),
                                 ({"2016-01-04": 1.0}, "date-keyed"), ({"20160104": 1.0}, "date-keyed"),
                                 ({"v": [[1.0] * 100] * 200}, "too many numbers"),
                                 ({"v": json.dumps(list(range(500)))}, "long text"), ({"x" * 500: 1}, "long or date-keyed"),
                                 ({"close_2016-01-04": 1.0}, "date-keyed"),
                                 ({"p": [[{"date": f"2016-{m:02d}-{d:02d}", "close": 1.0} for d in range(1, 29)] for m in range(1, 13)]}, "too many"),
                                 ({"p": [["x" * 500] * 100]}, "too many")):
            with self.assertRaisesRegex(ValueError, message):
                write_summary(payload, "s", self.path)

    def test_the_bounds_of_a_period_may_be_named(self):
        write_summary({"period": ["2017-10-12", "2026-07-23"], "by_year": [{"year": "2017", "slope": -0.1}]}, "s", self.path)


if __name__ == "__main__":
    unittest.main()
