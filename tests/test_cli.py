from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture

from tkn_codex_context.cli import main


def test_config_init_dry_run_has_no_file(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    target = tmp_path / "config.yaml"

    result = main(["--config", str(target), "config", "init", "--dry-run"])

    assert result == 0
    assert not target.exists()
    output = json.loads(capsys.readouterr().out)
    assert output["dryRun"] is True


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
