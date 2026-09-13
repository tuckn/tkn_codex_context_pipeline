from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from test_pipeline_workflow import config_for
from test_session_note_pipeline import FakeSummarizer, write_chat

from tkn_codex_chat_note.config import AppConfig, write_config
from tkn_codex_chat_note.pipeline import pipeline_status, run_pipeline
from tkn_codex_chat_note.provenance import validate_provenance
from tkn_codex_chat_note.session_notes import PipelineError
from tkn_codex_chat_note.storage_migration import migrate_storage


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None for path in root.rglob("*")
    }


def legacy_store(tmp_path: Path, *, raw_only: bool = False) -> tuple[AppConfig, Path, Path]:
    source = tmp_path / "old"
    with zipfile.ZipFile(Path(__file__).parent / "fixtures/storage-v2.zip") as archive:
        for name in archive.namelist():
            if raw_only and not name.startswith("raw/"):
                continue
            target = source / name
            assert target.resolve().is_relative_to(source.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.read(name)
            if name.startswith(("state/", "cache/")) and name.endswith(".json"):
                content = content.replace(b"C:/example/legacy", source.as_posix().encode())
            target.write_bytes(content)
    source_config = source / "old.yaml"
    source_config.write_text(
        'schema_version: "2.2.0"\nsource_id: windows\nraw_root: raw\ndata_root: data\nstate_root: state\n',
        encoding="utf-8",
    )
    return config_for(tmp_path / "new"), source_config, source


def test_migration_preserves_notes_ids_snapshots_and_regenerates_new_contract(tmp_path: Path) -> None:
    config, source_config, source = legacy_store(tmp_path)
    before = snapshot(tmp_path)
    plan = migrate_storage(config, from_config=source_config, dry_run=True)
    assert plan["status"] == "planned" and plan["files"]
    assert snapshot(tmp_path) == before
    assert all("sha256" in item for item in plan["files"])
    original = snapshot(source)
    assert migrate_storage(config, from_config=source_config, dry_run=False)["status"] == "migrated"
    assert snapshot(source) == original
    assert migrate_storage(config, from_config=source_config, dry_run=False)["status"] == "already-migrated"
    old_note = next((source / "data/thread-notes").rglob("*.md"))
    new_note = config.data_root / "session-notes" / old_note.relative_to(source / "data/thread-notes")
    assert new_note.read_bytes() == old_note.read_bytes()
    assert validate_provenance(config.data_root)["ok"]
    assert pipeline_status(config)["initialized"]
    summary = FakeSummarizer()
    report = run_pipeline(config, mode="pull", summarizer=summary)
    assert report["complete"] and summary.calls
    assert report["threads"][0]["noteRef"].startswith("data:/session-notes/")
    assert report["threads"][0]["sourceCaptureRef"].startswith("raw:/codex/windows/")
    assert validate_provenance(config.data_root)["ok"]
    assert snapshot(source) == original


def test_migration_preserves_hand_edited_note_bytes(tmp_path: Path) -> None:
    config, source_config, source = legacy_store(tmp_path)
    note = next((source / "data/thread-notes").rglob("*.md"))
    edited = note.read_bytes().replace(b"reviewStatus: unreviewed", b"reviewStatus: reviewed") + b"\nPersonal edit.\n"
    note.write_bytes(edited)
    migrate_storage(config, from_config=source_config, dry_run=False)
    new = config.data_root / "session-notes" / note.relative_to(source / "data/thread-notes")
    assert new.read_bytes() == edited
    assert validate_provenance(config.data_root)["ok"]
    summary = FakeSummarizer()
    run_pipeline(config, mode="pull", summarizer=summary)
    assert not summary.calls and new.read_bytes() == edited


@pytest.mark.parametrize("dry_run", [True, False])
def test_migration_rejects_conflicts_without_overwriting(tmp_path: Path, dry_run: bool) -> None:
    config, source_config, source = legacy_store(tmp_path)
    config.data_root.mkdir(parents=True)
    (config.data_root / "unrelated.txt").write_text("user data", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="unowned|conflict"):
        migrate_storage(config, from_config=source_config, dry_run=dry_run)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("interrupt", [False, True])
def test_migration_recovers_write_failure_and_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    import tkn_codex_chat_note.storage_migration as migration

    config, source_config, source = legacy_store(tmp_path)
    before = snapshot(source)
    original_write = migration.atomic_write_bytes
    failed = False

    def fail_once(path: Path, content: bytes) -> None:
        nonlocal failed
        if path == config.state_root / "pipeline.json" and not failed:
            failed = True
            if interrupt:
                raise SystemExit("interrupted")
            raise OSError("injected failure")
        original_write(path, content)

    monkeypatch.setattr(migration, "atomic_write_bytes", fail_once)
    with pytest.raises(SystemExit if interrupt else OSError):
        migrate_storage(config, from_config=source_config, dry_run=False)
    assert snapshot(source) == before
    with pytest.raises(PipelineError, match="storage migrate"):
        run_pipeline(config, mode="clone", dry_run=True)
    assert migrate_storage(config, from_config=source_config, dry_run=False)["status"] == "migrated"
    assert validate_provenance(config.data_root)["ok"]


def test_migration_rejects_corrupt_raw_and_wrong_source(tmp_path: Path) -> None:
    config, source_config, source = legacy_store(tmp_path)
    config.sources = {"other": config.source_settings}
    with pytest.raises(PipelineError, match="retain provider and source_id"):
        migrate_storage(config, from_config=source_config, dry_run=True)
    config.sources = {"windows": config.source_settings}
    next((source / "raw/windows/sessions").rglob("*.jsonl")).write_bytes(b"corrupt")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="hash/size mismatch"):
        migrate_storage(config, from_config=source_config, dry_run=False)
    assert snapshot(tmp_path) == before


def test_two_source_stores_are_independent_and_cannot_reuse_a_root(tmp_path: Path) -> None:
    first = config_for(tmp_path / "first")
    first.sources = {"pc-one": first.source_settings}
    write_chat(first.sessions_root / "one.jsonl", thread_id="same-thread", cwd=Path("C:/example/project"))
    report = run_pipeline(first, mode="clone", summarizer=FakeSummarizer())
    assert report["complete"]
    before = snapshot(first.data_root)
    second = config_for(tmp_path / "second")
    second.sources = {"pc-two": second.source_settings}
    write_chat(second.sessions_root / "one.jsonl", thread_id="same-thread", cwd=Path("C:/example/project"))
    assert run_pipeline(second, mode="clone", summarizer=FakeSummarizer())["complete"]
    assert snapshot(first.data_root) == before
    assert validate_provenance(first.data_root)["ok"] and validate_provenance(second.data_root)["ok"]
    second.source_settings.data_root = first.data_root
    with pytest.raises(PipelineError, match="different source identity"):
        run_pipeline(second, mode="clone", dry_run=True)


def test_explicit_paths_are_final_and_defaults_are_source_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config = AppConfig()
    assert config.data_root == tmp_path / ".tkn/codex_chat_note_pipeline/data/codex/windows"
    config.source_settings.raw_root = tmp_path / "arbitrary/location"
    assert config.raw_root == tmp_path / "arbitrary/location"
    assert config.source_cache_root == config.cache_root / "codex/windows"


def test_raw_only_legacy_store_is_usable_without_originals(tmp_path: Path) -> None:
    config, source_config, source = legacy_store(tmp_path, raw_only=True)
    before = snapshot(source)
    migrate_storage(config, from_config=source_config, dry_run=False)
    assert snapshot(source) == before
    report = run_pipeline(config, mode="pull", summarizer=FakeSummarizer())
    assert report["complete"] and validate_provenance(config.data_root)["ok"]


def test_current_store_can_be_relocated_without_inference_or_identity_changes(tmp_path: Path) -> None:
    old = config_for(tmp_path / "old")
    write_chat(old.sessions_root / "one.jsonl", thread_id="one", cwd=Path("C:/example"))
    assert run_pipeline(old, mode="clone", summarizer=FakeSummarizer())["complete"]
    source_config = tmp_path / "old.yaml"
    write_config(old, source_config)
    before = snapshot(tmp_path / "old")
    new = config_for(tmp_path / "new")
    migrate_storage(new, from_config=source_config, dry_run=False)
    note = next(new.data_root.rglob("*.md"))
    original = note.read_bytes()
    summary = FakeSummarizer()
    result = run_pipeline(new, mode="pull", summarizer=summary)
    assert result["complete"] and not summary.calls
    assert note.read_bytes() == original
    assert snapshot(tmp_path / "old") == before


def test_migration_requires_disjoint_destination_roots(tmp_path: Path) -> None:
    config, source_config, source = legacy_store(tmp_path)
    config.source_settings.raw_root = source / "raw/new"
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="fresh destination roots"):
        migrate_storage(config, from_config=source_config, dry_run=True)
    assert snapshot(tmp_path) == before


def test_shared_v4_store_is_split_without_copying_another_sources_provenance(tmp_path: Path) -> None:
    old = config_for(tmp_path / "seed")
    write_chat(old.sessions_root / "one.jsonl", thread_id="one", cwd=Path("C:/example"))
    assert run_pipeline(old, mode="clone", summarizer=FakeSummarizer())["complete"]
    shared = tmp_path / "shared"
    for kind in ("raw", "data", "state"):
        shutil.copytree(getattr(old, kind + "_root"), shared / kind / "codex/windows")
    public = shared / "data"
    for name in ("catalog", "provenance"):
        shutil.copytree(old.data_root / name, public / name)
    catalog = json.loads((public / "catalog/threads.json").read_text())
    for row in catalog["threads"]:
        row["noteRef"] = row["noteRef"].replace("data:/", "data:/codex/windows/", 1)
    catalog["threads"].append(
        {"threadId": "other", "threadKey": "other", "sourceProvider": "codex", "sourceId": "other", "status": "pending"}
    )
    (public / "catalog/threads.json").write_text(json.dumps(catalog), encoding="utf-8")
    index = json.loads((public / "provenance/index.json").read_text())
    for item in index["artifacts"]:
        item["ref"] = item["ref"].replace("data:/", "data:/codex/windows/", 1)
    (public / "provenance/index.json").write_text(json.dumps(index), encoding="utf-8")
    (public / "provenance/blobs/unrelated").mkdir()
    (public / "provenance/blobs/unrelated/private").write_bytes(b"other source")
    metadata = shared / "state/codex/windows/pipeline.json"
    document = json.loads(metadata.read_text())
    document["storageVersion"] = 4
    metadata.write_text(json.dumps(document), encoding="utf-8")
    source_config = shared / "old.yaml"
    source_config.write_text(
        'schema_version: "4.1.0"\nchat:\n  providers:\n    codex:\n      source_id: windows\n'
        "raw_root: raw\ndata_root: data\nstate_root: state\n",
        encoding="utf-8",
    )
    before = snapshot(shared)
    config = config_for(tmp_path / "new")
    migrate_storage(config, from_config=source_config, dry_run=False)
    assert snapshot(shared) == before
    assert not (config.data_root / "provenance/blobs/unrelated/private").exists()
    rows = json.loads((config.data_root / "catalog/threads.json").read_text())["threads"]
    assert len(rows) == 1 and rows[0]["sourceId"] == "windows"
    assert validate_provenance(config.data_root)["ok"]


def test_migrated_legacy_locators_resolve_inside_copied_store(tmp_path: Path) -> None:
    from tkn_codex_chat_note.references import resolve_store_ref

    config, old_config, old = legacy_store(tmp_path)
    migrate_storage(config, from_config=old_config, dry_run=False)
    descriptor = json.loads((config.data_root / "store.json").read_text(encoding="utf-8"))
    raw = next((config.raw_root / "sessions").rglob("*.jsonl"))
    ref = "raw:/windows/" + raw.relative_to(config.raw_root).as_posix()
    assert resolve_store_ref(config.raw_root, ref, descriptor, kind="raw") == raw.resolve()
    note = next((config.data_root / "session-notes").rglob("*.md"))
    old_ref = "data:/thread-notes/" + note.relative_to(config.data_root / "session-notes").as_posix()
    assert resolve_store_ref(config.data_root, old_ref, descriptor) == note.resolve()
    with pytest.raises(PipelineError, match="another source"):
        resolve_store_ref(config.raw_root, ref.replace("windows", "other"), descriptor, kind="raw")
    with pytest.raises(PipelineError, match="invalid"):
        resolve_store_ref(config.data_root, "data:/thread-notes/../escape", descriptor)


@pytest.mark.parametrize("kind", ["descriptor", "manifest", "source-config"])
def test_migration_rejects_invalid_identity_or_source_version_before_writes(tmp_path: Path, kind: str) -> None:
    config, old_config, old = legacy_store(tmp_path)
    if kind == "source-config":
        old_config.write_text('schema_version: "4.2.0"\n', encoding="utf-8")
    elif kind == "manifest":
        path = old / "raw/windows/manifest.jsonl"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["captureRef"] = "raw:/foreign/elsewhere.jsonl"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    else:
        run_pipeline(config, mode="clone", summarizer=FakeSummarizer())
        path = config.data_root / "store.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["sourceId"] = "foreign"
        path.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError):
        migrate_storage(config, from_config=old_config, dry_run=False)
    assert snapshot(tmp_path) == before


def test_storage_roots_cannot_overlap_a_disabled_source(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    config.sources["disabled"] = config.source_settings.model_copy(
        update={"enabled": False, "data_root": config.raw_root}
    )
    with pytest.raises(PipelineError, match="overlap"):
        run_pipeline(config, mode="clone", dry_run=True)


@pytest.mark.parametrize("setting", ["chat: null", "raw_root: 123", "data_root: {}"])
def test_malformed_migration_source_config_is_a_visible_read_only_error(tmp_path: Path, setting: str) -> None:
    config, source_config, source = legacy_store(tmp_path)
    source_config.write_text('schema_version: "4.1.0"\n' + setting + "\n", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(PipelineError, match="invalid migration source configuration"):
        migrate_storage(config, from_config=source_config, dry_run=False)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("schema", ["5.0.0", "6.0.0"])
def test_legacy_default_roots(schema: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from tempfile import TemporaryDirectory

    import yaml

    from tkn_codex_chat_note.config import config_document
    from tkn_codex_chat_note.storage_migration import _source_store

    # Keep the synthetic legacy layout below the Windows path length limit.
    with TemporaryDirectory(prefix="tkn-") as temporary:
        tmp_path = Path(temporary)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        legacy_root = Path.home() / ".tkn/genai_chat_note_pipeline"
        old = AppConfig.model_validate(
            {
                "sources": {
                    "windows": {
                        "source_root": tmp_path / "input",
                        **{kind + "_root": legacy_root / kind / "codex/windows" for kind in ("raw", "data", "state")},
                    }
                },
                "cache_root": Path.home() / ".cache/genai_chat_note_pipeline",
                "idle_minutes": 0,
            }
        )
        write_chat(old.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path / "project")
        run_pipeline(old, mode="clone", summarizer=FakeSummarizer())
        document = config_document(old)
        codex = document.pop("sources")["windows"]
        for kind in ("raw", "data", "state"):
            codex.pop(kind + "_root")
        document.pop("cache_root")
        if schema == "5.0.0":
            codex["source_id"] = "windows"
            codex["home"] = codex.pop("source_root")
        else:
            codex = {"sources": {"windows": codex}}
        document["chat"] = {"providers": {"codex": codex}}
        document["schema_version"] = schema
        path = tmp_path / "legacy.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        before = snapshot(legacy_root)
        source = _source_store(path, "windows")
        assert (source.raw, source.data, source.state) == (old.raw_root, old.data_root, old.state_root)
        new = config_for(tmp_path / "new")
        migrate_storage(new, from_config=path, dry_run=False)
        assert snapshot(legacy_root) == before
        summary = FakeSummarizer()
        assert run_pipeline(new, mode="pull", summarizer=summary)["complete"]
        assert not summary.calls
        assert validate_provenance(new.data_root)["ok"]
