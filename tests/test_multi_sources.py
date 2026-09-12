from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from test_config import write_yaml
from test_session_note_pipeline import FakeSummarizer, write_chat
from test_storage_layout import snapshot

from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import AppConfig, config_document, load_app_config, resolve_app_config, write_config
from tkn_genai_chat_note.pipeline import pipeline_status, run_pipeline
from tkn_genai_chat_note.provenance import validate_provenance
from tkn_genai_chat_note.session_notes import PipelineError, now_local
from tkn_genai_chat_note.storage_migration import migrate_storage


def multi_config(root: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "cache_root": root / "cache",
            "idle_minutes": 0,
            "chat": {
                "providers": {
                    "codex": {
                        "sources": {
                            name: {
                                "source_root": root / name / "input",
                                **{kind + "_root": root / name / kind for kind in ("raw", "data", "state")},
                            }
                            for name in ("pc-windows", "pc-wsl-ubuntu")
                        }
                    }
                }
            },
        }
    )


def source_chats(config: AppConfig) -> None:
    for source in config.enabled_source_configs():
        # Identical upstream thread IDs must still stay in independent source stores.
        write_chat(source.sessions_root / "chat.jsonl", thread_id="same-thread", cwd=source.source_root / "project")


@pytest.mark.parametrize("source_id", ["pc-windows", "PC_Windows.2023", "123"])
def test_source_id_preserves_valid_ascii_names(source_id: str) -> None:
    config = AppConfig.model_validate({"chat": {"providers": {"codex": {"sources": {source_id: {}}}}}})
    assert config.source_id == source_id
    assert config.raw_root.name == source_id


@pytest.mark.parametrize(
    "source_id",
    [
        "",
        " pc",
        "pc ",
        "My PC",
        "ＰＣ",
        "日本語",
        "../pc",
        "a/b",
        "a\\b",
        ".pc",
        "-pc",
        "pc.",
        "CON",
        "nul.txt",
        "Com1",
        "LPT9.log",
        123,
    ],
)
def test_invalid_or_nonportable_source_ids_are_rejected(source_id: object) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"chat": {"providers": {"codex": {"sources": {source_id: {}}}}}})


def test_case_only_source_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique ignoring case"):
        AppConfig.model_validate({"chat": {"providers": {"codex": {"sources": {"pc": {}, "PC": {}}}}}})


def test_duplicate_yaml_key_is_rejected_without_writes(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        'schema_version: "6.0.0"\nchat:\n  providers:\n    codex:\n      sources:\n        pc: {}\n        pc: {}\n',
        encoding="utf-8",
    )
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="duplicate YAML key"):
        load_app_config(explicit_path=path, cwd=tmp_path)
    assert snapshot(tmp_path) == before


def test_sources_merge_by_id_without_an_implicit_default_source(tmp_path: Path) -> None:
    global_path = Path.home() / ".tkn/genai_chat_note_pipeline/config.yaml"
    write_yaml(
        global_path,
        {
            "chat": {
                "providers": {
                    "codex": {
                        "sources": {
                            "pc-windows": {"source_root": "windows", "raw_root": "evidence"},
                            "pc-wsl": {"source_root": "wsl", "enabled": False},
                        }
                    }
                }
            }
        },
    )
    explicit = tmp_path / "explicit/config.yaml"
    write_yaml(
        explicit,
        {
            "chat": {
                "providers": {
                    "codex": {
                        "sources": {
                            "pc-windows": {"data_root": "notes", "include_archived": False},
                        }
                    }
                }
            }
        },
    )
    resolution = resolve_app_config(explicit_path=explicit, cwd=tmp_path)
    config = resolution.config
    assert list(config.chat.providers.codex.sources) == ["pc-windows", "pc-wsl"]
    assert config.source_root == global_path.parent / "windows"
    assert config.raw_root == global_path.parent / "evidence"
    assert config.data_root == explicit.parent / "notes"
    assert not config.include_archived
    assert resolution.sources["chat.providers.codex.sources.pc-windows.raw_root"].startswith("global:")
    assert resolution.sources["chat.providers.codex.sources.pc-windows.data_root"].startswith("explicit:")
    assert resolution.sources["chat.providers.codex.sources.pc-windows.state_root"] == "built-in defaults"
    assert not any("sources.windows." in key for key in resolution.sources)
    write_yaml(explicit, {"chat": {"providers": {"codex": {"sources": {}}}}})
    assert not load_app_config(explicit_path=explicit, cwd=tmp_path).chat.providers.codex.sources


def test_old_source_fields_are_rejected_in_schema_6(tmp_path: Path) -> None:
    for settings in (
        {"source_id": "pc", "home": "logs"},
        {"sources": {"pc": {"home": "logs"}}},
        {"sources": {"pc": {"source_id": "pc"}}},
        {"sources": [{"source_root": "logs"}]},
    ):
        path = tmp_path / "config.yaml"
        write_yaml(path, {"chat": {"providers": {"codex": settings}}})
        with pytest.raises(PipelineError, match="unknown configuration key|must be a mapping"):
            load_app_config(explicit_path=path, cwd=tmp_path)


def test_source_roundtrip_and_dry_run_leave_all_stores_unchanged(tmp_path: Path) -> None:
    config = multi_config(tmp_path)
    source_chats(config)
    path = tmp_path / "config.yaml"
    write_config(config, path)
    loaded = load_app_config(explicit_path=path, cwd=tmp_path)
    assert config_document(loaded) == config_document(config)
    before = snapshot(tmp_path)
    summarizer = FakeSummarizer()
    report = run_pipeline(loaded, mode="clone", dry_run=True, summarizer=summarizer)
    assert report["ok"] and not report["complete"]
    assert report["threadCounts"] == {"planned": 2}
    assert report["attemptedSessionNoteCount"] == 2
    assert not summarizer.calls and snapshot(tmp_path) == before
    status = pipeline_status(config)
    assert len(status["sourceResults"]) == 2 and not status["initialized"]
    assert snapshot(tmp_path) == before


def test_two_sources_generate_isolated_notes_and_resume_without_ai(tmp_path: Path) -> None:
    config = multi_config(tmp_path)
    source_chats(config)
    events: list[dict[str, Any]] = []
    report = run_pipeline(config, mode="clone", summarizer=FakeSummarizer(), progress=events.append)
    assert report["complete"] and report["generatedSessionNoteCount"] == 2
    assert len(report["reportPaths"]) == 2
    assert {event["sourceId"] for event in events} == set(config.chat.providers.codex.sources)
    note_ids = set()
    for source in config.enabled_source_configs():
        assert validate_provenance(source.data_root)["ok"]
        descriptor = json.loads((source.data_root / "store.json").read_text())
        assert descriptor["sourceId"] == source.source_id
        catalog = json.loads((source.data_root / "catalog/threads.json").read_text())
        assert len(catalog["threads"]) == 1
        row = catalog["threads"][0]
        assert row["sourceId"] == source.source_id
        assert row["sourceCaptureRef"].startswith(f"raw:/codex/{source.source_id}/")
        note_ids.add(row["noteId"])
    assert len(note_ids) == 2
    summarizer = FakeSummarizer()
    assert run_pipeline(config, mode="pull", summarizer=summarizer)["complete"]
    assert not summarizer.calls
    assert pipeline_status(config)["complete"]


def test_limit_is_shared_including_failed_attempts(tmp_path: Path) -> None:
    config = multi_config(tmp_path)
    source_chats(config)
    summarizer = FakeSummarizer(fail_thread="same-thread")
    report = run_pipeline(config, mode="clone", summarizer=summarizer, limit=1)
    assert report["attemptedSessionNoteCount"] == 1
    assert report["threadCounts"] == {"failed": 1, "deferred": 1}
    assert not report["ok"] and not report["complete"]
    assert len(summarizer.calls) == 1


def test_runtime_deadline_is_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_genai_chat_note.pipeline as pipeline

    config = multi_config(tmp_path)
    source_chats(config)
    original = pipeline._run_source_pipeline
    deadlines = []

    def capture_deadline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        deadlines.append(kwargs["deadline"])
        kwargs["deadline"] = now_local() - timedelta(seconds=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_run_source_pipeline", capture_deadline)
    summarizer = FakeSummarizer()
    report = run_pipeline(config, mode="clone", summarizer=summarizer)
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert report["threadCounts"] == {"deferred": 2} and not summarizer.calls


@pytest.mark.parametrize("invalid", ["duplicate-input", "overlap-input", "overlap-output", "unowned-output"])
def test_all_sources_preflight_before_any_write(tmp_path: Path, invalid: str) -> None:
    config = multi_config(tmp_path)
    first, second = config.enabled_source_configs()
    if invalid == "duplicate-input":
        second.source_settings.source_root = first.source_root
    elif invalid == "overlap-input":
        second.source_settings.source_root = first.source_root / "nested"
    elif invalid == "overlap-output":
        second.source_settings.raw_root = first.data_root
    else:
        second.data_root.mkdir(parents=True)
        (second.data_root / "personal.txt").write_text("Keep me")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="overlap|unowned"):
        run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
    assert snapshot(tmp_path) == before


def test_disabled_and_selected_sources_are_not_processed(tmp_path: Path) -> None:
    config = multi_config(tmp_path)
    first, second = config.enabled_source_configs()
    source_chats(config)
    report = run_pipeline(config, mode="raw", source_id=first.source_id)
    assert report["sourceId"] == first.source_id and report["ok"]
    assert not second.raw_root.exists() and not second.state_root.exists()
    second.source_settings.enabled = False
    from tkn_genai_chat_note.storage import validate_storage

    validate_storage(config)
    run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
    assert not second.raw_root.exists() and not second.state_root.exists()
    assert pipeline_status(config)["sourceId"] == first.source_id
    with pytest.raises(PipelineError, match="no supported chat source"):
        run_pipeline(config, mode="raw", source_id=second.source_id)
    with pytest.raises(PipelineError, match="unknown source_id"):
        run_pipeline(config, mode="raw", source_id="typo")


def test_one_source_error_does_not_hide_other_source_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_genai_chat_note.pipeline as pipeline

    config = multi_config(tmp_path)
    source_chats(config)
    original = pipeline._run_source_pipeline

    def fail_one(source: AppConfig, **kwargs: Any) -> dict[str, Any]:
        if source.source_id == "pc-windows":
            raise OSError("source disconnected")
        return original(source, **kwargs)

    monkeypatch.setattr(pipeline, "_run_source_pipeline", fail_one)
    report = run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
    assert not report["ok"] and not report["complete"]
    assert report["sourceResults"][1]["complete"]
    assert report["failed"][0]["sourceId"] == "pc-windows"


def test_cli_multi_source_show_status_selection_and_compact_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = multi_config(tmp_path)
    source_chats(config)
    path = tmp_path / "config.yaml"
    write_config(config, path)
    args = ["--config", str(path)]
    assert main([*args, "config", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown["storage"]["sourceRoots"]["codex"]) == set(config.chat.providers.codex.sources)
    assert main([*args, "clone", "--dry-run"]) == 0
    compact = json.loads(capsys.readouterr().out)
    assert len(compact["sourceResults"]) == 2
    assert all("threads" not in result and "rawIngest" not in result for result in compact["sourceResults"])
    assert main([*args, "--source", "pc-windows", "raw", "ingest", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["sourceId"] == "pc-windows"
    assert main([*args, "--source", "missing", "status"]) == 1
    assert "unknown source_id" in json.loads(capsys.readouterr().out)["error"]
    assert main([*args, "session-notes", "build", "--thread-id", "same-thread", "--dry-run"]) == 1
    assert "requires --source" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("schema", ["5.0.0", "6.0.0"])
def test_storage_5_relocation_accepts_old_and_multi_source_configs(tmp_path: Path, schema: str) -> None:
    config = multi_config(tmp_path / "old")
    source_chats(config)
    run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
    document = config_document(config)
    selected_id = "pc-wsl-ubuntu"
    if schema == "5.0.0":
        document["chat"]["providers"] = {
            "codex": {
                "source_id": selected_id,
                **document["chat"]["providers"]["codex"]["sources"][selected_id],
            }
        }
        codex = document["chat"]["providers"]["codex"]
        codex["home"] = codex.pop("source_root")
    document["schema_version"] = schema
    path = tmp_path / "old.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    destination = multi_config(tmp_path / "new").for_source("codex", selected_id)
    before = snapshot(tmp_path / "old")
    result = migrate_storage(destination, from_config=path, dry_run=False)
    assert result["status"] == "migrated"
    assert snapshot(tmp_path / "old") == before
    assert validate_provenance(destination.data_root)["ok"]
    assert not (tmp_path / "new/pc-windows").exists()


def test_case_collision_in_lower_layer_cannot_be_hidden(tmp_path: Path) -> None:
    write_yaml(
        Path.home() / ".tkn/genai_chat_note_pipeline/config.yaml",
        {
            "chat": {"providers": {"codex": {"sources": {"pc": {}, "PC": {}}}}},
        },
    )
    explicit = tmp_path / "clear.yaml"
    write_yaml(explicit, {"chat": {"providers": {"codex": {"sources": {}}}}})
    with pytest.raises(PipelineError, match="unique ignoring case"):
        load_app_config(explicit_path=explicit, cwd=tmp_path)


def test_processing_error_is_saved_in_its_source_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_genai_chat_note.pipeline as pipeline

    config = multi_config(tmp_path)
    source_chats(config)
    discover = pipeline.discover

    def fail_discovery(source: AppConfig, *args: Any, **kwargs: Any) -> Any:
        if source.source_id == "pc-windows":
            raise PipelineError("cannot read source catalog")
        return discover(source, *args, **kwargs)

    monkeypatch.setattr(pipeline, "discover", fail_discovery)
    report = run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
    failed = report["sourceResults"][0]
    assert failed["error"] == "cannot read source catalog" and failed["reportPath"]
    persisted = json.loads(Path(failed["reportPath"]).read_text())
    assert persisted == failed
    source = config.for_source("codex", "pc-windows")
    assert json.loads((source.state_root / "last-run.json").read_text()) == failed
    assert report["sourceResults"][1]["complete"]
