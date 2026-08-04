from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_codex_context.cli import (
    LOGGER,
    _configure_logging,
    _progress,
    _session_output,
    build_parser,
    main,
)
from tkn_codex_context.console_logging import SUCCESS, ColorFormatter


def write_app_state(
    home: Path,
    *,
    local_projects: dict[str, dict[str, object]] | None = None,
) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    projects = local_projects or {
        "local-project": {
            "id": "local-project",
            "name": "Project",
            "rootPaths": [str(home / "project")],
            "createdAt": "2026-07-01T00:00:00Z",
        }
    }
    (codex_home / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "local-projects": projects,
                "thread-project-assignments": {},
                "projectless-thread-ids": [],
            }
        ),
        encoding="utf-8",
    )


def write_decision_session_note(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: summary\n"
        "schemaVersion: 2\n"
        "title: Decision source\n"
        "status: done\n"
        "distillationStatus: pending\n"
        "distilledTo: []\n"
        "sessionId: 20260803T090000+0900\n"
        "sourceThreadIds:\n"
        "  - thread-1\n"
        "sourceRefs:\n"
        "  - sessions/example.jsonl\n"
        "---\n\n"
        "# Session Note\n\n"
        "## Summary\n\n- A decision was made.\n\n"
        "## Key Developments\n\n"
        "### Explicit Decision\n\n- Use Session Notes as the primary input.\n\n"
        "## Last Known State\n\n- Work State: done — decided.\n",
        encoding="utf-8",
    )


def test_logging_uses_readable_stderr_prefixes(
    capsys: CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(["config", "show"])

    _configure_logging(args)
    LOGGER.info("Readable progress")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "[INFO] Readable progress\n"
    assert logging.getLogger().level == logging.INFO


def test_quiet_and_verbose_logging_levels() -> None:
    quiet = build_parser().parse_args(["-q", "config", "show"])
    _configure_logging(quiet)
    assert logging.getLogger().level == logging.ERROR

    verbose = build_parser().parse_args(["-v", "config", "show"])
    _configure_logging(verbose)
    assert logging.getLogger().level == logging.DEBUG


def test_progress_events_are_human_readable(
    capsys: CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(["config", "show"])
    _configure_logging(args)

    _progress(
        {
            "type": "thread-start",
            "index": 2,
            "total": 7,
            "threadId": "thread-2",
        }
    )
    _progress(
        {
            "type": "thread-complete",
            "index": 2,
            "total": 7,
            "threadId": "thread-2",
            "sessionNotePath": r"C:\notes\thread-2.md",
            "durationSeconds": 12.5,
            "chunkCount": 2,
            "modelCalls": 3,
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "[INFO] Starting thread 2/7: thread-2",
        "[SUCCESS] Completed thread 2/7: thread-2 "
        r"(12.5s, 2 chunks, 3 model calls) — Session Note: C:\notes\thread-2.md",
    ]


def test_decision_batch_progress_logs_created_record_paths(
    capsys: CaptureFixture[str],
) -> None:
    args = build_parser().parse_args(["config", "show"])
    _configure_logging(args)

    _progress(
        {
            "type": "decision-batch-complete",
            "index": 1,
            "total": 2,
            "sessionNotes": ["sessions/source.md", "sessions/verification.md"],
            "createdCount": 2,
            "decisionRecordPaths": [
                r"C:\notes\decisions\DR-0001-first.md",
                r"C:\notes\decisions\DR-0002-second.md",
            ],
            "referencedCount": 0,
            "durationSeconds": 12.5,
            "modelCalls": 1,
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "[SUCCESS] Completed decision synthesis batch 1/2 "
        "(2 created, 0 updated, 0 existing) (12.5s, 1 model call)",
        r"[INFO] Decision Record: C:\notes\decisions\DR-0001-first.md",
        r"[INFO] Decision Record: C:\notes\decisions\DR-0002-second.md",
    ]


@pytest.mark.parametrize(
    ("level", "name", "color"),
    [
        (SUCCESS, "SUCCESS", "\x1b[32m"),
        (logging.ERROR, "ERROR", "\x1b[31m"),
    ],
)
def test_console_formatter_colors_success_and_error(
    level: int,
    name: str,
    color: str,
) -> None:
    formatter = ColorFormatter("[%(levelname)s] %(message)s", use_color=True)
    record = logging.LogRecord("test", level, __file__, 1, "message", (), None)

    assert formatter.format(record) == f"{color}[{name}] message\x1b[0m"


def test_console_formatter_keeps_redirected_output_plain() -> None:
    formatter = ColorFormatter("[%(levelname)s] %(message)s", use_color=False)
    record = logging.LogRecord("test", SUCCESS, __file__, 1, "message", (), None)

    assert formatter.format(record) == "[SUCCESS] message"


def test_config_show_reports_application_owned_summary_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert main(["-q", "config", "show"]) == 0
    output = json.loads(capsys.readouterr().out)

    profile = output["summaryProfile"]
    assert profile["name"] == "default"
    assert profile["source"].endswith("profiles/summary/default")
    assert len(profile["sha256"]) == 64
    assert profile["prompt"]["version"] == "2.0"
    assert profile["prompt"]["source"].endswith("profiles/summary/default/prompt.md")
    assert profile["schema"]["source"].endswith(
        "profiles/summary/default/output.schema.json"
    )
    assert len(profile["schema"]["sha256"]) == 64
    assert profile["template"]["version"] == "1.0"
    assert profile["template"]["source"].endswith(
        "profiles/summary/default/template.md"
    )
    decision_profile = output["decisionProfile"]
    assert decision_profile["name"] == "default"
    assert decision_profile["source"].endswith("profiles/decision/default")
    assert decision_profile["prompt"]["version"] == "2.1"
    assert decision_profile["schema"]["source"].endswith(
        "profiles/decision/default/output.schema.json"
    )
    assert decision_profile["template"]["version"] == "1.0"


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
    assert (app_root / "data/projects/local-project/decisions").is_dir()
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


def test_session_notes_pull_replaces_run_and_backfill() -> None:
    parser = build_parser()

    args = parser.parse_args(["session-notes", "pull", "--dry-run"])

    assert args.notes_command == "pull"
    assert args.dry_run is True
    assert args.backfill is False
    assert args.full_output is False

    historical = parser.parse_args(
        [
            "session-notes",
            "pull",
            "--backfill",
            "--project-id",
            "local-project",
            "--dry-run",
        ]
    )

    assert historical.notes_command == "pull"
    assert historical.backfill is True
    assert historical.project_id == "local-project"
    forced = parser.parse_args(["session-notes", "pull", "--force", "--dry-run"])
    assert forced.force is True
    full = parser.parse_args(
        ["session-notes", "rebuild", "--project-id", "Project", "--full-output"]
    )
    assert full.full_output is True
    for removed_command in ("run", "backfill"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["session-notes", removed_command])
        assert exc.value.code == 2


def test_decisions_build_is_read_only_by_default_and_write_is_explicit() -> None:
    parser = build_parser()

    planned = parser.parse_args(
        ["decisions", "build", "--project-id", "Project"]
    )
    writing = parser.parse_args(
        ["decisions", "build", "--project-id", "Project", "--write"]
    )

    assert planned.decisions_command == "build"
    assert planned.write is False
    assert writing.write is True


def test_decisions_build_dry_run_routes_and_emits_compact_summary(
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
    app_root = home / ".tkn" / "codex_context_pipeline"
    write_decision_session_note(
        app_root / "data" / "projects" / "local-project" / "sessions" / "one.md"
    )

    result = main(["decisions", "build", "--project-id", "local-project"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "decisions build"
    assert output["reportPath"] is None
    assert output["reportSummary"]["dryRun"] is True
    assert output["reportSummary"]["selectedCount"] == 1
    assert list((app_root / "data/projects/local-project/decisions").iterdir()) == []


def test_user_summary_prompt_commands_and_override_are_not_public() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as prompt_command:
        parser.parse_args(["prompt", "init"])
    assert prompt_command.value.code == 2

    with pytest.raises(SystemExit) as prompt_override:
        parser.parse_args(["--summary-prompt", "custom.md", "config", "show"])
    assert prompt_override.value.code == 2


def test_compact_session_output_summarizes_rebuild_counts(tmp_path: Path) -> None:
    report_path = tmp_path / "run.json"
    output = _session_output(
        "session-notes rebuild",
        {
            "dryRun": False,
            "projectCount": 21,
            "boundCount": 21,
            "newCount": 1,
            "pendingCount": 0,
            "threadAssignmentCount": 34,
            "projectlessThreadCount": 2,
            "projects": [{"large": "detail"}],
        },
        {
            "mode": "rebuild",
            "dryRun": False,
            "force": False,
            "projectId": "project-1",
            "selectedCount": 3,
            "generationCount": 3,
            "processed": [{}, {}, {}],
            "failed": [],
            "deferred": [],
            "warnings": ["warning"],
            "preservedCurrent": ["old.md"],
            "replacedCurrent": [{}, {}],
            "deletedLegacy": [],
            "scan": {"files": 370, "eligible": 3, "note": "omitted"},
        },
        report_path,
        full_output=False,
    )

    assert output["ok"] is True
    assert output["reportPath"] == str(report_path)
    assert output["projectFetchSummary"]["projectCount"] == 21
    assert "projects" not in output["projectFetchSummary"]
    assert output["reportSummary"]["processedCount"] == 3
    assert output["reportSummary"]["warningCount"] == 1
    assert output["reportSummary"]["preservedCurrentCount"] == 1
    assert output["reportSummary"]["replacedCurrentCount"] == 2
    assert output["reportSummary"]["scan"] == {"files": 370, "eligible": 3}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["session-notes", "pull", "--backfill", "--dry-run"],
            "--backfill requires --project-id <projectIdOrNameOrRoot> or --all",
        ),
        (
            ["session-notes", "pull", "--all", "--dry-run"],
            "--project-id and --all require --backfill",
        ),
    ],
)
def test_session_notes_pull_rejects_incomplete_backfill_options(
    arguments: list[str],
    message: str,
    capsys: CaptureFixture[str],
) -> None:
    result = main(arguments)

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": False, "error": message}


def test_session_notes_pull_backfill_all_uses_historical_mode(
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

    result = main(
        [
            "session-notes",
            "pull",
            "--backfill",
            "--all",
            "--dry-run",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "session-notes pull"
    assert output["ok"] is True
    assert output["reportSummary"]["mode"] == "backfill"
    assert output["reportSummary"]["dryRun"] is True
    assert output["reportPath"] is None
    assert "projects" not in output["projectFetchSummary"]


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "session-notes",
            "rebuild",
            "--project-id",
            "Project",
            "--dry-run",
        ],
        [
            "session-notes",
            "pull",
            "--backfill",
            "--project-id",
            "Project",
            "--dry-run",
        ],
    ],
)
def test_session_notes_project_selector_accepts_unique_name(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["--config", str(target), "init"]) == 0
    capsys.readouterr()

    result = main(["--config", str(target), *arguments])

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "Resolved Project Name 'Project' to ID local-project" in captured.err
    if output["reportSummary"]["mode"] == "rebuild":
        assert output["reportSummary"]["projectId"] == "local-project"


def test_session_notes_project_selector_accepts_current_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    current_root = home / "project"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["--config", str(target), "init"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "session-notes",
            "rebuild",
            "--project-id",
            str(current_root).replace("\\", "/") + "/",
            "--dry-run",
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "Resolved Project CURRENT ROOT" in captured.err
    assert output["reportSummary"]["projectId"] == "local-project"


def test_session_notes_full_output_preserves_detailed_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["--config", str(target), "init"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "session-notes",
            "rebuild",
            "--project-id",
            "Project",
            "--dry-run",
            "--full-output",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["report"]["mode"] == "rebuild"
    assert output["report"]["dryRun"] is True
    assert "projects" in output["projectFetch"]
    assert "reportSummary" not in output


def test_session_notes_write_run_emits_compact_summary_and_report_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["--config", str(target), "init"]) == 0
    capsys.readouterr()

    result = main(["--config", str(target), "session-notes", "pull"])

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["ok"] is True
    assert output["reportSummary"]["selectedCount"] == 0
    assert output["reportSummary"]["processedCount"] == 0
    assert output["reportSummary"]["failedCount"] == 0
    assert output["reportPath"]
    assert Path(output["reportPath"]).is_file()
    assert "\"report\":" not in captured.out
    assert "Run report:" in captured.err


def test_session_notes_project_selector_rejects_duplicate_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(
        home,
        local_projects={
            "project-one": {
                "id": "project-one",
                "name": "Duplicate",
                "rootPaths": [str(home / "one")],
                "createdAt": "2026-07-01T00:00:00Z",
            },
            "project-two": {
                "id": "project-two",
                "name": "Duplicate",
                "rootPaths": [str(home / "two")],
                "createdAt": "2026-07-01T00:00:00Z",
            },
        },
    )
    assert main(["--config", str(target), "init"]) == 0
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "session-notes",
            "rebuild",
            "--project-id",
            "Duplicate",
            "--dry-run",
        ]
    )

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == (
        "ambiguous Project Name 'Duplicate'; matching projectIds: "
        "project-one, project-two. Use --project-id with an exact Project ID."
    )


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
type: summary
schemaVersion: 2
promptId: f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11
promptVersion: "2.0"
outputSchemaSha256: 3ebffe117e29f76dfca25375a7e96ba0867de31a7ed68022dc6b65d91d651170
templateId: 4d19c51c-0d02-43a5-b6ad-6d67f9739b75
templateVersion: "1.0"
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
