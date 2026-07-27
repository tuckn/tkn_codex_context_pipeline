from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_codex_context.cli import build_parser, main


def write_app_state(home: Path) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "local-projects": {
                    "local-project": {
                        "id": "local-project",
                        "name": "Project",
                        "rootPaths": [str(home / "project")],
                        "createdAt": "2026-07-01T00:00:00Z",
                    }
                },
                "thread-project-assignments": {},
                "projectless-thread-ids": [],
            }
        ),
        encoding="utf-8",
    )


def test_init_dry_run_has_no_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)

    result = main(["--config", str(target), "init", "--dry-run"])

    assert result == 0
    assert not target.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["dryRun"] is True
    assert output["projectFetch"]["projects"][0]["projectId"] == "local-project"


def test_init_creates_config_and_project_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)

    result = main(["init"])

    assert result == 0
    assert target.is_file()
    app_root = target.parent
    assert (app_root / "data/projects/local-project/sessions").is_dir()
    assert (app_root / "state/projects/local-project").is_dir()
    assert (home / ".cache/codex_context_pipeline").is_dir()
    assert not any((app_root / "data/projects/local-project/sessions").iterdir())


def test_projects_fetch_replaces_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["init"]) == 0
    capsys.readouterr()

    result = main(["projects", "fetch", "--dry-run"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "projects fetch"
    assert output["projectCount"] == 1
    with pytest.raises(SystemExit) as exc:
        main(["projects", "sync"])
    assert exc.value.code == 2


def test_session_notes_pull_replaces_run() -> None:
    parser = build_parser()

    args = parser.parse_args(["session-notes", "pull", "--dry-run"])

    assert args.notes_command == "pull"
    assert args.dry_run is True
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["session-notes", "run"])
    assert exc.value.code == 2


def test_invalid_config_returns_machine_readable_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("unknown_key: true\n", encoding="utf-8")

    result = main(["--config", str(target), "config", "show"])

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False


def test_projects_list_has_human_and_json_output(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.yaml"
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    config_path.write_text(
        json.dumps(
            {
                "data_root": str(data_root),
                "state_root": str(state_root),
                "cache_root": str(tmp_path / "cache"),
            }
        ),
        encoding="utf-8",
    )
    registry = data_root / "project-registry.jsonl"
    registry.parent.mkdir(parents=True)
    records = [
        {
            "schemaVersion": 2,
            "identityKind": "codexAppLocalProject",
            "projectId": "local-notes",
            "title": "notes",
            "status": "active",
            "currentRoot": str(tmp_path / "notes"),
            "roots": [
                {
                    "path": str(tmp_path / "notes"),
                    "role": "primary",
                    "status": "active",
                }
            ],
        },
        {
            "schemaVersion": 2,
            "identityKind": "codexAppLocalProject",
            "projectId": "local-archive",
            "title": "Archive",
            "status": "inactive",
            "currentRoot": str(tmp_path / "archive"),
            "roots": [],
        },
    ]
    registry.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    result = main(["--config", str(config_path), "projects", "list"])

    assert result == 0
    human = capsys.readouterr().out
    assert "STATUS" in human
    assert "NAME" in human
    assert "PROJECT ID" in human
    assert "CURRENT ROOT" in human
    assert "notes" in human
    assert "local-notes" in human
    assert "inactive" in human

    result = main(["--config", str(config_path), "projects", "list", "--json"])

    assert result == 0
    machine = json.loads(capsys.readouterr().out)
    assert machine["command"] == "projects list"
    assert machine["projectCount"] == 2
    assert [project["projectId"] for project in machine["projects"]] == [
        "local-notes",
        "local-archive",
    ]
    assert machine["projects"][0]["roots"][0]["role"] == "primary"


def test_validate_command_accepts_session_note_v2(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        """---
type: session
schemaVersion: 2
reviewStatus: unreviewed
automatedValidation: passed
status: done
sourceType: codexChat
sourceThreadIds:
  - thread-1
sourceRefs:
  - windows/2026/chat.jsonl
sourceFingerprint: abc123
---

# Session Note

## Summary

- Completed.

## Key Developments

### Reported Result

- Completed.

## Last Known State

- Work State: done — Completed.
- Latest User Direction: Complete it.
""",
        encoding="utf-8",
    )

    result = main(["validate", str(note)])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
