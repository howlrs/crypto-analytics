"""update_japan.sh end to end with stub commands: when a refusal leads to the irreversible purge, and when not."""
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "data-tools/purge_gate.sh"
SCRIPT = ROOT / "data-tools/update_japan.sh"


def gate(reason: str, age_seconds: int, **env) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "denied"
        first.write_text("2026-11-12\n")
        stamp = time.time() - age_seconds
        os.utime(first, (stamp, stamp))
        code = subprocess.run(["bash", "--noprofile", "--norc", "-c", f'source "{GATE}"; purge_due {reason} "{first}"'],
                              env={"PATH": os.environ["PATH"], **env}).returncode
    return code == 0


class PurgeGateTest(unittest.TestCase):
    def test_a_plan_refusal_must_last_a_day(self):
        self.assertFalse(gate("plan", 3600))
        self.assertFalse(gate("plan", 23 * 3600))
        self.assertTrue(gate("plan", 24 * 3600 + 5))

    def test_any_other_refusal_must_last_a_week(self):
        self.assertFalse(gate("other", 24 * 3600 + 5))
        self.assertFalse(gate("other", 6 * 86400))
        self.assertTrue(gate("other", 7 * 86400 + 5))

    def test_settings_override_and_a_missing_record_never_purges(self):
        self.assertTrue(gate("plan", 3700, JQ_PURGE_CONFIRM_HOURS="1"))
        code = subprocess.run(["bash", "--noprofile", "--norc", "-c", f'source "{GATE}"; purge_due plan /nonexistent/denied']).returncode
        self.assertNotEqual(code, 0)


class UpdateScriptTest(unittest.TestCase):
    """Runs a copy of the script against stubs, so nothing reaches the network, GCS or the real data."""

    def run_update(self, sync_exit: int, *, cancel=True, refused_for=None, purged=False):
        tmp = Path(self.dir.name)
        repo, work = tmp / "repo", tmp / "work"
        (repo / "data-tools").mkdir(parents=True, exist_ok=True)
        (repo / "backtests").mkdir(exist_ok=True)
        work.mkdir(exist_ok=True)
        for name in ("update_japan.sh", "purge_gate.sh"):
            target = repo / "data-tools" / name
            target.write_text((ROOT / "data-tools" / name).read_text())
            target.chmod(target.stat().st_mode | stat.S_IEXEC)
        (repo / "data-tools/vpn_route.sh").write_text('vpn_check() { return 0; }\n')
        purge = repo / "data-tools/purge_jquants.sh"
        purge.write_text(f'#!/usr/bin/env bash\ntouch "{work}/stub-purge-ran" "{work}/jquants-purged"\n')
        purge.chmod(0o755)
        bin_dir = tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "python3"
        stub.write_text('#!/usr/bin/env bash\n'
                        f'case "$*" in\n  *fetch_hl_xyz*) exit 0 ;;\n  *"fetch_jquants.py sync"*) exit {sync_exit} ;;\n'
                        f'  *"fetch_jquants.py build"*) touch "{work}/stub-build-ran"; exit 0 ;;\n  *) exit 0 ;;\nesac\n')
        stub.chmod(0o755)
        (work / "jquants-cancel-requested").unlink(missing_ok=True)
        if cancel:
            (work / "jquants-cancel-requested").write_text("2026-10-31\n")
        if refused_for is not None:
            denied = work / "jquants-access-denied"
            denied.write_text("2026-11-12\n")
            stamp = time.time() - refused_for
            os.utime(denied, (stamp, stamp))
        if purged:
            (work / "jquants-purged").write_text("2026-11-13\n")
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp), "JQ_WORK_DIR": str(work), "JQ_GCS_URI": "",
               "JQ_DATA_DIR": str(tmp / "jquants"), "JQ_HL_DIR": str(tmp / "xyz")}
        done = subprocess.run(["bash", "--noprofile", "--norc", str(repo / "data-tools/update_japan.sh"), "--force"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return work

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_the_first_refusal_only_records_and_stays_due(self):
        work = self.run_update(3)
        self.assertTrue((work / "jquants-access-denied").exists())
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertFalse((work / "jquants-last-success").exists())  # rechecked next hour

    def test_a_plan_refusal_that_lasted_purges(self):
        work = self.run_update(3, refused_for=25 * 3600)
        self.assertTrue((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())

    def test_another_refusal_after_a_day_does_not_purge(self):
        work = self.run_update(4, refused_for=25 * 3600)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-cancel-requested").exists())

    def test_another_refusal_that_lasted_a_week_purges(self):
        self.assertTrue((self.run_update(4, refused_for=8 * 86400) / "stub-purge-ran").exists())

    def test_no_purge_without_the_cancellation_marker(self):
        work = self.run_update(3, cancel=False, refused_for=30 * 86400)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())

    def test_a_refusal_that_went_away_keeps_the_cancellation(self):
        work = self.run_update(0, refused_for=3600)
        self.assertFalse((work / "jquants-access-denied").exists())
        self.assertTrue((work / "jquants-cancel-requested").exists())  # the period can still end later
        self.assertTrue((work / "stub-build-ran").exists())

    def test_a_renewal_after_a_purge_clears_the_records(self):
        work = self.run_update(0, refused_for=30 * 86400, purged=True)
        for name in ("jquants-access-denied", "jquants-purged", "jquants-cancel-requested"):
            self.assertFalse((work / name).exists(), name)

    def test_a_purge_runs_once(self):
        work = self.run_update(3, refused_for=25 * 3600, purged=True)
        self.assertFalse((work / "stub-purge-ran").exists())
        self.assertTrue((work / "jquants-last-success").exists())


if __name__ == "__main__":
    unittest.main()
