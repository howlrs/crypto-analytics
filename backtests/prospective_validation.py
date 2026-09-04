#!/usr/bin/env python3
"""Sealed, prospective validation registry for the event-study strategy families.

``create`` freezes hypotheses using only the 2026 training columns in the
robustness output.  ``verify`` checks that freeze without touching market data.
``evaluate`` replays only explicitly registered sources and deliberately withholds
return statistics until the registered twelve-month observation window is complete.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtests.strategy_robustness import (benjamini_yekutieli, cluster_bootstrap,
                                           normalise_events, stable_seed)

BAR_GAP_TOLERANCE_MS = 60_000
DEFAULT_ANCHOR_REPOSITORY = "https://github.com/howlrs/crypto-analytics.git"
ANCHOR_REQUIRED_PATHS = (
    "results/prospective_validation/registry.json",
    "results/prospective_validation/registry.json.sha256",
    "results/research_manifest.json",
    "results/research_manifest.json.sha256",
)
_TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
RULES = {
    "window": "detect_ts in [evaluation_start, evaluation_end); 12 complete UTC calendar months",
    "baseline": "semantic prefix ends at source_data_cutoff minus maximum registered detect-to-exit lag and bar-gap tolerance; the remainder is quarantine",
    "primary_outcome": "net_bp", "purge": "greedy half-open intervals ordered by entry_ts, exit_ts, detect_ts",
    "minimum": {"events": 30, "active_months": 6}, "bootstrap": {"monthly_clusters": True, "samples": 2000, "one_sided": "positive"},
    "primary_fdr": "global BY q <= 0.10", "shadow": "nonconfirmatory; never auto-promoted",
    "disclosure": "No interim return, CI, p, or q statistics before complete coverage and follow-up.",
    "immutability_guardrail": "Hashes detect recorded input changes; true immutability requires a git commit/tag anchor.",
    "followup": f"evaluation_end plus the largest registered detect-to-exit lag; future lags may exceed it only by {BAR_GAP_TOLERANCE_MS:,} ms bar-gap tolerance.",
}
RESULT_COLUMNS = [
    "strategy", "family", "symbol", "tier", "confirmatory", "status",
    "events", "active_months", "eligible", "mean_bp", "ci_low_bp",
    "ci_high_bp", "p_positive", "by_q", "pass",
]


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("s")


def _ts(value: Any) -> pd.Timestamp:
    value = pd.Timestamp(value)
    return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")


def _ms(value: Any) -> int:
    return int(_ts(value).value // 1_000_000)


def _next_month(value: pd.Timestamp) -> pd.Timestamp:
    return value.normalize().replace(day=1) + pd.offsets.MonthBegin(1)


def _is_month_start(value: Any) -> bool:
    stamp = _ts(value)
    return stamp == stamp.normalize().replace(day=1)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_hash(path: Path) -> str:
    return _sha(path.read_bytes())


def _canonical(value: Any) -> Any:
    """Canonical JSON domain: NFC strings, sorted maps, and no binary floats."""
    if isinstance(value, Path): return unicodedata.normalize("NFC", value.as_posix())
    if isinstance(value, str): return unicodedata.normalize("NFC", value)
    if isinstance(value, (np.integer,)): return int(value)
    if isinstance(value, (np.floating, float)):
        if not math.isfinite(float(value)): return None
        return float(value).hex()
    if isinstance(value, (pd.Timestamp, datetime)): return _ts(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict): return {str(_canonical(k)): _canonical(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple)): return [_canonical(x) for x in value]
    if value is None or isinstance(value, (bool, int)): return value
    if pd.isna(value): return None
    return str(value)


def canonical_bytes(payload: dict[str, Any], include_integrity: bool = False) -> bytes:
    data = dict(payload)
    if not include_integrity: data.pop("integrity", None)
    return (json.dumps(_canonical(data), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def registry_hash(registry: dict[str, Any]) -> str:
    return _sha(canonical_bytes(registry))


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_for(row: pd.Series, results_root: Path) -> dict[str, Any]:
    family, filename, strategy = str(row.family), str(row.source_file), str(row.strategy)
    directory = "liq_reversion" if family == "liq_reversion" else "crowding"
    source: dict[str, Any] = {"path": (Path(directory) / filename).as_posix()}
    # B2 stores two horizons in one raw file; the suffix is the unambiguous selector.
    if family == "crowding_B2": source["selector"] = {"horizon": strategy.rsplit("_", 1)[-1]}
    return source


def _read_candidate_events(candidate: dict[str, Any], results_root: Path) -> pd.DataFrame:
    source = candidate["source"]
    path = results_root / source["path"]
    if not path.is_file(): raise FileNotFoundError(f"registered source missing: {path}")
    raw = pd.read_csv(path)
    selector = source.get("selector", {})
    for col, value in selector.items():
        if col not in raw: raise ValueError(f"{path}: registered selector column is missing: {col}")
        raw = raw[raw[col].astype(str).eq(str(value))]
    event = normalise_events(raw, str(candidate["strategy"]), str(candidate["family"]))
    return event.sort_values(["entry_ts", "exit_ts", "detect_ts"], kind="stable").reset_index(drop=True)


def semantic_event_hash(events: pd.DataFrame) -> str:
    """Hash timing/return semantics, independent of CSV formatting or extra columns."""
    lines = []
    for row in events.sort_values(["entry_ts", "exit_ts", "detect_ts", "net_bp"], kind="stable").itertuples(index=False):
        lines.append(f"{int(row.detect_ts)}|{int(row.entry_ts)}|{int(row.exit_ts)}|{float(row.net_bp).hex()}")
    return _sha(("\n".join(lines) + ("\n" if lines else "")).encode())


def greedy_replay(events: pd.DataFrame, initial_last_exit: int | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    ordered = events.sort_values(["entry_ts", "exit_ts", "detect_ts"], kind="stable").copy()
    last_exit = -1 if initial_last_exit is None else int(initial_last_exit)
    keep: list[bool] = []
    for row in ordered.itertuples(index=False):
        accepted = int(row.entry_ts) >= last_exit
        keep.append(accepted)
        if accepted: last_exit = int(row.exit_ts)
    kept = ordered.loc[keep].copy().reset_index(drop=True)
    return kept, {"raw_count": int(len(ordered)), "kept_count": int(len(kept)), "purged_count": int(len(ordered) - len(kept)), "last_exit_ts": None if not len(ordered) and initial_last_exit is None else int(last_exit)}


def baseline_snapshot(events: pd.DataFrame, cutoff_ms: int) -> dict[str, Any]:
    prefix = events[events.detect_ts < cutoff_ms].copy()
    _, state = greedy_replay(prefix)
    return {"cutoff_exclusive_ts": int(cutoff_ms), "raw_count": int(len(prefix)), "semantic_hash": semantic_event_hash(prefix), "purge_state": state}


def _baseline_cutoff(regimes: Path) -> int:
    daily = pd.read_csv(regimes)
    if not {"date", "symbol"}.issubset(daily): raise ValueError("daily regimes needs date and symbol")
    daily["date"] = pd.to_datetime(daily.date, utc=True).dt.normalize()
    maxima = daily[daily.symbol.isin(["BTCUSDT", "ETHUSDT"])].groupby("symbol").date.max()
    if set(maxima.index) != {"BTCUSDT", "ETHUSDT"}: raise ValueError("daily regimes requires BTCUSDT and ETHUSDT")
    return int((min(maxima) + pd.Timedelta(days=1)).value // 1_000_000)


def _provenance(paths: Iterable[Path], root: Path) -> dict[str, str]:
    return {_display_path(p, root): _file_hash(p) for p in sorted(set(paths)) if p.is_file()}


def _display_path(path: Path, root: Path) -> str:
    try: return path.relative_to(root).as_posix()
    except ValueError: return path.as_posix()


def _strict_bool(series: pd.Series, name: str) -> pd.Series:
    values = series.astype(str).str.strip().str.lower()
    allowed = {"true": True, "false": False, "1": True, "0": False}
    invalid = ~values.isin(allowed)
    if invalid.any():
        raise ValueError(f"{name} contains non-boolean values")
    return values.map(allowed).astype(bool)


def _candidate_rows(walk: pd.DataFrame) -> pd.DataFrame:
    required = {"strategy", "family", "symbol", "fold_year", "train_eligible", "train_mean_bp", "train_bh_q", "train_by_q", "primary_selection"}
    missing = required - set(walk.columns)
    if missing: raise ValueError(f"walk-forward snapshot missing columns: {sorted(missing)}")
    train = walk[walk.fold_year.eq(2026)].copy()
    if train.strategy.duplicated().any(): raise ValueError("2026 walk-forward rows must have one row per strategy")
    eligible = _strict_bool(train.train_eligible, "train_eligible")
    recorded_primary = _strict_bool(train.primary_selection, "primary_selection")
    train_mean = pd.to_numeric(train.train_mean_bp, errors="coerce")
    train_bh = pd.to_numeric(train.train_bh_q, errors="coerce")
    train_by = pd.to_numeric(train.train_by_q, errors="coerce")
    primary = eligible & train_mean.gt(0) & train_by.le(.10)
    if not recorded_primary.equals(primary):
        raise ValueError("primary_selection does not match the sealed global-BY training rule")
    shadow = eligible & train_mean.gt(0) & train_bh.le(.10) & ~primary
    train["train_eligible"] = eligible
    train["primary_selection"] = primary
    train["tier"] = np.where(primary, "primary", np.where(shadow, "shadow", ""))
    return train[train.tier.ne("")].sort_values(["tier", "family", "strategy"], kind="stable")


def _evidence(row: pd.Series) -> dict[str, Any]:
    # Explicit allow-list prevents test columns entering the sealed decision record.
    keys = ["train_events", "train_months", "train_eligible", "train_mean_bp", "train_ci_low_bp", "train_ci_high_bp", "train_p_positive", "train_p_two_sided", "train_bh_q", "train_by_q", "train_family_bh_q", "train_family_by_q"]
    return {k: _canonical(row[k]) for k in keys if k in row.index}


def create_registry(registry_path: Path, results_root: Path = ROOT / "results", regimes: Path = ROOT / "results/market_regimes/daily_regimes.csv", robustness_dir: Path = ROOT / "results/strategy_robustness", evaluation_start: str | pd.Timestamp | None = None, registered_at: pd.Timestamp | None = None, anchor_tag: str | None = None, anchor_repository: str = DEFAULT_ANCHOR_REPOSITORY) -> dict[str, Any]:
    """Create once.  Refuses overwrite even if a previous registry is identical."""
    if registry_path.exists() or _sidecar(registry_path).exists():
        raise FileExistsError(f"registry or sidecar already exists: {registry_path}")
    now = _ts(registered_at or utc_now())
    start = _ts(evaluation_start) if evaluation_start is not None else _next_month(now)
    if start <= now: raise ValueError("evaluation_start must be strictly after registered_at")
    if not _is_month_start(start):
        raise ValueError("evaluation_start must be 00:00 UTC on the first day of a month")
    anchor_tag = anchor_tag or f"prospective-validation-{start.strftime('%Y-%m-%d')}-v1"
    if (not _TAG_PATTERN.fullmatch(anchor_tag) or ".." in anchor_tag
            or anchor_tag.endswith(("/", ".lock"))):
        raise ValueError("anchor_tag is not a safe Git tag name")
    if not isinstance(anchor_repository, str) or not anchor_repository.startswith("https://"):
        raise ValueError("anchor_repository must be an HTTPS repository URL")
    end = start + pd.DateOffset(months=12)
    walk_path, inventory_path = robustness_dir / "walk_forward_folds.csv", robustness_dir / "strategy_inventory.csv"
    walk, inventory = pd.read_csv(walk_path), pd.read_csv(inventory_path)
    rows = _candidate_rows(walk)
    inventory = inventory.set_index("strategy", drop=False)
    source_data_cutoff = _baseline_cutoff(regimes)
    if source_data_cutoff > _ms(now):
        raise ValueError("source data cutoff cannot be later than registration time")
    if source_data_cutoff > _ms(start):
        raise ValueError("source data cutoff cannot be later than evaluation start")
    candidates = []
    event_cache: dict[str, pd.DataFrame] = {}
    for _, row in rows.iterrows():
        if row.strategy not in inventory.index: raise ValueError(f"candidate absent from inventory: {row.strategy}")
        inv = inventory.loc[row.strategy]
        source = _source_for(inv, results_root)
        candidate = {"strategy": str(row.strategy), "family": str(row.family), "symbol": str(row.symbol), "tier": str(row.tier),
                     "source": source, "horizon_ms": int(inv.horizon_ms), "train_evidence": _evidence(row)}
        if str(row.family) == "liq_reversion":
            candidate["liq_params"] = {k.removeprefix("meta_"): _canonical(inv[k]) for k in inv.index if k.startswith("meta_") and k in {"meta_direction", "meta_ret_threshold", "meta_oi_threshold", "meta_entry_delay_min", "meta_exit_hold_h"}}
        event = _read_candidate_events(candidate, results_root)
        event_cache[candidate["strategy"]] = event
        candidate["max_exit_lag_ms"] = int((event.exit_ts - event.detect_ts).max()) if len(event) else int(candidate["horizon_ms"])
        candidate["max_exit_lag_definition"] = "max(exit_ts - detect_ts) in pre-registration source events"
        candidates.append(candidate)
    candidates.sort(key=lambda x: (x["tier"], x["family"], x["strategy"]))
    max_exit_lag = max([c["max_exit_lag_ms"] for c in candidates] or [0])
    # Source runs omit signals whose exits are not available yet.  Lock only a
    # prefix old enough that a later refresh cannot backfill such an event.
    cutoff = source_data_cutoff - max_exit_lag - BAR_GAP_TOLERANCE_MS
    if cutoff <= 0:
        raise ValueError("source history is too short to establish a stable baseline prefix")
    for candidate in candidates:
        candidate["baseline"] = baseline_snapshot(event_cache[candidate["strategy"]], cutoff)
    inputs = [walk_path, inventory_path, regimes, Path(__file__), ROOT / "backtests/strategy_robustness.py", robustness_dir / "analysis_summary.json", ROOT / "backtests/liq_reversion.py", ROOT / "backtests/crowding_signals.py"]
    registry: dict[str, Any] = {
        "schema_version": 2, "registered_at": now, "evaluation_start": start, "evaluation_end": end,
        "followup_end": end + pd.Timedelta(milliseconds=max_exit_lag),
        "source_data_cutoff_exclusive_ts": source_data_cutoff,
        "baseline_cutoff_exclusive_ts": cutoff, "rules": RULES, "candidates": candidates,
        "selection_snapshot": {"path": _display_path(walk_path, ROOT), "sha256": _file_hash(walk_path), "relevant_columns": [c for c in walk.columns if c.startswith("train_") or c in {"strategy", "family", "symbol", "fold_year", "primary_selection"}]},
        "provenance": _provenance(inputs, ROOT),
        "external_anchor": {
            "kind": "git_annotated_tag",
            "repository": anchor_repository,
            "tag": anchor_tag,
            "required_paths": list(ANCHOR_REQUIRED_PATHS),
        },
        "integrity": {},
    }
    registry["integrity"]["registry_sha256"] = registry_hash(registry)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_bytes(canonical_bytes(registry, include_integrity=True))
    _sidecar(registry_path).write_text(registry["integrity"]["registry_sha256"] + "\n", encoding="ascii")
    return _read_json(registry_path)


def verify_registry(registry_path: Path) -> dict[str, Any]:
    registry = _read_json(registry_path)
    if registry_path.read_bytes() != canonical_bytes(registry, include_integrity=True):
        raise ValueError("registry is not canonical JSON")
    digest = registry_hash(registry)
    expected = registry.get("integrity", {}).get("registry_sha256")
    sidecar = _sidecar(registry_path)
    if not isinstance(expected, str) or digest != expected: raise ValueError("registry integrity hash mismatch")
    if not sidecar.is_file() or sidecar.read_text(encoding="ascii").strip() != digest: raise ValueError("registry sidecar hash mismatch")
    if registry.get("schema_version") != 2:
        raise ValueError("unsupported registry schema version")
    for key in ("registered_at", "evaluation_start", "evaluation_end", "followup_end",
                "source_data_cutoff_exclusive_ts", "baseline_cutoff_exclusive_ts",
                "rules", "candidates", "external_anchor"):
        if key not in registry: raise ValueError(f"registry missing {key}")
    if registry["rules"] != RULES: raise ValueError("registry rules mismatch")
    registered, start = _ts(registry["registered_at"]), _ts(registry["evaluation_start"])
    end, followup = _ts(registry["evaluation_end"]), _ts(registry["followup_end"])
    if start <= registered or not _is_month_start(start):
        raise ValueError("registry evaluation start is not a future UTC month boundary")
    if end != start + pd.DateOffset(months=12):
        raise ValueError("registry evaluation end is not exactly twelve calendar months after start")
    source_cutoff = int(registry["source_data_cutoff_exclusive_ts"])
    cutoff = int(registry["baseline_cutoff_exclusive_ts"])
    if source_cutoff > _ms(registered) or source_cutoff > _ms(start):
        raise ValueError("registry source data cutoff is outside the pre-evaluation period")
    rows = registry["candidates"]
    ids = [(x.get("strategy"), x.get("tier")) for x in rows]
    if len(ids) != len(set(ids)): raise ValueError("duplicate candidates")
    for candidate in rows:
        if candidate.get("tier") not in {"primary", "shadow"}: raise ValueError("invalid candidate tier")
        if not {"strategy", "family", "symbol", "source", "horizon_ms", "max_exit_lag_ms", "train_evidence", "baseline"}.issubset(candidate): raise ValueError("incomplete candidate")
        if any(k.startswith("test_") for k in candidate["train_evidence"]): raise ValueError("test evidence is prohibited")
        source_path = Path(candidate["source"].get("path", ""))
        if source_path.is_absolute() or ".." in source_path.parts or not source_path.parts or source_path.parts[0] not in {"liq_reversion", "crowding"}:
            raise ValueError("candidate source path must be a registered results-relative source")
        if int(candidate["max_exit_lag_ms"]) < int(candidate["horizon_ms"]):
            raise ValueError("candidate detect-to-exit lag is shorter than its holding horizon")
        if int(candidate["baseline"].get("cutoff_exclusive_ts", -1)) != cutoff:
            raise ValueError("candidate baseline cutoff does not match the registry boundary")
    expected_followup = end + pd.Timedelta(milliseconds=max([int(c["max_exit_lag_ms"]) for c in rows] or [0]))
    if followup != expected_followup:
        raise ValueError("registry follow-up boundary does not match the maximum sealed exit lag")
    expected_baseline = source_cutoff - max([int(c["max_exit_lag_ms"]) for c in rows] or [0]) - BAR_GAP_TOLERANCE_MS
    if cutoff != expected_baseline:
        raise ValueError("registry baseline boundary does not leave the sealed resolution buffer")
    anchor = registry["external_anchor"]
    if not isinstance(anchor, dict) or set(anchor) != {"kind", "repository", "tag", "required_paths"}:
        raise ValueError("registry external anchor schema mismatch")
    if anchor["kind"] != "git_annotated_tag":
        raise ValueError("registry external anchor must be an annotated Git tag")
    tag = anchor["tag"]
    if (not isinstance(tag, str) or not _TAG_PATTERN.fullmatch(tag) or ".." in tag
            or tag.endswith(("/", ".lock"))):
        raise ValueError("registry external anchor tag is invalid")
    if not isinstance(anchor["repository"], str) or not anchor["repository"].startswith("https://"):
        raise ValueError("registry external anchor repository is invalid")
    if anchor["required_paths"] != list(ANCHOR_REQUIRED_PATHS):
        raise ValueError("registry external anchor path set mismatch")
    return registry


def _git(args: Sequence[str], cwd: Path, *, text: bool = True) -> str | bytes:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=text,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"" if not text else "")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise ValueError(f"Git anchor verification failed: {str(detail).strip()}") from exc
    return completed.stdout


def verify_external_anchor(registry_path: Path, remote: str = "origin") -> dict[str, str]:
    """Verify that the declared annotated tag is public and contains this snapshot."""
    registry = verify_registry(registry_path)
    anchor = registry["external_anchor"]
    repo_root = Path(str(_git(["rev-parse", "--show-toplevel"], ROOT)).strip()).resolve()
    try:
        registry_path.resolve().relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("registry is outside the Git worktree") from exc
    actual_repository = str(_git(["remote", "get-url", remote], repo_root)).strip()
    if actual_repository.rstrip("/").removesuffix(".git") != anchor["repository"].rstrip("/").removesuffix(".git"):
        raise ValueError("Git remote does not match the registry external anchor repository")
    tag_ref = f"refs/tags/{anchor['tag']}"
    if str(_git(["cat-file", "-t", tag_ref], repo_root)).strip() != "tag":
        raise ValueError("external anchor is not an annotated Git tag")
    commit = str(_git(["rev-parse", f"{tag_ref}^{{commit}}"], repo_root)).strip()
    tag_object = str(_git(["rev-parse", tag_ref], repo_root)).strip()
    advertised = str(_git(["ls-remote", "--tags", remote, tag_ref, f"{tag_ref}^{{}}"], repo_root))
    remote_refs = {line.split()[1]: line.split()[0] for line in advertised.splitlines() if len(line.split()) == 2}
    if remote_refs.get(tag_ref) != tag_object or remote_refs.get(f"{tag_ref}^{{}}") != commit:
        raise ValueError("annotated tag object and commit are not advertised by the declared remote")
    for relative in anchor["required_paths"]:
        current = repo_root / relative
        if not current.is_file():
            raise ValueError(f"anchored file is missing locally: {relative}")
        anchored = _git(["show", f"{tag_ref}:{relative}"], repo_root, text=False)
        if anchored != current.read_bytes():
            raise ValueError(f"anchored file differs from the working tree: {relative}")
    return {"repository": anchor["repository"], "tag": anchor["tag"],
            "tag_object": tag_object, "commit": commit}


def _coverage_meta(manifest: dict[str, Any], symbol: str, kind: str) -> dict[str, Any]:
    """Read the explicit schema-v1 layouts emitted by both source studies."""
    symbol_keys = (symbol, symbol.replace("USDT", ""))
    # liq_reversion: ``kline_coverage[symbol]`` / ``oi_coverage[symbol]``.
    direct = manifest.get(f"{kind}_coverage", {})
    if isinstance(direct, dict):
        for key in symbol_keys:
            if isinstance(direct.get(key), dict):
                return direct[key]
    # crowding_signals: ``sources[symbol][kind]``.
    for container_name in ("sources", "coverage"):
        container = manifest.get(container_name, {})
        if not isinstance(container, dict):
            continue
        for key in symbol_keys:
            item = container.get(key)
            if not isinstance(item, dict):
                continue
            nested = item.get(kind)
            if isinstance(nested, dict):
                return nested
            legacy = item.get(f"{kind}_end_exclusive_ts")
            if legacy is not None:
                return {"coverage_end_exclusive_ts": legacy}
    # A common top-level kline end is retained only as a legacy fallback.
    value = manifest.get(f"{kind}_coverage_end_exclusive_ts")
    if value is None and kind == "kline":
        value = manifest.get("coverage_end_exclusive_ts")
    return {"coverage_end_exclusive_ts": value} if value is not None else {}


def _coverage_value(manifest: dict[str, Any], symbol: str, kind: str) -> int | None:
    value = _coverage_meta(manifest, symbol, kind).get("coverage_end_exclusive_ts")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_inputs(candidate: dict[str, Any]) -> set[str]:
    family = candidate["family"]
    if family == "liq_reversion": return {"kline"} | ({"oi"} if candidate.get("liq_params", {}).get("oi_threshold") is not None else set())
    return {"kline", "funding"} if family in {"crowding_A2", "crowding_A3"} else {"kline", "oi"} if family == "crowding_B2" else {"kline", "funding", "oi"}


def _coverage_status(registry: dict[str, Any], results_root: Path) -> tuple[str, list[str]]:
    notes = []
    generators = {"liq_reversion": "backtests/liq_reversion.py", "crowding": "backtests/crowding_signals.py"}
    for c in registry["candidates"]:
        directory = Path(c["source"]["path"]).parts[0]; manifest_path = results_root / directory / "run_manifest.json"
        if not manifest_path.is_file(): notes.append(f"{c['strategy']}:missing_run_manifest"); continue
        manifest = _read_json(manifest_path)
        expected_script = registry.get("provenance", {}).get(generators[directory])
        if expected_script and manifest.get("script_sha256") != expected_script:
            notes.append(f"{c['strategy']}:generator_script_mismatch")
        filename = Path(c["source"]["path"]).name
        recorded_hash = manifest.get("event_file_sha256", {}).get(filename)
        source_path = results_root / c["source"]["path"]
        if not recorded_hash:
            notes.append(f"{c['strategy']}:source_hash_missing")
        elif not source_path.is_file() or _file_hash(source_path) != recorded_hash:
            notes.append(f"{c['strategy']}:source_hash_mismatch")
        for kind in _required_inputs(c):
            required_end = _ms(registry["followup_end"]) if kind == "kline" else _ms(registry["evaluation_end"])
            meta = _coverage_meta(manifest, c["symbol"], kind)
            if (_coverage_value(manifest, c["symbol"], kind) or -1) < required_end:
                notes.append(f"{c['strategy']}:{kind}_coverage_incomplete")
            if kind in {"kline", "oi", "funding"}:
                try:
                    tail_start = int(meta.get("tail_contiguous_start_ts"))
                except (TypeError, ValueError):
                    notes.append(f"{c['strategy']}:{kind}_continuity_metadata_missing")
                else:
                    if tail_start > int(registry["baseline_cutoff_exclusive_ts"]):
                        notes.append(f"{c['strategy']}:{kind}_gap_after_baseline")
    notes = sorted(set(notes))
    return ("ready" if not notes else "awaiting_source_refresh"), notes


def _empty_result(candidates: list[tuple[dict[str, Any], pd.DataFrame]], status: str) -> pd.DataFrame:
    rows = []
    for c, score in candidates:
        row = {"strategy": c["strategy"], "family": c["family"], "symbol": c["symbol"], "tier": c["tier"],
               "confirmatory": c["tier"] == "primary", "status": status,
               "events": int(len(score)), "active_months": int(score.month.nunique()) if "month" in score else 0}
        for key in ("eligible", "mean_bp", "ci_low_bp", "ci_high_bp", "p_positive", "by_q", "pass"):
            if key not in row: row[key] = None
        rows.append(row)
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def evaluate_registry(registry_path: Path, output_dir: Path, results_root: Path = ROOT / "results", evaluation_time: pd.Timestamp | None = None) -> dict[str, Any]:
    registry = verify_registry(registry_path)
    if output_dir.exists() and any(output_dir.iterdir()): raise FileExistsError(f"evaluation output directory is non-empty: {output_dir}")
    now = _ts(evaluation_time or utc_now())
    # A code change changes interpretation: it must be sealed by a new registry.
    for rel in ("backtests/prospective_validation.py", "backtests/strategy_robustness.py"):
        expected = registry.get("provenance", {}).get(rel)
        if expected and _file_hash(ROOT / rel) != expected: raise ValueError(f"sealed code provenance mismatch: {rel}")
    start_ms, end_ms, cutoff_ms = _ms(registry["evaluation_start"]), _ms(registry["evaluation_end"]), int(registry["baseline_cutoff_exclusive_ts"])
    audits, scored = [], []
    for candidate in registry["candidates"]:
        events = _read_candidate_events(candidate, results_root)
        tolerance = 60_000
        if ((events.exit_ts - events.detect_ts) > int(candidate["max_exit_lag_ms"]) + tolerance).any():
            raise ValueError(f"unexpected detect-to-exit lag for {candidate['strategy']}; registry is invalid")
        baseline = baseline_snapshot(events, cutoff_ms)
        expected = candidate["baseline"]
        if baseline["semantic_hash"] != expected["semantic_hash"] or baseline["raw_count"] != expected["raw_count"] or baseline["purge_state"] != expected["purge_state"]:
            raise ValueError(f"baseline mutation detected for {candidate['strategy']}; evaluation fails closed")
        # Replay the entire chronological source.  Pre-start events are quarantine:
        # their overlap state is carried, but their return is never scored.
        ordered = events.sort_values(["entry_ts", "exit_ts", "detect_ts"], kind="stable")
        last_exit = -1
        keep, zone = [], []
        for row in ordered.itertuples(index=False):
            accepted = int(row.entry_ts) >= last_exit
            if accepted: last_exit = int(row.exit_ts)
            keep.append(accepted)
            zone.append("baseline" if row.detect_ts < cutoff_ms else "quarantine" if row.detect_ts < start_ms else "score" if row.detect_ts < end_ms else "after_window")
        replay = ordered.assign(purge_kept=keep, zone=zone)
        score = replay[(replay.zone == "score") & replay.purge_kept].copy()
        score["month"] = pd.to_datetime(score.detect_ts, unit="ms", utc=True).dt.strftime("%Y-%m")
        audits.append({"strategy": candidate["strategy"], "tier": candidate["tier"], "baseline_raw": int((replay.zone == "baseline").sum()), "quarantine_raw": int((replay.zone == "quarantine").sum()), "score_raw": int((replay.zone == "score").sum()), "score_kept": int(len(score))})
        scored.append((candidate, score))
    coverage, coverage_notes = _coverage_status(registry, results_root)
    start, end, followup = (_ts(registry[key]) for key in ("evaluation_start", "evaluation_end", "followup_end"))
    window_status = ("not_started" if now < start else "collecting" if now < end
                     else "awaiting_followup" if now < followup else "complete")
    window_complete = window_status == "complete"
    ready = window_complete and coverage == "ready"
    status = ("ready" if ready else "awaiting_source_refresh" if window_complete
              else window_status)
    primary = [c for c, _ in scored if c["tier"] == "primary"]
    shadow = [c for c, _ in scored if c["tier"] == "shadow"]
    if not primary and ready: status = "no_confirmatory_hypotheses"
    def results_for(tier: str) -> pd.DataFrame:
        picked = [(c, s) for c, s in scored if c["tier"] == tier]
        if not ready: return _empty_result(picked, status)
        out = []
        for c, s in picked:
            stats = cluster_bootstrap(s.net_bp, s.month, 2000, stable_seed(c["strategy"], "prospective", base_seed=20260904)) if len(s) else {"mean_bp": np.nan, "ci_low_bp": np.nan, "ci_high_bp": np.nan, "p_positive": np.nan}
            eligible = len(s) >= 30 and s.month.nunique() >= 6
            row_status = status if tier == "primary" else "nonconfirmatory_complete"
            out.append({"strategy": c["strategy"], "family": c["family"], "symbol": c["symbol"],
                        "tier": tier, "confirmatory": tier == "primary", "status": row_status,
                        "events": len(s), "active_months": int(s.month.nunique()), "eligible": eligible,
                        "mean_bp": stats["mean_bp"], "ci_low_bp": stats["ci_low_bp"],
                        "ci_high_bp": stats["ci_high_bp"], "p_positive": stats["p_positive"]})
        df = pd.DataFrame(out, columns=[c for c in RESULT_COLUMNS if c not in {"by_q", "pass"}])
        if tier == "primary" and len(df):
            df["by_q"] = benjamini_yekutieli(df.p_positive.where(df.eligible))
            df["pass"] = df.eligible & df.mean_bp.gt(0) & df.by_q.le(.10)
        else:
            df["by_q"] = np.nan
            df["pass"] = pd.NA
        return df.reindex(columns=RESULT_COLUMNS)
    primary_df, shadow_df = results_for("primary"), results_for("shadow")
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audits).to_csv(output_dir / "event_audit.csv", index=False)
    primary_df.to_csv(output_dir / "primary_results.csv", index=False)
    shadow_df.to_csv(output_dir / "shadow_results.csv", index=False)
    output_hashes = {name: _file_hash(output_dir / name) for name in ("event_audit.csv", "primary_results.csv", "shadow_results.csv")}
    manifest = {"registry_sha256": registry_hash(registry), "evaluation_time": now,
                "status": status, "window_status": window_status,
                "window_complete": window_complete, "coverage": coverage, "coverage_notes": coverage_notes,
                "candidate_counts": {"primary": len(primary), "shadow": len(shadow)}, "output_sha256": output_hashes,
                "stats_disclosed": bool(ready), "confirmatory_test_performed": bool(ready and primary),
                "note": "Shadow outcomes remain nonconfirmatory, receive no pass decision, and cannot promote a strategy to primary."}
    (output_dir / "evaluation_manifest.json").write_bytes(canonical_bytes(manifest, include_integrity=True))
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("create", "verify", "evaluate"))
    p.add_argument("--registry", type=Path, default=ROOT / "results/prospective_validation/registry.json")
    p.add_argument("--results-root", type=Path, default=ROOT / "results")
    p.add_argument("--regimes", type=Path, default=ROOT / "results/market_regimes/daily_regimes.csv")
    p.add_argument("--robustness-dir", type=Path, default=ROOT / "results/strategy_robustness")
    p.add_argument("--start")
    p.add_argument("--anchor-tag")
    p.add_argument("--anchor-repository", default=DEFAULT_ANCHOR_REPOSITORY)
    p.add_argument("--require-anchor", action="store_true")
    p.add_argument("--remote", default="origin")
    p.add_argument("--output-dir", type=Path, default=ROOT / "results/prospective_validation/evaluation")
    return p.parse_args(argv)


def _registry_summary(registry: dict[str, Any], registry_path: Path) -> dict[str, Any]:
    tiers = pd.Series([candidate["tier"] for candidate in registry["candidates"]]).value_counts()
    return {
        "registry": registry_path.as_posix(),
        "registry_sha256": registry_hash(registry),
        "registered_at": registry["registered_at"],
        "evaluation_start": registry["evaluation_start"],
        "evaluation_end": registry["evaluation_end"],
        "followup_end": registry["followup_end"],
        "external_anchor": registry["external_anchor"],
        "candidates": {"primary": int(tiers.get("primary", 0)),
                       "shadow": int(tiers.get("shadow", 0))},
    }


if __name__ == "__main__":
    args = parse_args()
    if args.command == "create":
        payload = create_registry(args.registry, args.results_root, args.regimes, args.robustness_dir, args.start,
                                  anchor_tag=args.anchor_tag, anchor_repository=args.anchor_repository)
        print(json.dumps(_registry_summary(payload, args.registry), indent=2, default=str))
    elif args.command == "verify":
        payload = verify_registry(args.registry)
        summary = _registry_summary(payload, args.registry)
        if args.require_anchor:
            summary["anchor_verification"] = verify_external_anchor(args.registry, args.remote)
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(json.dumps(evaluate_registry(args.registry, args.output_dir, args.results_root), indent=2, default=str))
