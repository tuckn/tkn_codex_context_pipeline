from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

import tkn_codex_context.raw_capture as raw_capture
from tkn_codex_context.raw_capture import RawCaptureError, ingest_raw_sources


def write_source(path: Path, text: str = '{"type":"event_msg","payload":{}}\n') -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = text.encode("utf-8")
    path.write_bytes(content)
    return content


def ingest(sessions: Path, raw: Path, *, dry_run: bool = False):
    return ingest_raw_sources(
        sessions,
        raw,
        "windows",
        dry_run=dry_run,
        captured_at="2026-09-04T09:00:00+09:00",
    )


def test_ingest_copies_exact_bytes_without_mutating_source_and_is_idempotent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "2026" / "09" / "chat.jsonl"
    original = write_source(source)

    inputs, report = ingest(sessions, raw)

    assert source.read_bytes() == original
    assert report["capturedCount"] == 1
    assert report["blobCreatedCount"] == 1
    assert len(inputs) == 1
    digest = sha256(original).hexdigest()
    capture = raw / "windows" / "sha256" / digest[:2] / f"{digest}.jsonl"
    assert capture.read_bytes() == original
    assert inputs[0].source_path == capture
    assert inputs[0].capture_sha256 == digest
    assert inputs[0].capture_ref == f"raw:/windows/sha256/{digest[:2]}/{digest}.jsonl"

    second_inputs, second_report = ingest(sessions, raw)

    assert second_report["capturedCount"] == 0
    assert second_report["unchangedCount"] == 1
    assert second_report["blobCreatedCount"] == 0
    assert second_inputs == inputs
    records = [
        json.loads(line)
        for line in (raw / "windows" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["sourceRef"] == "2026/09/chat.jsonl"


def test_changed_source_creates_new_capture_and_old_capture_remains(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "chat.jsonl"
    first = write_source(source, '{"value":1}\n')
    first_inputs, _report = ingest(sessions, raw)

    second = write_source(source, '{"value":2}\n')
    second_inputs, report = ingest(sessions, raw)

    assert report["capturedCount"] == 1
    assert first_inputs[0].source_path.read_bytes() == first
    assert second_inputs[0].source_path.read_bytes() == second
    assert first_inputs[0].capture_sha256 != second_inputs[0].capture_sha256
    manifest = raw / "windows" / "manifest.jsonl"
    assert len(manifest.read_text(encoding="utf-8").splitlines()) == 2


def test_reverted_source_appends_observation_and_becomes_latest_bronze_only(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "chat.jsonl"
    first = write_source(source, '{"value":1}\n')
    first_inputs, _report = ingest(sessions, raw)
    write_source(source, '{"value":2}\n')
    ingest(sessions, raw)
    write_source(source, first.decode("utf-8"))

    reverted_inputs, report = ingest(sessions, raw)

    assert report["capturedCount"] == 1
    assert reverted_inputs[0].capture_sha256 == first_inputs[0].capture_sha256
    source.unlink()
    bronze_only, _report = ingest(sessions, raw)
    assert bronze_only[0].capture_sha256 == first_inputs[0].capture_sha256
    manifest = raw / "windows" / "manifest.jsonl"
    assert len(manifest.read_text(encoding="utf-8").splitlines()) == 3


def test_removed_source_remains_available_as_bronze_only(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "chat.jsonl"
    write_source(source)
    first_inputs, _report = ingest(sessions, raw)
    source.unlink()

    inputs, report = ingest(sessions, raw)

    assert inputs == [raw_capture.RawSourceInput(**{**first_inputs[0].__dict__, "original_present": False})]
    assert report["bronzeOnlyCount"] == 1
    assert report["discoveredCount"] == 0


def test_dry_run_plans_capture_without_creating_raw_storage(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "chat.jsonl"
    original = write_source(source)

    inputs, report = ingest(sessions, raw, dry_run=True)

    assert report["plannedCaptureCount"] == 1
    assert report["capturedCount"] == 0
    assert inputs[0].source_path == source
    assert source.read_bytes() == original
    assert not raw.exists()


def test_nonempty_unowned_raw_root_is_rejected(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    write_source(sessions / "chat.jsonl")
    write_source(raw / "foreign.txt")

    with pytest.raises(RawCaptureError, match="non-empty raw_root"):
        ingest(sessions, raw)


def test_source_and_raw_roots_must_not_overlap(tmp_path: Path) -> None:
    sessions = tmp_path / "storage" / "sessions"
    write_source(sessions / "chat.jsonl")

    with pytest.raises(RawCaptureError, match="must not overlap"):
        ingest(sessions, sessions / "raw")

    with pytest.raises(RawCaptureError, match="must not overlap"):
        ingest(sessions, tmp_path / "storage")


def test_failed_current_capture_does_not_fall_back_to_stale_bronze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    raw = tmp_path / "raw"
    source = sessions / "chat.jsonl"
    write_source(source, '{"value":1}\n')
    ingest(sessions, raw)
    write_source(source, '{"value":2}\n')

    def fail_capture(_path: Path) -> bytes:
        raise RawCaptureError("injected capture failure")

    monkeypatch.setattr(raw_capture, "_stable_source_bytes", fail_capture)
    inputs, report = ingest(sessions, raw)

    assert inputs == []
    assert report["availableCaptureCount"] == 0
    assert report["failed"][0]["sourceRef"] == "chat.jsonl"
