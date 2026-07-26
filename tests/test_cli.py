from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_codex_context.cli import main


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
    assert output["projectSync"]["projects"][0]["projectId"] == "local-project"


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
