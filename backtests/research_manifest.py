#!/usr/bin/env python3
"""Create and verify a fail-closed reproducibility manifest for this analysis.

The manifest inventories the analysis code, documentation, declared Python
dependencies, published result artifacts, and the external ``market.db`` used
to produce them.  It is a provenance record, not a claim of statistical
confirmation: the source studies remain exploratory unless separately sealed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "results/research_manifest.json"
DEFAULT_DB = Path("/mnt/e/Datas/market/market.db")
RESULT_DIRECTORIES = (
    "results/market_regimes",
    "results/strategy_robustness",
    "results/liq_reversion",
    "results/crowding",
    "results/prospective_validation",
)
ANALYSIS_FILES = (
    "README.md",
    "requirements-backtests.txt",
    "backtests/market_regimes.py",
    "backtests/strategy_robustness.py",
    "backtests/liq_reversion.py",
    "backtests/crowding_signals.py",
    "backtests/prospective_validation.py",
    "backtests/research_manifest.py",
    "docs/data-pipeline.md",
    "docs/limitations.md",
    "docs/schema.md",
    "docs/market-regimes.md",
    "docs/strategy-robustness.md",
    "docs/prospective-validation.md",
    "docs/reproducibility.md",
    "tests/test_crowding_manifest.py",
    "tests/test_crowding_timing.py",
    "tests/test_liq_reversion_manifest.py",
    "tests/test_liq_reversion_timing.py",
    "tests/test_market_regimes.py",
    "tests/test_prospective_validation.py",
    "tests/test_research_manifest.py",
    "tests/test_strategy_robustness.py",
)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Path):
        return unicodedata.normalize("NFC", value.as_posix())
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {str(_canonical(key)): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def canonical_bytes(manifest: dict[str, Any], include_integrity: bool = False) -> bytes:
    payload = dict(manifest)
    if not include_integrity:
        payload.pop("integrity", None)
    return (json.dumps(_canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path is outside repository: {path}") from exc


def _record(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"manifest input is missing or not a regular file: {path}")
    before = path.stat()
    digest = _sha256_path(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino):
        raise ValueError(f"file changed while being hashed: {path}")
    return {"path": _relative(path, root), "sha256": digest, "byte_size": before.st_size}


def database_record(db_path: Path) -> dict[str, Any]:
    """Hash an external database only if its identity remains stable throughout."""
    if not db_path.is_file():
        raise FileNotFoundError(f"market database is missing or not a regular file: {db_path}")
    before = db_path.stat()
    digest = _sha256_path(db_path)
    after = db_path.stat()
    identity_before = (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
    identity_after = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if identity_before != identity_after:
        raise ValueError(f"market database changed while being hashed: {db_path}")
    return {
        "path": db_path.resolve().as_posix(),
        "sha256": digest,
        "byte_size": before.st_size,
        "mtime_ns": before.st_mtime_ns,
    }


def _result_paths(root: Path, manifest_path: Path) -> list[Path]:
    excluded = {manifest_path.resolve(), _sidecar(manifest_path).resolve()}
    paths: list[Path] = []
    for relative_dir in RESULT_DIRECTORIES:
        directory = root / relative_dir
        if not directory.is_dir():
            raise FileNotFoundError(f"claimed results directory is missing: {directory}")
        for path in directory.rglob("*"):
            if path.is_file() and path.resolve() not in excluded:
                paths.append(path)
    return sorted(paths, key=lambda path: _relative(path, root))


def _version(name: str) -> str:
    module = __import__(name)
    version = getattr(module, "__version__", None)
    if not isinstance(version, str):
        raise ValueError(f"cannot determine {name} version")
    return version


def _environment() -> dict[str, str]:
    return {
        "python": os.sys.version.split()[0],
        "numpy": _version("numpy"),
        "pandas": _version("pandas"),
        "scipy": _version("scipy"),
    }


def build_manifest(root: Path = ROOT, manifest_path: Path = DEFAULT_MANIFEST, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    """Build, but do not write, the immutable snapshot payload."""
    root, manifest_path, db_path = root.resolve(), manifest_path.resolve(), db_path.resolve()
    inputs = [_record(root / relative, root) for relative in ANALYSIS_FILES]
    artifacts = [_record(path, root) for path in _result_paths(root, manifest_path)]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": {
            "analysis_files": list(ANALYSIS_FILES),
            "result_directories": list(RESULT_DIRECTORIES),
            "excluded_paths": [_relative(manifest_path, root), _relative(_sidecar(manifest_path), root)],
        },
        "analysis_files": inputs,
        "result_artifacts": artifacts,
        "market_db": database_record(db_path),
        "environment": _environment(),
        "interpretation_note": "The source-study inference recorded here is exploratory and non-confirmatory; this manifest proves file provenance, not statistical validity or future performance.",
        "integrity": {},
    }
    manifest["integrity"]["manifest_sha256"] = manifest_hash(manifest)
    return manifest


def create_manifest(manifest_path: Path = DEFAULT_MANIFEST, root: Path = ROOT, db_path: Path = DEFAULT_DB) -> dict[str, Any]:
    """Create once and refuse both manifest and sidecar overwrite."""
    if manifest_path.exists() or _sidecar(manifest_path).exists():
        raise FileExistsError(f"manifest or sidecar already exists: {manifest_path}")
    manifest = build_manifest(root=root, manifest_path=manifest_path, db_path=db_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(canonical_bytes(manifest, include_integrity=True))
    _sidecar(manifest_path).write_text(manifest["integrity"]["manifest_sha256"] + "\n", encoding="ascii")
    return manifest


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid manifest JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def _verify_record(record: dict[str, Any], root: Path) -> None:
    if set(record) != {"path", "sha256", "byte_size"}:
        raise ValueError("invalid repository file record schema")
    relative = record["path"]
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("invalid repository-relative path")
    actual = _record(root / relative, root)
    if actual != record:
        raise ValueError(f"manifest input changed: {relative}")


def verify_manifest(manifest_path: Path = DEFAULT_MANIFEST, root: Path = ROOT) -> dict[str, Any]:
    root, manifest_path = root.resolve(), manifest_path.resolve()
    manifest = _read_manifest(manifest_path)
    if manifest_path.read_bytes() != canonical_bytes(manifest, include_integrity=True):
        raise ValueError("manifest is not canonical JSON")
    expected = manifest.get("integrity", {}).get("manifest_sha256")
    digest = manifest_hash(manifest)
    sidecar = _sidecar(manifest_path)
    if not isinstance(expected, str) or digest != expected:
        raise ValueError("manifest integrity hash mismatch")
    if not sidecar.is_file() or sidecar.read_text(encoding="ascii").strip() != digest:
        raise ValueError("manifest sidecar hash mismatch")
    required = {"schema_version", "scope", "analysis_files", "result_artifacts", "market_db", "environment", "interpretation_note", "integrity"}
    if not required.issubset(manifest):
        raise ValueError("manifest is missing required fields")
    if manifest["schema_version"] != 1 or "exploratory" not in manifest["interpretation_note"] or "non-confirmatory" not in manifest["interpretation_note"]:
        raise ValueError("manifest schema or interpretation note is invalid")
    scope = manifest["scope"]
    if scope.get("analysis_files") != list(ANALYSIS_FILES) or scope.get("result_directories") != list(RESULT_DIRECTORIES):
        raise ValueError("manifest scope differs from the declared analysis snapshot")
    expected_exclusions = [_relative(manifest_path, root), _relative(_sidecar(manifest_path), root)]
    if scope.get("excluded_paths") != expected_exclusions:
        raise ValueError("manifest self-exclusion is invalid")
    analysis_paths = [record.get("path") for record in manifest["analysis_files"]]
    if analysis_paths != list(ANALYSIS_FILES):
        raise ValueError("analysis file inventory differs from the declared scope")
    for record in manifest["analysis_files"] + manifest["result_artifacts"]:
        _verify_record(record, root)
    recorded_paths = [record["path"] for record in manifest["result_artifacts"]]
    if recorded_paths != [_relative(path, root) for path in _result_paths(root, manifest_path)]:
        raise ValueError("result artifact set changed")
    db = manifest["market_db"]
    if set(db) != {"path", "sha256", "byte_size", "mtime_ns"}:
        raise ValueError("invalid market database record schema")
    actual_db = database_record(Path(db["path"]))
    if actual_db != db:
        raise ValueError("market database changed")
    if manifest["environment"] != _environment():
        raise ValueError("Python analysis environment changed")
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "create":
        manifest = create_manifest(args.manifest, ROOT, args.db)
    else:
        manifest = verify_manifest(args.manifest, ROOT)
    print(json.dumps({"manifest": args.manifest.as_posix(), "manifest_sha256": manifest_hash(manifest), "analysis_files": len(manifest["analysis_files"]), "result_artifacts": len(manifest["result_artifacts"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
