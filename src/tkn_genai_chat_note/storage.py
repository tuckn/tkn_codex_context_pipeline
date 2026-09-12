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
from .session_notes import PipelineError, atomic_write_json, now_iso

STORAGE_VERSION = 4


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


def legacy_storage_pending(config: AppConfig) -> bool:
    current = read_json(config.source_state_root / "pipeline.json")
    if current.get("storageVersion") == STORAGE_VERSION:
        return False
    if current.get("storageVersion") == 3:
        return True
    legacy = read_json(config.state_root / "pipeline.json")
    if legacy.get("storageVersion") == STORAGE_VERSION and legacy.get("layout") == "provider-source":
        return False
    if legacy:
        source_id = str(legacy.get("sourceId") or "")
        # Resolve only a validated ID; never follow arbitrary metadata as a path.
        from .raw_capture import SOURCE_ID_PATTERN

        if not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise PipelineError("invalid legacy storage sourceId")
        migrated = read_json(config.state_root / "codex" / source_id / "pipeline.json")
        return migrated.get("migratedFromStorageVersion") != legacy.get("storageVersion")
    return bool(
        (config.raw_root / config.source_id / "manifest.jsonl").exists()
        and not (config.source_state_root / "pipeline.json").exists()
        or (config.data_root / "thread-notes").exists()
        and not (config.source_state_root / "pipeline.json").exists()
    )


def validate_storage(
    config: AppConfig, config_path: Path | None = None, *, allow_legacy: bool = False,
) -> tuple[Path, ...]:
    config.require_supported_chat_sources()
    roots = validate_reset_targets(config, config_path or global_config_path())
    for path in (config.data_root, config.state_root, config.raw_root, config.cache_root):
        if path.is_symlink():
            raise PipelineError(f"pipeline root must not be a symbolic link: {path}")
    for ownership in inspect_reset_target_ownership(roots):
        if ownership["status"] not in {"missing", "empty", "owned"}:
            raise PipelineError(f"refusing unowned or invalid pipeline root: {ownership['path']}; choose fresh roots")
    for kind, namespace in config.source_storage_paths(config.source_provider).items():
        for directory in (namespace.parent, namespace):
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise PipelineError(f"{kind} source namespace must not be a link: {directory}")
            if directory.exists() and not directory.is_dir():
                raise PipelineError(f"{kind} source namespace must be a directory: {directory}")
    if not allow_legacy and legacy_storage_pending(config):
        raise PipelineError("legacy storage layout requires `tkn-genai-chat-note storage migrate --dry-run` first")
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
    metadata_path = config.source_state_root / "pipeline.json"
    metadata = read_json(metadata_path)
    if metadata and metadata.get("storageVersion") != STORAGE_VERSION:
        raise PipelineError("unsupported pipeline storage version; choose fresh configured roots")
    if not metadata and not initialize:
        raise PipelineError("pipeline is not initialized; run `tkn-genai-chat-note clone` first")
    if metadata and (
        metadata.get("sourceId") != config.source_id or metadata.get("sourceProvider") != config.source_provider
    ):
        raise PipelineError("source identity differs from this namespace")
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
                    "applicationId": "tkn-genai-chat-note-pipeline",
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
                    "sourceProvider": config.source_provider,
                    "createdAt": now_iso(),
                },
            )
        elif (
            metadata.get("storageVersion") != STORAGE_VERSION or metadata.get("sourceId") != config.source_id
            or metadata.get("sourceProvider") != config.source_provider
        ):
            raise PipelineError("pipeline storage changed while acquiring its lock")
        yield
