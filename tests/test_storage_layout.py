from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest
from test_pipeline_workflow import config_for
from test_thread_note_pipeline import FakeSummarizer, write_chat

from tkn_genai_chat_note.catalog import thread_key
from tkn_genai_chat_note.config import AppConfig
from tkn_genai_chat_note.pipeline import pipeline_status, run_pipeline
from tkn_genai_chat_note.provenance import validate_provenance
from tkn_genai_chat_note.storage_migration import migrate_storage
from tkn_genai_chat_note.thread_notes import PipelineError


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def legacy_store(tmp_path: Path, *, raw_only: bool = False) -> AppConfig:
    config = config_for(tmp_path)
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/storage-v2.zip") as archive:
        for name in archive.namelist():
            if raw_only and not name.startswith("raw/"):
                continue
            target = tmp_path / name
            assert target.resolve().is_relative_to(tmp_path.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.read(name)
            if name.startswith(("state/", "cache/")) and name.endswith(".json"):
                content = content.replace(b"C:/example/legacy", tmp_path.as_posix().encode())
            target.write_bytes(content)
    return config


def test_migration_preserves_notes_ids_snapshots_and_resumes_without_generation(tmp_path: Path) -> None:
    config = legacy_store(tmp_path)
    before = snapshot(tmp_path)
    assert validate_provenance(config.data_root)["ok"]
    plan = migrate_storage(config, dry_run=True)
    assert plan["status"] == "planned" and plan["files"]
    assert snapshot(tmp_path) == before
    assert all("sha256" in item for item in plan["files"])

    result = migrate_storage(config, dry_run=False)
    assert result["status"] == "migrated"
    assert migrate_storage(config, dry_run=False)["status"] == "already-migrated"
    for relative, content in before.items():
        if content is not None and relative.startswith(("raw/", "data/thread-notes/", "data/source-aligned/")):
            assert (tmp_path / relative).read_bytes() == content
        if content is not None and relative.startswith("data/provenance/") and relative != "data/provenance/index.json":
            assert (tmp_path / relative).read_bytes() == content
    old_note = next((config.data_root / "thread-notes").rglob("*.md"))
    new_note = config.source_data_root / old_note.relative_to(config.data_root)
    assert new_note.read_bytes() == old_note.read_bytes()
    assert validate_provenance(config.data_root)["ok"]
    assert pipeline_status(config)["initialized"]
    # Original application logs are absent. Continue from the migrated Raw copies.
    summary = FakeSummarizer()
    report = run_pipeline(config, mode="pull", summarizer=summary)
    assert report["complete"] and not summary.calls
    assert new_note.read_bytes() == old_note.read_bytes()
    assert report["threads"][0]["noteRef"].startswith("data:/codex/windows/thread-notes/")
    assert report["threads"][0]["sourceCaptureRef"].startswith("raw:/codex/windows/")
    assert validate_provenance(config.data_root)["ok"]


def test_migration_preserves_hand_edited_note_bytes(tmp_path: Path) -> None:
    config = legacy_store(tmp_path)
    note = next((config.data_root / "thread-notes").rglob("*.md"))
    edited = note.read_bytes().replace(b"reviewStatus: unreviewed", b"reviewStatus: reviewed") + b"\nPersonal edit.\n"
    note.write_bytes(edited)
    migrate_storage(config, dry_run=False)
    assert (config.source_data_root / note.relative_to(config.data_root)).read_bytes() == edited
    summary = FakeSummarizer()
    run_pipeline(config, mode="pull", summarizer=summary)
    assert not summary.calls
    assert (config.source_data_root / note.relative_to(config.data_root)).read_bytes() == edited


@pytest.mark.parametrize("dry_run", [True, False])
def test_migration_rejects_conflicts_without_overwriting(tmp_path: Path, dry_run: bool) -> None:
    config = legacy_store(tmp_path)
    old = next((config.data_root / "thread-notes").rglob("*.md"))
    target = config.source_data_root / old.relative_to(config.data_root)
    target.parent.mkdir(parents=True)
    target.write_text("different user data", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="conflicts"):
        migrate_storage(config, dry_run=dry_run)
    assert snapshot(tmp_path) == before


def test_migration_rolls_back_file_changes_after_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkn_genai_chat_note.storage_migration as migration

    config = legacy_store(tmp_path)
    before = snapshot(tmp_path)
    original_write = migration.atomic_write_bytes
    failed = False

    def fail_once(path: Path, content: bytes) -> None:
        nonlocal failed
        if path == config.source_state_root / "pipeline.json" and not failed:
            failed = True
            raise OSError("injected migration failure")
        original_write(path, content)

    monkeypatch.setattr(migration, "atomic_write_bytes", fail_once)
    with pytest.raises(OSError, match="injected"):
        migrate_storage(config, dry_run=False)
    after = snapshot(tmp_path)
    for relative, content in before.items():
        if content is not None:
            assert after[relative] == content
    assert not (config.source_state_root / "pipeline.json").exists()
    # A retry after rollback can use the remaining empty namespace directories.
    assert migrate_storage(config, dry_run=False)["status"] == "migrated"
    assert validate_provenance(config.data_root)["ok"]


def test_migration_rejects_corrupt_raw_and_wrong_source(tmp_path: Path) -> None:
    config = legacy_store(tmp_path)
    config.chat.providers.codex.source_id = "another-pc"
    with pytest.raises(PipelineError, match="source_id of the legacy"):
        migrate_storage(config, dry_run=True)
    config.chat.providers.codex.source_id = "windows"
    capture = next((config.raw_root / "windows/sessions").rglob("*.jsonl"))
    capture.write_bytes(b"damaged")
    with pytest.raises(PipelineError, match="hash/size mismatch"):
        migrate_storage(config, dry_run=True)


def test_fresh_storage_migration_is_a_no_op(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    before = snapshot(tmp_path)
    assert migrate_storage(config, dry_run=False)["status"] == "no-legacy-storage"
    assert snapshot(tmp_path) == before


def test_two_sources_share_roots_without_overwriting_catalog_notes_or_state(tmp_path: Path) -> None:
    first = config_for(tmp_path)
    first.chat.providers.codex.source_id = "pc-a-windows"
    write_chat(first.sessions_root / "one.jsonl", thread_id="same-thread", cwd=Path("C:/example/project"))
    first_report = run_pipeline(first, mode="clone", summarizer=FakeSummarizer())
    assert first_report["complete"]
    first_note = first.data_root / first_report["threads"][0]["noteRef"].removeprefix("data:/")
    first_bytes = first_note.read_bytes()
    state_before = snapshot(first.source_state_root)
    second = config_for(tmp_path)
    second.chat.providers.codex.source_id = "pc-a-wsl"
    second_report = run_pipeline(second, mode="clone", summarizer=FakeSummarizer())
    assert second_report["complete"]
    catalog = json.loads((first.data_root / "catalog/threads.json").read_text())
    assert {entry["sourceId"] for entry in catalog["threads"]} == {"pc-a-windows", "pc-a-wsl"}
    assert all(entry["threadKey"] == thread_key("same-thread") for entry in catalog["threads"])
    assert first_note.read_bytes() == first_bytes and snapshot(first.source_state_root) == state_before
    index = json.loads((first.data_root / "provenance/index.json").read_text())
    assert len(index["artifacts"]) == 2 and all(item["status"] == "current" for item in index["artifacts"])
    assert len({item["id"] for item in index["artifacts"]}) == 2
    assert validate_provenance(first.data_root)["ok"]
    again = run_pipeline(first, mode="pull", summarizer=FakeSummarizer())
    assert again["complete"] and again["generatedThreadNoteCount"] == 0
    assert len(json.loads((first.data_root / "catalog/threads.json").read_text())["threads"]) == 2
    assert sha256(first_note.read_bytes()).digest() == sha256(first_bytes).digest()


def test_dry_run_does_not_create_namespaces(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="test", cwd=Path("C:/example/project"))
    before = snapshot(tmp_path)
    report = run_pipeline(config, mode="clone", dry_run=True)
    assert report["threads"][0]["sourceCaptureRef"].startswith("raw:/codex/windows/")
    assert snapshot(tmp_path) == before


def test_shared_index_completion_includes_prior_sources(tmp_path: Path) -> None:
    first = config_for(tmp_path)
    first.chat.providers.codex.source_id = "pc-one"
    write_chat(first.sessions_root / "one.jsonl", thread_id="one", cwd=Path("C:/example/project"))
    from tkn_genai_chat_note.provenance import ProvenanceStore

    store = ProvenanceStore(first.data_root)
    store.publish_index([], run_id="first", complete=False, source=("codex", "pc-one"))
    store.publish_index([], run_id="second", complete=True, source=("codex", "pc-two"))
    index = json.loads((store.root / "index.json").read_text())
    assert index["pipelineComplete"] is False
    assert index["sourceRuns"] == {"codex/pc-one": False, "codex/pc-two": True}


def test_raw_only_legacy_store_can_be_migrated_and_used_without_originals(tmp_path: Path) -> None:
    config = legacy_store(tmp_path, raw_only=True)
    before = snapshot(tmp_path)
    assert migrate_storage(config, dry_run=True)["status"] == "planned"
    assert snapshot(tmp_path) == before
    assert migrate_storage(config, dry_run=False)["status"] == "migrated"
    assert (config.source_raw_root / "manifest.jsonl").is_file()
    report = run_pipeline(config, mode="pull", summarizer=FakeSummarizer())
    assert report["complete"] and validate_provenance(config.data_root)["ok"]


def test_interrupted_migration_can_resume_after_shared_index_was_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tkn_genai_chat_note.storage_migration as migration

    config = legacy_store(tmp_path)
    original_write = migration.atomic_write_bytes
    stopped = False

    def interrupt_once(path: Path, content: bytes) -> None:
        nonlocal stopped
        if path == config.source_state_root / "pipeline.json" and not stopped:
            stopped = True
            raise SystemExit("simulate interrupted process")
        original_write(path, content)

    monkeypatch.setattr(migration, "atomic_write_bytes", interrupt_once)
    with pytest.raises(SystemExit, match="interrupted"):
        migrate_storage(config, dry_run=False)
    with pytest.raises(PipelineError, match="storage migrate"):
        run_pipeline(config, mode="pull", dry_run=True)
    assert migrate_storage(config, dry_run=False)["status"] == "migrated"
    assert validate_provenance(config.data_root)["ok"]
    summary = FakeSummarizer()
    assert run_pipeline(config, mode="pull", summarizer=summary)["complete"]
    assert not summary.calls
