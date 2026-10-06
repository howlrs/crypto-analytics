import json
import math
import socket
import struct
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from orderbook import book as bk
from orderbook.analyze import (
    AnalysisConfig, WallTracker, cross_venue, impact_quantile, imbalance_forward, main as analyze_main,
)
from orderbook.collect import (
    CaptureConfig, RateLimited, Stream, Transport, TransportError, backup, capture, collect, parse_dns_a,
    parse_response, request_for, status,
)
from orderbook.store import decode_book, encode_levels


def book(bids, asks, bid_cov=math.inf, ask_cov=math.inf):
    return bk.make_book([tuple(x) for x in bids], [tuple(x) for x in asks],
                        bid_coverage_bp=bid_cov, ask_coverage_bp=ask_cov)


def binance_payload(mid=100.0, levels=5, size=10.0, tick=0.1):
    bids = [[f"{mid - tick / 2 - i * tick:.2f}", str(size)] for i in range(levels)]
    asks = [[f"{mid + tick / 2 + i * tick:.2f}", str(size)] for i in range(levels)]
    return {"lastUpdateId": 1, "E": 1, "T": 2, "bids": bids, "asks": asks}


class BookMathTests(unittest.TestCase):
    def test_parse_levels_rejects_unsorted_and_non_positive(self):
        self.assertEqual(bk.parse_levels([["101", "1"], ["100", "2"]], descending=True), [(101.0, 1.0), (100.0, 2.0)])
        self.assertEqual(bk.parse_levels([{"px": "1.5", "sz": "2", "n": 3}], descending=False), [(1.5, 2.0, 3)])
        for raw in ([["100", "1"], ["101", "1"]], [["100", "0"]], [["100", "1"], ["100", "1"]]):
            with self.assertRaises(ValueError):
                bk.parse_levels(raw, descending=True)
        with self.assertRaisesRegex(ValueError, "crossed"):
            book([(100, 1)], [(100, 1)])

    def test_coverage_is_band_when_side_exhausted_else_furthest_level(self):
        levels = [(99.9, 1.0), (99.5, 1.0), (98.0, 1.0)]
        full = bk.side_coverage_bp(levels, 100.0, exhausted=True)
        self.assertEqual(full, math.inf)
        kept, coverage = bk.truncate(levels, 100.0, full, band_bp=100.0)
        self.assertEqual((len(kept), coverage), (2, 100.0))
        partial = bk.side_coverage_bp(levels, 100.0, exhausted=False)
        self.assertAlmostEqual(partial, 200.0)
        kept, coverage = bk.truncate(levels, 100.0, partial, band_bp=0, max_levels=2)
        self.assertAlmostEqual(coverage, 50.0)

    def test_depth_and_impact_are_unknown_beyond_coverage(self):
        b = book([(99.9, 10), (99.0, 10)], [(100.1, 10), (101.0, 10)], bid_cov=50.0, ask_cov=math.inf)
        self.assertAlmostEqual(bk.depth_usd(b, "ask", 50.0), 1001.0)
        self.assertTrue(math.isnan(bk.depth_usd(b, "bid", 100.0)))
        # Buy 1501 USD: 1001 at 100.1 then 500 at 101.
        expected_vwap = 1501.0 / (10 + 500 / 101.0)
        self.assertAlmostEqual(bk.impact_bp(b, "ask", 1501.0), (expected_vwap / 100.0 - 1) * 1e4)
        self.assertTrue(math.isnan(bk.impact_bp(b, "bid", 1500.0)))  # 99.0 lies beyond 50bp coverage
        self.assertTrue(math.isnan(bk.impact_bp(b, "ask", 1e9)))      # whole side cannot fill it
        self.assertAlmostEqual(bk.imbalance(300, 100), 0.5)
        self.assertTrue(math.isnan(bk.imbalance(np.nan, 100)))

    def test_walls_need_size_multiple_and_share(self):
        bids = [(100 - i * 0.1, 1.0) for i in range(1, 50)] + [(94.0, 30.0)]
        b = book(bids, [(100.1, 1), (100.2, 1), (100.3, 1)])
        found = bk.walls(b, band_bp=1000, multiple=5, min_usd=0, min_share=0.05)
        self.assertEqual([(w["side"], w["px"]) for w in found], [("bid", 94.0)])
        self.assertEqual(bk.walls(b, band_bp=1000, multiple=5, min_usd=0, min_share=0.9), [])

    def test_payload_round_trip_keeps_order_counts_and_infinite_coverage(self):
        b = book([(99.0, 1.5, 3)], [(101.0, 2.0, 1)], ask_cov=12.5)
        again = decode_book(encode_levels(b), math.inf, 12.5)
        self.assertEqual((again.bids, again.asks, again.bid_coverage_bp), ([(99.0, 1.5, 3)], [(101.0, 2.0, 1)], math.inf))


class CollectorTests(unittest.TestCase):
    def test_stream_parsing_and_requests(self):
        self.assertEqual(Stream.parse("hyperliquid:perp:BTC:4").key, "hyperliquid:perp:BTC:4")
        self.assertEqual(Stream.parse("binance:spot:btcusdt").symbol, "BTCUSDT")
        for bad in ("binance:perp:BTCUSDT:4", "hyperliquid:perp:BTC:6", "kraken:spot:XBT", "binance:perp"):
            with self.assertRaises(ValueError):
                Stream.parse(bad)
        cfg = CaptureConfig()
        self.assertEqual(request_for(Stream.parse("hyperliquid:perp:BTC:4"), cfg)[3],
                         {"type": "l2Book", "coin": "BTC", "nSigFigs": 4})
        method, host, path, _, limit = request_for(Stream.parse("bybit:spot:HYPEUSDT"), cfg)
        self.assertEqual((method, host, limit), ("GET", "api.bybit.com", 200))
        self.assertIn("category=spot", path)
        with self.assertRaises(ValueError):
            CaptureConfig(binance_limit=999).validate()

    def test_responses_and_venue_errors(self):
        stream = Stream.parse("bybit:perp:HYPEUSDT")
        with self.assertRaisesRegex(ValueError, "Bybit error"):
            parse_response(stream, {"retCode": 10001, "retMsg": "bad"})
        bids, asks, stamp = parse_response(Stream.parse("binance:perp:BTCUSDT"), binance_payload())
        self.assertEqual((len(bids), stamp), (5, 2))

    def test_capture_records_coverage_from_exhausted_and_truncated_sides(self):
        row = capture(Stream.parse("binance:perp:BTCUSDT"), lambda *a: binance_payload(levels=5),
                      CaptureConfig(binance_limit=5, band_bp=20.0))
        # Five levels returned for a request of five: not exhausted, coverage is the furthest level (~45bp)
        # capped by the 20bp storage band.
        self.assertEqual((row["bid_coverage_bp"], row["ask_coverage_bp"]), (20.0, 20.0))
        stored = decode_book(row["payload"], row["bid_coverage_bp"], row["ask_coverage_bp"])
        self.assertTrue(all(bk.distance_bp(px, stored.mid) <= 20.0 for px, *_ in stored.bids))
        row = capture(Stream.parse("binance:perp:BTCUSDT"), lambda *a: binance_payload(levels=3),
                      CaptureConfig(binance_limit=5, band_bp=0.0))
        self.assertEqual(row["bid_coverage_bp"], math.inf)  # fewer levels than requested: whole side
        crossed = binance_payload()
        crossed["asks"][0][0] = "99.0"
        with self.assertRaises(ValueError):
            capture(Stream.parse("binance:perp:BTCUSDT"), lambda *a: crossed, CaptureConfig(binance_limit=5))

    def test_transport_requires_explicit_choice(self):
        with self.assertRaisesRegex(ValueError, "source-interface"):
            Transport(source_interface=None, dns_server=None, allow_default_route=False)
        with self.assertRaisesRegex(ValueError, "dns-server"):
            Transport(source_interface=None, dns_server="10.0.0.1", allow_default_route=True)
        with self.assertRaisesRegex(ValueError, "system-dns"):
            Transport(source_interface="eth0", dns_server=None, allow_default_route=False)
        with self.assertRaises(TransportError):
            Transport(source_interface="definitely-missing0", dns_server="10.0.0.1", allow_default_route=False)

    def test_dns_parser_follows_compression_and_rejects_mismatch(self):
        qname = b"\x03api\x07example\x03com\x00"
        header = struct.pack(">HHHHHH", 7, 0x8180, 1, 2, 0, 0)
        cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, 4) + b"\x01x\xc0\x10"
        a_rec = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("1.2.3.4")
        data = header + qname + struct.pack(">HH", 1, 1) + cname + a_rec
        self.assertEqual(parse_dns_a(data, 7), ["1.2.3.4"])
        with self.assertRaises(TransportError):
            parse_dns_a(data, 8)
        truncated = struct.pack(">HHHHHH", 7, 0x8380, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
        with self.assertRaisesRegex(TransportError, "truncated"):
            parse_dns_a(truncated, 7)
        for garbage in (b"\x00\x07\x81", data[:-3], header + b"\x3f"):
            with self.assertRaises(TransportError):
                parse_dns_a(garbage, 7)

    def test_route_recheck_detects_a_destination_leaving_the_interface(self):
        routes = {"10.0.0.53": "10.0.0.53 from 10.8.0.2 via 10.8.0.1 dev tun0 uid 1000",
                  "192.0.2.1": "192.0.2.1 from 10.8.0.2 via 10.8.0.1 dev tun0 uid 1000"}
        def fake_run(cmd, **kw):
            return SimpleNamespace(stdout=routes[cmd[4]] + "\n    cache\n")
        with patch("orderbook.collect.interface_ipv4", return_value="10.8.0.2"), \
                patch("orderbook.collect.subprocess.run", side_effect=fake_run):
            transport = Transport(source_interface="tun0", dns_server="10.0.0.53", allow_default_route=False)
            transport._dns_cache["api.example"] = (math.inf, "192.0.2.1")
            self.assertEqual(transport.check_path(), "")
            routes["192.0.2.1"] = "192.0.2.1 from 10.8.0.2 via 192.168.0.1 dev eth0 uid 1000"  # VPN route lost
            self.assertIn("does not use tun0", transport.check_path())
        with patch("orderbook.collect.interface_ipv4", side_effect=["10.8.0.2", "10.8.0.9"]), \
                patch("orderbook.collect.subprocess.run", side_effect=fake_run):
            transport = Transport(source_interface="tun0", dns_server="10.0.0.53", allow_default_route=False)
            self.assertIn("address changed", transport.check_path())


class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


def fake_fetch(fail_symbol=None, error=RuntimeError("boom")):
    state = {"calls": 0}

    def fetch(method, host, path, body=None):
        state["calls"] += 1
        if fail_symbol and fail_symbol in path:
            raise error
        mid = 100.0 + 0.1 * (state["calls"] % 7)
        payload = binance_payload(mid=mid, levels=40, size=10.0)
        payload["bids"][5][1] = "500"  # a persistent bid wall about 55bp below mid
        return payload
    return fetch


class CollectLoopTests(unittest.TestCase):
    streams = [Stream.parse("binance:perp:BTCUSDT"), Stream.parse("binance:spot:BTCUSDT")]

    def _collect(self, root, fetch, **kw):
        clock = FakeClock()
        args = dict(interval_sec=60, count=3, duration_min=None, max_db_mb=100, min_free_gb=0,
                    transport_info={"allow_default_route": True}, clock=clock, sleep=clock.sleep)
        args.update(kw)
        return collect(root / "ob.db", self.streams, CaptureConfig(binance_limit=50), fetch, **args)

    def test_bounded_run_aligns_ticks_and_keeps_other_streams_on_errors(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._collect(root, fake_fetch(fail_symbol="api/v3"))
            self.assertEqual((result["ticks"], result["snapshots"], result["errors"], result["stop_reason"]),
                             (3, 3, 3, "completed"))
            info = status(root / "ob.db")
            self.assertEqual(info["streams"][0]["snapshots"], 3)
            self.assertIn("boom", info["errors"][0]["last_error"])
            self.assertEqual(info["recent_runs"][0]["stop_reason"], "completed")

    def test_rate_limit_transport_loss_and_budget_stop_the_run(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            limited = self._collect(root, fake_fetch(fail_symbol="fapi", error=RateLimited("429")))
            self.assertEqual((limited["ticks"], limited["stop_reason"]), (1, "rate_limited: 429"))
        with TemporaryDirectory() as tmp:
            lost = self._collect(Path(tmp), fake_fetch(fail_symbol="fapi", error=TransportError("vpn down")))
            self.assertEqual(lost["stop_reason"], "transport_unavailable: vpn down")
        with TemporaryDirectory() as tmp:
            guard = self._collect(Path(tmp), fake_fetch(), check_transport=lambda: "interface gone")
            self.assertEqual((guard["ticks"], guard["stop_reason"]), (0, "transport_unavailable: interface gone"))
        with TemporaryDirectory() as tmp:
            full = self._collect(Path(tmp), fake_fetch(), max_db_mb=1e-6)
            self.assertEqual((full["snapshots"], full["stop_reason"]), (0, "db_budget_reached"))
        with TemporaryDirectory() as tmp:
            dead = self._collect(Path(tmp), fake_fetch(fail_symbol="BTCUSDT"), count=10)
            self.assertEqual((dead["ticks"], dead["stop_reason"]), (3, "all_streams_failing"))

    def test_backup_is_a_consistent_copy_and_never_overwrites(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._collect(root, fake_fetch())
            result = backup(root / "ob.db", root / "copy" / "ob.db")
            self.assertEqual(result["snapshots"], 6)
            self.assertFalse(Path(f"{root / 'copy' / 'ob.db'}-wal").exists())
            self.assertEqual(status(root / "copy" / "ob.db")["streams"][0]["snapshots"], 3)
            with self.assertRaises(ValueError):
                backup(root / "ob.db", root / "copy" / "ob.db")

    def test_unexpected_error_is_recorded_not_reported_as_completed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            def broken_sleep(seconds):
                raise RuntimeError("disk gone")
            with self.assertRaisesRegex(RuntimeError, "disk gone"):
                collect(root / "ob.db", self.streams, CaptureConfig(binance_limit=50), fake_fetch(),
                        interval_sec=60, count=3, duration_min=None, max_db_mb=100, min_free_gb=0,
                        transport_info={}, clock=clock, sleep=broken_sleep)
            run = status(root / "ob.db")["recent_runs"][0]
            # The first (aligned, no-sleep) tick committed both streams before the failure.
            self.assertEqual((run["stop_reason"], run["snapshots"]), ("error: RuntimeError: disk gone", 2))


class AnalysisTests(unittest.TestCase):
    cfg = AnalysisConfig(wall_min_usd=0, wall_multiple=3, wall_min_share=0.2, wall_approach_bp=5)

    def _book(self, mid, wall_px=None, wall_size=100.0):
        bids = [(round(mid - 0.05 - i * 0.1, 2), 1.0) for i in range(20)]
        if wall_px is not None:
            bids = [(px, wall_size if px == wall_px else size) for px, size in bids]
        asks = [(round(mid + 0.05 + i * 0.1, 2), 1.0) for i in range(20)]
        return book(bids, asks)

    def test_wall_episode_endings(self):
        tracker = WallTracker("s", self.cfg)
        wall = 99.45
        for t, b in ((0, self._book(100.0, wall)), (60_000, self._book(100.0, wall)), (120_000, self._book(100.0))):
            tracker.update(t, b, bk.walls(b, band_bp=500, multiple=3, min_usd=0, min_share=0.2), 150_000)
        crossed = self._book(100.0, 99.65)
        tracker.update(180_000, crossed, bk.walls(crossed, band_bp=500, multiple=3, min_usd=0, min_share=0.2), 150_000)
        below = self._book(99.3)
        tracker.update(240_000, below, bk.walls(below, band_bp=500, multiple=3, min_usd=0, min_share=0.2), 150_000)
        episodes = {(e["px"], e["end_reason"]): e for e in tracker.finish()}
        first = episodes[(wall, "removed_while_mid_away")]
        self.assertEqual((first["snapshots"], first["duration_sec"]), (2, 60.0))
        self.assertIn((99.65, "price_crossed"), episodes)

    def test_wall_decay_is_measured_against_its_peak(self):
        tracker = WallTracker("s", self.cfg)
        sizes = [100.0, 60.0, 40.0]  # 40 is below half the peak although above half the previous size
        for i, size in enumerate(sizes):
            b = self._book(100.0, 99.45, wall_size=size)
            found = bk.walls(b, band_bp=500, multiple=3, min_usd=5_000, min_share=0.2)  # 40 units is not a wall
            tracker.update(i * 60_000, b, found, 150_000)
        episode = next(e for e in tracker.finish() if e["px"] == 99.45)
        self.assertEqual((episode["snapshots"], episode["end_reason"]), (2, "removed_while_mid_away"))

    def test_cancelled_best_level_wall_is_not_price_crossed(self):
        tracker = WallTracker("s", self.cfg)
        b = self._book(100.0, 99.95)  # the best bid is the wall
        tracker.update(0, b, bk.walls(b, band_bp=500, multiple=3, min_usd=0, min_share=0.2), 150_000)
        tracker.update(60_000, self._book(100.0), [], 150_000)
        self.assertEqual(tracker.finish()[0]["end_reason"], "removed_after_approach")

    def test_single_snapshot_run_still_detects_an_observation_gap(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for start in (1_800_000_000.0, 1_800_036_000.0):  # two one-tick runs ten hours apart
                clock = FakeClock(start)
                collect(root / "ob.db", CollectLoopTests.streams[:1], CaptureConfig(binance_limit=50), fake_fetch(),
                        interval_sec=60, count=1, duration_min=None, max_db_mb=100, min_free_gb=0,
                        transport_info={}, clock=clock, sleep=clock.sleep)
            with redirect_stdout(StringIO()):
                analyze_main(["--db", str(root / "ob.db"), "--output-dir", str(root / "out"), "--wall-min-usd", "0",
                              "--bootstrap-samples", "20"])
            walls = pd.read_csv(root / "out" / "book_walls.csv")
            self.assertIn("observation_gap", set(walls.end_reason))
            manifest = json.loads((root / "out" / "book_manifest.json").read_text())
            self.assertEqual(manifest["inputs"][0]["snapshots_used"], 2)

    def test_observation_gap_closes_walls(self):
        tracker = WallTracker("s", self.cfg)
        b = self._book(100.0, 99.45)
        found = bk.walls(b, band_bp=500, multiple=3, min_usd=0, min_share=0.2)
        tracker.update(0, b, found, 150_000)
        tracker.update(600_000, b, found, 150_000)
        reasons = [e["end_reason"] for e in tracker.finish()]
        self.assertEqual(reasons, ["observation_gap", "censored_at_end"])

    def test_imbalance_bins_are_fixed_and_forward_match_needs_a_nearby_snapshot(self):
        ts = np.arange(0, 40) * 60_000
        imb = np.where(np.arange(40) % 2 == 0, 0.8, -0.8)
        # The move into t+1 follows the imbalance observed at t.
        mid = 100 + np.cumsum(np.r_[0.0, np.where(imb[:-1] > 0, 0.01, -0.01)])
        jitter = np.random.default_rng(3).integers(50, 400, len(ts))  # receive latency must not drop matches
        metrics = pd.DataFrame({"stream": "s", "tick_ms": ts, "received_ms": ts + jitter, "mid": mid,
                                "imbalance_10bp": imb})
        out = imbalance_forward(metrics, AnalysisConfig(horizons_sec=(60.0,), bootstrap_samples=50))
        bins = out[out.row == "bin"].set_index("bin")
        self.assertEqual(int(bins.loc["[0.6,1]", "obs"]), 20)
        self.assertEqual(int(bins.loc["[-1,-0.6)", "obs"]), 19)  # last snapshot has no forward match
        summary = out[out.row == "top_minus_bottom"].iloc[0]
        self.assertEqual(summary.status, "insufficient_sample")  # one day only
        self.assertGreater(summary.mean_fwd_bp, 0)

    def test_cross_venue_pairs_are_not_shrunk_by_a_sparse_stream(self):
        metrics = pd.DataFrame({
            "asset": "BTC", "aggregated": [False, False, True] * 3, "stream": ["a", "b", "c"] * 3,
            "tick_ms": [0, 0, 0, 1, 1, 1, 2, 2, 2], "mid": [100, 100.02, 100.1] * 3,
            "buy_impact_bp_10k": [1, 2, np.nan, 3, 1, np.nan, 0.5, 2, 5],
            "sell_impact_bp_10k": [1.0] * 9})
        out = cross_venue(metrics, AnalysisConfig(notionals_usd=(10_000.0,)))
        buy = out[(out.metric == "impact_bp") & (out.side == "buy")].set_index(["stream_a", "stream_b"])
        self.assertEqual(int(buy.loc[("a", "b"), "common_ticks"]), 3)
        self.assertAlmostEqual(buy.loc[("a", "b"), "a_better_share"], 2 / 3)
        self.assertEqual(int(buy.loc[("a", "c"), "common_ticks"]), 1)
        premium = out[out.metric == "mid_premium_bp"]
        self.assertEqual(list(zip(premium.stream_a, premium.stream_b)), [("a", "b")])  # aggregated c excluded
        self.assertAlmostEqual(premium.diff_p50.iloc[0], (100 / 100.02 - 1) * 1e4)

    def test_unknown_impact_ranks_as_most_expensive(self):
        series = pd.Series([1.0, 2.0, np.nan, np.nan])
        self.assertEqual(impact_quantile(series, 0.25), 2.0)
        self.assertTrue(math.isnan(impact_quantile(series, 0.5)))

    def test_end_to_end_analysis_is_read_only_and_reproducible(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = FakeClock()
            collect(root / "ob.db", CollectLoopTests.streams, CaptureConfig(binance_limit=50), fake_fetch(),
                    interval_sec=60, count=6, duration_min=None, max_db_mb=100, min_free_gb=0,
                    transport_info={}, clock=clock, sleep=clock.sleep)
            before = (root / "ob.db").stat().st_mtime_ns
            args = ["--db", str(root / "ob.db"), "--output-dir", str(root / "out"), "--wall-min-usd", "0",
                    "--bootstrap-samples", "20"]
            with redirect_stdout(StringIO()):
                self.assertEqual(analyze_main(args), 0)
            self.assertEqual((root / "ob.db").stat().st_mtime_ns, before)
            manifest = json.loads((root / "out" / "book_manifest.json").read_text())
            self.assertEqual(manifest["snapshots"], 12)
            self.assertEqual(set(manifest["output_sha256"]), {
                "book_metrics.csv", "book_summary.csv", "book_by_hour.csv", "book_cross_venue.csv",
                "book_walls.csv", "book_wall_summary.csv", "book_imbalance.csv"})
            summary = pd.read_csv(root / "out" / "book_summary.csv")
            self.assertEqual(list(summary.stream), ["binance:perp:BTCUSDT", "binance:spot:BTCUSDT"])
            walls = pd.read_csv(root / "out" / "book_walls.csv")
            # The wall price moves with the synthetic mid, so episodes exist on both streams.
            self.assertEqual(set(walls.stream), {"binance:perp:BTCUSDT", "binance:spot:BTCUSDT"})
            self.assertTrue((walls.snapshots >= 1).all())
            with self.assertRaises(SystemExit), redirect_stdout(StringIO()):
                analyze_main(args)  # non-empty output directory


if __name__ == "__main__":
    unittest.main()
