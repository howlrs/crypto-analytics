"""The GCE collector's shell scripts with stub commands: when a local database may be removed, when the
collector counts as no longer delivering, what the heartbeat reports, and what the alert setup creates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / "orderbook/deploy/gce/upload-closed.sh"
FRESH = ROOT / "orderbook/deploy/gce/check-freshness.sh"
HEARTBEAT = ROOT / "orderbook/deploy/gce/heartbeat.sh"
ALERTING = ROOT / "orderbook/deploy/gce/alerting.sh"

GCLOUD_STUB = r"""#!/usr/bin/env bash
# Stub: a directory stands in for the bucket.
set -euo pipefail
echo "$*" >> "$BUCKET_DIR/../gcloud.log"
if [[ "$1 $2" == "storage cp" ]]; then
  src=${@: -2:1}; dest=${@: -1}
  [ -e "$BUCKET_DIR/$(basename "$dest")" ] || cp "$src" "$BUCKET_DIR/$(basename "$dest")"
elif [[ "$1 $2 $3" == "storage objects describe" ]]; then
  file="$BUCKET_DIR/$(basename "$4")"
  [ -e "$file" ] || exit 1
  stat -c %s "$file"
elif [[ "$1 $2" == "storage ls" ]]; then
  [ -z "${GCLOUD_FAIL_LS:-}" ] || exit 1
  for file in "$BUCKET_DIR"/*; do [ -e "$file" ] && echo "${3%/}/$(basename "$file")"; done
  exit 0
else
  echo "unexpected gcloud call: $*" >&2; exit 64
fi
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
# Stub for `python3 -m orderbook.collect backup --db X --output Y`: a plain copy.
set -euo pipefail
while [ $# -gt 0 ]; do
  case "$1" in --db) db=$2; shift 2 ;; --output) out=$2; shift 2 ;; *) shift ;; esac
done
cp "$db" "$out"
"""


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


class Sandbox:
    def __init__(self, temp: str) -> None:
        self.root = Path(temp)
        self.data, self.stage, self.bucket, self.bin = (self.root / name for name in ("data", "stage", "bucket", "bin"))
        for directory in (self.data, self.stage, self.bucket, self.bin):
            directory.mkdir()
        for name, body in (("gcloud", GCLOUD_STUB), ("python3", PYTHON_STUB)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": f"{self.bin}:{os.environ['PATH']}", "DATA": str(self.data), "STAGE": str(self.stage),
                "BUCKET_DIR": str(self.bucket), **extra}

    def db(self, offset: int, *, uploaded: bool = False, in_bucket: bytes | None = None, wal: bool = False) -> Path:
        name = f"orderbook-{_day(offset)}.db"
        path = self.data / name
        path.write_bytes(b"local-" + name.encode())
        if wal:
            (self.data / f"{name}-wal").write_bytes(b"wal")
        if uploaded:
            (self.stage / f"{name}.uploaded").touch()
        if in_bucket is not None:
            (self.bucket / name).write_bytes(in_bucket)
        return path

    def run(self, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "--noprofile", "--norc", str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True)


class UploadClosedTest(unittest.TestCase):
    def test_uploads_closed_days_and_prunes_only_confirmed_old_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            today = box.db(0)
            yesterday = box.db(1)
            old_confirmed = box.db(20, uploaded=True, in_bucket=b"copy", wal=True)
            old_missing = box.db(21, uploaded=True)                       # marker but no object in the bucket
            old_empty = box.db(22, uploaded=True, in_bucket=b"")           # zero-size object is not a copy
            old_new_upload = box.db(15)                                   # uploaded now, already past KEEP_DAYS
            edge = box.db(14, uploaded=True, in_bucket=b"copy")            # exactly KEEP_DAYS old: kept
            result = box.run(UPLOAD, "gs://bucket/orderbook")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(today.exists())
            self.assertFalse((box.bucket / today.name).exists())            # the open day is never uploaded
            self.assertTrue(yesterday.exists())                             # recent: uploaded and kept
            self.assertEqual((box.bucket / yesterday.name).read_bytes(), yesterday.read_bytes())
            self.assertTrue((box.stage / f"{yesterday.name}.uploaded").exists())
            self.assertFalse(old_confirmed.exists())
            self.assertFalse((box.data / f"{old_confirmed.name}-wal").exists())
            self.assertEqual((box.bucket / old_confirmed.name).read_bytes(), b"copy")   # the bucket copy is untouched
            self.assertTrue(old_missing.exists())
            self.assertTrue(old_empty.exists())
            self.assertFalse(old_new_upload.exists())
            self.assertTrue((box.bucket / old_new_upload.name).exists())
            self.assertTrue(edge.exists())
            self.assertIn(f"pruned {old_confirmed.name}", result.stdout)
            self.assertIn(f"kept {old_missing.name}", result.stderr)
            self.assertIn(f"kept {old_empty.name}", result.stderr)
            self.assertEqual(list(box.stage.glob("*.db")), [])               # no staged copy left behind

    def test_second_run_uploads_nothing_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            box.db(1)
            box.run(UPLOAD, "gs://bucket/orderbook")
            log = box.root / "gcloud.log"
            first = log.read_text(encoding="utf-8").count("storage cp")
            result = box.run(UPLOAD, "gs://bucket/orderbook")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").count("storage cp"), first)

    def test_keep_days_can_be_changed_and_must_be_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            three_days = box.db(3, uploaded=True, in_bucket=b"copy")
            for bad in ("0", "-1", "two", ""):
                result = box.run(UPLOAD, "gs://bucket/orderbook", KEEP_DAYS=bad) if bad else None
                if result is not None:
                    self.assertEqual(result.returncode, 2, bad)
                    self.assertTrue(three_days.exists())
            self.assertEqual(box.run(UPLOAD, "gs://bucket/orderbook").returncode, 0)
            self.assertTrue(three_days.exists())                             # default keeps 14 days
            self.assertEqual(box.run(UPLOAD, "gs://bucket/orderbook", KEEP_DAYS="2").returncode, 0)
            self.assertFalse(three_days.exists())


class CheckFreshnessTest(unittest.TestCase):
    @staticmethod
    def _epoch(text: str) -> str:
        return str(int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()))

    def _bucket(self, box: Sandbox, *days: str) -> None:
        for day in days:
            (box.bucket / f"orderbook-{day}.db").write_bytes(b"x")

    def test_fresh_when_the_last_closed_day_has_arrived(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            self._bucket(box, "2026-11-17", "2026-11-18")
            result = box.run(FRESH, "gs://bucket/orderbook", NOW_EPOCH=self._epoch("2026-11-19T03:30:00"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("fresh: newest daily database is 2026-11-18", result.stdout)

    def test_stale_reports_how_many_days_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            self._bucket(box, "2026-11-04", "2026-11-05")
            result = box.run(FRESH, "gs://bucket/orderbook", NOW_EPOCH=self._epoch("2026-11-19T03:30:00"))
            self.assertEqual(result.returncode, 1)
            self.assertIn("newest daily database is 2026-11-05, expected 2026-11-18 (13 day(s) missing)", result.stdout)

    def test_upload_lag_is_allowed_only_for_the_first_hour_of_the_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            self._bucket(box, "2026-11-17")
            early = box.run(FRESH, "gs://bucket/orderbook", NOW_EPOCH=self._epoch("2026-11-19T00:30:00"))
            late = box.run(FRESH, "gs://bucket/orderbook", NOW_EPOCH=self._epoch("2026-11-19T01:30:00"))
            self.assertEqual(early.returncode, 0, early.stdout)
            self.assertEqual(late.returncode, 1, late.stdout)
            self.assertIn("(1 day(s) missing)", late.stdout)

    def test_empty_bucket_is_stale_and_a_listing_failure_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            box = Sandbox(temp)
            (box.bucket / "notes.txt").write_text("not a database", encoding="utf-8")
            empty = box.run(FRESH, "gs://bucket/orderbook", NOW_EPOCH=self._epoch("2026-11-19T03:30:00"))
            self.assertEqual(empty.returncode, 1)
            self.assertIn("no daily database", empty.stdout)
            failed = box.run(FRESH, "gs://bucket/orderbook", GCLOUD_FAIL_LS="1")
            self.assertEqual(failed.returncode, 2)
            self.assertIn("could not list", failed.stderr)


SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "${SERVICE_STATE:-active}"
[ "${SERVICE_STATE:-active}" = active ]
"""

LOGGING_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$LOG_DIR/gcloud.log"
[ -z "${GCLOUD_FAIL:-}" ] || exit 1
"""


def _snapshot_db(path: Path, rows: list[tuple[str, int]]) -> None:
    """A daily database with the collector's column names; rows are (stream key, received seconds)."""
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE snapshots (snapshot_id INTEGER PRIMARY KEY, venue TEXT, market TEXT, symbol TEXT, "
                    "aggregation TEXT, received_ms INTEGER)")
        for key, received in rows:
            venue, market, symbol, aggregation = (key.split(":") + [""])[:4]
            con.execute("INSERT INTO snapshots (venue, market, symbol, aggregation, received_ms) VALUES (?,?,?,?,?)",
                        (venue, market, symbol, aggregation, received * 1000))


class HeartbeatTest(unittest.TestCase):
    NOW = int(datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc).timestamp())
    STREAMS = [f"v{i}:perp:S{i}" for i in range(11)]

    def _box(self, temp: str) -> tuple[Path, Path, Path, dict]:
        root = Path(temp)
        data, stage, bin_dir = root / "data", root / "stage", root / "bin"
        for directory in (data, stage, bin_dir):
            directory.mkdir()
        for name, body in (("systemctl", SYSTEMCTL_STUB), ("gcloud", LOGGING_STUB)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "DATA": str(data), "STAGE": str(stage),
               "CODE_LINK": str(root / "code"), "LOG_DIR": str(root), "NOW_EPOCH": str(self.NOW)}
        return root, data, stage, env

    def _run(self, env: dict, **extra: str) -> tuple[subprocess.CompletedProcess, dict]:
        result = subprocess.run(["bash", "--noprofile", "--norc", str(HEARTBEAT)], env={**env, **extra},
                                capture_output=True, text=True)
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        return result, payload

    def test_healthy_collector_is_ok_and_written_as_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, data, stage, env = self._box(temp)
            _snapshot_db(data / "orderbook-2026-11-19.db", [(key, self.NOW - 30) for key in self.STREAMS])
            _snapshot_db(data / "orderbook-2026-11-18.db", [(key, self.NOW - 50_000) for key in self.STREAMS])
            (stage / "orderbook-2026-11-18.db.uploaded").touch()
            result, payload = self._run(env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["problems"], "")
            self.assertEqual((payload["newest_age_sec"], payload["streams_recent"], payload["pending_uploads"]), (30, 11, 0))
            log = (root / "gcloud.log").read_text(encoding="utf-8")
            self.assertIn("logging write orderbook-heartbeat", log)
            self.assertIn("--severity=INFO", log)

    def test_each_failure_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, data, stage, env = self._box(temp)
            # ten streams are fresh, one stopped 20 minutes ago; yesterday closed 12 hours ago and is not uploaded
            rows = [(key, self.NOW - 400) for key in self.STREAMS[:10]] + [(self.STREAMS[10], self.NOW - 1200)]
            _snapshot_db(data / "orderbook-2026-11-19.db", rows)
            _snapshot_db(data / "orderbook-2026-11-18.db", [(self.STREAMS[0], self.NOW - 50_000)])
            result, payload = self._run(env, SERVICE_STATE="failed")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["status"], "stale")
            self.assertEqual(payload["problems"].split(","),
                             ["service_failed", "newest_snapshot_400s_old", "streams_0_of_11", "uploads_pending_1"])
            self.assertIn("--severity=ERROR", (root / "gcloud.log").read_text(encoding="utf-8"))

    def test_missing_stream_and_upload_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, data, _, env = self._box(temp)
            _snapshot_db(data / "orderbook-2026-11-19.db", [(key, self.NOW - 10) for key in self.STREAMS[:10]])
            _snapshot_db(data / "orderbook-2026-11-18.db", [])
            just_after_midnight = int(datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc).timestamp())
            _, early = self._run(env, NOW_EPOCH=str(just_after_midnight))
            self.assertNotIn("uploads_pending", early["problems"])       # 90 minutes of grace after the day closes
            _, payload = self._run(env)
            self.assertEqual(payload["problems"].split(","), ["streams_10_of_11", "uploads_pending_1"])

    def test_no_database_dry_run_and_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _, env = self._box(temp)
            result, payload = self._run(env, DRY_RUN="1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(payload["newest_age_sec"], -1)
            self.assertIn("no_snapshots", payload["problems"])
            self.assertFalse((root / "gcloud.log").exists())             # dry run writes nothing
            failed, _ = self._run(env, GCLOUD_FAIL="1")
            self.assertEqual(failed.returncode, 3)
            bad, _ = self._run(env, MAX_AGE_SEC="five")
            self.assertEqual(bad.returncode, 2)


ALERT_GCLOUD_STUB = r"""#!/usr/bin/env bash
# Stub: files in $STATE stand for existing resources; every call is logged.
set -euo pipefail
printf '%s\n' "$*" >> "$STATE/calls.log"
args=" $* "
case "$args" in
  *" config get-value account "*) echo "someone@example.invalid" ;;
  *" logging metrics describe "*) [ -e "$STATE/metric" ] ;;
  *" logging metrics create "*) touch "$STATE/metric" ;;
  *" monitoring channels list "*) [ -e "$STATE/channel" ] && cat "$STATE/channel" || true ;;
  *" monitoring channels create "*) echo "projects/p/notificationChannels/1" | tee "$STATE/channel" ;;
  *" monitoring policies list "*) [ -e "$STATE/policy" ] && cat "$STATE/policy" || true ;;
  *" monitoring policies create "*)
    file=$(printf '%s\n' "$@" | sed -n 's/^--policy-from-file=//p'); cp "$file" "$STATE/policy.json"
    echo "projects/p/alertPolicies/9" | tee "$STATE/policy" ;;
  *" monitoring policies update "*)
    file=$(printf '%s\n' "$@" | sed -n 's/^--policy-from-file=//p'); cp "$file" "$STATE/policy.json" ;;
  *) : ;;
esac
"""


class AlertingTest(unittest.TestCase):
    def _run(self, state: Path, **extra: str) -> subprocess.CompletedProcess:
        bin_dir = state / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "gcloud"
        stub.write_text(ALERT_GCLOUD_STUB, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "STATE": str(state), "PROJECT": "p",
               "ALERT_EMAIL": "alerts@example.invalid", **extra}
        return subprocess.run(["bash", "--noprofile", "--norc", str(ALERTING)], env=env, capture_output=True, text=True)

    def test_first_run_creates_and_second_run_updates_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            first = self._run(state)
            self.assertEqual(first.returncode, 0, first.stderr)
            calls = (state / "calls.log").read_text(encoding="utf-8")
            self.assertIn("services enable logging.googleapis.com monitoring.googleapis.com", calls)
            self.assertIn("--member=serviceAccount:orderbook-collector@p.iam.gserviceaccount.com "
                          "--role=roles/logging.logWriter", calls)
            self.assertIn('logName="projects/p/logs/orderbook-heartbeat" AND jsonPayload.status="ok"', calls)
            self.assertEqual(calls.count("logging metrics create"), 1)
            self.assertEqual(calls.count("channels create"), 1)
            self.assertEqual(calls.count("policies create"), 1)
            policy = json.loads((state / "policy.json").read_text(encoding="utf-8"))
            condition = policy["conditions"][0]["conditionAbsent"]
            self.assertEqual(condition["duration"], "1800s")
            self.assertIn("logging.googleapis.com/user/orderbook_heartbeat_ok", condition["filter"])
            self.assertEqual(policy["notificationChannels"], ["projects/p/notificationChannels/1"])
            second = self._run(state)
            self.assertEqual(second.returncode, 0, second.stderr)
            calls = (state / "calls.log").read_text(encoding="utf-8")
            self.assertEqual(calls.count("logging metrics create"), 1)
            self.assertEqual(calls.count("channels create"), 1)
            self.assertEqual(calls.count("policies create"), 1)
            self.assertEqual(calls.count("policies update projects/p/alertPolicies/9"), 1)
            self.assertIn("updated policy", second.stdout)

    def test_address_is_required_and_the_absence_window_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            missing = self._run(state, ALERT_EMAIL="")
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("ALERT_EMAIL", missing.stderr)
            short = self._run(state, ORDERBOOK_ALERT_ABSENT_SEC="60")
            self.assertEqual(short.returncode, 2)
            self.assertFalse((state / "policy.json").exists())


if __name__ == "__main__":
    unittest.main()
