"""The store of aggregates that outlive the J-Quants data must refuse anything shaped like the data."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from backtests.japan_retained import JQ_DIR, ROOT, check_aggregate, retained_path, write_summary, write_table


class CheckTest(unittest.TestCase):
    good = pd.DataFrame({"code": ["a", "b"], "days": [2000, 900], "worst": [0.2, 0.3]})

    def test_one_row_per_entity_passes(self):
        check_aggregate(self.good, ["code"], "days")

    def test_a_repeated_entity_is_refused(self):
        with self.assertRaisesRegex(ValueError, "more than one row"):
            check_aggregate(pd.concat([self.good, self.good]), ["code"], "days")

    def test_dates_are_refused_as_text_and_as_a_column(self):
        with self.assertRaisesRegex(ValueError, "holds dates"):
            check_aggregate(self.good.assign(worst_day=["2020-03-24", "2025-04-08"]), ["code"])
        with self.assertRaisesRegex(ValueError, "holds dates"):
            check_aggregate(self.good.assign(month=["2020-03", None]), ["code"])
        with self.assertRaisesRegex(ValueError, "date column"):
            check_aggregate(self.good.assign(day=pd.to_datetime(["2020-03-24", "2025-04-08"])), ["code"])

    def test_a_statistic_over_a_few_observations_is_refused(self):
        with self.assertRaisesRegex(ValueError, "fewer than"):
            check_aggregate(self.good.assign(days=[2000, 5]), ["code"], "days")


class PathTest(unittest.TestCase):
    def test_the_repository_and_the_purged_directory_are_refused(self):
        for bad in (ROOT / "results/x", JQ_DIR / "results/x", JQ_DIR):
            with self.assertRaises(ValueError):
                retained_path(bad)

    def test_the_default_store_survives_the_purge(self):
        from backtests.japan_retained import RETAINED_DIR
        self.assertNotIn(JQ_DIR.resolve(), [RETAINED_DIR.resolve(), *RETAINED_DIR.resolve().parents])
        purge = (ROOT / "data-tools/purge_jquants.sh").read_text()
        self.assertNotIn(str(RETAINED_DIR), purge.replace("Kept:", ""))  # the script must never name it for deletion


class WriteTest(unittest.TestCase):
    def test_tables_and_summaries_land_in_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = write_table(CheckTest.good, "cards", ["code"], "days", Path(tmp))
            self.assertEqual(len(pd.read_csv(target)), 2)
            path = write_summary({"verdict": "x", "t": np.float64(1.5), "n": np.int64(3), "bad": float("nan")}, "s", Path(tmp))
            self.assertEqual(json.loads(path.read_text())["n"], 3)

    def test_a_long_list_in_a_summary_is_refused_as_a_series(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, "series"):
            write_summary({"nested": {"values": list(range(500))}}, "s", Path(tmp))


if __name__ == "__main__":
    unittest.main()
