"""Immutable, dataset-versioned Lego result cache."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PRECOMPUTE_WORK_DIR, RESULT_CACHE_DIR
from .engine import load_result_directory, persist_result_reference, write_result_directory
from .platform_lock import FileLock


RESULT_ENGINE_VERSION = "lego-result-v1"


def enabled() -> bool:
    return os.environ.get("QUANTWEAVE_RESULT_CACHE_ENABLED", "1") != "0"


def make_cache_key(
    pipeline: dict[str, Any],
    dataset_version: int,
    feature_schema_version: int,
    feature_calculator_version: str,
) -> str:
    payload = {
        "pipeline": pipeline,
        "dataset_version": int(dataset_version),
        "feature_schema_version": int(feature_schema_version),
        "feature_calculator_version": str(feature_calculator_version),
        "engine_version": RESULT_ENGINE_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_directory(key: str) -> Path:
    return RESULT_CACHE_DIR / key


def load_cached_result(key: str) -> dict[str, Any] | None:
    path = cache_directory(key)
    if not (path / "metadata.json").is_file():
        return None
    try:
        return load_result_directory(path)
    except Exception:
        return None


def store_cached_result(result: dict[str, Any], key: str) -> Path:
    RESULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    final = cache_directory(key)
    if (final / "metadata.json").is_file():
        return final
    temporary = RESULT_CACHE_DIR / f".{key}.{uuid.uuid4().hex}.tmp"
    lock_path = PRECOMPUTE_WORK_DIR / "result-cache-locks" / f"{key}.lock"
    try:
        with FileLock(lock_path):
            if (final / "metadata.json").is_file():
                return final
            write_result_directory(result, temporary)
            temporary.replace(final)
    except (BlockingIOError, OSError):
        if (final / "metadata.json").is_file():
            return final
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return final


def create_cached_run(
    cached: dict[str, Any],
    key: str,
    hit: bool,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    result = dict(cached)
    if hit:
        result["run_id"] = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        result["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    info = dict(result.get("cache_info") or {})
    info.update({
        "status": "hit" if hit else "miss",
        "cache_key": key,
        "engine_version": RESULT_ENGINE_VERSION,
        "source_run_id": source_run_id,
    })
    result["cache_info"] = info
    persist_result_reference(result, cache_directory(key))
    return result


def prune_unreferenced() -> dict[str, int]:
    """Delete only cache entries that have no lightweight run reference."""
    from .config import RUNS_DIR

    referenced: set[str] = set()
    for metadata in RUNS_DIR.glob("*/metadata.json") if RUNS_DIR.exists() else []:
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            ref = str(payload.get("artifact_ref") or "")
            if ref.startswith("cache/results/"):
                referenced.add(Path(ref).name)
        except Exception:
            continue
    removed = 0
    if RESULT_CACHE_DIR.exists():
        for path in RESULT_CACHE_DIR.iterdir():
            if path.is_dir() and path.name not in referenced:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    return {"removed": removed, "referenced": len(referenced)}
