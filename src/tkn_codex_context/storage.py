"""Non-destructive storage initialization and OS-released pipeline locks."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .config import AppConfig, global_config_path
from .initialization import (
    ROOT_KINDS,
    ROOT_OWNERSHIP_MARKER,
    inspect_reset_target_ownership,
    validate_reset_targets,
)
from .thread_notes import PipelineError, atomic_write_json, now_iso

STORAGE_VERSION = 2


def read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise PipelineError(f"cannot read pipeline state: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"pipeline state must be an object: {path}")
    return value


def validate_storage(config: AppConfig, config_path: Path | None = None) -> tuple[Path, ...]:
    roots = validate_reset_targets(config, config_path or global_config_path())
    for path in (config.data_root, config.state_root, config.raw_root, config.cache_root):
        if path.is_symlink():
            raise PipelineError(f"pipeline root must not be a symbolic link: {path}")
    for ownership in inspect_reset_target_ownership(roots):
        if ownership["status"] not in {"missing", "empty", "owned"}:
            raise PipelineError(f"refusing unowned or invalid pipeline root: {ownership['path']}; choose fresh roots")
    return roots


@contextmanager
def _root_lock(root: Path) -> Iterator[None]:
    path = root / ".pipeline.lock"
    if path.is_symlink():
        raise PipelineError(f"lock must not be a symbolic link: {path}")
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PipelineError(f"another pipeline run holds the storage lock: {root}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def pipeline_storage(
    config: AppConfig,
    *,
    initialize: bool,
    dry_run: bool,
    config_path: Path | None = None,
) -> Iterator[None]:
    roots = validate_storage(config, config_path)
    metadata_path = config.state_root / "pipeline.json"
    metadata = read_json(metadata_path)
    if metadata and metadata.get("storageVersion") != STORAGE_VERSION:
        raise PipelineError("unsupported pipeline storage version; choose fresh configured roots")
    if not metadata and not initialize:
        raise PipelineError("pipeline is not initialized; run `tkn-codex-context clone` first")
    if metadata and metadata.get("sourceId") != config.source_id:
        raise PipelineError("source_id differs from this store; use a separate store for another source")
    if not metadata and (config.data_root / "project-registry.jsonl").exists():
        raise PipelineError(
            "legacy Project storage cannot be used by clone; choose fresh roots; existing data is retained"
        )
    if dry_run:
        yield
        return
    for kind, root in zip(ROOT_KINDS, roots, strict=True):
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ROOT_OWNERSHIP_MARKER
        if not marker.exists():
            atomic_write_json(
                marker,
                {
                    "schemaVersion": 1,
                    "applicationId": "tkn-codex-context-pipeline",
                    "rootKind": kind,
                },
            )
    with ExitStack() as stack:
        for root in sorted(roots):
            stack.enter_context(_root_lock(root))
        # Check again under the lock; clone never resets an existing store.
        metadata = read_json(metadata_path)
        if not metadata:
            atomic_write_json(
                metadata_path,
                {
                    "storageVersion": STORAGE_VERSION,
                    "sourceId": config.source_id,
                    "createdAt": now_iso(),
                },
            )
        elif metadata.get("storageVersion") != STORAGE_VERSION or metadata.get("sourceId") != config.source_id:
            raise PipelineError("pipeline storage changed while acquiring its lock")
        yield
