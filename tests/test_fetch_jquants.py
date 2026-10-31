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
OPTION_FIELDS = ["Date", "Code", "O", "H", "L", "C", "EO", "EH", "EL", "EC", "AO", "AH", "AL", "AC", "Vo", "OI",
                 "Va", "CM", "Strike", "VoOA", "EmMrgnTrgDiv", "PCDiv", "LTD", "SQD", "Settle", "Theo", "BaseVol",
                 "UnderPx", "IV", "IR"]


def gz_csv(fields, rows) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(text.getvalue().encode())


def option(day, strike, pc, settle="100", iv="20", under="38010", kind="002", sqd="2024-09-13"):
    row = dict.fromkeys(OPTION_FIELDS, "0")
    row.update({"Date": day, "Code": f"{strike}{pc}{kind}", "Strike": strike, "PCDiv": pc, "EmMrgnTrgDiv": kind,
                "Settle": settle, "IV": iv, "UnderPx": under, "BaseVol": "21", "SQD": sqd, "LTD": "2024-09-12",
                "CM": "2024-09", "OI": "5", "Vo": "1"})
    return row


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


class ExpiredSubscriptionTest(unittest.TestCase):
    """After cancellation nothing changes locally; after a renewal the missing files are fetched."""

    def test_a_refused_dataset_stops_the_sync_before_any_download(self):
        topix = "indices/bars/daily/topix/historical/2024/t_202408.csv.gz"
        cal = "markets/calendar/live/markets_calendar.csv.gz"
        files = {topix: (T1, gz_csv(["Date", "O", "H", "L", "C"], [{"Date": "2024-08-01", "O": "1", "H": "1",
                                                                     "L": "1", "C": "2"}])),
                 cal: (T1, gz_csv(["Date", "HolDiv"], [{"Date": "2024-08-01", "HolDiv": "1"}]))}
        bulk = FakeBulk(files)
        listing = bulk.bulk_list

        def refused(endpoint):
            if "calendar" in endpoint:
                raise fj.JQuantsError("GET /bulk/list failed with HTTP 403", 403)
            return listing(endpoint)
        with tempfile.TemporaryDirectory() as tmp:
            bulk.bulk_list = refused
            with self.assertRaisesRegex(fj.AccessDenied, "calendar"):
                fj.sync(bulk, Path(tmp), ["topix", "calendar"], "2016-10", None)
            self.assertEqual(bulk.downloads, [])  # topix was listable but is not fetched either
            fresh = Path(tmp) / "purged" / "jquants"
            with self.assertRaises(fj.AccessDenied):
                fj.sync(bulk, fresh, ["topix", "calendar"], "2016-10", None)
            self.assertFalse(fresh.exists())  # a refused sync recreates nothing after a purge
            bulk.bulk_list = listing  # renewed: everything missing locally is fetched
            self.assertEqual(fj.sync(bulk, Path(tmp), ["topix", "calendar"], "2016-10", None)["downloaded"], 2)

    def test_http_status_is_kept_and_access_denied_exits_3(self):
        error = HTTPError("https://api.jquants.com/v2/bulk/list", 401, "Unauthorized", Message(), None)
        c, _ = client([error])
        with self.assertRaises(fj.JQuantsError) as caught:
            c.api("/bulk/list", {"endpoint": "/markets/calendar"})
        self.assertEqual(caught.exception.status, 401)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fj, "load_api_key", return_value=SECRET), \
                mock.patch.object(fj, "sync", side_effect=fj.AccessDenied("refused")):
            self.assertEqual(fj.main(["sync", "--data-dir", tmp]), 3)


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
                cal = conn.execute("SELECT t.date, t.hol_div, s.key FROM trading_calendar t "
                                   "JOIN source_files s ON s.id = t.source_id").fetchall()
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
                atm = conn.execute("SELECT a.date, a.atm_iv, s.key FROM n225_option_atm a "
                                   "JOIN source_files s ON s.id = a.source_id").fetchall()
                full = conn.execute("SELECT date, code, iv, open_interest FROM n225_options ORDER BY date, code").fetchall()
                topix = conn.execute("SELECT * FROM topix_daily").fetchall()
            self.assertEqual(atm, [("2024-08-20", 0.3, live)])  # stale 08-21 row is dropped, not kept
            # every option row is kept in full, also the 08-21 rows that give no ATM pair
            self.assertEqual(full, [("2024-08-20", "380001002", 30.0, 5.0), ("2024-08-20", "380002002", 30.0, 5.0),
                                    ("2024-08-21", "380001002", 20.0, 5.0), ("2024-08-21", "380002002", 20.0, 5.0)])
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
                  "ShrtStdVol", "LongStdVol", "ShrtVal", "LongVal", "ShrtNegVal", "LongNegVal", "ShrtStdVal", "LongStdVal"]

        def margin(day, code, long, pub=""):
            return {"PubDate": pub, "Date": day, "Code": code, "IssType": "2", "ShrtVol": "10", "LongVol": long,
                    "ShrtNegVol": "0", "LongNegVol": "0", "ShrtStdVol": "10", "LongStdVol": long,
                    "ShrtVal": "", "LongVal": "", "ShrtNegVal": "", "LongNegVal": "", "ShrtStdVal": "", "LongStdVal": ""}
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


    def test_republished_investor_week_keeps_both_publications(self):
        fields = ["PubDate", "StDate", "EnDate", "Section"] + [f"{g}{k}" for g in fj._INVESTOR_GROUPS
                                                               for k in ("Sell", "Buy", "Tot", "Bal")]

        def week(pub, frgn):
            row = {name: "1" for name in fields}
            row.update({"PubDate": pub, "StDate": "2024-08-26", "EnDate": "2024-08-30", "Section": "TokyoNagoya",
                        "FrgnBal": frgn})
            return row
        key = "equities/investor-types/historical/2024/i_202409.csv.gz"
        files = {key: (T1, gz_csv(fields, [week("2024-09-05", "-5"), week("2024-09-10", "-6")]))}
        with tempfile.TemporaryDirectory() as tmp:
            fj.sync(FakeBulk(files), Path(tmp), ["investor_types"], "2016-10", None)
            fj.build(Path(tmp))
            with sqlite3.connect(Path(tmp) / "jquants.db") as conn:
                rows = conn.execute("SELECT pub_date, frgn_bal FROM investor_types ORDER BY pub_date").fetchall()
            self.assertEqual(rows, [("2024-09-05", -5.0), ("2024-09-10", -6.0)])


class FullLoadTest(unittest.TestCase):
    """Every column of every dataset, master compression, typed cleaning and verbatim duplicates."""

    def build(self, files, datasets):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fj.sync(FakeBulk(files), Path(tmp.name), datasets, "2016-10", None)
        stats = fj.build(Path(tmp.name))
        return stats, sqlite3.connect(Path(tmp.name) / "jquants.db")

    def test_master_snapshots_compress_into_intervals_and_the_view_rebuilds_them(self):
        fields = ["Date", "Code", "CoName", "CoNameEn", "S17", "S17Nm", "S33", "S33Nm", "ScaleCat", "Mkt", "MktNm",
                  "Mrgn", "MrgnNm", "ProdCat"]

        def row(day, code, mkt):
            r = dict.fromkeys(fields, "x")
            r.update({"Date": day, "Code": code, "Mkt": mkt, "CoNameEn": ""})
            return r
        days = ["2024-08-01", "2024-08-02", "2024-08-05", "2024-08-06"]
        snapshots = [row(days[0], "A", "0111"), row(days[1], "A", "0111"), row(days[2], "A", "0112"),
                     row(days[3], "A", "0111"), row(days[0], "B", "0111"), row(days[2], "B", "0111")]
        hist = "equities/master/historical/2024/m_202408.csv.gz"
        live = "equities/master/live/m_20240806.csv.gz"
        stats, conn = self.build({hist: (T1, gz_csv(fields, snapshots)),
                                  live: (T2, gz_csv(fields, [row(days[3], "A", "0112")]))}, ["equities_master"])
        with conn:
            history = conn.execute("SELECT code, valid_from, valid_to, mkt, co_name_en, snapshots "
                                   "FROM equities_master_history ORDER BY code, valid_from").fetchall()
            view = conn.execute("SELECT date, code, mkt FROM equities_master ORDER BY date, code").fetchall()
        # A changes market on 08-05 (and the newer 08-06 file keeps it there); B is missing on 08-02
        self.assertEqual(history, [("A", days[0], days[1], "0111", None, 2), ("A", days[2], days[3], "0112", None, 2),
                                   ("B", days[0], days[0], "0111", None, 1), ("B", days[2], days[2], "0111", None, 1)])
        self.assertEqual(view, [(days[0], "A", "0111"), (days[0], "B", "0111"), (days[1], "A", "0111"),
                                (days[2], "A", "0112"), (days[2], "B", "0111"), (days[3], "A", "0112")])
        self.assertEqual((stats["equities_master_snapshot_rows"], stats["equities_master_history_rows"]), (6, 4))

    def test_margin_alert_reasons_become_flags_and_unusable_numbers_are_counted(self):
        fields = [s for s, _, _ in fj.TABLES["margin_alert"].columns]

        def alert(code, reason, ratio="1.5"):
            r = dict.fromkeys(fields, "0")
            r.update({"PubDate": "2024-08-01", "Code": code, "AppDate": "2024-07-31", "PubReason": reason,
                      "ShrtOutRatio": ratio, "TSEMrgnRegCls": "001"})
            return r
        known = "{'Restricted': '0', 'DailyPublication': '1', 'Monitoring': '0', 'RestrictedByJSF': '0', " \
                "'PrecautionByJSF': '0', 'UnclearOrSecOnAlert': '0'}"
        key = "markets/margin-alert/historical/2024/a_202408.csv.gz"
        stats, conn = self.build({key: (T1, gz_csv(fields, [
            alert("13010", known), alert("13050", "{'Restricted': '1', 'NewReason': '1'}", ratio="*"),
            alert("13060", "not a dict")]))}, ["margin_alert"])
        with conn:
            rows = conn.execute("SELECT code, restricted, daily_publication, pub_reason_extra, shrt_out_ratio, "
                                "tse_margin_reg_cls FROM margin_alert ORDER BY code").fetchall()
            quality = conn.execute("SELECT * FROM build_quality").fetchall()
        self.assertEqual(rows, [("13010", 0, 1, None, 1.5, "001"), ("13050", 1, None, '{"NewReason": "1"}', None, "001"),
                                ("13060", None, None, "not a dict", 1.5, "001")])
        self.assertEqual(quality, [("margin_alert", "shrt_out_ratio", "*", 1)])
        self.assertEqual(stats["quality_issues"], 1)

    def test_fins_times_booleans_and_a_disclosure_moved_to_a_later_file(self):
        fields = list(fj._FINS)

        def disc(day, time, no, flag="true"):
            r = dict.fromkeys(fields, "")
            r.update({"DiscDate": day, "DiscTime": time, "Code": "99840", "DiscNo": no, "DocType": "FY",
                      "MatChgSub": flag, "Sales": "100"})
            return r
        hist = "fins/summary/historical/2017/f_201705.csv.gz"
        live = "fins/summary/live/f_20170512.csv.gz"
        files = {hist: (T1, gz_csv(fields, [disc("2017-05-10", "15:00", "1"), disc("2017-05-10", "15:30", "2", "x")])),
                 live: (T2, gz_csv(fields, [disc("2017-05-12", "16:00:00", "2", "false")]))}
        stats, conn = self.build(files, ["fins_summary"])
        with conn:
            rows = conn.execute("SELECT disc_date, disc_time, disc_no, mat_chg_sub, sales FROM fins_summary "
                                "ORDER BY disc_no").fetchall()
        self.assertEqual(rows, [("2017-05-10", "15:00:00", "1", 1, 100.0), ("2017-05-12", "16:00:00", "2", 0, 100.0)])
        files[live] = (T3, gz_csv(fields, [disc("2017-05-12", "16:00:00", "3")] * 2))
        with self.assertRaisesRegex(fj.JQuantsError, "duplicate"):
            self.build(files, ["fins_summary"])

    def test_verbatim_duplicate_reports_are_kept_and_extra_columns_reported(self):
        fields = [s for s, _, _ in fj.TABLES["short_positions"].columns] + ["NewColumn"]
        report = dict.fromkeys(fields, "-")
        report.update({"DiscDate": "2024-08-01", "CalcDate": "2024-07-30", "Code": "78970", "SSName": "個人",
                       "ShrtPosToSO": "0.006", "ShrtPosShares": "1", "ShrtPosUnits": "1", "PrevRptRatio": "0.005"})
        key = "markets/short-sale-report/historical/2024/s_202408.csv.gz"
        stats, conn = self.build({key: (T1, gz_csv(fields, [report, report]))}, ["short_positions"])
        with conn:
            rows = conn.execute("SELECT code, ss_name, shrt_pos_to_so, prev_rpt_date FROM short_positions").fetchall()
        self.assertEqual(rows, [("78970", "個人", 0.006, "-")] * 2)
        self.assertEqual(stats["unloaded_columns"], {"short_positions": ["NewColumn"]})

    def test_emergency_margin_rows_are_kept_beside_clearing_rows(self):
        key = "derivatives/bars/daily/options/225/historical/2016/o_201611.csv.gz"
        rows = [option("2016-11-10", "17000", pc, kind=kind) for pc in "12" for kind in ("001", "002")]
        for r in rows:
            r["Code"] = f"17000{r['PCDiv']}"  # the same contract code under both kinds
        _, conn = self.build({key: (T1, gz_csv(OPTION_FIELDS, rows))}, ["options225"])
        with conn:
            kinds = conn.execute("SELECT code, em_mrgn_trg_div FROM n225_options ORDER BY 1, 2").fetchall()
            atm = conn.execute("SELECT COUNT(*) FROM n225_option_atm").fetchone()[0]
        self.assertEqual(kinds, [("170001", "001"), ("170001", "002"), ("170002", "001"), ("170002", "002")])
        self.assertEqual(atm, 1)  # ATM uses the clearing-price rows only

    def test_undecided_earnings_dates_are_kept(self):
        fields = ["PubDate", "SchDate", "FQName", "FYE", "Code", "CoName", "CoNameEn"]
        key = "fins/earnings-date/historical/2016/e_201610.csv.gz"
        rows = [{"PubDate": "2016-10-28", "SchDate": "", "FQName": "2Q", "FYE": "0331", "Code": "52850",
                 "CoName": "x", "CoNameEn": "x"},
                {"PubDate": "2016-10-28", "SchDate": "2016-11-10", "FQName": "2Q", "FYE": "0331", "Code": "99840",
                 "CoName": "y", "CoNameEn": "y"}]
        _, conn = self.build({key: (T1, gz_csv(fields, rows))}, ["fins_earnings_date"])
        with conn:
            got = conn.execute("SELECT code, sch_date FROM fins_earnings_date ORDER BY code").fetchall()
        self.assertEqual(got, [("52850", None), ("99840", "2016-11-10")])

    def test_work_dir_build_swaps_the_finished_file_into_the_data_dir(self):
        key = "markets/short-ratio/historical/2024/r_202408.csv.gz"
        fields = ["Date", "S33", "SellExShortVa", "ShrtWithResVa", "ShrtNoResVa"]
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as work:
            fj.sync(FakeBulk({key: (T1, gz_csv(fields, [dict.fromkeys(fields, "1") | {"Date": "2024-08-01"}]))}),
                    Path(data), ["short_ratio"], "2016-10", None)
            fj.build(Path(data), Path(work))
            with sqlite3.connect(Path(data) / "jquants.db") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM short_ratio").fetchone()[0], 1)
            self.assertEqual(sorted(p.name for p in Path(work).iterdir()), [])
            self.assertEqual(sorted(p.name for p in Path(data).iterdir()), ["jquants.db", "raw"])
        with self.assertRaises(ValueError):
            fj.build(Path(data), ROOT / "results")

    def test_query_plans_the_analyses_rely_on(self):
        key = "equities/bars/daily/historical/2024/e_202408.csv.gz"
        fields = [s for s, _, _ in fj.TABLES["equities_daily"].columns]
        rows = [dict.fromkeys(fields, "1") | {"Date": d, "Code": c, "ExRT": ""} for d in ("2024-08-01", "2024-08-02")
                for c in ("13010", "99840")]
        _, conn = self.build({key: (T1, gz_csv(fields, rows))}, ["equities_daily"])
        with conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'sqlite_stat%'").fetchone()[0], 0)
            conn.execute("CREATE TEMP TABLE price_dates (date TEXT PRIMARY KEY)")
            join = " | ".join(r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT e.date, e.code, e.close FROM equities_daily e JOIN price_dates p ON e.date = p.date"))
            by_code = " | ".join(r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT date, close FROM equities_daily WHERE code=? AND date>=?", ("99840", "2024")))
        self.assertNotIn("equities_daily_code_date", join)  # japan_positioning.py: not an index scan + lookups
        self.assertIn("equities_daily_code_date", by_code)  # hl_japan_carry.py and japan_forward.py

    def test_every_dataset_has_a_table(self):
        self.assertEqual(set(fj.TABLES), set(fj.DATASETS))


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
