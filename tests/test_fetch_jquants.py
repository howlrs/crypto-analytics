"""Key handling, request routing, mirroring and ATM extraction for data-tools/fetch_jquants.py."""
import csv
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_jquants", ROOT / "data-tools" / "fetch_jquants.py")
fj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fj)
fred_spec = importlib.util.spec_from_file_location("fetch_fred_series", ROOT / "data-tools" / "fetch_fred_series.py")
fred = importlib.util.module_from_spec(fred_spec)
fred_spec.loader.exec_module(fred)
T1, T2, T3, T4 = (f"2026-0{m}-01T00:00:00+00:00" for m in (1, 2, 3, 4))

SECRET = "k3y-not-real"
OPTION_FIELDS = ["Date", "Code", "Strike", "PCDiv", "EmMrgnTrgDiv", "Settle", "IV", "UnderPx", "BaseVol",
                 "SQD", "LTD", "CM", "OI", "Vo"]


def gz_csv(fields, rows) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(text.getvalue().encode())


def option(day, strike, pc, settle="100", iv="20", under="38010", kind="002", sqd="2024-09-13"):
    return {"Date": day, "Code": f"{strike}{pc}{kind}", "Strike": strike, "PCDiv": pc, "EmMrgnTrgDiv": kind,
            "Settle": settle, "IV": iv, "UnderPx": under, "BaseVol": "21", "SQD": sqd, "LTD": "2024-09-12",
            "CM": "2024-09", "OI": "5", "Vo": "1"}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return _Response(result)


def client(responses):
    opener = FakeOpener(responses)
    return fj.Client(SECRET, opener=opener, sleep=lambda s: None, monotonic=lambda: 0.0), opener


class KeyTest(unittest.TestCase):
    def test_environment_first_then_legacy_env_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("OTHER=1\nJQUANTS_APIKEY='legacy'\nJQUANTS_APIKEY_EXPIREDAT=2026/10/13\n")
            self.assertEqual(fj.load_api_key(env, environ={}), "legacy")
            self.assertEqual(fj.load_api_key(env, environ={"JQUANTS_API_KEY": "canonical"}), "canonical")
            env.write_text("\ufeffexport\tJQUANTS_API_KEY=old\nJQUANTS_API_KEY=rotated\n")
            self.assertEqual(fj.load_api_key(env, environ={}), "rotated")
            for bad in ("a b", "abc\u3042def"):
                env.write_text(f"JQUANTS_API_KEY={bad}\n")
                with self.assertRaises(ValueError) as ctx:
                    fj.load_api_key(env, environ={})
                self.assertNotIn(bad[1:], str(ctx.exception))
            with self.assertRaises(ValueError):
                fj.load_api_key(Path(tmp) / "missing", environ={})


PRESIGNED = "https://acct.r2.cloudflarestorage.com/x.csv.gz?sig=1"


class ClientTest(unittest.TestCase):
    def test_key_only_goes_to_api_and_never_to_presigned_url(self):
        c, opener = client([json.dumps({"url": PRESIGNED}).encode(), b"payload"])
        self.assertEqual(c.download("indices/bars/daily/topix/x.csv.gz"), b"payload")
        api, presigned = opener.requests
        self.assertTrue(api.full_url.startswith("https://api.jquants.com/v2/bulk/get?"))
        self.assertEqual(api.get_header("X-api-key"), SECRET)
        self.assertEqual(presigned.full_url, PRESIGNED)
        self.assertIsNone(presigned.get_header("X-api-key"))

    def test_unsafe_url_and_errors_do_not_leak(self):
        for url in ("http://acct.r2.cloudflarestorage.com/x", "https://user:pw@acct.r2.cloudflarestorage.com/x",
                    "https://acct.r2.cloudflarestorage.com:8443/x", "https://files.example/x",
                    "https://r2.cloudflarestorage.com.evil.example/x", "https://169.254.169.254/x"):
            c, opener = client([json.dumps({"url": url}).encode()])
            with self.assertRaises(fj.JQuantsError) as ctx:
                c.download("k")
            self.assertEqual(len(opener.requests), 1, url)  # refused before any download request
            self.assertNotIn("/x", str(ctx.exception))
        c, _ = client([HTTPError("https://api.jquants.com/v2/x", 403, "Forbidden", {}, None)])
        with self.assertRaises(fj.JQuantsError) as ctx:
            c.api("/x", {})
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertIsNone(fj._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil/"))

    def test_redirect_through_real_opener_is_not_followed(self):
        class Redirecting(BaseHandler):
            handler_order = 100  # answer before urllib's real HTTPS handler

            def __init__(self):
                self.urls = []

            def https_open(self, request):
                self.urls.append(request.full_url)
                headers = Message()
                headers["Location"] = "https://evil.example/steal"
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, code=302)
                response.msg = "Found"
                return response
        fake = Redirecting()
        c = fj.Client(SECRET, opener=build_opener(fj._NoRedirect(), fake), sleep=lambda s: None, retries=0)
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            with self.assertRaises(fj.JQuantsError) as ctx:
                c.api("/bulk/list", {"endpoint": "/x"})
        self.assertIn("HTTP 302", str(ctx.exception))
        self.assertEqual(len(fake.urls), 1)
        self.assertTrue(fake.urls[0].startswith("https://api.jquants.com/"))

    def test_retries_server_errors_and_paginates(self):
        pages = [HTTPError("u", 503, "busy", {}, None),
                 json.dumps({"data": [{"Key": "indices/bars/daily/topix/a_202401.csv.gz",
                                       "LastModified": "t", "Size": 3.0}], "pagination_key": "p"}).encode(),
                 json.dumps({"data": [{"Key": "indices/bars/daily/topix/a_202402.csv.gz",
                                       "LastModified": "t", "Size": 4}]}).encode()]
        c, opener = client(pages)
        items = c.bulk_list("/indices/bars/daily/topix")
        self.assertEqual([x["size"] for x in items], [3, 4])
        self.assertIn("pagination_key=p", opener.requests[-1].full_url)
        for key in ("markets/other/x.csv.gz", "indices/bars/daily/topix/../../x.csv.gz"):
            c, _ = client([json.dumps({"data": [{"Key": key, "LastModified": "t", "Size": 1}]}).encode()])
            with self.assertRaises(fj.JQuantsError):
                c.bulk_list("/indices/bars/daily/topix")

    def test_month_filter(self):
        self.assertTrue(fj.in_months("x/a_201610.csv.gz", "2016-10", None))
        self.assertFalse(fj.in_months("x/a_20161031.csv.gz", "2016-11", None))
        self.assertFalse(fj.in_months("x/a_202608.csv.gz", "2016-10", "2026-07"))
        self.assertTrue(fj.in_months("markets/calendar/live/markets_calendar.csv.gz", "2030-01", "2030-01"))


class FakeBulk:
    def __init__(self, files):
        self.files, self.downloads = files, []

    def bulk_list(self, endpoint):
        return [{"key": k, "last_modified": m, "size": len(d)} for k, (m, d) in self.files.items()
                if k.startswith(endpoint.strip("/"))]

    def download(self, key):
        self.downloads.append(key)
        return self.files[key][1]


class MirrorAndBuildTest(unittest.TestCase):
    def test_atm_choice(self):
        rows = [option("2024-08-20", "38000", "1", iv="25"), option("2024-08-20", "38000", "2", iv="23"),
                option("2024-08-20", "38020", "1", iv="99", kind="001"), option("2024-08-20", "38020", "2", iv="99", kind="001"),
                option("2024-08-20", "38010", "1", iv="0"), option("2024-08-20", "38010", "2", iv="30"),
                option("2024-08-20", "38020", "1", iv="21"), option("2024-08-20", "38020", "2", iv="19")]
        (row,), bad = fj.atm_by_expiry(rows, "k")
        self.assertEqual((row["strike"], bad, row["days_to_sq"]), (38000.0, 0, 24))  # tie at 10 -> lower strike
        self.assertAlmostEqual(row["atm_iv"], 0.24)
        with self.assertRaises(ValueError):
            fj.atm_by_expiry(rows + [option("2024-08-20", "38000", "1")], "k")

    def test_sync_skip_supersede_and_build_latest_wins(self):
        topix_key = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        live_key = "indices/bars/daily/topix/live/t_20240805.csv.gz"
        opt_key = "derivatives/bars/daily/options/225/historical/2024/o_202408.csv.gz"
        cal_key = "markets/calendar/live/markets_calendar.csv.gz"
        files = {
            topix_key: (T1, gz_csv(["Date", "O", "H", "L", "C"], [
                {"Date": "2024-08-02", "O": "1", "H": "1", "L": "1", "C": "2537.6"},
                {"Date": "2024-08-05", "O": "1", "H": "1", "L": "1", "C": "1"}])),
            live_key: (T2, gz_csv(["Date", "O", "H", "L", "C"], [
                {"Date": "2024-08-05", "O": "2488.25", "H": "2489.22", "L": "2206.73", "C": "2227.15"}])),
            opt_key: (T1, gz_csv(OPTION_FIELDS, [option("2024-08-20", "38000", "1"),
                                                           option("2024-08-20", "38000", "2")])),
            cal_key: (T4, gz_csv(["Date", "HolDiv"], [{"Date": "2024-08-05", "HolDiv": "1"}])),
        }
        with tempfile.TemporaryDirectory() as tmp:
            bulk = FakeBulk(files)
            first = fj.sync(bulk, Path(tmp), ["topix", "options225", "calendar"], "2016-10", None)
            self.assertEqual(first["downloaded"], 4)
            self.assertEqual(fj.sync(bulk, Path(tmp), ["topix"], "2016-10", None)["skipped"], 2)
            files[topix_key] = (T3, gz_csv(["Date", "O", "H", "L", "C"], [
                {"Date": "2024-08-02", "O": "1", "H": "1", "L": "1", "C": "2537.6"}]))
            counts = fj.sync(bulk, Path(tmp), ["topix"], "2016-10", None)
            self.assertEqual((counts["downloaded"], counts["superseded"]), (1, 1))
            kept = [p for p in (Path(tmp) / "raw" / "_superseded").rglob("*") if p.is_file()]
            self.assertEqual(len(kept), 1)
            stats = fj.build(Path(tmp))
            with sqlite3.connect(Path(tmp) / "jquants.db") as conn:
                closes = dict(conn.execute("SELECT date, close FROM topix_daily"))
                atm = conn.execute("SELECT strike, atm_iv, days_to_sq FROM n225_option_atm").fetchall()
                cal = conn.execute("SELECT * FROM trading_calendar").fetchall()
            self.assertEqual(closes, {"2024-08-02": 2537.6, "2024-08-05": 2227.15})
            self.assertEqual(atm, [(38000.0, 0.2, 24)])
            self.assertEqual(cal, [("2024-08-05", "1", cal_key)])
            self.assertEqual(stats["topix_rows"], 2)
            (Path(tmp) / "raw" / opt_key).write_bytes(b"tampered")
            with self.assertRaises(fj.JQuantsError):
                fj.build(Path(tmp))

    def test_size_mismatch_and_repository_paths_are_refused(self):
        key = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        bulk = FakeBulk({key: (T1, gz_csv(["Date"], [{"Date": "2024-08-01"}]))})
        bulk.bulk_list = lambda endpoint: [{"key": key, "last_modified": T1, "size": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(fj.JQuantsError):
                fj.sync(bulk, Path(tmp), ["topix"], "2016-10", None)
            self.assertFalse((Path(tmp) / "raw" / key).exists())
            corrupt = FakeBulk({key: (T1, b"not gzip")})
            with self.assertRaises(fj.JQuantsError):
                fj.sync(corrupt, Path(tmp), ["topix"], "2016-10", None)
            self.assertEqual([p.name for p in (Path(tmp) / "raw").rglob("*") if p.is_file()], [".sync.lock"])
            with open(Path(tmp) / "raw" / ".sync.lock", "w") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with self.assertRaises(fj.JQuantsError):
                    fj.sync(FakeBulk({key: (T1, gz_csv(["Date"], []))}), Path(tmp), ["topix"], "2016-10", None)
        with self.assertRaises(ValueError):
            fj.sync(bulk, ROOT / "results" / "jq", ["topix"], "2016-10", None)
        self.assertEqual(fj.main(["build", "--data-dir", str(ROOT / "results")]), 1)

    def test_symlink_below_raw_into_repository_is_refused(self):
        key = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        bulk = FakeBulk({key: (T1, gz_csv(["Date"], [{"Date": "2024-08-01"}]))})
        with tempfile.TemporaryDirectory() as tmp:
            repo, data = Path(tmp, "repo").resolve(), Path(tmp, "data")
            (repo / "docs").mkdir(parents=True)
            (data / "raw").mkdir(parents=True)
            (data / "raw" / "indices").symlink_to(repo / "docs", target_is_directory=True)
            with mock.patch.object(fj, "REPO", repo), self.assertRaises(ValueError):
                fj.sync(bulk, data, ["topix"], "2016-10", None)
            self.assertEqual(bulk.downloads, [])
            self.assertEqual(list((repo / "docs").iterdir()), [])

    def test_manifest_records_digest(self):
        key = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        data = gz_csv(["Date", "O", "H", "L", "C"], [{"Date": "2024-08-01", "O": "", "H": "", "L": "", "C": "1"}])
        with tempfile.TemporaryDirectory() as tmp:
            fj.sync(FakeBulk({key: (T1, data)}), Path(tmp), ["topix"], "2016-10", None)
            entry = json.loads((Path(tmp) / "raw" / "manifest.json").read_text())["files"][key]
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            fj.build(Path(tmp))
            with sqlite3.connect(Path(tmp) / "jquants.db") as conn:
                self.assertEqual(conn.execute("SELECT open, close FROM topix_daily").fetchone(), (None, 1.0))


class NewestFileTest(unittest.TestCase):
    def test_newer_file_replaces_every_date_it_contains(self):
        hist = "derivatives/bars/daily/options/225/historical/2024/o_202408.csv.gz"
        live = "derivatives/bars/daily/options/225/live/o_20240821.csv.gz"
        thist = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        tlive = "indices/bars/daily/topix/live/t_20240821.csv.gz"
        files = {
            hist: (T1, gz_csv(OPTION_FIELDS, [option(d, "38000", pc) for d in ("2024-08-20", "2024-08-21")
                                              for pc in "12"])),
            live: (T2, gz_csv(OPTION_FIELDS, [option("2024-08-20", "38000", "1", iv="30"),
                                              option("2024-08-20", "38000", "2", iv="30"),
                                              option("2024-08-21", "38000", "1", settle="0"),
                                              option("2024-08-21", "38000", "2", settle="0")])),
            thist: (T1, gz_csv(["Date", "O", "H", "L", "C"], [{"Date": "2024-08-21", "O": "", "H": "", "L": "",
                                                               "C": "999"}])),
            tlive: (T2, gz_csv(["Date", "O", "H", "L", "C"], [{"Date": "2024-08-21", "O": "", "H": "", "L": "",
                                                               "C": ""}])),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fj.sync(FakeBulk(files), Path(tmp), ["topix", "options225"], "2016-10", None)
            stats = fj.build(Path(tmp))
            with sqlite3.connect(Path(tmp) / "jquants.db") as conn:
                atm = conn.execute("SELECT date, atm_iv, source_key FROM n225_option_atm").fetchall()
                topix = conn.execute("SELECT * FROM topix_daily").fetchall()
            self.assertEqual(atm, [("2024-08-20", 0.3, live)])  # stale 08-21 row is dropped, not kept
            self.assertEqual(topix, [])
            self.assertEqual(stats["option_dates_without_atm"], 1)
            unusable = dict(files)
            unusable[live] = (T3, gz_csv(OPTION_FIELDS, [option("2024-08-21", "38000", "1", kind="001")]))
            fj.sync(FakeBulk(unusable), Path(tmp), ["options225"], "2016-10", None)
            with self.assertRaises(fj.JQuantsError):
                fj.build(Path(tmp))
            (Path(tmp) / "raw" / hist).unlink()
            with self.assertRaises(fj.JQuantsError):
                fj.build(Path(tmp))
            self.assertEqual([p.name for p in Path(tmp).glob(".jquants.db.tmp-*")], [])


    def test_plain_tables_replace_dates_and_reject_schema_changes(self):
        fields = ["PubDate", "Date", "Code", "IssType", "ShrtVol", "LongVol", "ShrtNegVol", "LongNegVol",
                  "ShrtStdVol", "LongStdVol", "ShrtVal", "LongVal"]

        def margin(day, code, long, pub=""):
            return {"PubDate": pub, "Date": day, "Code": code, "IssType": "2", "ShrtVol": "10", "LongVol": long,
                    "ShrtNegVol": "0", "LongNegVol": "0", "ShrtStdVol": "10", "LongStdVol": long,
                    "ShrtVal": "", "LongVal": ""}
        hist = "markets/margin-interest/historical/2024/m_202408.csv.gz"
        live = "markets/margin-interest/live/m_20240816.csv.gz"
        files = {
            hist: (T1, gz_csv(fields, [margin("2024-08-09", "13010", "100"), margin("2024-08-16", "13010", "1"),
                                       margin("2024-08-16", "13050", "1")])),
            live: (T2, gz_csv(fields, [margin("2024-08-16", "13010", "200", pub="2024-08-20")])),
        }
        with tempfile.TemporaryDirectory() as tmp:
            fj.sync(FakeBulk(files), Path(tmp), ["margin_weekly"], "2016-10", None)
            stats = fj.build(Path(tmp))
            with sqlite3.connect(Path(tmp) / "jquants.db") as conn:
                rows = conn.execute("SELECT date, code, pub_date, long_shares, short_value FROM margin_weekly "
                                    "ORDER BY date, code").fetchall()
            # The newer live file replaces the whole 08-16 date, so 13050 from the older file is dropped.
            self.assertEqual(rows, [("2024-08-09", "13010", None, 100.0, None),
                                    ("2024-08-16", "13010", "2024-08-20", 200.0, None)])
            self.assertEqual((stats["margin_weekly_rows"], stats["overridden_dates"]), (2, 1))
            files[live] = (T3, gz_csv([f for f in fields if f != "LongVol"],
                                      [{k: v for k, v in margin("2024-08-16", "13010", "1").items() if k != "LongVol"}]))
            fj.sync(FakeBulk(files), Path(tmp), ["margin_weekly"], "2016-10", None)
            with self.assertRaisesRegex(fj.JQuantsError, "missing columns"):
                fj.build(Path(tmp))
            files[live] = (T4, gz_csv(fields, [margin("2024-08-16", "13010", "1")] * 2))
            fj.sync(FakeBulk(files), Path(tmp), ["margin_weekly"], "2016-10", None)
            with self.assertRaisesRegex(fj.JQuantsError, "duplicate"):
                fj.build(Path(tmp))


class FredTest(unittest.TestCase):
    def test_validate_rejects_bad_series(self):
        good = b"observation_date,VIXCLS\n2026-10-15,16.46\n2026-10-16,\n"
        self.assertEqual(fred.validate("VIXCLS", good), 2)
        for bad in (b"observation_date,VIXCLS\n", b"date,VIXCLS\n2026-10-15,1\n",
                    b"observation_date,VIXCLS\n2026-10-16,1\n2026-10-15,1\n",
                    b"observation_date,VIXCLS\n2026-10-15,nan\n"):
            with self.assertRaises(ValueError):
                fred.validate("VIXCLS", bad)

    def test_append_only_refresh(self):
        old = b"observation_date,VIXCLS\n2026-10-15,16.46\n"
        self.assertTrue(fred.extends(old, old + b"2026-10-16,19.99\n"))
        self.assertFalse(fred.extends(old, b"observation_date,VIXCLS\n2026-10-15,17.00\n"))

    def test_repository_out_dir_is_refused_before_download(self):
        with mock.patch.object(fred.subprocess, "run", side_effect=AssertionError("network")):
            with self.assertRaises(SystemExit) as ctx:
                fred.main(["--series", "VIXCLS", "--out-dir", str(ROOT / "results")])
        self.assertIn("outside the public repository", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
