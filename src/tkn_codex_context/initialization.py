"""Create or transactionally rebuild application-owned pipeline storage."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .app_state import load_codex_app_state
from .config import AppConfig, config_document, initialization_config, write_config
from .projects import create_fresh_projects
from .session_notes import PipelineError


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
) -> dict[str, Any]:
    config, target, removed_config_keys = initialization_config(config_path, overrides=overrides)
    reset_targets = validate_reset_targets(config, target)
    app_state = load_codex_app_state(config.app_state_path)
    _records, project_report = create_fresh_projects(config, app_state, dry_run=True)
    existing = [
        str(path)
        for path in (target, *reset_targets)
        if path.exists() or path.is_symlink()
    ]
    if existing and not force:
        raise PipelineError(
            "pipeline is already initialized; run `tkn-codex-context init --force --dry-run` "
            "to inspect a clean rebuild"
        )
    report: dict[str, Any] = {
        "dryRun": dry_run,
        "force": force,
        "configPath": str(target),
        "removedConfigKeys": list(removed_config_keys),
        "resetTargets": [str(path) for path in reset_targets],
        "existingTargets": existing,
        "config": config_document(config),
        "projectSync": project_report,
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
        _records, applied_project_report = create_fresh_projects(config, app_state, dry_run=False)
        report["projectSync"] = applied_project_report
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
    return report
