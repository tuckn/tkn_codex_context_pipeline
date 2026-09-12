from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pytest import CaptureFixture

from tkn_genai_chat_note.chat_logs import read_thread_source
from tkn_genai_chat_note.cli import LOGGER, _configure_logging, _progress, build_parser, main
from tkn_genai_chat_note.config import CONFIG_SCHEMA_VERSION
from tkn_genai_chat_note.console_logging import SUCCESS, ColorFormatter


@pytest.mark.parametrize(
    "args",
    [
        ["init"],
        ["projects", "fetch"],
        ["session-notes", "pull"],
        ["session-notes", "rebuild"],
        ["pull", "--backfill"],
        ["decisions", "build", "--write"],
        ["working-context", "build", "--write"],
    ],
)
def test_retired_commands_and_compatibility_flags_are_removed(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(args)


@pytest.mark.parametrize("args", [["clone"], ["pull"], ["session-notes", "build"]])
def test_builds_write_by_default_and_have_explicit_dry_run(args: list[str]) -> None:
    assert not build_parser().parse_args(args).dry_run
    assert build_parser().parse_args([*args, "--dry-run"]).dry_run


def test_clone_dry_run_and_pull_initialization_boundary(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    from test_pipeline_workflow import config_for
    from test_session_note_pipeline import write_chat

    from tkn_genai_chat_note.config import write_config

    config = config_for(tmp_path)
    target = tmp_path / "config.yaml"
    write_config(config, target)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert main(["--config", str(target), "pull", "--dry-run"]) == 1
    assert "clone" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--config", str(target), "clone", "--dry-run", "--full-output"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["threads"][0]["status"] == "planned"
    assert not config.data_root.exists() and result["reportPath"] is None


@pytest.mark.parametrize("complete,failed,expected", [(True, False, 0), (False, False, 2), (False, True, 1)])
def test_pipeline_exit_codes_and_compact_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
    complete: bool,
    failed: bool,
    expected: int,
) -> None:
    import tkn_genai_chat_note.pipeline as pipeline

    def fake_run(*args, **kwargs):
        return {
            "complete": complete,
            "ok": complete,
            "failed": ["failure"] if failed else [],
            "threadCounts": {},
            "scopeCounts": {},
            "threads": [{"private": "detail"}],
            "scopeResults": [],
            "rawIngest": {},
            "reportPath": None,
        }

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    assert main(["clone"]) == expected
    result = json.loads(capsys.readouterr().out)
    assert "threads" not in result and result["complete"] == complete


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
    assert profile["name"] == "default-jp"
    assert profile["source"].endswith("profiles/default-jp")
    assert len(profile["sha256"]) == 64
    assert profile["prompt"]["version"] == "3.4"
    assert profile["prompt"]["source"].endswith("profiles/default-jp/prompt.md")
    assert profile["schema"]["source"].endswith("profiles/default-jp/output.schema.json")
    assert len(profile["schema"]["sha256"]) == 64
    assert profile["template"]["version"] == "4.1"
    assert profile["template"]["source"].endswith("profiles/default-jp/template.md")
    assert "decisionProfile" not in output and "workingContextProfile" not in output
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
    target = home / ".tkn/genai_chat_note_pipeline/config.yaml"
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
    assert target.read_text(encoding="utf-8").splitlines()[0] == (f'schema_version: "{CONFIG_SCHEMA_VERSION}"')


def test_config_init_rejects_runtime_overrides(capsys: CaptureFixture[str]) -> None:
    result = main(["--model", "temporary", "config", "init"])

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert "cannot be used with config init" in output["error"]


def test_user_summary_prompt_commands_and_override_are_not_public() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as prompt_command:
        parser.parse_args(["prompt", "init"])
    assert prompt_command.value.code == 2

    with pytest.raises(SystemExit) as prompt_override:
        parser.parse_args(["--summary-prompt", "custom.md", "config", "show"])
    assert prompt_override.value.code == 2


def test_invalid_config_returns_machine_readable_error(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("unknown_key: true\n", encoding="utf-8")

    result = main(["--config", str(target), "config", "show"])

    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False


def test_validate_command_accepts_legacy_thread_note_v3(
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

    result = main(["session-notes", "validate", str(note)])

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True


@pytest.mark.parametrize("provider", ["claude-code", "github-copilot"])
def test_future_chat_provider_can_be_inspected_but_not_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str], provider: str
) -> None:
    from test_config import write_yaml

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    target = tmp_path / "config.yaml"
    write_yaml(
        target,
        {"chat": {"providers": {provider: {"sources": {"windows": {"enabled": True, "source_root": "~/source"}}}}}},
    )
    assert main(["--config", str(target), "config", "show"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["config"]["chat"]["providers"][provider]["sources"]["windows"]["enabled"] is True
    assert output["sources"][f"chat.providers.{provider}.sources.windows.enabled"].startswith("explicit:")
    assert main(["--config", str(target), "raw", "ingest", "--dry-run"]) == 1
    assert "chat acquisition is not implemented" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / "user").exists()


def test_session_note_command_and_legacy_reader(capsys: CaptureFixture[str]) -> None:
    assert main(["session-notes", "validate", str(Path(__file__).parent / "fixtures/session-note-v6.md")]) == 0
    assert json.loads(capsys.readouterr().out)["schemaVersion"] == 6
    assert main(["session-notes", "validate", str(Path(__file__).parent / "fixtures/thread-note-v5.md")]) == 0
    assert json.loads(capsys.readouterr().out)["schemaVersion"] == 5
    with pytest.raises(SystemExit):
        main(["thread-notes", "build"])
