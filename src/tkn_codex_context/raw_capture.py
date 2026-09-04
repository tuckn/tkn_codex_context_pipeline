"""Immutable content-addressed capture of source Codex JSONL logs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .chat_logs import read_thread_source, source_ref

RAW_MANIFEST_SCHEMA_VERSION = 1
RAW_OWNERSHIP_MARKER = ".tkn-codex-context-root.json"
RAW_OWNER_APPLICATION_ID = "tkn-codex-context-pipeline"
RAW_OWNER_SCHEMA_VERSION = 1
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RawCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class RawSourceInput:
    """One latest source version and the file used for deterministic processing."""

    source_path: Path
    source_ref: str
    capture_ref: str
    capture_sha256: str
    byte_count: int
    thread_id: str
    last_event_at: str
    original_present: bool


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp-{uuid4().hex[:12]}"
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _ownership_document() -> dict[str, str | int]:
    return {
        "schemaVersion": RAW_OWNER_SCHEMA_VERSION,
        "applicationId": RAW_OWNER_APPLICATION_ID,
        "rootKind": "raw",
    }


def _ensure_raw_root(raw_root: Path, *, dry_run: bool) -> None:
    marker = raw_root / RAW_OWNERSHIP_MARKER
    if raw_root.is_symlink():
        raise RawCaptureError(f"raw_root must not be a symbolic link: {raw_root}")
    if raw_root.exists() and not raw_root.is_dir():
        raise RawCaptureError(f"raw_root is not a directory: {raw_root}")
    if not raw_root.exists():
        if not dry_run:
            raw_root.mkdir(parents=True)
            _atomic_write_json(marker, _ownership_document())
        return
    if marker.is_symlink():
        raise RawCaptureError(f"raw_root ownership marker must not be a symbolic link: {marker}")
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RawCaptureError(f"invalid raw_root ownership marker: {marker}: {exc}") from exc
        expected = _ownership_document()
        if not isinstance(value, dict) or any(
            value.get(key) != expected_value
            for key, expected_value in expected.items()
        ):
            raise RawCaptureError(f"raw_root ownership marker does not match this application: {marker}")
        return
    try:
        empty = next(raw_root.iterdir(), None) is None
    except OSError as exc:
        raise RawCaptureError(f"cannot inspect raw_root: {raw_root}: {exc}") from exc
    if not empty:
        raise RawCaptureError(
            f"refusing to write to non-empty raw_root without a valid ownership marker: {raw_root}"
        )
    if not dry_run:
        _atomic_write_json(marker, _ownership_document())


def _source_root(raw_root: Path, source_id: str) -> Path:
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise RawCaptureError(f"source_id is not safe for raw storage: {source_id!r}")
    return raw_root / source_id


def _capture_path(source_root: Path, digest: str) -> Path:
    return source_root / "sha256" / digest[:2] / f"{digest}.jsonl"


def _capture_ref(source_id: str, digest: str) -> str:
    return f"raw:/{source_id}/sha256/{digest[:2]}/{digest}.jsonl"


def _read_manifest(path: Path, source_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise RawCaptureError(f"raw manifest must be a regular file: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RawCaptureError(f"invalid raw manifest JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise RawCaptureError(f"raw manifest record must be an object: {path}:{line_number}")
            source_reference = str(value.get("sourceRef") or "")
            digest = str(value.get("sha256") or "")
            if (
                value.get("schemaVersion") != RAW_MANIFEST_SCHEMA_VERSION
                or value.get("sourceId") != source_id
                or not source_reference
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(value.get("byteCount"), int)
                or int(value["byteCount"]) < 0
                or value.get("captureRef") != _capture_ref(source_id, digest)
            ):
                raise RawCaptureError(f"invalid raw manifest record: {path}:{line_number}")
            records.append(value)
    return records


def _manifest_text(records: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")


def _stable_source_bytes(path: Path) -> bytes:
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(content) != after.st_size
    ):
        raise RawCaptureError(f"source changed during raw capture: {path}")
    return content


def _record_for_source(
    *,
    source_id: str,
    source_reference: str,
    digest: str,
    byte_count: int,
    captured_at: str,
    parse_path: Path,
) -> dict[str, Any]:
    parsed = read_thread_source(parse_path)
    return {
        "schemaVersion": RAW_MANIFEST_SCHEMA_VERSION,
        "sourceId": source_id,
        "sourceRef": source_reference,
        "captureRef": _capture_ref(source_id, digest),
        "sha256": digest,
        "byteCount": byte_count,
        "capturedAt": captured_at,
        "threadId": parsed.thread_log.id if parsed.thread_log else None,
        "lastEventAt": parsed.last_event_at or None,
    }


def ingest_raw_sources(
    sessions_root: Path,
    raw_root: Path,
    source_id: str,
    *,
    dry_run: bool,
    captured_at: str,
) -> tuple[list[RawSourceInput], dict[str, Any]]:
    """Capture current logs and return the latest owned version of every sourceRef."""

    sessions = sessions_root.expanduser().absolute()
    raw = raw_root.expanduser().absolute()
    if sessions.exists() and not sessions.is_dir():
        raise RawCaptureError(f"sessions root is not a directory: {sessions}")
    sessions_resolved = sessions.resolve(strict=False)
    raw_resolved = raw.resolve(strict=False)
    if (
        raw_resolved == sessions_resolved
        or raw_resolved.is_relative_to(sessions_resolved)
        or sessions_resolved.is_relative_to(raw_resolved)
    ):
        raise RawCaptureError(f"raw_root and source sessions root must not overlap: {raw}")
    _ensure_raw_root(raw, dry_run=dry_run)
    owned_source_root = _source_root(raw, source_id)
    manifest_path = owned_source_root / "manifest.jsonl"
    records = _read_manifest(manifest_path, source_id)
    latest: dict[str, dict[str, Any]] = {str(record["sourceRef"]): record for record in records}
    processing_paths: dict[str, Path] = {}
    current_refs: set[str] = set()
    blocked_refs: set[str] = set()
    statuses: dict[str, str] = {}
    failed: list[dict[str, str]] = []
    new_records: list[dict[str, Any]] = []
    blob_created_count = 0

    for source_path in sorted(sessions.rglob("*.jsonl")):
        relative = source_ref(source_path, sessions)
        current_refs.add(relative)
        try:
            content = _stable_source_bytes(source_path)
            digest = sha256(content).hexdigest()
            destination = _capture_path(owned_source_root, digest)
            if destination.is_symlink():
                raise RawCaptureError(f"raw capture must not be a symbolic link: {destination}")
            if destination.is_file():
                actual = sha256(destination.read_bytes()).hexdigest()
                if actual != digest:
                    raise RawCaptureError(f"raw capture hash mismatch: {destination}")
            elif not dry_run:
                _atomic_write_bytes(destination, content)
                if sha256(destination.read_bytes()).hexdigest() != digest:
                    raise RawCaptureError(f"raw capture verification failed: {destination}")
                blob_created_count += 1
            parse_path = destination if destination.is_file() else source_path
            prior = latest.get(relative)
            if prior is None or prior["sha256"] != digest:
                record = _record_for_source(
                    source_id=source_id,
                    source_reference=relative,
                    digest=digest,
                    byte_count=len(content),
                    captured_at=captured_at,
                    parse_path=parse_path,
                )
                latest[relative] = record
                if not dry_run:
                    new_records.append(record)
                statuses[relative] = "planned" if dry_run else "captured"
            else:
                latest[relative] = prior
                statuses[relative] = "unchanged"
            processing_paths[relative] = parse_path
        except (OSError, RawCaptureError) as exc:
            failed.append({"sourceRef": relative, "sourcePath": str(source_path), "error": str(exc)})
            statuses[relative] = "deferred"
            blocked_refs.add(relative)

    if new_records:
        records.extend(new_records)
        _atomic_write_bytes(manifest_path, _manifest_text(records))

    inputs: list[RawSourceInput] = []
    source_details: list[dict[str, Any]] = []
    for relative, record in sorted(latest.items()):
        if relative in blocked_refs:
            continue
        digest = str(record["sha256"])
        capture_path = _capture_path(owned_source_root, digest)
        process_path = processing_paths.get(relative, capture_path)
        if not process_path.is_file():
            failed.append(
                {
                    "sourceRef": relative,
                    "sourcePath": str(process_path),
                    "error": "registered raw capture is missing",
                }
            )
            continue
        if process_path == capture_path and sha256(process_path.read_bytes()).hexdigest() != digest:
            failed.append(
                {
                    "sourceRef": relative,
                    "sourcePath": str(process_path),
                    "error": "registered raw capture hash mismatch",
                }
            )
            continue
        status = statuses.get(relative, "bronze-only")
        item = RawSourceInput(
            source_path=process_path,
            source_ref=relative,
            capture_ref=str(record.get("captureRef") or _capture_ref(source_id, digest)),
            capture_sha256=digest,
            byte_count=int(record["byteCount"]),
            thread_id=str(record.get("threadId") or ""),
            last_event_at=str(record.get("lastEventAt") or ""),
            original_present=relative in current_refs,
        )
        inputs.append(item)
        source_details.append(
            {
                "sourceRef": relative,
                "captureRef": item.capture_ref,
                "captureSha256": digest,
                "byteCount": item.byte_count,
                "threadId": item.thread_id or None,
                "lastEventAt": item.last_event_at or None,
                "originalPresent": item.original_present,
                "status": status,
            }
        )

    report = {
        "schemaVersion": 1,
        "mode": "raw-ingest",
        "dryRun": dry_run,
        "sourceId": source_id,
        "sessionsRoot": str(sessions),
        "rawRoot": str(raw),
        "manifestPath": str(manifest_path),
        "discoveredCount": len(current_refs),
        "availableCaptureCount": len(inputs),
        "capturedCount": sum(value == "captured" for value in statuses.values()),
        "plannedCaptureCount": sum(value == "planned" for value in statuses.values()),
        "unchangedCount": sum(value == "unchanged" for value in statuses.values()),
        "bronzeOnlyCount": sum(not item.original_present for item in inputs),
        "blobCreatedCount": blob_created_count,
        "failed": failed,
        "sources": source_details,
    }
    return inputs, report
