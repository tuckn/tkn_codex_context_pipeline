"""Resumable end-to-end clone/pull orchestration over immutable captured evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .catalog import CATALOG_SCHEMA_VERSION, Discovery, capture_sources, discover
from .config import AppConfig
from .frontmatter import parse_simple_frontmatter
from .provenance import ProvenanceStore, json_bytes
from .raw_capture import RawCaptureError
from .session_notes import (
    Candidate,
    PipelineConfig,
    PipelineError,
    ProviderSummarizer,
    Summarizer,
    atomic_write_json,
    current_note_matches_generation,
    find_note_matches,
    generation_fingerprint,
    now_iso,
    now_local,
    validate_session_note,
    write_candidate_note,
)
from .storage import legacy_storage_pending, pipeline_storage, read_json

LEDGER_SCHEMA_VERSION = 1
Progress = Callable[[dict[str, Any]], None]


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _signature(value: Any) -> str:
    return sha256(json_bytes(value)).hexdigest()


def _load_ledger(config: AppConfig) -> dict[str, Any]:
    value = read_json(
        config.source_state_root / "ledger.json",
        {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "threads": {},
        },
    )
    if value.get("schemaVersion") != LEDGER_SCHEMA_VERSION:
        raise PipelineError("unsupported processing ledger schema")
    if not isinstance(value.get("threads"), dict):
        raise PipelineError("invalid processing ledger")
    return value


def _save_ledger(config: AppConfig, ledger: dict[str, Any], dry_run: bool) -> None:
    if not dry_run:
        atomic_write_json(config.source_state_root / "ledger.json", ledger)


def _agent(config: PipelineConfig, stage: str) -> dict[str, Any]:
    profile = config.summary_profile
    return {
        "software": "tkn-genai-chat-note-pipeline",
        "version": "0.14.0",
        "provider": config.provider,
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
        "profile": profile.name,
        "profileSha256": profile.sha256,
        "promptVersion": profile.prompt.version,
        "promptSha256": profile.prompt.sha256,
        "schemaSha256": profile.schema.sha256,
        "templateSha256": profile.template.sha256,
    }


def _existing_note(candidate: Candidate) -> Path | None:
    matches = find_note_matches(candidate.project, candidate.thread_id)
    if len(matches) > 1:
        raise PipelineError(f"multiple notes for thread {candidate.thread_id}; resolve the duplicate explicitly")
    return matches[0] if matches else None


def _notes(
    config: AppConfig,
    pipeline_config: PipelineConfig,
    discovery: Discovery,
    ledger: dict[str, Any],
    provenance: ProvenanceStore,
    *,
    run_id: str,
    deadline: datetime,
    generate: bool,
    force: bool,
    allow_edited: bool,
    thread_id: str | None,
    limit: int | None,
    summarizer: Summarizer | None,
    progress: Progress | None,
) -> int:
    attempted = 0
    if thread_id and thread_id not in {entry["threadId"] for entry in discovery.entries}:
        raise PipelineError(f"unknown thread ID: {thread_id}")
    for entry in discovery.entries:
        key = entry["threadKey"]
        candidate = discovery.candidates.get(key)
        if candidate is None:
            continue
        prior = ledger["threads"].get(key, {})
        candidate = replace(candidate, artifact_id=prior.get("noteId"))
        try:
            existing = _existing_note(candidate)
            if existing:
                entry.update(
                    noteRef="data:/" + existing.relative_to(config.data_root).as_posix(),
                    noteId=parse_simple_frontmatter(existing.read_text(encoding="utf-8-sig")).get("id"),
                )
            if entry["status"] == "deferred":
                continue
            selected = not thread_id or entry["threadId"] == thread_id
            fingerprint = generation_fingerprint(pipeline_config, candidate)
            unchanged = bool(
                existing
                and prior.get("generationFingerprint") == fingerprint
                and prior.get("noteHash") == _hash(existing)
                and provenance.has_activity(prior.get("activityId"))
                and current_note_matches_generation(candidate, pipeline_config)
            )
            if unchanged and not (force and selected):
                validate_session_note(existing)  # type: ignore[arg-type]
                entry.update(status="current", reason=None)
                continue
            if not generate or not selected:
                entry.update(status="pending", reason="session-note-needs-build")
                continue
            if existing:
                metadata = parse_simple_frontmatter(existing.read_text(encoding="utf-8-sig"))
                if metadata.get("reviewStatus") != "unreviewed":
                    entry.update(status="blocked", reason="reviewed-session-note")
                    continue
                internal_state = read_json(candidate.project.state_path)
                internal_thread = (
                    internal_state.get("sources", {})
                    .get(config.source_id, {})
                    .get("threads", {})
                    .get(
                        entry["threadId"],
                        {},
                    )
                )
                recoverable = (
                    prior.get("status") in {"running", "failed"}
                    and internal_thread.get("noteHash") == _hash(existing)
                    and internal_thread.get("generationFingerprint") == fingerprint
                )
                if prior.get("noteHash") != _hash(existing) and not allow_edited and not recoverable:
                    entry.update(status="blocked", reason="edited-session-note")
                    continue
            if now_local() >= deadline or limit is not None and attempted >= limit:
                entry.update(status="deferred", reason="runtime-deadline" if now_local() >= deadline else "limit")
                continue
            if provenance.dry_run:
                entry.update(status="planned", reason="session-note-needs-build")
                attempted += 1
                continue
            attempted += 1
            started = now_iso()
            ledger["threads"][key] = {**prior, "status": "running", "attemptedAt": started}
            _save_ledger(config, ledger, False)
            if summarizer is None:
                summarizer = ProviderSummarizer(pipeline_config)
            if hasattr(summarizer, "set_deadline"):
                summarizer.set_deadline(deadline + timedelta(minutes=9))
            if progress:
                progress(
                    {
                        "type": "thread-start",
                        "threadId": entry["threadId"],
                        "index": attempted,
                        "total": len(discovery.candidates),
                    }
                )
            note = write_candidate_note(
                candidate,
                pipeline_config,
                summarizer,
                work_cache_root=config.source_cache_root,
                force=force,
            )
            output = provenance.artifact(note)
            used = list(discovery.evidence[key])
            if discovery.metadata_entity:
                used.append(discovery.metadata_entity)
            if existing and prior.get("noteId"):
                # The previous output version remains available in the provenance store.
                prior_activity = read_json(provenance.root / "activities" / f"{prior.get('activityId')}.json")
                used.extend(prior_activity.get("generated", []))
            activity = provenance.activity(
                run_id=run_id,
                stage="session-note",
                subject=key,
                started_at=started,
                used=used,
                generated=[output],
                agent=_agent(pipeline_config, "session-note"),
            )
            ledger["threads"][key] = {
                "status": "current",
                "threadId": entry["threadId"],
                "generationFingerprint": fingerprint,
                "sourceCaptureSha256": candidate.source_capture_sha256,
                "noteRef": output["ref"],
                "noteHash": output["sha256"],
                "noteId": output["id"],
                "activityId": activity,
                "appliedAt": now_iso(),
            }
            entry.update(status="current", reason=None, noteRef=output["ref"], noteId=output["id"], generated=True)
            if progress:
                progress(
                    {
                        "type": "thread-complete",
                        "threadId": entry["threadId"],
                        "sessionNotePath": str(note),
                        "index": attempted,
                        "total": len(discovery.candidates),
                    }
                )
        except Exception as exc:
            entry.update(status="failed", reason="session-note-failed", error=str(exc))
            ledger["threads"][key] = {**prior, "status": "failed", "error": str(exc), "attemptedAt": now_iso()}
            if progress:
                progress(
                    {
                        "type": "thread-failed",
                        "threadId": entry["threadId"],
                        "error": str(exc),
                        "index": attempted,
                        "total": len(discovery.candidates),
                    }
                )
        finally:
            _save_ledger(config, ledger, provenance.dry_run)
    return attempted


def _run_source_pipeline(
    config: AppConfig,
    *,
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    allow_edited: bool = False,
    thread_id: str | None = None,
    limit: int | None = None,
    config_path: Path | None = None,
    summarizer: Summarizer | None = None,
    progress: Progress | None = None,
    deadline: datetime | None = None,
) -> dict[str, Any]:
    if mode not in {"clone", "pull", "raw", "session-notes"}:
        raise PipelineError(f"unknown pipeline mode: {mode}")
    if limit is not None and limit < 0:
        raise PipelineError("limit must be positive")
    run_id = str(uuid4())
    started = now_local()
    deadline = deadline or started + timedelta(minutes=config.runtime_minutes)
    report: dict[str, Any] = {
        "schemaVersion": 2,
        "sourceProvider": config.source_provider,
        "sourceId": config.source_id,
        "runId": run_id,
        "mode": mode,
        "dryRun": dry_run,
        "startedAt": started.isoformat(),
        "complete": False,
        "ok": False,
        "failed": [],
        "reportPath": None,
        "coverage": {
            "provider": "codex",
            "cloudHistoryFetched": False,
            "includesArchivedDirectory": config.include_archived,
            "internalThreads": "captured-and-normalized-but-not-summarized",
        },
    }
    with pipeline_storage(config, initialize=mode in {"clone", "raw"}, dry_run=dry_run, config_path=config_path):
        if not dry_run:
            atomic_write_json(config.source_state_root / "last-run.json", {**report, "status": "running"})
        if mode == "raw":
            _inputs, raw_report = capture_sources(config, dry_run=dry_run, captured_at=now_iso())
            report.update(
                rawIngest=raw_report,
                failed=raw_report["failed"],
                ok=not raw_report["failed"],
                threadCounts={},
                threads=[],
                generatedSessionNoteCount=0,
                attemptedSessionNoteCount=0,
                finishedAt=now_iso(),
            )
            if not dry_run:
                report["reportPath"] = str(config.reports_root / f"{run_id}.json")
                atomic_write_json(Path(report["reportPath"]), report)
                atomic_write_json(config.source_state_root / "last-run.json", report)
            return report
        ledger = _load_ledger(config)
        provenance = ProvenanceStore(config.data_root, dry_run=dry_run)
        discovery = discover(config, provenance, run_id=run_id)
        report.update(rawIngest=discovery.raw_report, warnings=discovery.warnings)
        report["failed"].extend(discovery.failures)
        pipeline_config = config.session_note_pipeline_config(allow_missing_watermark=True)
        report["attemptedSessionNoteCount"] = _notes(
            config,
            pipeline_config,
            discovery,
            ledger,
            provenance,
            run_id=run_id,
            deadline=deadline,
            generate=mode in {"clone", "pull", "session-notes"},
            force=force and mode in {"clone", "pull", "session-notes"},
            allow_edited=allow_edited,
            thread_id=thread_id,
            limit=limit,
            summarizer=summarizer,
            progress=progress,
        )
        report["threads"] = discovery.entries
        report["threadCounts"] = dict(Counter(entry["status"] for entry in discovery.entries))
        report["generatedSessionNoteCount"] = sum(bool(entry.get("generated")) for entry in discovery.entries)
        failures = [entry for entry in discovery.entries if entry["status"] in {"failed", "blocked"}]
        report["ok"] = not report["failed"] and not failures
        report["complete"] = bool(
            report["ok"]
            and mode in {"clone", "pull", "session-notes"}
            and not dry_run
            and all(entry["status"] in {"current", "excluded"} for entry in discovery.entries)
        )
        report["finishedAt"] = now_iso()
        if not dry_run:
            for entry in discovery.entries:
                if entry.get("noteRef"):
                    note_path = config.data_root / entry["noteRef"].removeprefix("data:/")
                    if note_path.is_file():
                        entry["noteSha256"] = _hash(note_path)
            atomic_write_json(
                config.data_root / "catalog" / "threads.json",
                {
                    "schemaVersion": CATALOG_SCHEMA_VERSION,
                    "asOf": report["finishedAt"],
                    "threads": [
                        *[
                            entry
                            for entry in read_json(config.data_root / "catalog" / "threads.json").get("threads", [])
                            if (entry.get("sourceProvider"), entry.get("sourceId"))
                            != (config.source_provider, config.source_id)
                        ],
                        *discovery.entries,
                    ],
                },
            )
            artifacts: list[dict[str, Any]] = []
            for entry in discovery.entries:
                if entry.get("noteRef"):
                    path = config.data_root / entry["noteRef"].removeprefix("data:/")
                    if path.is_file():
                        artifacts.append(
                            {
                                **provenance.artifact(path),
                                "status": entry["status"],
                                "threadKey": entry["threadKey"],
                                "sourceProvider": config.source_provider,
                                "sourceId": config.source_id,
                            }
                        )
            provenance.publish_index(
                artifacts,
                run_id=run_id,
                complete=report["complete"],
                source=(config.source_provider, config.source_id),
            )
            report_path = config.reports_root / f"{run_id}.json"
            report["reportPath"] = str(report_path)
            atomic_write_json(report_path, report)
            atomic_write_json(config.source_state_root / "last-run.json", report)
    return report


def _source_status(config: AppConfig) -> dict[str, Any]:
    last = read_json(config.source_state_root / "last-run.json")
    return {
        "initialized": (config.source_state_root / "pipeline.json").is_file(),
        "migrationRequired": legacy_storage_pending(config),
        "sourceProvider": config.source_provider,
        "sourceId": config.source_id,
        "storageRoot": str(config.source_state_root),
        "asOf": last.get("finishedAt"),
        "lastMode": last.get("mode"),
        "complete": last.get("complete", False),
        "threadCounts": last.get("threadCounts", {}),
        "reportPath": last.get("reportPath"),
        "liveSourceScan": False,
    }


def run_pipeline(
    config: AppConfig,
    *,
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    allow_edited: bool = False,
    thread_id: str | None = None,
    limit: int | None = None,
    config_path: Path | None = None,
    summarizer: Summarizer | None = None,
    progress: Progress | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Run enabled sources in map order with one invocation-wide generation budget."""
    if mode not in {"clone", "pull", "raw", "session-notes"}:
        raise PipelineError(f"unknown pipeline mode: {mode}")
    if limit is not None and limit <= 0:
        raise PipelineError("limit must be positive")
    sources = config.enabled_source_configs(source_id)
    if thread_id is not None and len(sources) != 1:
        raise PipelineError("--thread-id with multiple sources requires --source <source_id>")
    # Validate every destination before the first one creates files or calls AI.
    for source in sources:
        with pipeline_storage(source, initialize=mode in {"clone", "raw"}, dry_run=True, config_path=config_path):
            pass
    started = now_local()
    deadline = started + timedelta(minutes=config.runtime_minutes)
    remaining = limit
    reports: list[dict[str, Any]] = []
    for source in sources:
        identity = {"sourceProvider": source.source_provider, "sourceId": source.source_id}
        started_attempts = 0

        def source_progress(event: dict[str, Any], identity: dict[str, str] = identity) -> None:
            nonlocal started_attempts
            if event.get("type") == "thread-start":
                started_attempts += 1
            if progress is not None:
                progress({**event, **identity})

        source_progress({"type": "source-start"})
        try:
            report = _run_source_pipeline(
                source,
                mode=mode,
                dry_run=dry_run,
                force=force,
                allow_edited=allow_edited,
                thread_id=thread_id,
                limit=remaining,
                config_path=config_path,
                summarizer=summarizer,
                progress=source_progress,
                deadline=deadline,
            )
        except (PipelineError, RawCaptureError, OSError, ValueError) as exc:
            if len(sources) == 1:
                raise
            report = {
                "schemaVersion": 2,
                "runId": str(uuid4()),
                "mode": mode,
                "dryRun": dry_run,
                "startedAt": started.isoformat(),
                "finishedAt": now_iso(),
                **identity,
                "ok": False,
                "complete": False,
                "error": str(exc),
                "failed": [{**identity, "error": str(exc)}],
                "threadCounts": {},
                "threads": [],
                "generatedSessionNoteCount": 0,
                "attemptedSessionNoteCount": started_attempts,
                "reportPath": None,
            }
            if not dry_run and (source.state_root / "pipeline.json").is_file():
                try:
                    with pipeline_storage(source, initialize=False, dry_run=False, config_path=config_path):
                        report_path = source.reports_root / f"{report['runId']}.json"
                        report["reportPath"] = str(report_path)
                        atomic_write_json(report_path, report)
                        atomic_write_json(source.state_root / "last-run.json", report)
                except (PipelineError, OSError, ValueError) as save_error:
                    report["reportPath"] = None
                    report["warnings"] = [f"Could not persist source failure report: {save_error}"]
        reports.append(report)
        if remaining is not None:
            remaining = max(0, remaining - report["attemptedSessionNoteCount"])
    if len(reports) == 1:
        return reports[0]
    counts: Counter[str] = Counter()
    for report in reports:
        counts.update(report["threadCounts"])
    return {
        "schemaVersion": 3,
        "mode": mode,
        "dryRun": dry_run,
        "startedAt": started.isoformat(),
        "finishedAt": now_iso(),
        "ok": all(report["ok"] for report in reports),
        "complete": all(report["complete"] for report in reports),
        "sourceResults": reports,
        "threadCounts": dict(counts),
        "generatedSessionNoteCount": sum(report["generatedSessionNoteCount"] for report in reports),
        "attemptedSessionNoteCount": sum(report["attemptedSessionNoteCount"] for report in reports),
        "failed": [
            {**failure, "sourceProvider": report["sourceProvider"], "sourceId": report["sourceId"]}
            for report in reports
            for failure in report["failed"]
        ],
        "warnings": [
            f"{report['sourceProvider']}/{report['sourceId']}: {warning}"
            for report in reports
            for warning in report.get("warnings", [])
        ],
        "reportPaths": [report["reportPath"] for report in reports if report.get("reportPath")],
    }


def pipeline_status(config: AppConfig, *, source_id: str | None = None) -> dict[str, Any]:
    results = [_source_status(source) for source in config.enabled_source_configs(source_id)]
    if len(results) == 1:
        return results[0]
    return {
        "sourceResults": results,
        "liveSourceScan": False,
        "initialized": all(result["initialized"] for result in results),
        "migrationRequired": any(result["migrationRequired"] for result in results),
        "complete": all(result["complete"] for result in results),
    }
