from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tkn_codex_context.app_state import (
    CodexAppProject,
    CodexAppState,
    ThreadAssignment,
    load_codex_app_state,
)
from tkn_codex_context.config import AppConfig
from tkn_codex_context.projects import (
    fetch_projects,
    list_registered_projects,
    resolve_project_selector,
    runtime_projects,
)
from tkn_codex_context.session_notes import PipelineError, Project


def app_project(project_id: str, name: str, roots: list[Path]) -> CodexAppProject:
    return CodexAppProject(
        id=project_id,
        name=name,
        rootPaths=roots,
        createdAt=datetime.fromisoformat("2026-07-01T00:00:00+00:00"),
    )


def runtime_project(project_id: str, name: str, root: Path) -> Project:
    return Project(
        project_id=project_id,
        title=name,
        current_root=root,
        context_path=root / "context",
    )


def test_resolve_project_selector_accepts_id_name_or_current_root(tmp_path: Path) -> None:
    projects = [
        runtime_project("project-one", "Shared selector", tmp_path / "one"),
        runtime_project("project-two", "Unique Name", tmp_path / "two"),
    ]

    assert resolve_project_selector(projects, "project-one").project_id == "project-one"
    assert resolve_project_selector(projects, "Unique Name").project_id == "project-two"
    root_selector = str(tmp_path / "two").upper().replace("\\", "/") + "/"
    assert resolve_project_selector(projects, root_selector).project_id == "project-two"


def test_resolve_project_selector_prefers_id_over_name(tmp_path: Path) -> None:
    projects = [
        runtime_project("preferred", "First", tmp_path / "one"),
        runtime_project("other", "preferred", tmp_path / "two"),
    ]

    assert resolve_project_selector(projects, "preferred").project_id == "preferred"


def test_resolve_project_selector_prefers_name_over_current_root(tmp_path: Path) -> None:
    root_selector = str(tmp_path / "root")
    projects = [
        runtime_project("named-project", root_selector, tmp_path / "one"),
        runtime_project("root-project", "Second", tmp_path / "root"),
    ]

    assert resolve_project_selector(projects, root_selector).project_id == "named-project"


def test_resolve_project_selector_rejects_duplicate_name(tmp_path: Path) -> None:
    projects = [
        runtime_project("project-one", "Duplicate", tmp_path / "one"),
        runtime_project("project-two", "Duplicate", tmp_path / "two"),
    ]

    with pytest.raises(PipelineError, match="ambiguous Project Name 'Duplicate'") as exc:
        resolve_project_selector(projects, "Duplicate")

    assert "project-one, project-two" in str(exc.value)


def test_resolve_project_selector_rejects_duplicate_current_root(tmp_path: Path) -> None:
    shared_root = tmp_path / "shared"
    projects = [
        runtime_project("project-one", "First", shared_root),
        runtime_project("project-two", "Second", shared_root),
    ]

    with pytest.raises(PipelineError, match="ambiguous Project CURRENT ROOT") as exc:
        resolve_project_selector(projects, str(shared_root))

    assert "project-one, project-two" in str(exc.value)


def test_fetch_binds_multi_root_and_preserves_unknown_fields(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    data_root = tmp_path / "pipeline" / "data"
    state_root = tmp_path / "pipeline" / "state"
    registry = data_root / "project-registry.jsonl"
    registry.parent.mkdir(parents=True)
    existing = {
        "schemaVersion": 2,
        "identityKind": "codexAppLocalProject",
        "projectId": "source-1",
        "title": "Old",
        "currentRoot": str(primary),
        "projectDataPath": str(data_root / "projects/existing"),
        "projectStatePath": str(state_root / "projects/existing"),
        "status": "active",
        "customField": {"keep": True},
    }
    registry.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=data_root,
        state_root=state_root,
        cache_root=tmp_path / "cache",
    )
    source = app_project("source-1", "Project", [primary, secondary])
    state = CodexAppState(
        projects=(source,),
        assignments={"thread-1": ThreadAssignment(projectKind="local", projectId="source-1", cwd=secondary)},
        projectless_thread_ids=frozenset({"thread-2"}),
    )

    before = registry.read_bytes()
    records, report = fetch_projects(config, state, dry_run=True)
    assert registry.read_bytes() == before
    assert report["projects"][0]["method"] == "project-id"
    assert records[0]["customField"] == {"keep": True}

    projects = runtime_projects(records, state)
    assert projects[0].active_roots == (primary.absolute(), secondary.absolute())
    assert projects[0].assigned_thread_ids == frozenset({"thread-1"})
    assert projects[0].projectless_thread_ids == frozenset({"thread-2"})
    assert projects[0].project_id == "source-1"
    assert projects[0].sessions_path == data_root / "projects/source-1/sessions"
    assert projects[0].state_path == state_root / "projects/source-1/chat-refresh-state.json"


def test_same_name_and_root_remain_distinct_projects(tmp_path: Path) -> None:
    data_root = tmp_path / "pipeline" / "data"
    state_root = tmp_path / "pipeline" / "state"
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=data_root,
        state_root=state_root,
        cache_root=tmp_path / "cache",
    )
    shared = tmp_path / "shared"
    state = CodexAppState(
        projects=(
            app_project("source-one", "Same", [shared]),
            app_project("source-two", "Same", [shared]),
        ),
        assignments={},
        projectless_thread_ids=frozenset(),
    )
    records, report = fetch_projects(config, state, dry_run=True)
    assert {item["projectId"] for item in records} == {"source-one", "source-two"}
    assert report["pendingCount"] == 0
    assert report["boundCount"] == 2


def test_replaced_active_root_becomes_historical_alias(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    data_root = tmp_path / "pipeline" / "data"
    state_root = tmp_path / "pipeline" / "state"
    registry = data_root / "project-registry.jsonl"
    registry.parent.mkdir(parents=True)
    record = {
        "schemaVersion": 2,
        "identityKind": "codexAppLocalProject",
        "projectId": "source",
        "title": "Project",
        "currentRoot": str(old),
        "projectDataPath": str(data_root / "projects/existing"),
        "projectStatePath": str(state_root / "projects/existing"),
        "status": "active",
        "roots": [{"path": str(old), "role": "primary", "status": "active"}],
    }
    registry.write_text(json.dumps(record) + "\n", encoding="utf-8")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=data_root,
        state_root=state_root,
        cache_root=tmp_path / "cache",
    )
    state = CodexAppState(
        projects=(app_project("source", "Project", [new]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )

    records, _report = fetch_projects(config, state, dry_run=True)

    assert records[0]["roots"] == [
        {"path": str(new.absolute()), "role": "primary", "status": "active"},
        {"path": str(old), "role": "alias", "status": "historical"},
    ]
    assert records[0]["projectId"] == "source"
    assert records[0]["title"] == "Project"


def test_same_id_survives_drive_and_name_change(tmp_path: Path) -> None:
    old = Path(r"C:\path\to\project")
    new = Path(r"D:\path\to\project")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=tmp_path / "pipeline/data",
        state_root=tmp_path / "pipeline/state",
        cache_root=tmp_path / "cache",
    )
    record = {
        "schemaVersion": 2,
        "identityKind": "codexAppLocalProject",
        "projectId": "source",
        "title": "Old Name",
        "currentRoot": str(old),
        "projectDataPath": str(config.projects_data_root / "source"),
        "projectStatePath": str(config.projects_state_root / "source"),
        "sessionsPath": str(config.projects_data_root / "source/sessions"),
        "status": "active",
        "roots": [{"path": str(old), "role": "primary", "status": "active"}],
    }
    config.registry_path.parent.mkdir(parents=True)
    config.registry_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    state = CodexAppState(
        projects=(app_project("source", "New Name", [new]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )

    records, report = fetch_projects(config, state, dry_run=True)

    assert report["projects"][0]["method"] == "project-id"
    assert records[0]["projectId"] == "source"
    assert records[0]["title"] == "New Name"
    assert records[0]["currentRoot"] == str(new.absolute())
    assert records[0]["projectDataPath"] == str(config.projects_data_root / "source")
    assert {"path": str(old), "role": "alias", "status": "historical"} in records[0]["roots"]


def test_missing_app_project_becomes_inactive_and_can_reactivate(tmp_path: Path) -> None:
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=tmp_path / "pipeline/data",
        state_root=tmp_path / "pipeline/state",
        cache_root=tmp_path / "cache",
    )
    record = {
        "schemaVersion": 2,
        "identityKind": "codexAppLocalProject",
        "projectId": "source",
        "title": "Old",
        "currentRoot": str(tmp_path / "old"),
        "projectDataPath": str(config.projects_data_root / "source"),
        "projectStatePath": str(config.projects_state_root / "source"),
        "sessionsPath": str(config.projects_data_root / "source/sessions"),
        "status": "active",
        "roots": [],
    }
    config.registry_path.parent.mkdir(parents=True)
    config.registry_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    empty_state = CodexAppState(projects=(), assignments={}, projectless_thread_ids=frozenset())

    inactive, _report = fetch_projects(config, empty_state, dry_run=True)

    assert inactive[0]["status"] == "inactive"
    returning = CodexAppState(
        projects=(app_project("source", "Renamed", [tmp_path / "new"]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )
    active, _report = fetch_projects(config, returning, dry_run=True)
    assert active[0]["status"] == "active"
    assert active[0]["title"] == "Renamed"


def test_fetch_rejects_old_registry_schema(tmp_path: Path) -> None:
    config = AppConfig(
        codex_home=tmp_path / "codex",
        data_root=tmp_path / "pipeline" / "data",
        state_root=tmp_path / "pipeline" / "state",
        cache_root=tmp_path / "cache",
    )
    config.registry_path.parent.mkdir(parents=True)
    config.registry_path.write_text(
        json.dumps(
            {
                "projectId": "legacy",
                "title": "Project",
                "currentRoot": str(tmp_path / "repo"),
                "status": "active",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = CodexAppState(
        projects=(app_project("source", "Project", [tmp_path / "repo"]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )

    with pytest.raises(PipelineError, match="init --force"):
        fetch_projects(config, state, dry_run=True)
    with pytest.raises(PipelineError, match="init --force"):
        list_registered_projects(config.registry_path)


def test_app_state_reader_fails_closed_on_key_id_mismatch(tmp_path: Path) -> None:
    path = tmp_path / ".codex-global-state.json"
    path.write_text(
        json.dumps(
            {
                "local-projects": {
                    "key": {
                        "id": "different",
                        "name": "Project",
                        "rootPaths": [str(tmp_path / "root")],
                        "createdAt": "2026-07-01T00:00:00Z",
                    }
                },
                "thread-project-assignments": {},
                "projectless-thread-ids": [],
            }
        ),
        encoding="utf-8",
    )

    try:
        load_codex_app_state(path)
    except PipelineError as exc:
        assert "key/id mismatch" in str(exc)
    else:
        raise AssertionError("invalid app state was accepted")
