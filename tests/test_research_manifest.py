import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtests import research_manifest as subject


class ResearchManifestTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        for relative in subject.ANALYSIS_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {relative}\n", encoding="utf-8")
        for directory in subject.RESULT_DIRECTORIES:
            path = root / directory / "artifact.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"artifact {directory}\n", encoding="utf-8")
        db = root / "external" / "market.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"database snapshot")
        return root / "results/research_manifest.json", db

    def _versions(self):
        return patch.object(subject, "_version", return_value="test-version")

    def test_create_verify_records_all_scoped_files_and_database(self):
        with tempfile.TemporaryDirectory() as temp, self._versions():
            root = Path(temp); manifest_path, db = self._fixture(root)
            manifest = subject.create_manifest(manifest_path, root, db)
            self.assertEqual(subject.verify_manifest(manifest_path, root), manifest)
            self.assertEqual(len(manifest["analysis_files"]), len(subject.ANALYSIS_FILES))
            self.assertEqual(len(manifest["result_artifacts"]), len(subject.RESULT_DIRECTORIES))
            self.assertNotIn("results/research_manifest.json", [row["path"] for row in manifest["result_artifacts"]])
            self.assertEqual(manifest["market_db"]["sha256"], hashlib.sha256(b"database snapshot").hexdigest())
            self.assertIn("exploratory", manifest["interpretation_note"])
            self.assertIn("non-confirmatory", manifest["interpretation_note"])
            self.assertTrue((manifest_path.with_name("research_manifest.json.sha256")).is_file())
            self.assertEqual(manifest["environment"]["numpy"], "test-version")

    def test_create_refuses_overwrite_and_detects_changed_artifact_and_database(self):
        with tempfile.TemporaryDirectory() as temp, self._versions():
            root = Path(temp); manifest_path, db = self._fixture(root)
            subject.create_manifest(manifest_path, root, db)
            with self.assertRaises(FileExistsError):
                subject.create_manifest(manifest_path, root, db)
            (root / "results/crowding/artifact.txt").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest input changed"):
                subject.verify_manifest(manifest_path, root)
            (root / "results/crowding/artifact.txt").write_text("artifact results/crowding\n", encoding="utf-8")
            db.write_bytes(b"changed database")
            with self.assertRaisesRegex(ValueError, "market database changed"):
                subject.verify_manifest(manifest_path, root)

    def test_integrity_and_result_set_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp, self._versions():
            root = Path(temp); manifest_path, db = self._fixture(root)
            subject.create_manifest(manifest_path, root, db)
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["scope"]["result_directories"] = []
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical JSON|integrity"):
                subject.verify_manifest(manifest_path, root)
            # Restore from a fresh fixture to exercise extra-artifact detection.
            manifest_path.unlink(); manifest_path.with_name("research_manifest.json.sha256").unlink()
            subject.create_manifest(manifest_path, root, db)
            extra = root / "results/prospective_validation/new.csv"; extra.write_text("new\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "result artifact set changed"):
                subject.verify_manifest(manifest_path, root)

    def test_database_hash_fails_when_identity_changes_during_read(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "market.db"; db.write_bytes(b"before")
            real_hash = subject._sha256_path

            def changing_hash(path: Path) -> str:
                digest = real_hash(path)
                os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
                return digest

            with patch.object(subject, "_sha256_path", side_effect=changing_hash):
                with self.assertRaisesRegex(ValueError, "changed while being hashed"):
                    subject.database_record(db)


if __name__ == "__main__":
    unittest.main()
