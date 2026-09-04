from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tkn_codex_context.initialization as initialization
from tkn_codex_context.config import CONFIG_SCHEMA_VERSION
from tkn_codex_context.initialization import (
    ROOT_KINDS,
    ROOT_OWNER_APPLICATION_ID,
    ROOT_OWNERSHIP_MARKER,
    ROOT_OWNERSHIP_SCHEMA_VERSION,
    initialize_application,
)
from tkn_codex_context.thread_notes import PipelineError


def write_app_state(codex_home: Path) -> None:
    codex_home.mkdir(parents=True)
    (codex_home / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "local-projects": {
                    "local-project": {
                        "id": "local-project",
                        "name": "Project",
                        "rootPaths": [str(codex_home.parent / "project")],
                        "createdAt": "2026-07-01T00:00:00Z",
                    }
                },
                "thread-project-assignments": {},
                "projectless-thread-ids": [],
            }
        ),
        encoding="utf-8",
    )


def write_config(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": CONFIG_SCHEMA_VERSION, **value}
    schema_version = document.pop("schema_version")
    schema_text = (
        json.dumps(schema_version) if isinstance(schema_version, str) else str(schema_version)
    )
    path.write_text(
        f"schema_version: {schema_text}\n{yaml.safe_dump(document, sort_keys=False)}",
        encoding="utf-8",
    )


def write_root_ownership_markers(*roots: Path) -> None:
    for kind, root in zip(ROOT_KINDS, roots, strict=True):
        root.mkdir(parents=True, exist_ok=True)
        (root / ROOT_OWNERSHIP_MARKER).write_text(
            json.dumps(
                {
                    "schemaVersion": ROOT_OWNERSHIP_SCHEMA_VERSION,
                    "applicationId": ROOT_OWNER_APPLICATION_ID,
                    "rootKind": kind,
                }
            ),
            encoding="utf-8",
        )


def test_init_requires_config_init_first(tmp_path: Path) -> None:
    target = tmp_path / "app/config.yaml"

    with pytest.raises(PipelineError, match="config init"):
        initialize_application(target, overrides=None, force=False, dry_run=True)


def test_force_dry_run_and_apply_preserve_settings_and_remove_old_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    raw_root = tmp_path / "app/raw"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "schema_version": 2,
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "context_store_root": str(tmp_path / "legacy"),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
            "raw_root": str(raw_root),
            "generation": {
                "active_provider": "codex",
                "providers": {
                    "codex": {
                        "model": "custom-model",
                        "reasoning_effort": "high",
                        "executable": "codex",
                    }
                },
            },
            "summary_prompt": "retired-custom.md",
        },
    )
    for root in (data_root, state_root, cache_root, raw_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
    write_root_ownership_markers(data_root, state_root, cache_root, raw_root)
    before = config_path.read_bytes()

    preview = initialize_application(config_path, overrides=None, force=True, dry_run=True)

    assert config_path.read_bytes() == before
    assert all((root / "old.txt").is_file() for root in (data_root, state_root, cache_root, raw_root))
    assert preview["removedConfigKeys"] == ["summary_prompt", "context_store_root"]
    assert preview["projectFetch"]["projects"][0]["projectId"] == "local-project"
    assert [item["status"] for item in preview["rootOwnership"]] == [
        "owned",
        "owned",
        "owned",
        "owned",
    ]
    assert preview["safeToReset"] is True

    initialize_application(config_path, overrides=None, force=True, dry_run=False)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config_path.read_text(encoding="utf-8").splitlines()[0] == (
        f'schema_version: "{CONFIG_SCHEMA_VERSION}"'
    )
    assert saved["schema_version"] == CONFIG_SCHEMA_VERSION
    assert saved["generation"]["providers"]["codex"]["model"] == "custom-model"
    assert saved["installed_at"] != "2026-01-01T00:00:00+00:00"
    assert "context_store_root" not in saved
    assert "summary_prompt" not in saved
    assert not any((root / "old.txt").exists() for root in (data_root, state_root, cache_root, raw_root))
    assert (data_root / "projects/local-project/thread-notes").is_dir()
    assert not any((data_root / "projects/local-project/thread-notes").iterdir())
    assert (state_root / "projects/local-project").is_dir()
    assert not (state_root / "projects/local-project/chat-refresh-state.json").exists()
    assert all(
        (root / ROOT_OWNERSHIP_MARKER).is_file()
        for root in (data_root, state_root, cache_root, raw_root)
    )


def test_init_refuses_existing_storage_without_force(tmp_path: Path) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    raw_root = tmp_path / "app/raw"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "codex_home": str(codex_home),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
            "raw_root": str(raw_root),
        },
    )
    for root in (data_root, state_root, cache_root, raw_root):
        root.mkdir(parents=True)
        (root / "existing.txt").write_text("existing", encoding="utf-8")
    write_root_ownership_markers(data_root, state_root, cache_root, raw_root)
    config_before = config_path.read_bytes()

    with pytest.raises(PipelineError, match="init --force --dry-run"):
        initialize_application(config_path, overrides=None, force=False, dry_run=False)

    assert config_path.read_bytes() == config_before
    assert all(
        (root / "existing.txt").read_text(encoding="utf-8") == "existing"
        for root in (data_root, state_root, cache_root, raw_root)
    )


def test_force_rolls_back_storage_and_config_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    raw_root = tmp_path / "app/raw"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
            "raw_root": str(raw_root),
        },
    )
    for root in (data_root, state_root, cache_root, raw_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
    write_root_ownership_markers(data_root, state_root, cache_root, raw_root)
    config_before = config_path.read_bytes()
    real_create = initialization.create_fresh_projects

    def fail_after_preview(*args: object, **kwargs: object) -> object:
        if kwargs.get("dry_run") is False:
            raise PipelineError("injected failure")
        return real_create(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(initialization, "create_fresh_projects", fail_after_preview)

    with pytest.raises(PipelineError, match="injected failure"):
        initialize_application(config_path, overrides=None, force=True, dry_run=False)

    assert config_path.read_bytes() == config_before
    assert all(
        (root / "old.txt").read_text(encoding="utf-8") == "old"
        for root in (data_root, state_root, cache_root, raw_root)
    )


def test_force_does_not_delete_storage_when_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    raw_root = tmp_path / "app/raw"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
            "raw_root": str(raw_root),
        },
    )
    for root in (data_root, state_root, cache_root, raw_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
    write_root_ownership_markers(data_root, state_root, cache_root, raw_root)
    real_replace = Path.replace

    def fail_data_stage(path: Path, target: Path) -> Path:
        if path == data_root:
            raise PermissionError("injected staging failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_data_stage)

    with pytest.raises(PipelineError, match="cannot stage reset target"):
        initialize_application(config_path, overrides=None, force=True, dry_run=False)

    assert all(
        (root / "old.txt").read_text(encoding="utf-8") == "old"
        for root in (data_root, state_root, cache_root, raw_root)
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_force_rejects_unowned_nonempty_storage(
    tmp_path: Path,
    dry_run: bool,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    roots = (tmp_path / "app/data", tmp_path / "app/state", tmp_path / "cache", tmp_path / "app/raw")
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(roots[0]),
            "state_root": str(roots[1]),
            "cache_root": str(roots[2]),
            "raw_root": str(roots[3]),
        },
    )
    for root in roots:
        root.mkdir(parents=True)
        (root / "keep.txt").write_text("keep", encoding="utf-8")
    config_before = config_path.read_bytes()

    with pytest.raises(PipelineError, match="without valid ownership markers") as exc:
        initialize_application(config_path, overrides=None, force=True, dry_run=dry_run)

    assert "--adopt-existing --dry-run" in str(exc.value)
    assert config_path.read_bytes() == config_before
    assert all((root / "keep.txt").read_text(encoding="utf-8") == "keep" for root in roots)
    assert not any((root / ROOT_OWNERSHIP_MARKER).exists() for root in roots)


def test_adopt_existing_previews_then_marks_roots_without_rebuilding(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    roots = (tmp_path / "app/data", tmp_path / "app/state", tmp_path / "cache", tmp_path / "app/raw")
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(roots[0]),
            "state_root": str(roots[1]),
            "cache_root": str(roots[2]),
            "raw_root": str(roots[3]),
        },
    )
    for root in roots:
        root.mkdir(parents=True)
        (root / "keep.txt").write_text("keep", encoding="utf-8")
    config_before = config_path.read_bytes()

    preview = initialize_application(
        config_path,
        overrides=None,
        force=False,
        dry_run=True,
        adopt_existing=True,
    )

    assert preview["adoptExisting"] is True
    assert preview["plannedAdoptions"] == [str(root.resolve()) for root in roots]
    assert preview["adoptedTargets"] == []
    assert [item["status"] for item in preview["rootOwnership"]] == [
        "unowned",
        "unowned",
        "unowned",
        "unowned",
    ]
    assert config_path.read_bytes() == config_before
    assert not any((root / ROOT_OWNERSHIP_MARKER).exists() for root in roots)

    applied = initialize_application(
        config_path,
        overrides=None,
        force=False,
        dry_run=False,
        adopt_existing=True,
    )

    assert applied["adoptedTargets"] == [str(root.resolve()) for root in roots]
    assert [item["status"] for item in applied["rootOwnership"]] == [
        "owned",
        "owned",
        "owned",
        "owned",
    ]
    assert config_path.read_bytes() == config_before
    assert all((root / "keep.txt").read_text(encoding="utf-8") == "keep" for root in roots)
    for kind, root in zip(ROOT_KINDS, roots, strict=True):
        marker = json.loads((root / ROOT_OWNERSHIP_MARKER).read_text(encoding="utf-8"))
        assert marker == {
            "schemaVersion": ROOT_OWNERSHIP_SCHEMA_VERSION,
            "applicationId": ROOT_OWNER_APPLICATION_ID,
            "rootKind": kind,
        }

    write_app_state(codex_home)
    force_preview = initialize_application(
        config_path,
        overrides=None,
        force=True,
        dry_run=True,
    )
    assert force_preview["safeToReset"] is True


def test_adopt_existing_rejects_foreign_marker(tmp_path: Path) -> None:
    config_path = tmp_path / "app/config.yaml"
    roots = (tmp_path / "app/data", tmp_path / "app/state", tmp_path / "cache", tmp_path / "app/raw")
    write_config(
        config_path,
        {
            "codex_home": str(tmp_path / "codex"),
            "data_root": str(roots[0]),
            "state_root": str(roots[1]),
            "cache_root": str(roots[2]),
            "raw_root": str(roots[3]),
        },
    )
    for root in roots:
        root.mkdir(parents=True)
    (roots[0] / ROOT_OWNERSHIP_MARKER).write_text(
        json.dumps(
            {
                "schemaVersion": ROOT_OWNERSHIP_SCHEMA_VERSION,
                "applicationId": "another-application",
                "rootKind": "data",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PipelineError, match="invalid ownership state"):
        initialize_application(
            config_path,
            overrides=None,
            force=False,
            dry_run=True,
            adopt_existing=True,
        )

    assert not (roots[1] / ROOT_OWNERSHIP_MARKER).exists()
    assert not (roots[2] / ROOT_OWNERSHIP_MARKER).exists()
    assert not (roots[3] / ROOT_OWNERSHIP_MARKER).exists()


def test_force_rejects_unsafe_reset_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    config_path = home / "app/config.yaml"
    write_config(
        config_path,
        {
            "codex_home": str(home / ".codex"),
            "data_root": str(home),
            "state_root": str(home / "app/state"),
            "cache_root": str(home / "cache"),
            "raw_root": str(home / "app/raw"),
        },
    )

    with pytest.raises(PipelineError, match="unsafe reset target"):
        initialize_application(config_path, overrides=None, force=True, dry_run=True)
