import importlib.util
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


ROOT = os.path.dirname(os.path.dirname(__file__))
SPEC = importlib.util.spec_from_file_location("hl_watch", os.path.join(ROOT, "hl-watch", "hl_watch.py"))
hl_watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hl_watch)


class ObservationHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = hl_watch.DB_PATH
        hl_watch.DB_PATH = os.path.join(self.tmp.name, "observations.db")
        self.conn = hl_watch.get_conn()
        self.old_candidates = hl_watch.CANDIDATES
        hl_watch.CANDIDATES = hl_watch.CandidateSet()

    def tearDown(self):
        self.conn.close()
        hl_watch.DB_PATH = self.old_path
        hl_watch.CANDIDATES = self.old_candidates
        self.tmp.cleanup()

    def test_empty_success_invalidates_an_older_coin_position(self):
        position = {"marginSummary": {"accountValue": "10"}, "assetPositions": [{"position": {
            "coin": "BTC", "szi": "1", "entryPx": "100", "positionValue": "100",
            "liquidationPx": "50", "leverage": {"value": "2", "type": "cross"},
        }}]}
        with patch.object(hl_watch, "hl_info", side_effect=[position, {"marginSummary": {}, "assetPositions": []}]):
            hl_watch.snapshot_position(self.conn, "0xuser")
            self.assertIsNotNone(self.conn.execute(hl_watch.latest_position_cte() +
                "SELECT szi FROM latest_positions WHERE user='0xuser' AND coin='BTC'").fetchone())
            hl_watch.snapshot_position(self.conn, "0xuser")
        self.assertIsNone(self.conn.execute(hl_watch.latest_position_cte() +
            "SELECT szi FROM latest_positions WHERE user='0xuser' AND coin='BTC'").fetchone())
        self.assertEqual("empty", self.conn.execute(
            "SELECT status FROM position_attempts ORDER BY attempt_id DESC LIMIT 1").fetchone()[0])

    def test_failure_preserves_last_success_and_aggregate_freezes_inputs(self):
        response = {"marginSummary": {}, "assetPositions": [{"position": {
            "coin": "BTC", "szi": "1", "positionValue": "1000", "liquidationPx": "80",
        }}]}
        with patch.object(hl_watch, "hl_info", side_effect=[response, None]):
            hl_watch.snapshot_position(self.conn, "0xuser")
            hl_watch.snapshot_position(self.conn, "0xuser")
        self.assertIsNotNone(self.conn.execute(hl_watch.latest_position_cte() +
            "SELECT szi FROM latest_positions WHERE user='0xuser' AND coin='BTC'").fetchone())
        self.conn.execute("INSERT INTO twap_fills(tid, coin, ts, px, sz, side) VALUES (1, 'BTC', ?, 100, 1, 'B')",
                          (hl_watch.now_ms(),))
        hl_watch.CANDIDATES.touch("0xuser", "BTC")
        with patch.object(hl_watch, "fetch_all_mids", return_value={"BTC": 100.0}):
            hl_watch.aggregate_flow(self.conn, ["BTC"])
        frame = self.conn.execute("SELECT frame_json FROM observation_frames").fetchone()[0]
        self.assertIn('"ladder_config"', frame)
        self.assertIn('"declaration_rate"', frame)
        self.assertIn('"price_type":"mid"', frame)

    def test_legacy_rows_remain_readable_until_a_new_successful_attempt(self):
        self.conn.execute("INSERT INTO watch_positions(user, ts, coin, szi) VALUES ('legacy', 1, 'BTC', 2)")
        self.conn.commit()
        self.assertEqual(2, self.conn.execute(hl_watch.latest_position_cte() +
            "SELECT szi FROM latest_positions WHERE user='legacy' AND coin='BTC'").fetchone()[0])

    def test_public_frame_does_not_expose_addresses_by_default(self):
        frame = {"cohort": [{"user": "0xsecret", "coins": ["BTC"]}], "coins": {
            "BTC": {"ladder_positions": [{"user": "0xsecret"}], "twap": {"orders": [{"user": "0xsecret"}]}}}}
        self.assertNotIn("0xsecret", str(hl_watch.public_observation_frame(frame)))

    def test_decomposition_separates_repricing_and_common_empty_closure(self):
        old = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": 100, "bin": "down:1"}]
        current = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": 100, "bin": "down:0"}]
        d = hl_watch._frame_decomposition(old, current, common_cohort={"same"}, old_mark=100,
                                          new_mark=95, config={"band_pct": 10, "range_pct": 30})
        self.assertEqual(0, d["fixed_old_mid_bin_changed_count"])
        self.assertEqual(1, d["repricing_bin_changed_count"])
        self.assertEqual(-100, d["per_bin"]["down:1"]["mid_or_bin_change"])
        self.assertEqual(100, d["per_bin"]["down:0"]["mid_or_bin_change"])
        self.assertEqual(0, d["common_cohort_position_change"])
        self.assertTrue(d["reconciled"])
        closed = hl_watch._frame_decomposition(old, [], common_cohort={"same"})
        self.assertEqual(0, closed["exit_notional"])
        self.assertEqual(-100, closed["common_cohort_position_change"])
        self.assertTrue(closed["reconciled"])

    @staticmethod
    def position(coin="BTC", liq="80", value="1000"):
        return {"assetPositions": [{"position": {"coin": coin, "szi": "1",
                "positionValue": value, "liquidationPx": liq}}], "time": 1234}

    def observe(self, user, response, at):
        with patch.object(hl_watch, "now_ms", return_value=at), patch.object(hl_watch, "hl_info", return_value=response):
            hl_watch.snapshot_position(self.conn, user)

    def freeze(self, at, mid=100.):
        with patch.object(hl_watch, "now_ms", return_value=at), patch.object(hl_watch, "fetch_all_mids", return_value={"BTC": mid}):
            hl_watch.aggregate_flow(self.conn, ["BTC"])
        row = self.conn.execute("SELECT frame_id,frame_json FROM observation_frames ORDER BY frame_id DESC LIMIT 1").fetchone()
        return row[0], json.loads(row[1])

    def test_quality_distinguishes_empty_failed_stale_and_null_liquidation(self):
        now = 10_000_000
        for user in ("empty", "failed", "stale", "null_liq"):
            hl_watch.CANDIDATES.touch(user, "BTC")
        self.observe("empty", {"assetPositions": []}, now - 100)
        self.observe("failed", None, now - 100)
        self.observe("null_liq", self.position(liq=None), now - 100)
        self.observe("stale", self.position(), now - 3_600_001)
        self.observe("stale", None, now - 10)
        _, frame = self.freeze(now)
        quality = frame["coins"]["BTC"]["freshness"]
        self.assertEqual(quality["candidate_denominator"], 4)
        self.assertEqual(quality["fresh_success_addresses"], 2)
        self.assertEqual(quality["fresh_success_rate"], .5)
        self.assertEqual(quality["stale_candidates"], 1)
        self.assertEqual(quality["null_liq"], 1)
        self.assertEqual(quality["attempt_status"]["failure"], 2)
        self.assertEqual(frame["attempts_since_previous"], {"empty": 1, "failure": 2, "success": 2})
        self.assertEqual(frame["coins"]["BTC"]["buckets"], {"excluded_null_liquidation": 1000.})
        states = {s["user"]: s for s in quality["candidate_states"]}
        self.assertGreater(states["stale"]["success_age_ms"], 3_600_000)

    def test_latest_failure_does_not_refresh_or_erase_prior_success_age(self):
        hl_watch.CANDIDATES.touch("same", "BTC")
        self.observe("same", self.position(), 1000)
        self.observe("same", None, 3000)
        _, frame = self.freeze(5000)
        quality = frame["coins"]["BTC"]["freshness"]
        self.assertEqual(quality["fresh_success_addresses"], 1)
        self.assertEqual(quality["candidate_states"][0]["success_age_ms"], 4000)
        self.assertEqual(quality["attempt_status"]["failure"], 1)

    def test_zero_denominator_and_legacy_are_not_new_observation_history(self):
        self.conn.execute("INSERT INTO watch_positions(user,ts,coin,szi,position_value,liq_px) VALUES ('legacy',9000,'BTC',1,1000,80)")
        self.conn.commit()
        _, frame = self.freeze(10_000)
        coin = frame["coins"]["BTC"]
        self.assertIsNone(coin["freshness"]["fresh_success_rate"])
        self.assertIsNone(coin["twap"]["declaration_rate"])
        self.assertEqual(coin["ladder_inputs"], [])
        self.assertEqual(coin["freshness"]["legacy_or_outside_cohort_positions"], 1)

    def test_malformed_account_never_claims_empty_and_omitted_coin_stays_closed(self):
        self.observe("same", self.position(), 1000)
        for i, bad in enumerate(({}, {"assetPositions": None}, {"assetPositions": [None]},
                                 {"assetPositions": [{"position": {}}]})):
            self.observe("same", bad, 2000 + i)
            self.assertEqual(self.conn.execute("SELECT status FROM position_attempts ORDER BY attempt_id DESC LIMIT 1").fetchone()[0], "failure")
        self.observe("same", self.position(coin="ETH"), 3000)
        self.assertEqual(self.conn.execute(hl_watch.latest_position_cte() + "SELECT coin FROM latest_positions").fetchall(), [("ETH",)])

    def test_saved_replay_is_immutable_after_declaration_price_and_cohort_change(self):
        user = "0xprivate_example_001"
        hl_watch.CANDIDATES.touch(user, "BTC")
        self.observe(user, self.position(), 1000)
        self.conn.execute("INSERT INTO twap_orders(twap_id,user,coin,side,status,declared_sz,cum_sz) VALUES (1,?,'BTC','B','active',NULL,0)", (user,))
        self.conn.commit()
        frame_id, first = self.freeze(2000)
        self.conn.execute("UPDATE twap_orders SET declared_sz=20,status='completed'")
        self.conn.commit()
        hl_watch.CANDIDATES.touch("0xprivate_example_002", "BTC")
        self.observe("0xprivate_example_002", self.position(), 2500)
        _, second = self.freeze(3000, 110.)
        saved = json.loads(self.conn.execute("SELECT frame_json FROM observation_frames WHERE frame_id=?", (frame_id,)).fetchone()[0])
        self.assertEqual(saved, first)
        self.assertIsNone(saved["coins"]["BTC"]["twap"]["orders"][0]["declared_sz"])
        output = io.StringIO()
        with patch.object(hl_watch, "hl_info", side_effect=AssertionError("replay must not fetch")), contextlib.redirect_stdout(output):
            hl_watch.cmd_replay(SimpleNamespace(frame_id=frame_id, json=True, include_addresses=False))
        public = json.loads(output.getvalue())
        self.assertNotIn(user, output.getvalue())
        self.assertNotIn("0xprivate", json.dumps(hl_watch.public_observation_frame(second)))
        self.assertEqual(public["coins"]["BTC"]["mid"]["value"], 100.)

    def test_join_only_and_staleness_have_no_common_position_change(self):
        old = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": 100}]
        new = old + [{"user": "join", "szi": 1, "liq_px": 85, "position_value": 200}]
        d = hl_watch._frame_decomposition(old, new, common_cohort={"same"}, old_mark=100, new_mark=100)
        self.assertEqual(d["common_cohort_position_change"], 0)
        self.assertEqual(d["join_notional"], 200)
        self.assertEqual(d["delta_notional"], 200)
        self.assertTrue(d["reconciled"])
        stale = hl_watch._frame_decomposition(old, [], stale_users={"same"}, common_cohort={"same"}, old_mark=100, new_mark=100)
        self.assertEqual(stale["stale_notional"], 100)
        self.assertEqual(stale["common_cohort_position_change"], 0)
        self.assertTrue(stale["reconciled"])

    def test_missing_mid_and_bin_boundary_are_explicit(self):
        self.assertEqual(hl_watch._frame_bin(1, 70, 100, 1, 30), "down:29")
        positions = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": 100}]
        self.assertEqual(hl_watch._frame_buckets(positions, None, {}), {"unknown_mid": 100})

    def test_ttl_departure_is_staleness_in_both_cohort_and_notional_ledger(self):
        hl_watch.CANDIDATES = hl_watch.CandidateSet(ttl_s=1)
        with patch.object(hl_watch.time, "time", return_value=1):
            hl_watch.CANDIDATES.touch("expired", "BTC")
        self.observe("expired", self.position(), 900)
        self.freeze(1000)
        with patch.object(hl_watch.time, "time", return_value=3):
            hl_watch.CANDIDATES.purge_stale()
        _, frame = self.freeze(3000)
        self.assertEqual([s["user"] for s in frame["cohort_changes"]["stale"]], ["expired"])
        self.assertEqual(frame["cohort_changes"]["exited"], [])
        self.assertTrue(frame["cohort_changes"]["reconciled"])
        decomposition = frame["coins"]["BTC"]["decomposition"]
        self.assertEqual(decomposition["stale_notional"], 1000)
        self.assertEqual(decomposition["exit_notional"], 0)
        self.assertEqual(sum(p["staleness"] for p in decomposition["per_bin"].values()), -1000)
        self.assertTrue(decomposition["reconciled"])

    def test_unknown_valuation_cannot_be_reported_as_zero_change(self):
        old = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": None}]
        new = [{"user": "same", "szi": 1, "liq_px": 90, "position_value": 100}]
        result = hl_watch._frame_decomposition(old, new, common_cohort={"same"}, old_mark=100, new_mark=100)
        self.assertIsNone(result["delta_notional"])
        self.assertFalse(result["valuation_complete"])
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["known_subtotal_delta_notional"], 100)

    def test_first_frame_has_no_invented_baseline_but_next_frame_is_comparable(self):
        hl_watch.CANDIDATES.touch("same", "BTC")
        self.observe("same", self.position(), 1000)
        frame_id, first = self.freeze(2000)
        d = first["coins"]["BTC"]["decomposition"]
        self.assertFalse(d["comparison_available"])
        self.assertEqual(d["comparison_unavailable_reason"], "no_previous_observation")
        self.assertIsNone(d["delta_notional"])
        self.assertIsNone(d["join_notional"])
        self.assertFalse(d["reconciled"])
        self.assertTrue(d["valuation_complete"])
        self.assertEqual(d["unknown_valuation_count"], 0)
        self.assertEqual(sum(first["coins"]["BTC"]["buckets"].values()), 1000)
        self.assertFalse(first["cohort_changes"]["comparison_available"])
        self.assertNotIn("joined", first["cohort_changes"])
        output = io.StringIO()
        with patch.object(hl_watch, "hl_info", side_effect=AssertionError("replay must not fetch")), contextlib.redirect_stdout(output):
            hl_watch.cmd_replay(SimpleNamespace(frame_id=frame_id, json=False, include_addresses=False))
        self.assertIn("comparison unavailable", output.getvalue())
        _, second = self.freeze(3000)
        d = second["coins"]["BTC"]["decomposition"]
        self.assertTrue(d["comparison_available"])
        self.assertEqual(d["delta_notional"], 0)
        self.assertEqual(d["join_notional"], 0)
        self.assertTrue(d["reconciled"])

        unknown = hl_watch._frame_decomposition(None, [{"user": "unknown", "position_value": None}])
        self.assertFalse(unknown["valuation_complete"])
        self.assertEqual(unknown["unknown_valuation_count"], 1)
        self.assertFalse(unknown["comparison_available"])
        self.assertIsNone(unknown["delta_notional"])

    def test_observed_empty_baseline_is_distinct_from_missing_history(self):
        self.freeze(1000)
        hl_watch.CANDIDATES.touch("new", "BTC")
        self.observe("new", self.position(), 2000)
        _, frame = self.freeze(3000)
        d = frame["coins"]["BTC"]["decomposition"]
        self.assertTrue(d["comparison_available"])
        self.assertEqual(d["join_notional"], 1000)
        self.assertEqual(d["delta_notional"], 1000)
        self.assertTrue(d["reconciled"])

    def test_coin_discovery_counts_and_new_coin_comparison_are_explicit(self):
        hl_watch.CANDIDATES.touch("btc_user", "BTC")
        hl_watch.CANDIDATES.touch("eth_user", "ETH")
        self.observe("btc_user", self.position(), 1000)
        self.observe("eth_user", None, 1000)
        self.freeze(2000)
        with patch.object(hl_watch, "now_ms", return_value=3000), patch.object(hl_watch, "fetch_all_mids", return_value={"BTC": 100., "ETH": 200.}):
            hl_watch.aggregate_flow(self.conn, ["BTC", "ETH"])
        frame = json.loads(self.conn.execute("SELECT frame_json FROM observation_frames ORDER BY frame_id DESC LIMIT 1").fetchone()[0])
        public = hl_watch.public_observation_frame(frame)
        for coin in ("BTC", "ETH"):
            quality = public["coins"][coin]["freshness"]
            self.assertEqual(quality["candidate_denominator"], 2)
            self.assertEqual(quality["fresh_success_rate"], .5)
            self.assertEqual(quality["discovery_candidate_count"], 1)
        self.assertEqual(public["coins"]["BTC"]["freshness"]["discovery_fresh_success_rate"], 1)
        self.assertEqual(public["coins"]["ETH"]["freshness"]["discovery_fresh_success_rate"], 0)
        self.assertTrue(frame["coins"]["BTC"]["decomposition"]["comparison_available"])
        self.assertFalse(frame["coins"]["ETH"]["decomposition"]["comparison_available"])
        self.assertNotIn("btc_user", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
