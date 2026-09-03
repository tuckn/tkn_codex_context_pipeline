from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_codex_context.chat_logs import read_thread_source
from tkn_codex_context.cli import (
    LOGGER,
    _configure_logging,
    _decision_output,
    _progress,
    _thread_note_output,
    build_parser,
    main,
)
from tkn_codex_context.config import CONFIG_SCHEMA_VERSION
from tkn_codex_context.console_logging import SUCCESS, ColorFormatter
from tkn_codex_context.initialization import ROOT_OWNERSHIP_MARKER


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


def write_decision_thread_note(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: threadNote\n"
        "schemaVersion: 3\n"
        "title: Decision source\n"
        "status: done\n"
        "threadNoteId: 20260803T090000+0900\n"
        "sourceThreadIds:\n"
        "  - thread-1\n"
        "sourceRefs:\n"
        "  - sessions/example.jsonl\n"
        "---\n\n"
        "# Thread Note\n\n"
        "## Summary\n\n- A decision was made.\n\n"
        "## Key Developments\n\n"
        "### Explicit Decision\n\n- Use Thread Notes as the primary input.\n\n"
        "## Last Known State\n\n- Work State: done — decided.\n",
        encoding="utf-8",
    )


def initialize_cli_pipeline(config_path: Path | None = None) -> None:
    prefix = ["--config", str(config_path)] if config_path else []
    assert main([*prefix, "config", "init"]) == 0
    assert main([*prefix, "init"]) == 0


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


def test_jsonl_parse_warning_uses_configured_logging(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    source = tmp_path / "invalid.jsonl"
    source.write_text("{invalid}\n", encoding="utf-8")

    normal = build_parser().parse_args(["config", "show"])
    _configure_logging(normal)
    read_thread_source(source)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"[WARNING] {source}:1:" in captured.err

    quiet = build_parser().parse_args(["--quiet", "config", "show"])
    _configure_logging(quiet)
    read_thread_source(source)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


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
            "threadNotePath": r"C:\notes\thread-2.md",
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
        r"(12.5s, 2 chunks, 3 model calls) — Thread Note: C:\notes\thread-2.md",
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
            "threadNotes": ["thread-notes/source.md", "thread-notes/verification.md"],
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
    assert profile["template"]["version"] == "2.0"
    assert profile["template"]["source"].endswith(
        "profiles/summary/default/template.md"
    )
    decision_profile = output["decisionProfile"]
    assert decision_profile["name"] == "default"
    assert decision_profile["source"].endswith("profiles/decision/default")
    assert decision_profile["prompt"]["version"] == "4.0"
    assert decision_profile["schema"]["source"].endswith(
        "profiles/decision/default/output.schema.json"
    )
    assert decision_profile["template"]["version"] == "2.0"
    working_context_profile = output["workingContextProfile"]
    assert working_context_profile["name"] == "default"
    assert working_context_profile["source"].endswith("profiles/working_context/default")
    assert working_context_profile["prompt"]["version"] == "1.0"
    assert working_context_profile["schema"]["source"].endswith(
        "profiles/working_context/default/output.schema.json"
    )
    assert working_context_profile["template"]["version"] == "1.0"
    assert output["config"]["schema_version"] == CONFIG_SCHEMA_VERSION
    assert output["configSchema"] == {
        "effectiveVersion": CONFIG_SCHEMA_VERSION,
        "hasInMemoryMigrations": False,
    }
    assert output["sources"]["generation.providers.codex.model"] == "built-in defaults"
    assert [layer["kind"] for layer in output["layers"]] == ["built-in", "global", "project"]
    assert output["layers"][0]["schemaVersion"] == CONFIG_SCHEMA_VERSION


def test_config_init_cli_creates_then_keeps_the_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn/codex_context_pipeline/config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    assert main(["config", "init"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert main(["config", "init"]) == 0
    unchanged = json.loads(capsys.readouterr().out)

    assert created == {
        "command": "config init",
        "status": "created",
        "configPath": str(target),
        "backupPath": None,
    }
    assert unchanged["status"] == "unchanged"
    assert target.is_file()
    assert target.read_text(encoding="utf-8").splitlines()[0] == (
        f'schema_version: "{CONFIG_SCHEMA_VERSION}"'
    )


def test_config_init_rejects_runtime_overrides(capsys: CaptureFixture[str]) -> None:
    result = main(["--model", "temporary", "config", "init"])

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert "cannot be used with config init" in output["error"]


def test_init_dry_run_keeps_config_and_creates_no_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["--config", str(target), "config", "init"]) == 0
    before = target.read_bytes()
    capsys.readouterr()

    result = main(["--config", str(target), "init", "--dry-run"])

    assert result == 0
    assert target.read_bytes() == before
    assert not (target.parent / "data").exists()
    assert not (target.parent / "state").exists()
    assert not (home / ".cache/codex_context_pipeline").exists()
    output = json.loads(capsys.readouterr().out)
    assert output["dryRun"] is True
    assert output["projectFetch"]["projects"][0]["projectId"] == "local-project"


def test_init_parser_keeps_force_and_adoption_mutually_exclusive() -> None:
    parser = build_parser()

    adoption = parser.parse_args(["init", "--adopt-existing", "--dry-run"])

    assert adoption.adopt_existing is True
    assert adoption.force is False
    assert adoption.dry_run is True
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["init", "--force", "--adopt-existing"])
    assert exc.value.code == 2


def test_init_adopt_existing_cli_only_marks_configured_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    roots = (
        target.parent / "data",
        target.parent / "state",
        home / ".cache/codex_context_pipeline",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert main(["config", "init"]) == 0
    capsys.readouterr()
    for root in roots:
        root.mkdir(parents=True)
        (root / "keep.txt").write_text("keep", encoding="utf-8")
    config_before = target.read_bytes()

    assert main(["init", "--adopt-existing", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)

    assert preview["plannedAdoptions"] == [str(root.resolve()) for root in roots]
    assert preview["adoptedTargets"] == []
    assert not any((root / ROOT_OWNERSHIP_MARKER).exists() for root in roots)

    assert main(["init", "--adopt-existing"]) == 0
    applied = json.loads(capsys.readouterr().out)

    assert applied["adoptedTargets"] == [str(root.resolve()) for root in roots]
    assert target.read_bytes() == config_before
    assert all((root / "keep.txt").read_text(encoding="utf-8") == "keep" for root in roots)
    assert all((root / ROOT_OWNERSHIP_MARKER).is_file() for root in roots)


def test_init_uses_existing_config_and_creates_project_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    assert main(["config", "init"]) == 0

    result = main(["init"])

    assert result == 0
    assert target.is_file()
    app_root = target.parent
    assert (app_root / "data/projects/local-project/thread-notes").is_dir()
    assert (app_root / "data/projects/local-project/decisions").is_dir()
    assert (app_root / "state/projects/local-project").is_dir()
    assert (home / ".cache/codex_context_pipeline").is_dir()
    assert not any((app_root / "data/projects/local-project/thread-notes").iterdir())
    assert (app_root / "data" / ROOT_OWNERSHIP_MARKER).is_file()
    assert (app_root / "state" / ROOT_OWNERSHIP_MARKER).is_file()
    assert (home / ".cache/codex_context_pipeline" / ROOT_OWNERSHIP_MARKER).is_file()


def test_projects_fetch_replaces_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline()
    capsys.readouterr()

    result = main(["projects", "fetch", "--dry-run"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "projects fetch"
    assert output["projectCount"] == 1
    with pytest.raises(SystemExit) as exc:
        main(["projects", "sync"])
    assert exc.value.code == 2


def test_thread_notes_pull_replaces_run_and_backfill() -> None:
    parser = build_parser()

    args = parser.parse_args(["thread-notes", "pull", "--dry-run"])

    assert args.notes_command == "pull"
    assert args.dry_run is True
    assert args.backfill is False
    assert args.full_output is False

    historical = parser.parse_args(
        [
            "thread-notes",
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
    forced = parser.parse_args(["thread-notes", "pull", "--force", "--dry-run"])
    assert forced.force is True
    full = parser.parse_args(
        ["thread-notes", "rebuild", "--project-id", "Project", "--full-output"]
    )
    assert full.full_output is True
    for removed_command in ("run", "backfill"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["thread-notes", removed_command])
        assert exc.value.code == 2


def test_decisions_build_writes_by_default_and_dry_run_is_explicit() -> None:
    parser = build_parser()

    normal = parser.parse_args(
        ["decisions", "build", "--project-id", "Project"]
    )
    planned = parser.parse_args(
        ["decisions", "build", "--project-id", "Project", "--dry-run"]
    )
    compatibility = parser.parse_args(
        ["decisions", "build", "--project-id", "Project", "--write"]
    )

    assert normal.decisions_command == "build"
    assert normal.dry_run is False
    assert normal.write is False
    assert planned.dry_run is True
    assert compatibility.write is True


def test_working_context_build_writes_by_default_and_dry_run_is_explicit() -> None:
    parser = build_parser()

    normal = parser.parse_args(
        ["working-context", "build", "--project-id", "Project"]
    )
    planned = parser.parse_args(
        ["working-context", "build", "--project-id", "Project", "--dry-run"]
    )
    compatibility = parser.parse_args(
        ["working-context", "build", "--project-id", "Project", "--write"]
    )

    assert normal.working_context_command == "build"
    assert normal.dry_run is False
    assert normal.write is False
    assert normal.allow_edited is False
    assert planned.dry_run is True
    assert compatibility.write is True


@pytest.mark.parametrize("command", ["decisions", "working-context"])
def test_build_rejects_dry_run_with_deprecated_write(command: str) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [command, "build", "--project-id", "Project", "--dry-run", "--write"]
        )

    assert exc.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["init", "--help"],
        ["projects", "fetch", "--help"],
        ["thread-notes", "pull", "--help"],
        ["thread-notes", "rebuild", "--help"],
        ["decisions", "build", "--help"],
        ["working-context", "build", "--help"],
    ],
)
def test_mutating_command_help_states_that_normal_execution_writes(
    arguments: list[str],
    capsys: CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(arguments)

    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "by default" in help_text
    assert "--dry-run" in help_text


def test_working_context_build_dry_run_routes_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    project_root = home / "project"
    project_root.mkdir(parents=True)
    (project_root / "README.md").write_text("# Project\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline()
    capsys.readouterr()

    result = main(
        ["working-context", "build", "--project-id", "local-project", "--dry-run"]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "working-context build"
    assert output["reportSummary"]["dryRun"] is True
    assert output["reportSummary"]["selectedCount"] == 1
    assert not (
        home
        / ".tkn/codex_context_pipeline/data/projects/local-project/working-context.md"
    ).exists()


def test_decisions_build_dry_run_routes_and_emits_compact_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline()
    capsys.readouterr()
    app_root = home / ".tkn" / "codex_context_pipeline"
    write_decision_thread_note(
        app_root / "data" / "projects" / "local-project" / "thread-notes" / "one.md"
    )

    result = main(
        ["decisions", "build", "--project-id", "local-project", "--dry-run"]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "decisions build"
    assert output["reportPath"] is None
    assert output["reportSummary"]["dryRun"] is True
    assert output["reportSummary"]["selectedCount"] == 1
    assert list((app_root / "data/projects/local-project/decisions").iterdir()) == []


def test_decisions_build_without_dry_run_executes_write_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline()
    capsys.readouterr()

    result = main(["decisions", "build", "--project-id", "local-project"])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["reportSummary"]["dryRun"] is False
    assert output["reportPath"] is not None
    assert Path(output["reportPath"]).is_file()


def test_user_summary_prompt_commands_and_override_are_not_public() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as prompt_command:
        parser.parse_args(["prompt", "init"])
    assert prompt_command.value.code == 2

    with pytest.raises(SystemExit) as prompt_override:
        parser.parse_args(["--summary-prompt", "custom.md", "config", "show"])
    assert prompt_override.value.code == 2


def test_compact_thread_note_output_summarizes_rebuild_counts(tmp_path: Path) -> None:
    report_path = tmp_path / "run.json"
    output = _thread_note_output(
        "thread-notes rebuild",
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
            "excluded": [{"threadId": "excluded-1"}, {"threadId": "excluded-2"}],
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
    assert output["reportSummary"]["excludedCount"] == 2
    assert output["reportSummary"]["preservedCurrentCount"] == 1
    assert output["reportSummary"]["replacedCurrentCount"] == 2
    assert output["reportSummary"]["scan"] == {"files": 370, "eligible": 3}


def test_compact_decision_output_includes_index_warning() -> None:
    output = _decision_output(
        {"pendingCount": 0},
        {
            "dryRun": True,
            "projectId": "project-1",
            "failed": [],
            "warnings": ["Existing Decision prompt index reached its limit."],
            "existingDecisionCount": 201,
            "existingDecisionIndexLimit": 200,
            "existingDecisionIndexOmittedCount": 1,
        },
        None,
        full_output=False,
    )

    assert output["reportSummary"]["warningCount"] == 1
    assert output["reportSummary"]["existingDecisionIndexLimit"] == 200
    assert output["reportSummary"]["existingDecisionIndexOmittedCount"] == 1


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["thread-notes", "pull", "--backfill", "--dry-run"],
            "--backfill requires --project-id <projectIdOrNameOrRoot> or --all",
        ),
        (
            ["thread-notes", "pull", "--all", "--dry-run"],
            "--project-id and --all require --backfill",
        ),
    ],
)
def test_thread_notes_pull_rejects_incomplete_backfill_options(
    arguments: list[str],
    message: str,
    capsys: CaptureFixture[str],
) -> None:
    result = main(arguments)

    assert result == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"ok": False, "error": message}


def test_thread_notes_pull_backfill_all_uses_historical_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline()
    capsys.readouterr()

    result = main(
        [
            "thread-notes",
            "pull",
            "--backfill",
            "--all",
            "--dry-run",
        ]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "thread-notes pull"
    assert output["ok"] is True
    assert output["reportSummary"]["mode"] == "backfill"
    assert output["reportSummary"]["dryRun"] is True
    assert output["reportPath"] is None
    assert "projects" not in output["projectFetchSummary"]


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "thread-notes",
            "rebuild",
            "--project-id",
            "Project",
            "--dry-run",
        ],
        [
            "thread-notes",
            "pull",
            "--backfill",
            "--project-id",
            "Project",
            "--dry-run",
        ],
    ],
)
def test_thread_notes_project_selector_accepts_unique_name(
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
    initialize_cli_pipeline(target)
    capsys.readouterr()

    result = main(["--config", str(target), *arguments])

    assert result == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "Resolved Project Name 'Project' to ID local-project" in captured.err
    if output["reportSummary"]["mode"] == "rebuild":
        assert output["reportSummary"]["projectId"] == "local-project"


def test_thread_notes_project_selector_accepts_current_root(
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
    initialize_cli_pipeline(target)
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "thread-notes",
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


def test_thread_notes_full_output_preserves_detailed_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline(target)
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "thread-notes",
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
    assert "excluded" in output["report"]
    assert "projects" in output["projectFetch"]
    assert "reportSummary" not in output


def test_thread_notes_write_run_emits_compact_summary_and_report_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    target = home / ".tkn" / "codex_context_pipeline" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    write_app_state(home)
    initialize_cli_pipeline(target)
    capsys.readouterr()

    result = main(["--config", str(target), "thread-notes", "pull"])

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


def test_thread_notes_project_selector_rejects_duplicate_name(
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
    initialize_cli_pipeline(target)
    capsys.readouterr()

    result = main(
        [
            "--config",
            str(target),
            "thread-notes",
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
                "schema_version": CONFIG_SCHEMA_VERSION,
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


def test_validate_command_accepts_thread_note_v3(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        """---
type: threadNote
schemaVersion: 3
promptId: f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11
promptVersion: "2.0"
outputSchemaSha256: 3ebffe117e29f76dfca25375a7e96ba0867de31a7ed68022dc6b65d91d651170
templateId: 4d19c51c-0d02-43a5-b6ad-6d67f9739b75
templateVersion: "2.0"
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

# Thread Note

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
