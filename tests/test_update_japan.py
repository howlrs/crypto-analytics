"""update_japan.sh and purge_jquants.sh with stub commands: when a refusal leads to the irreversible purge."""
import os
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "data-tools/purge_gate.sh"
PAST = "not-before 2026-01-01\n"
FUTURE = f"not-before {(datetime.now(timezone.utc) + timedelta(days=30)).date()}\n"


def age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def gate(reason: str, seconds: int, marker: str = "2026-10-31\n", **env) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        first, cancel = Path(tmp) / "denied", Path(tmp) / "cancel"
        first.write_text("2026-11-12\n")
        cancel.write_text(marker)
        age(first, seconds)
        code = subprocess.run(["bash", "--noprofile", "--norc", "-c", f'source "{GATE}"; purge_due {reason} "{first}" "{cancel}"'],
                              env={"PATH": os.environ["PATH"], **env}).returncode
    return code == 0


class PurgeGateTest(unittest.TestCase):
    def test_a_plan_refusal_must_last_a_day(self):
        self.assertFalse(gate("plan", 23 * 3600))
        self.assertTrue(gate("plan", 24 * 3600 + 5))

    def test_any_other_refusal_must_last_a_week(self):
        self.assertFalse(gate("other", 6 * 86400))
        self.assertTrue(gate("other", 7 * 86400 + 5))

    def test_nothing_is_due_before_the_end_of_the_paid_period(self):
        for reason in ("plan", "other"):
            self.assertFalse(gate(reason, 90 * 86400, f"2026-10-31\n{FUTURE}"))
            self.assertTrue(gate(reason, 90 * 86400, f"2026-10-31\n{PAST}"))

    def test_an_unreadable_date_never_allows_a_purge(self):
        self.assertFalse(gate("plan", 90 * 86400, "2026-10-31\nnot-before soon\n"))

    def test_settings_override_and_missing_records_never_purge(self):
        self.assertTrue(gate("plan", 3700, JQ_PURGE_CONFIRM_HOURS="1"))
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.write_text("x")
            for first, marker in (("/nonexistent/a", real), (real, "/nonexistent/b")):
                code = subprocess.run(["bash", "--noprofile", "--norc", "-c", f'source "{GATE}"; purge_due plan "{first}" "{marker}" 9999999999']).returncode
                self.assertNotEqual(code, 0)


class Stubbed(unittest.TestCase):
    """Runs copies of the scripts against stubs, so nothing reaches the network, GCS or the real data."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.repo, self.work, self.bin = self.tmp / "repo", self.tmp / "work", self.tmp / "bin"
        for path in (self.repo / "data-tools", self.repo / "backtests", self.work, self.bin):
            path.mkdir(parents=True)
        for name in ("update_japan.sh", "purge_gate.sh", "purge_jquants.sh"):
            target = self.repo / "data-tools" / name
            target.write_text((ROOT / "data-tools" / name).read_text())
            target.chmod(target.stat().st_mode | stat.S_IEXEC)
        (self.repo / "data-tools/vpn_route.sh").write_text("vpn_check() { return 0; }\n")

    def script(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(0o755)

    def env(self, gcs: str = "") -> dict:
        return {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.tmp), "JQ_WORK_DIR": str(self.work), "JQ_GCS_URI": gcs,
                "JQ_DATA_DIR": str(self.tmp / "jquants"), "JQ_HL_DIR": str(self.tmp / "xyz")}


class UpdateScriptTest(Stubbed):
    def run_update(self, sync_exit: int, *, marker: str | None = PAST, refused_for=None, purged=False, purge_exit=0,
                   build_exit=0, monitor_exit=0, gcs="", force=True):
        purge = self.repo / "data-tools/purge_jquants.sh"
        purge.write_text(f'#!/usr/bin/env bash\ntouch "{self.work}/stub-purge-ran"\n[[ {purge_exit} != 0 ]] || touch "{self.work}/jquants-purged"\nexit {purge_exit}\n')
        self.script("python3", f"""case "$*" in
  *fetch_hl_xyz*) exit 0 ;;
  *"fetch_jquants.py sync"*) touch "{self.work}/stub-sync-ran"; exit {sync_exit} ;;
  *"fetch_jquants.py build"*) touch "{self.work}/stub-build-ran"; exit {build_exit} ;;
  *japan_perp_monitor*) touch "{self.work}/stub-monitor-ran"; exit {monitor_exit} ;;
  *) exit 0 ;;
esac""")
        self.script("gcloud", f'touch "{self.work}/stub-gcloud-ran"')
        if marker is not None:
            (self.work / "jquants-cancel-requested").write_text(f"2026-10-31\n{marker}")
        if refused_for is not None:
            denied = self.work / "jquants-access-denied"
            denied.write_text("2026-11-12\n")
            age(denied, refused_for)
        if purged:
            (self.work / "jquants-purged").write_text("2026-11-13\n")
        done = subprocess.run(["bash", "--noprofile", "--norc", str(self.repo / "data-tools/update_japan.sh"), *(["--force"] if force else [])],
                              env=self.env(gcs), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return self.work

    def test_the_first_refusal_only_records_and_stays_due(self):
        work = self.run_update(3)
        self.assertTrue((work / "jquants-access-denied").exists())
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertFalse((work / "jquants-last-success").exists())  # rechecked next hour

    def test_a_plan_refusal_that_lasted_purges(self):
        work = self.run_update(3, refused_for=25 * 3600)
        self.assertTrue((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())

    def test_nothing_is_deleted_during_the_paid_period_however_long_the_refusal(self):
        for code in (3, 4):
            work = self.run_update(code, marker=FUTURE, refused_for=30 * 86400)
            self.assertFalse((work / "stub-purge-ran").exists())
            self.assertFalse((work / "jquants-last-success").exists())

    def test_another_refusal_after_a_day_does_not_purge(self):
        work = self.run_update(4, refused_for=25 * 3600)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-cancel-requested").exists())

    def test_another_refusal_that_lasted_a_week_purges(self):
        self.assertTrue((self.run_update(4, refused_for=8 * 86400) / "stub-purge-ran").exists())

    def test_a_failed_purge_stays_due(self):
        work = self.run_update(3, refused_for=25 * 3600, purge_exit=1)
        self.assertTrue((work / "stub-purge-ran").exists())
        self.assertFalse((work / "jquants-last-success").exists())

    def test_no_purge_without_the_cancellation_marker(self):
        work = self.run_update(3, marker=None, refused_for=30 * 86400)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())

    def test_manual_only_skips_jquants_on_the_schedule_and_keeps_xyz(self):
        (self.work / "jquants-manual-only").touch()
        work = self.run_update(0, marker=None, force=False, gcs="gs://stub")
        self.assertFalse((work / "stub-sync-ran").exists())
        self.assertFalse((work / "stub-build-ran").exists())
        self.assertFalse((work / "jquants-last-success").exists())
        self.assertTrue((work / "stub-monitor-ran").exists())
        self.assertTrue((work / "hl-last-success").exists())

    def test_manual_only_with_xyz_up_to_date_does_nothing(self):
        (self.work / "jquants-manual-only").touch()
        (self.work / "hl-last-success").touch()
        work = self.run_update(0, marker=None, force=False)
        self.assertFalse((work / "stub-sync-ran").exists())
        self.assertFalse((work / "stub-monitor-ran").exists())

    def test_manual_only_still_fetches_when_asked(self):
        (self.work / "jquants-manual-only").touch()
        work = self.run_update(0, marker=None)
        self.assertTrue((work / "stub-sync-ran").exists())
        self.assertTrue((work / "stub-build-ran").exists())

    def test_the_schedule_fetches_jquants_without_the_switch(self):
        self.assertTrue((self.run_update(0, marker=None, force=False) / "stub-sync-ran").exists())

    def test_a_refusal_that_went_away_keeps_the_cancellation(self):
        work = self.run_update(0, refused_for=3600)
        self.assertFalse((work / "jquants-access-denied").exists())
        self.assertTrue((work / "jquants-cancel-requested").exists())  # the period can still end later
        self.assertTrue((work / "stub-build-ran").exists())

    def test_a_renewal_after_a_purge_clears_the_records(self):
        work = self.run_update(0, refused_for=30 * 86400, purged=True)
        for name in ("jquants-access-denied", "jquants-purged", "jquants-cancel-requested"):
            self.assertFalse((work / name).exists(), name)

    def test_a_manual_purge_while_access_works_keeps_the_marker(self):
        work = self.run_update(0, purged=True)
        self.assertFalse((work / "jquants-purged").exists())  # otherwise it would block the real purge
        self.assertTrue((work / "jquants-cancel-requested").exists())

    def test_a_purge_runs_once(self):
        work = self.run_update(3, refused_for=25 * 3600, purged=True)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())

    def test_a_failed_build_does_not_stop_the_hyperliquid_backup(self):
        work = self.run_update(0, build_exit=1, gcs="gs://stub")
        self.assertTrue((work / "stub-gcloud-ran").exists())
        self.assertFalse((work / "jquants-last-success").exists())  # retried next hour
        self.assertTrue((work / "hl-last-success").exists())

    def test_a_failed_monitor_changes_nothing_else(self):
        work = self.run_update(0, monitor_exit=1)
        self.assertTrue((work / "stub-monitor-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())


class PurgeScriptTest(Stubbed):
    def run_purge(self, ls_body: str):
        data = self.tmp / "jquants"
        data.mkdir()
        (data / "jquants.db").write_text("x")
        self.script("gcloud", f"""echo "$*" >> "{self.work}/gcloud-calls"
case "$*" in
  *"storage ls"*) {ls_body} ;;
esac""")
        done = subprocess.run(["bash", "--noprofile", "--norc", str(self.repo / "data-tools/purge_jquants.sh"), "--yes"],
                              env=self.env("gs://stub-bucket"), capture_output=True, text=True)
        calls = self.work / "gcloud-calls"
        return done, data, calls.read_text() if calls.exists() else ""

    def test_a_listing_failure_stops_before_anything_is_deleted(self):
        done, data, calls = self.run_purge('echo "ERROR: (gcloud.storage.ls) 503 unavailable" >&2; exit 1')
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(data.exists())
        self.assertNotIn("storage rm", calls)
        self.assertFalse((self.work / "jquants-purged").exists())  # so the next hour tries again

    def test_every_listed_version_is_deleted(self):
        done, data, calls = self.run_purge("echo gs://stub-bucket/jquants/raw/a.csv.gz#1; echo gs://stub-bucket/jquants/raw/a.csv.gz#2")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(data.exists())
        self.assertIn("--clear-soft-delete", calls)
        self.assertIn("storage rm -r --all-versions gs://stub-bucket/jquants/", calls)
        self.assertTrue((self.work / "jquants-purged").exists())

    def test_an_empty_backup_is_not_a_failure(self):
        done, data, calls = self.run_purge('echo "ERROR: (gcloud.storage.ls) One or more URLs matched no objects." >&2; exit 1')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(data.exists())
        self.assertNotIn("storage rm", calls)
        self.assertTrue((self.work / "jquants-purged").exists())


if __name__ == "__main__":
    unittest.main()
