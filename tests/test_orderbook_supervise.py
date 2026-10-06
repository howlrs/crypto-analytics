import unittest
from datetime import datetime, timezone
from pathlib import Path

from orderbook.collect import CaptureConfig, Stream, TransportError
from orderbook.supervise import BACKOFF_SEC, chunk_minutes, next_period_start, period_db, supervise


def ts(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class FakeTransport:
    def request(self, *a):
        raise AssertionError("not called by the fake chunk")

    def describe(self):
        return {}

    def check_path(self):
        return ""


class Harness:
    """Fake clock, sleep and chunk runner; each chunk consumes its requested duration."""

    def __init__(self, start, reasons, transport_failures=0):
        self.now, self.reasons, self.failures = start, list(reasons), transport_failures
        self.chunks, self.sleeps, self.events = [], [], []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def make_transport(self):
        if self.failures:
            self.failures -= 1
            raise TransportError("eth1 has no IPv4 address")
        return FakeTransport()

    def run_chunk(self, db, streams, cfg, fetch, **kw):
        self.chunks.append((db.name, kw["duration_min"]))
        reason = self.reasons.pop(0) if self.reasons else "completed"
        if reason == "completed":
            self.now += kw["duration_min"] * 60
        return {"stop_reason": reason, "run_id": len(self.chunks)}

    def run(self, **kw):
        args = dict(data_dir=Path("/data"), streams=[Stream.parse("binance:perp:BTCUSDT")], cfg=CaptureConfig(),
                    interval_sec=60, chunk_min=360, until=None, max_db_mb=100, min_free_gb=0,
                    make_transport=self.make_transport, run_chunk=self.run_chunk, clock=self.clock,
                    sleep=self.sleep, log=self.events.append)
        args.update(kw)
        return supervise(**args)


class SupervisorTests(unittest.TestCase):
    def test_month_helpers(self):
        self.assertEqual(period_db(Path("/d"), ts("2026-12-31T23:59:00")).name, "orderbook-2026-12.db")
        self.assertEqual(period_db(Path("/d"), ts("2026-12-31T23:59:00"), "day").name, "orderbook-2026-12-31.db")
        self.assertEqual(next_period_start(ts("2026-12-31T23:59:00")), ts("2027-01-01T00:00:00"))
        self.assertEqual(next_period_start(ts("2026-10-06T13:00:00"), "day"), ts("2026-10-07T00:00:00"))
        self.assertEqual(chunk_minutes(ts("2026-10-31T23:00:00"), 360, None), 60)
        self.assertEqual(chunk_minutes(ts("2026-10-06T22:00:00"), 360, None, "day"), 120)
        self.assertEqual(chunk_minutes(ts("2026-10-06T00:00:00"), 360, ts("2026-10-06T01:30:00")), 90)

    def test_chunks_roll_over_months_and_stop_at_until(self):
        h = Harness(ts("2026-10-31T20:00:00"), [])
        reason = h.run(until=ts("2026-11-01T08:00:00"))
        self.assertEqual(reason, "until_reached")
        self.assertEqual(h.chunks, [("orderbook-2026-10.db", 240), ("orderbook-2026-11.db", 360),
                                    ("orderbook-2026-11.db", 120)])

    def test_daily_databases_split_at_utc_midnight(self):
        h = Harness(ts("2026-10-06T20:00:00"), [])
        h.run(until=ts("2026-10-07T03:00:00"), period="day")
        self.assertEqual(h.chunks, [("orderbook-2026-10-06.db", 240), ("orderbook-2026-10-07.db", 180)])

    def test_backoff_by_stop_reason_and_terminal_storage_limit(self):
        h = Harness(ts("2026-10-06T00:00:00"), ["transport_unavailable: route lost", "rate_limited: 429",
                                                "all_streams_failing", "error: OperationalError: locked",
                                                "db_budget_reached"])
        self.assertEqual(h.run(), "db_budget_reached")
        self.assertEqual(h.sleeps, [BACKOFF_SEC["transport_unavailable"], BACKOFF_SEC["rate_limited"],
                                    BACKOFF_SEC["all_streams_failing"], BACKOFF_SEC["error"]])

    def test_transport_setup_failure_is_retried_without_running_a_chunk(self):
        h = Harness(ts("2026-10-06T00:00:00"), ["interrupted"], transport_failures=2)
        self.assertEqual(h.run(), "interrupted")
        self.assertEqual(h.sleeps, [BACKOFF_SEC["transport_unavailable"]] * 2)
        self.assertEqual(len(h.chunks), 1)
        self.assertEqual(h.events[0]["event"], "transport_unavailable")


if __name__ == "__main__":
    unittest.main()
