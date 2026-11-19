"""upload-closed.sh and check-freshness.sh with stub gcloud/python3: when a local database may be removed,
and when the collector counts as no longer delivering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / "orderbook/deploy/gce/upload-closed.sh"
FRESH = ROOT / "orderbook/deploy/gce/check-freshness.sh"

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


if __name__ == "__main__":
    unittest.main()
