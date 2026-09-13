"""Discover every local conversation, preserve evidence, and observe membership."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .app_state import CodexAppState, load_codex_app_state
from .chat_logs import (
    ThreadSource,
    fingerprint_events,
    has_clean_user_message,
    is_approval_review,
    is_known_internal_thread,
    path_is_within,
    read_thread_source,
)
from .config import AppConfig
from .histories import combine_histories
from .provenance import ProvenanceStore, immutable_bytes, json_bytes
from .raw_capture import RawCaptureError, RawSourceInput, _stable_source_bytes, ingest_raw_sources
from .session_notes import (
    Candidate,
    PipelineError,
    Project,
    atomic_write_json,
    now_iso,
    now_local,
    path_variants,
    source_event_time,
    source_timestamp,
)
from .storage import read_json

CATALOG_SCHEMA_VERSION = "1.0.0"
EVENT_SCHEMA_VERSION = "1.0.0"
PARSER_VERSION = "2"


def thread_key(thread_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, "codex-thread:" + thread_id))


@dataclass
class Discovery:
    entries: list[dict[str, Any]]
    candidates: dict[str, Candidate]
    evidence: dict[str, list[dict[str, Any]]]
    raw_report: dict[str, Any]
    projects: dict[str, dict[str, Any]]
    warnings: list[str]
    failures: list[dict[str, Any]]
    metadata_entity: dict[str, Any] | None


def observe_app_state(
    config: AppConfig,
    provenance: ProvenanceStore,
) -> tuple[CodexAppState | None, dict[str, Any] | None, list[str]]:
    if not config.app_state_path.is_file():
        return None, None, ["Codex app metadata is unavailable; conversations are still ingested"]
    try:
        content = _stable_source_bytes(config.app_state_path)
        digest = sha256(content).hexdigest()
        relative = f"metadata/{digest}.json"
        capture = config.raw_root / relative
        if not provenance.dry_run:
            immutable_bytes(capture, content)
        entity = provenance.entity(
            content,
            identity=f"codex-app-state:{config.source_id}:{digest}",
            ref=f"raw:/{config.source_provider}/{config.source_id}/" + relative,
            kind="sourceMetadata",
        )
        try:
            state = load_codex_app_state(capture if capture.is_file() else config.app_state_path)
        except PipelineError as exc:
            return None, entity, [f"Unsupported Codex app metadata; membership is unknown: {exc}"]
        return state, entity, []
    except (OSError, RawCaptureError) as exc:
        return None, None, [f"Cannot read Codex app metadata; membership is unknown: {exc}"]


def membership(source: ThreadSource, state: CodexAppState | None) -> dict[str, Any]:
    log = source.thread_log
    assert log is not None
    result: dict[str, Any] = {
        "status": "unmatched",
        "sourceProjectId": None,
        "projectKind": None,
        "candidateProjectIds": [],
    }
    if state is None:
        return {**result, "status": "state-unavailable"}
    if log.id in state.projectless_thread_ids:
        return {**result, "status": "projectless"}
    assignment = state.assignments.get(log.id)
    if assignment:
        return {
            **result,
            "status": "explicit",
            "sourceProjectId": assignment.project_id,
            "projectKind": assignment.project_kind,
        }
    matches = [
        project.id
        for project in state.projects
        if any(
            path_is_within(cwd, root_variant)
            for event in source.events
            if event.cwd
            for root in project.root_paths
            for cwd in path_variants(event.cwd)
            for root_variant in path_variants(root)
        )
    ]
    if len(matches) == 1:
        return {**result, "status": "inferred", "projectKind": "local", "sourceProjectId": matches[0]}
    if matches:
        return {**result, "status": "ambiguous", "candidateProjectIds": sorted(matches)}
    return result


def _diagnostics(path: Path) -> dict[str, Any]:
    invalid: list[int] = []
    unknown: set[str] = set()
    metadata_ids: set[str] = set()
    # JSONL uses physical newlines, not Unicode string separators.
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                invalid.append(number)
                continue
        except ValueError:
            invalid.append(number)
            continue
        kind = str(value.get("type") or "")
        legacy_meta = not kind and value.get("id") and "instructions" in value
        if kind not in {
            "session_meta", "turn_context", "response_item", "event_msg", "compacted",
            "world_state", "token_usage_record", "message", "reasoning", "function_call", "function_call_output",
        } and not legacy_meta and not (not kind and value.get("record_type")):
            unknown.add(kind)
        if kind == "session_meta" and isinstance(value.get("payload"), dict):
            identity = value["payload"].get("id") or value["payload"].get("session_id")
            if identity:
                metadata_ids.add(str(identity))
        elif legacy_meta:
            metadata_ids.add(str(value["id"]))
    return {"invalidLines": invalid, "unknownRecordTypes": sorted(unknown), "metadataThreadIds": sorted(metadata_ids)}


def capture_sources(
    config: AppConfig, *, dry_run: bool, captured_at: str
) -> tuple[list[RawSourceInput], dict[str, Any]]:
    roots = [("sessions/", config.sessions_root)]
    if config.include_archived:
        roots.append(("archived_sessions/", config.codex_home / "archived_sessions"))
    inputs, raw_report = ingest_raw_sources(
        config.sessions_root,
        config.raw_root,
        config.source_id,
        dry_run=dry_run,
        captured_at=captured_at,
        scan_roots=roots,
    )
    if not any(root.is_dir() for _prefix, root in roots) and not inputs:
        raw_report["failed"].append(
            {"reason": "source-unavailable", "error": "no source directories or saved captures"}
        )
    return inputs, raw_report


def discover(config: AppConfig, provenance: ProvenanceStore, *, run_id: str) -> Discovery:
    started = now_iso()
    previous = {
        entry["threadKey"]: entry
        for entry in read_json(config.data_root / "catalog" / "threads.json").get("threads", [])
        if entry.get("sourceProvider") == config.source_provider and entry.get("sourceId") == config.source_id
    }
    inputs, raw_report = capture_sources(config, dry_run=provenance.dry_run, captured_at=started)
    app_state, metadata_entity, warnings = observe_app_state(config, provenance)
    projects: dict[str, dict[str, Any]] = {}
    if app_state:
        projects = {
            project.id: {"id": project.id, "title": project.name, "roots": [str(root) for root in project.root_paths]}
            for project in app_state.projects
        }
    failures: list[dict[str, Any]] = [{"stage": "raw", **failure} for failure in raw_report["failed"]]
    groups: dict[str, list[tuple[RawSourceInput, ThreadSource]]] = {}
    for item in inputs:
        try:
            source = read_thread_source(item.source_path)
            if source.thread_log is None:
                failures.append(
                    {"stage": "normalize", "sourceRef": item.source_ref, "reason": "missing-thread-metadata"}
                )
                continue
            groups.setdefault(source.thread_log.id, []).append((item, source))
        except (OSError, ValueError) as exc:
            failures.append({"stage": "normalize", "sourceRef": item.source_ref, "error": str(exc)})
    entries: list[dict[str, Any]] = []
    candidates: dict[str, Candidate] = {}
    evidence: dict[str, list[dict[str, Any]]] = {}
    for thread_id, versions in sorted(groups.items()):
        key = thread_key(thread_id)
        entry: dict[str, Any] = {
            "threadKey": key,
            "threadId": thread_id,
            "sourceProvider": "codex",
            "sourceId": config.source_id,
            "sourceRefs": sorted({item.source_ref for item, _source in versions}),
            "captureRefs": sorted({item.capture_ref for item, _source in versions}),
            "observedAt": started,
            "status": "pending",
            "reason": None,
        }
        entries.append(entry)
        combined = combine_histories(versions)
        item, source = combined.captures[0], combined.source
        log = source.thread_log
        assert log is not None
        observed = membership(source, app_state)
        history = list(previous.get(key, {}).get("membershipHistory", []))
        if not history or history[-1]["membership"] != observed:
            history.append(
                {
                    "observedAt": started,
                    "membership": observed,
                    "metadataRef": metadata_entity["ref"] if metadata_entity else None,
                }
            )
        entry["membershipHistory"] = history
        entry.update(
            membership=observed,
            sourceCaptureRef=item.capture_ref,
            sourceCaptureSha256=item.capture_sha256,
            lastEventAt=source.last_event_at,
            startedAt=log.timestamp,
            title=(log.user_messages[0].text[:100] if log.user_messages else "Codex conversation"),
        )
        source_diagnostics = [{"sourceRef": raw.source_ref, **_diagnostics(raw.source_path)} for raw, _ in versions]
        diagnostics = {
            "invalidLines": sorted({line for value in source_diagnostics for line in value["invalidLines"]}),
            "unknownRecordTypes": sorted({
                kind for value in source_diagnostics for kind in value["unknownRecordTypes"]
            }),
            "metadataThreadIds": sorted({id for value in source_diagnostics for id in value["metadataThreadIds"]}),
        }
        entry["diagnostics"] = diagnostics
        branch_fields = {
            "historyBranches": list(combined.branches), "sourceSetSha256": combined.source_set_sha256,
        } if combined.branches else {}
        entry.update(branch_fields)
        canonical = {
            "schemaVersion": EVENT_SCHEMA_VERSION,
            "parserVersion": PARSER_VERSION,
            "threadKey": key,
            "threadId": thread_id,
            "sourceProvider": "codex",
            "sourceCaptureRef": item.capture_ref,
            "sourceCaptureSha256": item.capture_sha256,
            "startedAt": log.timestamp,
            "lastEventAt": source.last_event_at,
            "diagnostics": diagnostics,
            "sourceDiagnostics": source_diagnostics,
            **branch_fields,
            "events": [
                {
                    "kind": event.kind,
                    "actor": event.actor,
                    "name": event.name,
                    "text": event.text,
                    "timestamp": event.timestamp,
                    "turnId": event.turn_id,
                    "cwd": event.cwd,
                    "id": f"{key}:{item.capture_sha256}:{event.id}",
                    "localId": event.id,
                    "rawRef": event.raw_ref or f"{item.capture_ref}#{event.id}",
                    **({"branchId": event.branch_id} if event.branch_id else {}),
                }
                for event in source.events
            ],
        }
        encoded = json_bytes(canonical)
        canonical_hash = sha256(encoded).hexdigest()
        canonical_path = config.source_data_root / "source-aligned" / key / f"{canonical_hash}.json"
        if not provenance.dry_run:
            immutable_bytes(canonical_path, encoded)
        raw_entities = [provenance.entity(
            raw.source_path.read_bytes(),
            identity=f"codex-raw:{key}:{raw.capture_sha256}",
            ref=raw.capture_ref,
            kind="raw",
            media_type="application/x-ndjson",
        ) for raw, _ in versions]
        normalized_entity = provenance.entity(
            encoded,
            identity=f"codex-events:{key}",
            ref="data:/" + canonical_path.relative_to(config.data_root).as_posix(),
            kind="canonicalEvents",
        )
        evidence[key] = [*raw_entities, normalized_entity]
        entry.update(canonicalRef=normalized_entity["ref"], canonicalSha256=canonical_hash)
        # Deterministic normalization needs no inference and retains its own input lineage.
        activity_marker = config.source_state_root / "normalization" / f"{canonical_hash}.json"
        if not provenance.dry_run and not provenance.has_activity(read_json(activity_marker).get("activityId")):
            activity = provenance.activity(
                run_id=run_id,
                stage="normalize",
                subject=key,
                started_at=started,
                used=raw_entities,
                generated=[normalized_entity],
                agent={"software": "tkn-codex-chat-note-pipeline", "parserVersion": PARSER_VERSION},
            )
            atomic_write_json(activity_marker, {"activityId": activity})
        if diagnostics["invalidLines"] or any(
            value["metadataThreadIds"] != [thread_id] for value in source_diagnostics
        ):
            entry.update(status="failed", reason="invalid-jsonl")
            failures.append({"stage": "normalize", "threadId": thread_id, "reason": entry["reason"]})
            continue
        if diagnostics["unknownRecordTypes"]:
            warnings.append(
                f"Thread {thread_id}: unknown record types retained in Raw: {diagnostics['unknownRecordTypes']}"
            )
        if any(is_approval_review(s.thread_log) or is_known_internal_thread(s.thread_log)
               for _, s in versions if s.thread_log):
            entry.update(status="excluded", reason="approval-or-internal")
            continue
        if not has_clean_user_message(log):
            entry.update(status="excluded", reason="without-user-message")
            continue
        event_time = source_event_time(source.last_event_at)
        if event_time is None or source_event_time(log.timestamp) is None:
            entry.update(status="failed", reason="without-event-time")
            failures.append({"stage": "normalize", "threadId": thread_id, "reason": entry["reason"]})
            continue
        if event_time > now_local() - timedelta(minutes=config.idle_minutes):
            entry.update(status="deferred", reason="active-conversation")
        project = Project(
            project_id=key,
            title="Conversation (preserve every independent work item)",
            current_root=config.source_data_root / "threads" / key,
            context_path=config.data_root,
            note_directory=(
                config.source_data_root / "session-notes" / source_timestamp(log.timestamp).strftime("%Y/%m")
            ),
            assigned_thread_ids=frozenset({thread_id}),
            source_project_id=str(observed["sourceProjectId"] or ""),
            state_directory=config.source_state_root / "threads" / key,
        )
        qualified_ref = f"codex/{thread_id}"
        candidates[key] = Candidate(
            project=project,
            thread_id=thread_id,
            started_at=log.timestamp,
            source_path=item.source_path,
            source_ref=qualified_ref,
            source_relative_ref=item.source_ref,
            fingerprint=fingerprint_events(thread_id, source.events, qualified_ref),
            events=source.events,
            source_last_event_at=source.last_event_at,
            source_capture_ref=item.capture_ref,
            source_capture_sha256=item.capture_sha256,
            source_captures=combined.captures if combined.branches else (),
            history_branches=combined.branches,
            source_set_sha256=combined.source_set_sha256,
        )
    return Discovery(entries, candidates, evidence, raw_report, projects, warnings, failures, metadata_entity)
