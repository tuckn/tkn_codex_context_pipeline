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
from .decisions import (
    DECISION_PROFILE,
    DecisionGenerator,
    _source_refs_fingerprint,
    decision_generation_fingerprint,
    execute_decision_build,
    load_existing_decisions,
    scan_decision_sources,
)
from .frontmatter import parse_simple_frontmatter
from .provenance import ProvenanceStore, json_bytes
from .scopes import Scope, make_scopes
from .storage import pipeline_storage, read_json
from .thread_notes import (
    SUMMARY_PROFILE,
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
    validate_thread_note,
    write_candidate_note,
)
from .working_context import (
    WORKING_CONTEXT_PROFILE,
    WorkingContextGenerator,
    collect_working_context_sources,
    execute_working_context_build,
    working_context_generation_fingerprint,
)

LEDGER_SCHEMA_VERSION = 1
Progress = Callable[[dict[str, Any]], None]


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _signature(value: Any) -> str:
    return sha256(json_bytes(value)).hexdigest()


def _load_ledger(config: AppConfig) -> dict[str, Any]:
    value = read_json(
        config.state_root / "ledger.json",
        {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "threads": {},
            "scopes": {},
        },
    )
    if value.get("schemaVersion") != LEDGER_SCHEMA_VERSION:
        raise PipelineError("unsupported processing ledger schema")
    if not isinstance(value.get("threads"), dict) or not isinstance(value.get("scopes"), dict):
        raise PipelineError("invalid processing ledger")
    return value


def _save_ledger(config: AppConfig, ledger: dict[str, Any], dry_run: bool) -> None:
    if not dry_run:
        atomic_write_json(config.state_root / "ledger.json", ledger)


def _agent(config: PipelineConfig, stage: str) -> dict[str, Any]:
    profiles: dict[str, Any] = {
        "thread-note": SUMMARY_PROFILE,
        "decisions": DECISION_PROFILE,
        "working-context": WORKING_CONTEXT_PROFILE,
    }
    profile = profiles[stage]
    return {
        "software": "tkn-codex-context-pipeline",
        "version": "0.7.0",
        "provider": config.provider,
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
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
) -> None:
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
                validate_thread_note(existing)  # type: ignore[arg-type]
                entry.update(status="current", reason=None)
                continue
            if not generate or not selected:
                entry.update(status="pending", reason="thread-note-needs-build")
                continue
            if existing:
                metadata = parse_simple_frontmatter(existing.read_text(encoding="utf-8-sig"))
                if metadata.get("reviewStatus") != "unreviewed":
                    entry.update(status="blocked", reason="reviewed-thread-note")
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
                    entry.update(status="blocked", reason="edited-thread-note")
                    continue
            if now_local() >= deadline or limit is not None and attempted >= limit:
                entry.update(status="deferred", reason="runtime-deadline" if now_local() >= deadline else "limit")
                continue
            if provenance.dry_run:
                entry.update(status="planned", reason="thread-note-needs-build")
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
                work_cache_root=config.cache_root,
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
                stage="thread-note",
                subject=key,
                started_at=started,
                used=used,
                generated=[output],
                agent=_agent(pipeline_config, "thread-note"),
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
                        "threadNotePath": str(note),
                        "index": attempted,
                        "total": len(discovery.candidates),
                    }
                )
        except Exception as exc:
            entry.update(status="failed", reason="thread-note-failed", error=str(exc))
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


def _decision_outputs(scope: Scope) -> list[Path]:
    return sorted((scope.project.context_path / "decisions").glob("DR-*.md"))


def _active_decisions(scope: Scope, confirmed: list[str]) -> tuple[tuple[Path, ...], list[str]]:
    allowed = {scope.project.artifact_ref(path) for path in scope.project.iter_note_paths()}
    active: list[Path] = []
    stale: list[str] = []
    for record in load_existing_decisions(scope.project):
        metadata = parse_simple_frontmatter(record.path.read_text(encoding="utf-8-sig"))
        valid_sources = set(record.source_refs).issubset(allowed) and bool(record.source_refs)
        same_sources = metadata.get("sourceThreadNoteSetSha256") == _source_refs_fingerprint(
            scope.project, record.source_refs
        )
        if valid_sources and (same_sources or record.decision_id in confirmed):
            active.append(record.path)
        else:
            stale.append(scope.project.artifact_ref(record.path))
    return tuple(active), stale


def _build_scope(
    config: AppConfig,
    pipeline_config: PipelineConfig,
    scope: Scope,
    ledger: dict[str, Any],
    provenance: ProvenanceStore,
    *,
    run_id: str,
    deadline: datetime,
    mode: str,
    force: bool,
    allow_edited: bool,
    decision_generator: DecisionGenerator | None,
    context_generator: WorkingContextGenerator | None,
    progress: Progress | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {**scope.document(config), "status": "current", "stages": {}}
    prior = ledger["scopes"].get(scope.id, {})
    stages = dict(prior.get("stages", {}))
    scope_entity = provenance.entity(
        json_bytes(scope.document(config)),
        identity=f"codex-scope:{scope.id}",
        ref="data:/catalog/scopes.json#" + scope.id,
        kind="scope",
    )
    scope_signature = _signature(scope.document(config))
    source_signature = _signature(
        {
            "scope": scope.document(config),
            "notes": {scope.project.artifact_ref(path): _hash(path) for path in scope.project.iter_note_paths()},
        }
    )
    if mode in {"clone", "pull", "decisions"}:
        outputs = _decision_outputs(scope)
        old = stages.get("decisions", {})
        fingerprint = _signature([source_signature, decision_generation_fingerprint(pipeline_config)])
        output_hashes = {scope.project.artifact_ref(path): _hash(path) for path in outputs}
        unchanged = (
            old.get("fingerprint") == fingerprint
            and old.get("outputs") == output_hashes
            and provenance.has_activity(old.get("activityId"))
        )
        needs_build = force or not unchanged
        edited = [
            path
            for path in outputs
            if old.get("outputs", {}).get(scope.project.artifact_ref(path)) != _hash(path)
            and parse_simple_frontmatter(path.read_text(encoding="utf-8-sig")).get("reviewStatus") == "unreviewed"
        ]
        if needs_build and edited and not allow_edited:
            raise PipelineError(f"edited decision records are protected: {', '.join(path.name for path in edited)}")
        if needs_build and provenance.dry_run:
            selected, counts, errors = scan_decision_sources(scope.project, pipeline_config, force=True)
            result["stages"]["decisions"] = {"status": "planned", "selectedCount": len(selected), "scan": counts}
            if errors:
                raise PipelineError(f"invalid decision inputs: {errors}")
            result["status"] = "planned"
        elif needs_build:
            if now_local() >= deadline:
                result.update(status="deferred", reason="runtime-deadline")
                return result
            from .decisions import ProviderDecisionGenerator

            used = [
                scope_entity,
                *[provenance.artifact(path) for path in scope.project.iter_note_paths()],
                *[provenance.artifact(path) for path in outputs],
            ]
            started = now_iso()
            missing_outputs = bool(set(old.get("outputs", {})) - set(output_hashes))
            report, _report_path = execute_decision_build(
                pipeline_config,
                scope.project,
                generator=decision_generator or ProviderDecisionGenerator(pipeline_config, observer=progress),
                write=True,
                force=(
                    force
                    or missing_outputs
                    or old.get("scopeSignature") != scope_signature
                    or not provenance.has_activity(old.get("activityId"))
                ),
                cache_root=config.reports_root,
                progress=progress,
                deadline=deadline,
            )
            generated = [
                provenance.artifact(path)
                for path in _decision_outputs(scope)
                if output_hashes.get(scope.project.artifact_ref(path)) != _hash(path)
            ]
            activity = provenance.activity(
                run_id=run_id,
                stage="decisions",
                subject=scope.id,
                started_at=started,
                used=used,
                generated=generated,
                agent=_agent(pipeline_config, "decisions"),
                status="partial" if report["failed"] or report["deferred"] else "completed",
            )
            stages["decisions"] = {
                "status": "pending" if report["failed"] or report["deferred"] else "current",
                "fingerprint": fingerprint if not report["failed"] and not report["deferred"] else None,
                "sourceSignature": source_signature,
                "scopeSignature": scope_signature,
                "outputs": {scope.project.artifact_ref(path): _hash(path) for path in _decision_outputs(scope)},
                "activityId": activity,
                "confirmed": report["referencedExisting"],
                "appliedAt": now_iso(),
            }
            ledger["scopes"][scope.id] = {"stages": stages}
            _save_ledger(config, ledger, False)
            result["stages"]["decisions"] = report
            if report["failed"] or report["deferred"]:
                result.update(status="failed" if report["failed"] else "deferred", reason="decisions-incomplete")
                return result
        else:
            result["stages"]["decisions"] = {"status": "current", "changed": False}
    decision_state = stages.get("decisions", {})
    if mode == "working-context" and (
        decision_state.get("status") != "current"
        or decision_state.get("fingerprint")
        != _signature([source_signature, decision_generation_fingerprint(pipeline_config)])
        or decision_state.get("outputs")
        != {scope.project.artifact_ref(path): _hash(path) for path in _decision_outputs(scope)}
        or not provenance.has_activity(decision_state.get("activityId"))
    ):
        result.update(status="blocked", reason="decisions-need-build")
        return result
    active, stale = _active_decisions(scope, decision_state.get("confirmed", []))
    result["staleDecisionRefs"] = stale
    context_project = replace(scope.project, decision_paths=active)
    if mode in {"clone", "pull", "working-context"}:
        if result["status"] == "planned":
            result["stages"]["working-context"] = {"status": "awaiting-upstream"}
            return result
        if now_local() >= deadline:
            result.update(status="deferred", reason="runtime-deadline")
            return result
        sources, errors = collect_working_context_sources(context_project)
        if errors:
            raise PipelineError(f"invalid Working Context inputs: {errors}")
        old = stages.get("working-context", {})
        fingerprint = _signature(
            [
                source_signature,
                working_context_generation_fingerprint(pipeline_config),
                [(source.source_ref, source.sha256) for source in sources],
            ]
        )
        output_path = context_project.context_path / "working-context.md"
        unchanged = (
            old.get("fingerprint") == fingerprint
            and output_path.is_file()
            and old.get("outputHash") == _hash(output_path)
            and provenance.has_activity(old.get("activityId"))
        )
        if unchanged and not force:
            result["stages"]["working-context"] = {"status": "current", "changed": False}
        else:
            from .working_context import ProviderWorkingContextGenerator

            if output_path.is_file():
                metadata = parse_simple_frontmatter(output_path.read_text(encoding="utf-8-sig"))
                if metadata.get("reviewStatus") not in {None, "unreviewed"}:
                    raise PipelineError("reviewed Working Context is protected")
            # Retain exact source bytes and the normalized/bounded input as distinct entities.
            used = [scope_entity]
            for source in sources:
                metadata = (
                    parse_simple_frontmatter(source.text) if source.kind in {"threadNote", "decisionRecord"} else {}
                )
                source_entity = provenance.entity(
                    source.source_bytes if source.source_bytes is not None else source.text.encode("utf-8"),
                    identity=metadata.get("id") or f"{scope.id}:{source.source_ref}",
                    ref=source.source_ref,
                    kind=source.kind,
                    media_type="text/markdown" if metadata else "text/plain",
                )
                input_entity = provenance.entity(
                    json_bytes(source.as_prompt_dict()),
                    identity=f"inference-input:{scope.id}:{source.source_ref}",
                    ref=source.source_ref + "#inference-input",
                    kind="inferenceInput",
                )
                provenance.activity(
                    run_id=run_id,
                    stage="prepare-input",
                    subject=scope.id,
                    started_at=now_iso(),
                    used=[source_entity],
                    generated=[input_entity],
                    agent={"software": "tkn-codex-context-pipeline", "version": "0.7.0"},
                )
                used.append(input_entity)
            if output_path.is_file():
                used.append(provenance.artifact(output_path))
            started = now_iso()
            report, _report_path = execute_working_context_build(
                pipeline_config,
                context_project,
                generator=None
                if provenance.dry_run
                else context_generator
                or ProviderWorkingContextGenerator(
                    pipeline_config,
                    observer=progress,
                ),
                write=not provenance.dry_run,
                force=True,
                allow_edited=allow_edited,
                cache_root=config.reports_root,
                progress=progress,
                deadline=deadline,
                prepared_sources=sources,
            )
            result["stages"]["working-context"] = report
            if report["failed"]:
                result.update(status="failed", reason="working-context-failed")
                return result
            if provenance.dry_run:
                result["status"] = "planned"
            else:
                output = provenance.artifact(output_path)
                activity = provenance.activity(
                    run_id=run_id,
                    stage="working-context",
                    subject=scope.id,
                    started_at=started,
                    used=used,
                    generated=[output],
                    agent=_agent(pipeline_config, "working-context"),
                )
                stages["working-context"] = {
                    "status": "current",
                    "fingerprint": fingerprint,
                    "outputHash": output["sha256"],
                    "activityId": activity,
                    "appliedAt": now_iso(),
                }
    ledger["scopes"][scope.id] = {"stages": stages}
    _save_ledger(config, ledger, provenance.dry_run)
    if mode == "decisions" and result["status"] == "current":
        result["status"] = "stage-complete"
    return result


def run_pipeline(
    config: AppConfig,
    *,
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    allow_edited: bool = False,
    scope_id: str | None = None,
    thread_id: str | None = None,
    limit: int | None = None,
    config_path: Path | None = None,
    summarizer: Summarizer | None = None,
    decision_generator: DecisionGenerator | None = None,
    context_generator: WorkingContextGenerator | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    if mode not in {"clone", "pull", "raw", "thread-notes", "decisions", "working-context"}:
        raise PipelineError(f"unknown pipeline mode: {mode}")
    if limit is not None and limit <= 0:
        raise PipelineError("limit must be positive")
    run_id = str(uuid4())
    started = now_local()
    deadline = started + timedelta(minutes=config.runtime_minutes)
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": run_id,
        "mode": mode,
        "dryRun": dry_run,
        "startedAt": started.isoformat(),
        "complete": False,
        "ok": False,
        "scopeResults": [],
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
            atomic_write_json(config.state_root / "last-run.json", {**report, "status": "running"})
        if mode == "raw":
            _inputs, raw_report = capture_sources(config, dry_run=dry_run, captured_at=now_iso())
            report.update(
                rawIngest=raw_report,
                failed=raw_report["failed"],
                ok=not raw_report["failed"],
                threadCounts={},
                scopeCounts={},
                threads=[],
                generatedThreadNoteCount=0,
                finishedAt=now_iso(),
            )
            if not dry_run:
                report["reportPath"] = str(config.reports_root / f"{run_id}.json")
                atomic_write_json(Path(report["reportPath"]), report)
                atomic_write_json(config.state_root / "last-run.json", report)
            return report
        ledger = _load_ledger(config)
        provenance = ProvenanceStore(config.data_root, dry_run=dry_run)
        discovery = discover(config, provenance, run_id=run_id)
        report.update(rawIngest=discovery.raw_report, warnings=discovery.warnings)
        report["failed"].extend(discovery.failures)
        scopes = make_scopes(config, discovery)  # Validate explicit selectors before inference.
        if scope_id and scope_id not in {scope.id for scope in scopes}:
            raise PipelineError(f"unknown scope ID: {scope_id}")
        pipeline_config = config.thread_note_pipeline_config(allow_missing_watermark=True)
        _notes(
            config,
            pipeline_config,
            discovery,
            ledger,
            provenance,
            run_id=run_id,
            deadline=deadline,
            generate=mode in {"clone", "pull", "thread-notes"},
            force=force and mode in {"clone", "pull", "thread-notes"},
            allow_edited=allow_edited,
            thread_id=thread_id,
            limit=limit,
            summarizer=summarizer,
            progress=progress,
        )
        scopes = make_scopes(config, discovery)
        entries_by_key = {entry["threadKey"]: entry for entry in discovery.entries}
        for scope in scopes:
            pending = [key for key in scope.thread_keys if entries_by_key[key]["status"] != "current"]
            if scope_id and scope.id != scope_id or mode in {"raw", "thread-notes"}:
                report["scopeResults"].append({**scope.document(config), "status": "not-run"})
                continue
            unassigned_failures = [failure for failure in discovery.failures if not failure.get("threadId")]
            if pending or unassigned_failures:
                planning = (
                    dry_run
                    and not discovery.failures
                    and all(entries_by_key[key]["status"] in {"planned", "deferred", "pending"} for key in pending)
                )
                report["scopeResults"].append(
                    {
                        **scope.document(config),
                        "status": "awaiting-upstream" if planning else "blocked",
                        "reason": "upstream-incomplete",
                        "pendingThreadKeys": pending,
                    }
                )
                continue
            try:
                result = _build_scope(
                    config,
                    pipeline_config,
                    scope,
                    ledger,
                    provenance,
                    run_id=run_id,
                    deadline=deadline,
                    mode=mode,
                    force=force,
                    allow_edited=allow_edited,
                    decision_generator=decision_generator,
                    context_generator=context_generator,
                    progress=progress,
                )
            except Exception as exc:
                result = {**scope.document(config), "status": "failed", "error": str(exc)}
            report["scopeResults"].append(result)
        report["threads"] = discovery.entries
        report["threadCounts"] = dict(Counter(entry["status"] for entry in discovery.entries))
        report["scopeCounts"] = dict(Counter(scope["status"] for scope in report["scopeResults"]))
        report["generatedThreadNoteCount"] = sum(bool(entry.get("generated")) for entry in discovery.entries)
        failures = [entry for entry in discovery.entries if entry["status"] in {"failed", "blocked"}]
        scope_failures = [scope for scope in report["scopeResults"] if scope["status"] in {"failed", "blocked"}]
        report["ok"] = not report["failed"] and not failures and not scope_failures
        report["complete"] = bool(
            report["ok"]
            and mode in {"clone", "pull"}
            and not dry_run
            and all(entry["status"] in {"current", "excluded"} for entry in discovery.entries)
            and all(scope["status"] == "current" for scope in report["scopeResults"])
        )
        report["finishedAt"] = now_iso()
        if not dry_run:
            atomic_write_json(
                config.data_root / "catalog" / "threads.json",
                {
                    "schemaVersion": CATALOG_SCHEMA_VERSION,
                    "asOf": report["finishedAt"],
                    "threads": discovery.entries,
                },
            )
            atomic_write_json(
                config.data_root / "catalog" / "scopes.json",
                {
                    "schemaVersion": CATALOG_SCHEMA_VERSION,
                    "asOf": report["finishedAt"],
                    "scopes": report["scopeResults"],
                },
            )
            artifacts: list[dict[str, Any]] = []
            for entry in discovery.entries:
                if entry.get("noteRef"):
                    path = config.data_root / entry["noteRef"].removeprefix("data:/")
                    if path.is_file():
                        artifacts.append(
                            {**provenance.artifact(path), "status": entry["status"], "threadKey": entry["threadKey"]}
                        )
            for scope in scopes:
                result = next(item for item in report["scopeResults"] if item["id"] == scope.id)
                for path in [*_decision_outputs(scope), scope.project.context_path / "working-context.md"]:
                    if path.is_file():
                        entity = provenance.artifact(path)
                        status = "stale" if entity["ref"] in result.get("staleDecisionRefs", []) else result["status"]
                        if mode == "decisions" and status == "stage-complete":
                            status = "pending" if path.name == "working-context.md" else "current"
                        artifacts.append({**entity, "status": status, "scopeId": scope.id})
            provenance.publish_index(artifacts, run_id=run_id, complete=report["complete"])
            report_path = config.reports_root / f"{run_id}.json"
            report["reportPath"] = str(report_path)
            atomic_write_json(report_path, report)
            atomic_write_json(config.state_root / "last-run.json", report)
    return report


def pipeline_status(config: AppConfig) -> dict[str, Any]:
    last = read_json(config.state_root / "last-run.json")
    return {
        "initialized": (config.state_root / "pipeline.json").is_file(),
        "asOf": last.get("finishedAt"),
        "lastMode": last.get("mode"),
        "complete": last.get("complete", False),
        "threadCounts": last.get("threadCounts", {}),
        "scopeCounts": last.get("scopeCounts", {}),
        "reportPath": last.get("reportPath"),
        "liveSourceScan": False,
    }
