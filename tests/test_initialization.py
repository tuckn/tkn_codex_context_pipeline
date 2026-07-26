from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tkn_codex_context.initialization as initialization
from tkn_codex_context.initialization import initialize_application
from tkn_codex_context.session_notes import PipelineError


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
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_force_dry_run_and_apply_preserve_settings_and_remove_old_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "schema_version": 1,
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "context_store_root": str(tmp_path / "legacy"),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
            "model": "custom-model",
        },
    )
    for root in (data_root, state_root, cache_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
    before = config_path.read_bytes()

    preview = initialize_application(config_path, overrides=None, force=True, dry_run=True)

    assert config_path.read_bytes() == before
    assert all((root / "old.txt").is_file() for root in (data_root, state_root, cache_root))
    assert preview["removedConfigKeys"] == ["context_store_root"]
    assert preview["projectSync"]["projects"][0]["projectId"] == "local-project"

    initialize_application(config_path, overrides=None, force=True, dry_run=False)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "custom-model"
    assert saved["installed_at"] != "2026-01-01T00:00:00+00:00"
    assert "context_store_root" not in saved
    assert not any((root / "old.txt").exists() for root in (data_root, state_root, cache_root))
    assert (data_root / "projects/local-project/sessions").is_dir()
    assert not any((data_root / "projects/local-project/sessions").iterdir())
    assert (state_root / "projects/local-project").is_dir()
    assert not (state_root / "projects/local-project/chat-refresh-state.json").exists()


def test_init_refuses_existing_storage_without_force(tmp_path: Path) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    write_app_state(codex_home)
    write_config(config_path, {"codex_home": str(codex_home)})

    with pytest.raises(PipelineError, match="init --force --dry-run"):
        initialize_application(config_path, overrides=None, force=False, dry_run=False)


def test_force_rolls_back_storage_and_config_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
        },
    )
    for root in (data_root, state_root, cache_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
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
    assert all((root / "old.txt").read_text(encoding="utf-8") == "old" for root in (data_root, state_root, cache_root))


def test_force_does_not_delete_storage_when_staging_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "app/config.yaml"
    codex_home = tmp_path / "codex"
    data_root = tmp_path / "app/data"
    state_root = tmp_path / "app/state"
    cache_root = tmp_path / "cache"
    write_app_state(codex_home)
    write_config(
        config_path,
        {
            "installed_at": "2026-01-01T00:00:00+00:00",
            "codex_home": str(codex_home),
            "data_root": str(data_root),
            "state_root": str(state_root),
            "cache_root": str(cache_root),
        },
    )
    for root in (data_root, state_root, cache_root):
        root.mkdir(parents=True)
        (root / "old.txt").write_text("old", encoding="utf-8")
    real_replace = Path.replace

    def fail_data_stage(path: Path, target: Path) -> Path:
        if path == data_root:
            raise PermissionError("injected staging failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_data_stage)

    with pytest.raises(PipelineError, match="cannot stage reset target"):
        initialize_application(config_path, overrides=None, force=True, dry_run=False)

    assert all((root / "old.txt").read_text(encoding="utf-8") == "old" for root in (data_root, state_root, cache_root))


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
        },
    )

    with pytest.raises(PipelineError, match="unsafe reset target"):
        initialize_application(config_path, overrides=None, force=True, dry_run=True)
