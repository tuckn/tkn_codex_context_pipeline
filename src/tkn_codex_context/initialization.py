"""Create or transactionally rebuild application-owned pipeline storage."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .app_state import load_codex_app_state
from .config import AppConfig, config_document, initialization_config, write_config
from .projects import create_fresh_projects
from .thread_notes import PipelineError, atomic_write_json

ROOT_OWNERSHIP_MARKER = ".tkn-codex-context-root.json"
ROOT_OWNERSHIP_SCHEMA_VERSION = 1
ROOT_OWNER_APPLICATION_ID = "tkn-codex-context-pipeline"
ROOT_KINDS = ("data", "state", "cache")
SAFE_RESET_OWNERSHIP_STATUSES = frozenset({"missing", "empty", "owned"})


def _resolved(path: Path) -> Path:
    return path.expanduser().absolute().resolve(strict=False)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def validate_reset_targets(config: AppConfig, config_path: Path) -> tuple[Path, ...]:
    targets = tuple(_resolved(path) for path in (config.data_root, config.state_root, config.cache_root))
    home = _resolved(Path.home())
    codex_home = _resolved(config.codex_home)
    resolved_config = _resolved(config_path)
    for target in targets:
        if target.parent == target or target == home or home.is_relative_to(target):
            raise PipelineError(f"unsafe reset target: {target}")
        if _overlaps(target, codex_home):
            raise PipelineError(f"reset target overlaps Codex home: {target}")
        if resolved_config == target or resolved_config.is_relative_to(target):
            raise PipelineError(f"reset target contains config: {target}")
    for index, left in enumerate(targets):
        for right in targets[index + 1 :]:
            if _overlaps(left, right):
                raise PipelineError(f"reset targets overlap: {left} and {right}")
    return targets


def _ownership_marker_document(kind: str) -> dict[str, str | int]:
    return {
        "schemaVersion": ROOT_OWNERSHIP_SCHEMA_VERSION,
        "applicationId": ROOT_OWNER_APPLICATION_ID,
        "rootKind": kind,
    }


def _inspect_root_ownership(kind: str, path: Path) -> dict[str, Any]:
    marker = path / ROOT_OWNERSHIP_MARKER
    report: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "exists": path.exists() or path.is_symlink(),
        "markerPath": str(marker),
        "status": "missing",
        "reason": None,
    }
    if not report["exists"]:
        return report
    if not path.is_dir():
        report.update(status="not-directory", reason="configured root is not a directory")
        return report
    if marker.is_symlink():
        report.update(status="invalid-marker", reason="ownership marker must not be a symbolic link")
        return report
    if marker.exists() and not marker.is_file():
        report.update(status="invalid-marker", reason="ownership marker must be a regular file")
        return report
    if not marker.is_file():
        try:
            empty = next(path.iterdir(), None) is None
        except OSError as exc:
            raise PipelineError(f"cannot inspect reset target ownership: {path}: {exc}") from exc
        report.update(
            status="empty" if empty else "unowned",
            reason=None if empty else "ownership marker is missing from a non-empty directory",
        )
        return report
    try:
        value = json.loads(marker.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        report.update(status="invalid-marker", reason=f"cannot read ownership marker: {exc}")
        return report
    expected = _ownership_marker_document(kind)
    if not isinstance(value, dict) or any(value.get(key) != expected_value for key, expected_value in expected.items()):
        report.update(
            status="invalid-marker",
            reason=("ownership marker does not match this application, root kind, or supported marker schema"),
        )
        return report
    report["status"] = "owned"
    return report


def inspect_reset_target_ownership(targets: tuple[Path, ...]) -> list[dict[str, Any]]:
    return [_inspect_root_ownership(kind, path) for kind, path in zip(ROOT_KINDS, targets, strict=True)]


def _unsafe_reset_ownership(ownership: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in ownership if item["status"] not in SAFE_RESET_OWNERSHIP_STATUSES]


def _ownership_details(items: list[dict[str, Any]]) -> str:
    return "; ".join(f"{item['kind']}={item['path']} ({item['status']}: {item['reason']})" for item in items)


def _require_owned_reset_targets(ownership: list[dict[str, Any]]) -> None:
    unsafe = _unsafe_reset_ownership(ownership)
    if unsafe:
        raise PipelineError(
            "refusing to reset targets without valid ownership markers: "
            f"{_ownership_details(unsafe)}; inspect and explicitly adopt existing directories with "
            "`tkn-codex-context init --adopt-existing --dry-run` before using --force"
        )


def _adopt_existing_targets(
    ownership: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> list[str]:
    invalid = [item for item in ownership if item["status"] in {"invalid-marker", "not-directory"}]
    if invalid:
        raise PipelineError(f"refusing to adopt targets with invalid ownership state: {_ownership_details(invalid)}")
    existing = [item for item in ownership if item["exists"]]
    if not existing:
        raise PipelineError("no existing reset targets to adopt; run `tkn-codex-context init` instead")
    adoptable = [item for item in ownership if item["status"] in {"empty", "unowned"}]
    planned = [str(item["path"]) for item in adoptable]
    if dry_run:
        return planned
    created: list[Path] = []
    try:
        for item in adoptable:
            marker = Path(str(item["markerPath"]))
            atomic_write_json(marker, _ownership_marker_document(str(item["kind"])))
            created.append(marker)
    except Exception:
        for marker in reversed(created):
            if marker.is_file() or marker.is_symlink():
                marker.unlink()
        raise
    return planned


def _write_ownership_markers(targets: tuple[Path, ...]) -> None:
    for kind, path in zip(ROOT_KINDS, targets, strict=True):
        atomic_write_json(path / ROOT_OWNERSHIP_MARKER, _ownership_marker_document(kind))


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _stage_existing(targets: tuple[Path, ...]) -> list[tuple[Path, Path]]:
    staged: list[tuple[Path, Path]] = []
    current: Path | None = None
    try:
        for target in targets:
            current = target
            if not target.exists() and not target.is_symlink():
                continue
            staging = target.with_name(f".{target.name}.reset-{uuid4().hex}")
            target.replace(staging)
            staged.append((target, staging))
    except OSError as exc:
        for target, staging in reversed(staged):
            if staging.exists() or staging.is_symlink():
                staging.replace(target)
        raise PipelineError(f"cannot stage reset target {current}: {exc}") from exc
    return staged


def _rollback(targets: tuple[Path, ...], staged: list[tuple[Path, Path]]) -> None:
    for target in targets:
        _remove_path(target)
    for target, staging in reversed(staged):
        if staging.exists() or staging.is_symlink():
            staging.replace(target)


def initialize_application(
    config_path: Path | None,
    *,
    overrides: dict[str, Any] | None,
    force: bool,
    dry_run: bool,
    adopt_existing: bool = False,
) -> dict[str, Any]:
    if force and adopt_existing:
        raise PipelineError("--force and --adopt-existing cannot be used together")
    config, target, removed_config_keys = initialization_config(
        config_path,
        overrides=overrides,
        refresh_installed_at=not adopt_existing,
    )
    reset_targets = validate_reset_targets(config, target)
    ownership = inspect_reset_target_ownership(reset_targets)
    if adopt_existing:
        planned_adoptions = _adopt_existing_targets(ownership, dry_run=dry_run)
        return {
            "dryRun": dry_run,
            "force": False,
            "adoptExisting": True,
            "configPath": str(target),
            "resetTargets": [str(path) for path in reset_targets],
            "rootOwnership": (ownership if dry_run else inspect_reset_target_ownership(reset_targets)),
            "plannedAdoptions": planned_adoptions,
            "adoptedTargets": [] if dry_run else planned_adoptions,
            "config": config_document(config),
        }
    existing = [
        str(path)
        for path in reset_targets
        if path.exists() or path.is_symlink()
    ]
    if existing and not force:
        unsafe = _unsafe_reset_ownership(ownership)
        if unsafe:
            raise PipelineError(
                "existing pipeline storage is not marked as application-owned: "
                f"{_ownership_details(unsafe)}; inspect and explicitly adopt it with "
                "`tkn-codex-context init --adopt-existing --dry-run`"
            )
        raise PipelineError(
            "pipeline is already initialized; run `tkn-codex-context init --force --dry-run` "
            "to inspect a clean rebuild"
        )
    if force:
        _require_owned_reset_targets(ownership)
    app_state = load_codex_app_state(config.app_state_path)
    _records, project_report = create_fresh_projects(config, app_state, dry_run=True)
    report: dict[str, Any] = {
        "dryRun": dry_run,
        "force": force,
        "configPath": str(target),
        "removedConfigKeys": list(removed_config_keys),
        "resetTargets": [str(path) for path in reset_targets],
        "existingTargets": existing,
        "rootOwnership": ownership,
        "safeToReset": not _unsafe_reset_ownership(ownership),
        "config": config_document(config),
        "projectFetch": project_report,
    }
    if dry_run:
        return report

    old_config = target.read_bytes() if target.is_file() else None
    staged: list[tuple[Path, Path]] = []
    config_written = False
    staging_completed = False
    try:
        if force:
            staged = _stage_existing(reset_targets)
        staging_completed = True
        for directory in reset_targets:
            directory.mkdir(parents=True, exist_ok=True)
        _write_ownership_markers(reset_targets)
        _records, applied_project_report = create_fresh_projects(config, app_state, dry_run=False)
        report["projectFetch"] = applied_project_report
        write_config(config, target)
        config_written = True
    except Exception:
        if staging_completed:
            _rollback(reset_targets, staged)
        if old_config is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(old_config)
        raise

    cleanup_failures: list[str] = []
    for _target, staging in staged:
        try:
            _remove_path(staging)
        except OSError as exc:
            cleanup_failures.append(f"{staging}: {exc}")
    if cleanup_failures:
        raise PipelineError("initialization succeeded, but old storage cleanup failed: " + "; ".join(cleanup_failures))
    if not config_written:
        raise PipelineError("initialization did not write config")
    report["rootOwnership"] = inspect_reset_target_ownership(reset_targets)
    report["safeToReset"] = True
    return report
