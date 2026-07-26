from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tkn_codex_context.app_state import (
    CodexAppProject,
    CodexAppState,
    ThreadAssignment,
    load_codex_app_state,
)
from tkn_codex_context.config import AppConfig
from tkn_codex_context.projects import runtime_projects, sync_projects
from tkn_codex_context.session_notes import PipelineError


def app_project(project_id: str, name: str, roots: list[Path]) -> CodexAppProject:
    return CodexAppProject(
        id=project_id,
        name=name,
        rootPaths=roots,
        createdAt=datetime.fromisoformat("2026-07-01T00:00:00+00:00"),
    )


def test_sync_binds_multi_root_and_preserves_unknown_fields(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    context_root = tmp_path / "context"
    registry = context_root / "state/index.jsonl"
    registry.parent.mkdir(parents=True)
    existing = {
        "projectId": "existing",
        "title": "Old",
        "currentRoot": str(primary),
        "projectContextPath": str(context_root / "state/existing"),
        "status": "active",
        "customField": {"keep": True},
    }
    registry.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        context_store_root=context_root,
        pipeline_root=tmp_path / "pipeline",
    )
    source = app_project("source-1", "Project", [primary, secondary])
    state = CodexAppState(
        projects=(source,),
        assignments={"thread-1": ThreadAssignment(projectKind="local", projectId="source-1", cwd=secondary)},
        projectless_thread_ids=frozenset({"thread-2"}),
    )

    before = registry.read_bytes()
    records, report = sync_projects(config, state, dry_run=True)
    assert registry.read_bytes() == before
    assert report["projects"][0]["method"] == "root"
    assert records[0]["customField"] == {"keep": True}

    projects = runtime_projects(records, state)
    assert projects[0].active_roots == (primary.absolute(), secondary.absolute())
    assert projects[0].assigned_thread_ids == frozenset({"thread-1"})
    assert projects[0].projectless_thread_ids == frozenset({"thread-2"})


def test_ambiguous_name_stays_pending(tmp_path: Path) -> None:
    context_root = tmp_path / "context"
    registry = context_root / "state/index.jsonl"
    registry.parent.mkdir(parents=True)
    records = [
        {
            "projectId": value,
            "title": "Same",
            "currentRoot": str(tmp_path / value),
            "projectContextPath": str(context_root / "state" / value),
            "status": "active",
        }
        for value in ("one", "two")
    ]
    registry.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        context_store_root=context_root,
        pipeline_root=tmp_path / "pipeline",
    )
    state = CodexAppState(
        projects=(app_project("source", "Same", [tmp_path / "new"]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )
    _records, report = sync_projects(config, state, dry_run=True)
    assert report["pendingCount"] == 1
    assert report["boundCount"] == 0


def test_replaced_active_root_becomes_historical_alias(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    context_root = tmp_path / "context"
    registry = context_root / "state/index.jsonl"
    registry.parent.mkdir(parents=True)
    record = {
        "projectId": "existing",
        "title": "Project",
        "currentRoot": str(old),
        "projectContextPath": str(context_root / "state/existing"),
        "status": "active",
        "sourceBindings": [{"kind": "codexAppLocalProject", "sourceProjectId": "source"}],
        "roots": [{"path": str(old), "role": "primary", "status": "active"}],
    }
    registry.write_text(json.dumps(record) + "\n", encoding="utf-8")
    config = AppConfig(
        codex_home=tmp_path / "codex",
        context_store_root=context_root,
        pipeline_root=tmp_path / "pipeline",
    )
    state = CodexAppState(
        projects=(app_project("source", "Project", [new]),),
        assignments={},
        projectless_thread_ids=frozenset(),
    )

    records, _report = sync_projects(config, state, dry_run=True)

    assert records[0]["roots"] == [
        {"path": str(new.absolute()), "role": "primary", "status": "active"},
        {"path": str(old), "role": "alias", "status": "historical"},
    ]


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
