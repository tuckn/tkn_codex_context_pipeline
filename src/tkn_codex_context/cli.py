"""Command-line interface for Tkn Codex Context Pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .app_state import load_codex_app_state
from .artifact_ids import migrate_artifact_ids
from .config import (
    config_document,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from .console_logging import ColorFormatter, log_success, supports_color
from .decision_resources import load_decision_profile
from .decisions import (
    ProviderDecisionGenerator,
    execute_decision_build,
    validate_decision_record,
)
from .inference import provider_name
from .initialization import initialize_application
from .projects import (
    fetch_projects,
    list_registered_projects,
    resolve_project_selector,
    runtime_projects,
)
from .raw_capture import RawCaptureError, ingest_raw_sources
from .summary_resources import load_summary_profile
from .thread_notes import (
    PipelineError,
    ProviderSummarizer,
    execute_pipeline,
    execute_rebuild,
    now_iso,
    validate_thread_note,
    write_run_report,
)
from .working_context import (
    ProviderWorkingContextGenerator,
    execute_working_context_build,
    validate_working_context,
)
from .working_context_resources import load_working_context_profile

LOGGER = logging.getLogger("tkn_codex_context")


def _utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="Explicit YAML config path")
    parser.add_argument(
        "--provider",
        choices=("codex", "claude-code", "github-copilot", "ollama"),
        help="Inference provider; the source provider remains Codex",
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--idle-minutes", type=int)
    parser.add_argument("--runtime-minutes", type=int)
    parser.add_argument("--model-timeout-seconds", type=int)
    parser.add_argument("--codex-executable")
    parser.add_argument("--claude-executable")
    parser.add_argument("--copilot-executable")
    parser.add_argument("--ollama-base-url")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress logs; errors are still shown",
    )
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed diagnostic logs",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tkn-codex-context")
    _add_runtime_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init",
        help="Initialize or cleanly rebuild pipeline storage (writes by default)",
        description=(
            "Initializes or rebuilds pipeline config, raw, data, state, and registry storage by default. "
            "--adopt-existing writes only ownership markers for existing configured roots. "
            "Use --dry-run for a read-only preview."
        ),
    )
    init.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview storage changes without writing config, raw, data, state, cache, or reports",
    )
    init_mode = init.add_mutually_exclusive_group()
    init_mode.add_argument(
        "--force",
        action="store_true",
        help="Rebuild storage only when each existing non-empty root has a valid ownership marker",
    )
    init_mode.add_argument(
        "--adopt-existing",
        action="store_true",
        help=(
            "Mark existing configured data, state, cache, and raw directories as application-owned "
            "without rebuilding them"
        ),
    )

    config = commands.add_parser("config", help="Manage pipeline configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_init = config_commands.add_parser(
        "init",
        help="Create the user config from the packaged example",
        description=(
            "Creates config.yaml when absent and leaves an identical file unchanged. "
            "An edited file is protected unless --force is supplied; forced replacement "
            "first creates a timestamped backup."
        ),
    )
    config_init.add_argument(
        "--force",
        action="store_true",
        help="Back up and replace an existing config with different content",
    )
    config_commands.add_parser("show", help="Show resolved configuration")

    projects = commands.add_parser("projects", help="Manage Project bindings")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    project_list = project_commands.add_parser("list", help="List registered Projects")
    project_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    fetch = project_commands.add_parser(
        "fetch",
        help="Fetch Projects from the Codex app and update the registry (writes by default)",
        description=(
            "Reads Project metadata from local Codex app state and updates the registry by default. "
            "Use --dry-run for a read-only preview."
        ),
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview registry changes without writing application-owned files",
    )

    raw = commands.add_parser("raw", help="Manage immutable Bronze chat captures")
    raw_commands = raw.add_subparsers(dest="raw_command", required=True)
    raw_ingest = raw_commands.add_parser(
        "ingest",
        help="Copy source JSONL into content-addressed Bronze storage (writes by default)",
        description=(
            "Copies new source JSONL bytes into application-owned immutable Bronze storage and "
            "updates its append-only manifest. Source files are never moved or changed."
        ),
    )
    raw_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and validate captures without writing Bronze files, manifests, or reports",
    )
    raw_ingest.add_argument("--full-output", action="store_true", help="Emit the full ingest report JSON")

    artifacts = commands.add_parser("artifacts", help="Manage application-owned Markdown artifacts")
    artifact_commands = artifacts.add_subparsers(dest="artifacts_command", required=True)
    migrate_ids = artifact_commands.add_parser(
        "migrate-ids",
        help="Assign stable UUIDv4 id metadata (writes by default)",
        description=(
            "Adds or validates UUIDv4 id metadata without changing artifact bodies. The write run is "
            "transactional and restores original bytes if validation fails."
        ),
    )
    migrate_selector = migrate_ids.add_mutually_exclusive_group(required=True)
    migrate_selector.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    migrate_selector.add_argument("--all", action="store_true", help="Migrate every active Project")
    migrate_ids.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate artifacts and report changes without assigning IDs or writing files",
    )
    migrate_ids.add_argument("--full-output", action="store_true", help="Emit the full migration report JSON")

    notes = commands.add_parser("thread-notes", help="Generate Thread Note v4 artifacts")
    note_commands = notes.add_subparsers(dest="notes_command", required=True)
    pull = note_commands.add_parser(
        "pull",
        help="Pull eligible chats into Thread Notes (generates and writes by default)",
        description=(
            "Calls generative AI and writes Thread Notes, state, cache, and a run report by default. "
            "Use --dry-run for a read-only preview."
        ),
    )
    pull.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and validate inputs without calling generative AI or writing files",
    )
    pull.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and run report JSON",
    )
    pull.add_argument("--limit", type=int)
    pull.add_argument(
        "--force",
        action="store_true",
        help="Regenerate eligible notes even when unchanged",
    )
    pull.add_argument(
        "--backfill",
        action="store_true",
        help="Pull chats from before installed_at",
    )
    selector = pull.add_mutually_exclusive_group()
    selector.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    selector.add_argument("--all", action="store_true")

    rebuild = note_commands.add_parser(
        "rebuild",
        help="Re-evaluate all chats for one Project (generates and writes by default)",
        description=(
            "Calls generative AI and writes rebuilt Thread Notes, state, cache, and a run report by default. "
            "Use --dry-run for a read-only preview."
        ),
    )
    rebuild.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        required=True,
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    rebuild.add_argument("--force", action="store_true")
    rebuild.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and validate inputs without calling generative AI or writing files",
    )
    rebuild.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and run report JSON",
    )

    decisions = commands.add_parser("decisions", help="Distill durable decision records")
    decision_commands = decisions.add_subparsers(dest="decisions_command", required=True)
    decision_build = decision_commands.add_parser(
        "build",
        help="Build decision records from Thread Notes (generates and writes by default)",
        description=(
            "Calls generative AI and writes Decision Records, state, and a run report by default. "
            "Use --dry-run for a read-only preview."
        ),
    )
    decision_build.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        required=True,
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    decision_mode = decision_build.add_mutually_exclusive_group()
    decision_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and validate inputs without calling generative AI or writing files",
    )
    decision_mode.add_argument(
        "--write",
        action="store_true",
        help="Deprecated compatibility option; normal execution already generates and writes",
    )
    decision_build.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate selected Thread Notes even when decision state is unchanged",
    )
    decision_build.add_argument("--limit", type=int)
    decision_build.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and decision build report JSON",
    )
    decision_validate = decision_commands.add_parser(
        "validate",
        help="Validate one supported Decision Record file through v5",
    )
    decision_validate.add_argument("decision_record", type=Path)

    working_context = commands.add_parser(
        "working-context",
        help="Build the current Project orientation dashboard",
    )
    working_context_commands = working_context.add_subparsers(
        dest="working_context_command",
        required=True,
    )
    working_context_build = working_context_commands.add_parser(
        "build",
        help="Build Working Context v4 from Project evidence (generates and writes by default)",
        description=(
            "Calls generative AI and writes Working Context, state, and a run report by default. "
            "Use --dry-run for a read-only preview."
        ),
    )
    working_context_build.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        required=True,
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    working_context_mode = working_context_build.add_mutually_exclusive_group()
    working_context_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and validate sources without calling generative AI or writing files",
    )
    working_context_mode.add_argument(
        "--write",
        action="store_true",
        help="Deprecated compatibility option; normal execution already generates and writes",
    )
    working_context_build.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate sources even when the input and generation profile are unchanged",
    )
    working_context_build.add_argument(
        "--allow-edited",
        action="store_true",
        help="Replace an existing Working Context that differs from its recorded hash",
    )
    working_context_build.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and Working Context build report JSON",
    )
    working_context_validate = working_context_commands.add_parser(
        "validate",
        help="Validate one supported Working Context file through v4",
    )
    working_context_validate.add_argument("working_context", type=Path)

    validate = commands.add_parser("validate", help="Validate one supported Thread Note file through v4")
    validate.add_argument("thread_note", type=Path)
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = (
        "provider",
        "model",
        "reasoning_effort",
        "idle_minutes",
        "runtime_minutes",
        "model_timeout_seconds",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    )
    return {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        ColorFormatter(
            "[%(levelname)s] %(message)s",
            use_color=supports_color(sys.stderr),
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _table_text(value: Any) -> str:
    return " ".join(str(value).split()) or "-"


def _emit_project_table(projects: list[dict[str, Any]]) -> None:
    if not projects:
        print("No registered Projects.")
        return
    headers = ("STATUS", "NAME", "PROJECT ID", "CURRENT ROOT")
    rows = [
        (
            _table_text(project["status"]),
            _table_text(project["name"]),
            _table_text(project["projectId"]),
            _table_text(project["currentRoot"]),
        )
        for project in projects
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(3)
    ]
    print(
        f"{headers[0]:<{widths[0]}}  "
        f"{headers[1]:<{widths[1]}}  "
        f"{headers[2]:<{widths[2]}}  "
        f"{headers[3]}"
    )
    for status, name, project_id, current_root in rows:
        print(
            f"{status:<{widths[0]}}  "
            f"{name:<{widths[1]}}  "
            f"{project_id:<{widths[2]}}  "
            f"{current_root}"
        )


def _metric_summary(value: dict[str, Any]) -> str:
    metrics: list[str] = []
    duration = value.get("durationSeconds")
    if isinstance(duration, int | float):
        metrics.append(f"{duration:g}s")
    chunks = value.get("chunkCount")
    if isinstance(chunks, int):
        metrics.append(f"{chunks} chunk{'s' if chunks != 1 else ''}")
    calls = value.get("modelCalls")
    if isinstance(calls, int):
        metrics.append(f"{calls} model call{'s' if calls != 1 else ''}")
    transport_retries = value.get("transportRetries")
    if isinstance(transport_retries, int) and transport_retries:
        metrics.append(f"{transport_retries} transport retries")
    semantic_retries = value.get("semanticRetries")
    if isinstance(semantic_retries, int) and semantic_retries:
        metrics.append(f"{semantic_retries} semantic retries")
    return f" ({', '.join(metrics)})" if metrics else ""


def _thread_note_path_summary(value: dict[str, Any]) -> str:
    path = value.get("threadNotePath")
    return f" — Thread Note: {path}" if isinstance(path, str) and path else ""


def _progress(value: dict[str, Any]) -> None:
    LOGGER.debug(
        "Progress event: %s",
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )
    event_type = value.get("type")
    if event_type == "thread-start":
        LOGGER.info(
            "Starting thread %s/%s: %s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadId", "unknown"),
        )
    elif event_type == "thread-resumed":
        LOGGER.info(
            "Resuming completed thread %s/%s from cache: %s%s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadId", "unknown"),
            _thread_note_path_summary(value),
        )
    elif event_type == "chunk-start":
        LOGGER.info(
            "Generating chunk %s/%s for thread %s",
            value.get("chunk", "?"),
            value.get("chunkCount", "?"),
            value.get("threadId", "unknown"),
        )
    elif event_type == "model-attempt":
        LOGGER.info(
            "Calling Codex (attempt %s, timeout %ss)",
            value.get("attempt", "?"),
            value.get("timeoutSeconds", "?"),
        )
    elif event_type == "thread-complete":
        log_success(
            LOGGER,
            "Completed thread %s/%s: %s%s%s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadId", "unknown"),
            _metric_summary(value),
            _thread_note_path_summary(value),
        )
    elif event_type == "thread-failed":
        LOGGER.error(
            "Failed thread %s/%s: %s — %s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadId", "unknown"),
            value.get("error", "unknown error"),
        )
    elif event_type == "decision-batch-start":
        thread_notes = value.get("threadNotes")
        source_count = len(thread_notes) if isinstance(thread_notes, list) else 0
        LOGGER.info(
            "Starting decision synthesis batch %s/%s: %s Thread Notes",
            value.get("index", "?"),
            value.get("total", "?"),
            source_count,
        )
    elif event_type == "decision-batch-complete":
        log_success(
            LOGGER,
            "Completed decision synthesis batch %s/%s (%s created, %s updated, %s existing)%s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("createdCount", 0),
            value.get("updatedCount", 0),
            value.get("referencedCount", 0),
            _metric_summary(value),
        )
        paths = value.get("decisionRecordPaths")
        if isinstance(paths, list):
            for path in paths:
                if isinstance(path, str) and path:
                    LOGGER.info("Decision Record: %s", path)
    elif event_type == "decision-batch-failed":
        LOGGER.error(
            "Failed decision synthesis batch %s/%s: %s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("error", "unknown error"),
        )
    elif event_type == "decision-source-start":
        LOGGER.info(
            "Starting decision source %s/%s: %s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadNote", "unknown"),
        )
    elif event_type == "decision-source-complete":
        log_success(
            LOGGER,
            "Completed decision source %s/%s: %s (%s created, %s existing)%s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadNote", "unknown"),
            value.get("createdCount", 0),
            value.get("referencedCount", 0),
            _metric_summary(value),
        )
        paths = value.get("decisionRecordPaths")
        if isinstance(paths, list):
            for path in paths:
                if isinstance(path, str) and path:
                    LOGGER.info("Decision Record: %s", path)
    elif event_type == "decision-source-failed":
        LOGGER.error(
            "Failed decision source %s/%s: %s — %s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("threadNote", "unknown"),
            value.get("error", "unknown error"),
        )
    elif event_type == "working-context-start":
        LOGGER.info(
            "Starting Working Context synthesis for Project %s: %s sources",
            value.get("projectId", "unknown"),
            value.get("sourceCount", 0),
        )
    elif event_type == "working-context-complete":
        log_success(
            LOGGER,
            "Completed Working Context synthesis for Project %s%s — Working Context: %s",
            value.get("projectId", "unknown"),
            _metric_summary(value),
            value.get("workingContextPath", "unknown"),
        )
    elif event_type == "working-context-failed":
        LOGGER.error(
            "Failed Working Context synthesis for Project %s: %s",
            value.get("projectId", "unknown"),
            value.get("error", "unknown error"),
        )


def _log_project_fetch(report: dict[str, Any]) -> None:
    LOGGER.info(
        "Project metadata ready: %s total, %s bound, %s new, %s pending",
        report.get("projectCount", 0),
        report.get("boundCount", 0),
        report.get("newCount", 0),
        report.get("pendingCount", 0),
    )


def _log_thread_note_report(report: dict[str, Any], report_path: Path | None) -> None:
    selected = report.get("selectedCount", 0)
    failed = len(report.get("failed", []))
    deferred = len(report.get("deferred", []))
    if report.get("dryRun"):
        generation = report.get("generationCount")
        generation_text = f", {generation} require generation" if isinstance(generation, int) else ""
        LOGGER.info(
            "Dry run complete: %s selected%s, %s failed, %s deferred",
            selected,
            generation_text,
            failed,
            deferred,
        )
    else:
        message = "Thread Note run complete: %s processed, %s failed, %s deferred"
        values = (len(report.get("processed", [])), failed, deferred)
        if failed:
            LOGGER.error(message, *values)
        else:
            log_success(LOGGER, message, *values)
    warnings = report.get("warnings", [])
    if warnings:
        LOGGER.warning("Run completed with %s warning%s", len(warnings), "s" if len(warnings) != 1 else "")
    if report_path is not None:
        LOGGER.info("Run report: %s", report_path)


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _project_fetch_summary(report: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "dryRun",
        "projectCount",
        "boundCount",
        "newCount",
        "pendingCount",
        "threadAssignmentCount",
        "projectlessThreadCount",
    )
    return {key: report[key] for key in keys if key in report}


def _thread_note_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    failed_count = _list_count(report.get("failed"))
    summary: dict[str, Any] = {
        "ok": failed_count == 0,
        "dryRun": bool(report.get("dryRun")),
        "mode": report.get("mode"),
        "force": bool(report.get("force")),
        "selectedCount": int(report.get("selectedCount") or 0),
        "processedCount": _list_count(report.get("processed")),
        "failedCount": failed_count,
        "deferredCount": _list_count(report.get("deferred")),
        "warningCount": _list_count(report.get("warnings")),
        "excludedCount": _list_count(report.get("excluded")),
    }
    optional_scalars = (
        "projectId",
        "generationCount",
        "resumedCount",
        "resumeAvailable",
        "startedAt",
        "finishedAt",
    )
    for key in optional_scalars:
        if key in report:
            summary[key] = report[key]
    optional_lists = (
        ("preservedCurrentCount", "preservedCurrent"),
        ("replacedCurrentCount", "replacedCurrent"),
        ("deletedLegacyCount", "deletedLegacy"),
    )
    for output_key, report_key in optional_lists:
        if report_key in report:
            summary[output_key] = _list_count(report.get(report_key))
    scan = report.get("scan")
    if isinstance(scan, dict):
        summary["scan"] = {
            str(key): value
            for key, value in scan.items()
            if type(value) is int
        }
    raw_ingest = report.get("rawIngest")
    if isinstance(raw_ingest, dict):
        summary["rawIngest"] = {
            key: raw_ingest[key]
            for key in (
                "discoveredCount",
                "availableCaptureCount",
                "capturedCount",
                "plannedCaptureCount",
                "unchangedCount",
                "bronzeOnlyCount",
            )
            if key in raw_ingest
        }
    return summary


def _thread_note_output(
    command: str,
    fetch_report: dict[str, Any],
    report: dict[str, Any],
    report_path: Path | None,
    *,
    full_output: bool,
) -> dict[str, Any]:
    if full_output:
        return {
            "command": command,
            "projectFetch": fetch_report,
            "reportPath": str(report_path) if report_path else None,
            "report": report,
        }
    summary = _thread_note_report_summary(report)
    return {
        "command": command,
        "ok": bool(summary["ok"]) and not bool(fetch_report.get("pendingCount")),
        "reportPath": str(report_path) if report_path else None,
        "projectFetchSummary": _project_fetch_summary(fetch_report),
        "reportSummary": summary,
    }


def _decision_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    failed_count = _list_count(report.get("failed"))
    summary: dict[str, Any] = {
        "ok": failed_count == 0,
        "dryRun": bool(report.get("dryRun")),
        "projectId": report.get("projectId"),
        "force": bool(report.get("force")),
        "selectedCount": int(report.get("selectedCount") or 0),
        "processedCount": _list_count(report.get("processed")),
        "createdCount": _list_count(report.get("created")),
        "updatedCount": _list_count(report.get("updated")),
        "referencedExistingCount": _list_count(report.get("referencedExisting")),
        "noActionCount": _list_count(report.get("noAction")),
        "failedCount": failed_count,
        "deferredCount": _list_count(report.get("deferred")),
        "warningCount": _list_count(report.get("warnings")),
        "existingDecisionCount": int(report.get("existingDecisionCount") or 0),
        "existingDecisionIndexLimit": int(report.get("existingDecisionIndexLimit") or 0),
        "existingDecisionIndexOmittedCount": int(report.get("existingDecisionIndexOmittedCount") or 0),
        "synthesisBatchCount": int(report.get("synthesisBatchCount") or 0),
    }
    scan = report.get("scan")
    if isinstance(scan, dict):
        summary["scan"] = {
            str(key): value
            for key, value in scan.items()
            if type(value) is int
        }
    raw_ingest = report.get("rawIngest")
    if isinstance(raw_ingest, dict):
        summary["rawIngest"] = {
            key: raw_ingest[key]
            for key in (
                "discoveredCount",
                "availableCaptureCount",
                "capturedCount",
                "plannedCaptureCount",
                "unchangedCount",
                "bronzeOnlyCount",
            )
            if key in raw_ingest
        }
    return summary


def _decision_output(
    fetch_report: dict[str, Any],
    report: dict[str, Any],
    report_path: Path | None,
    *,
    full_output: bool,
) -> dict[str, Any]:
    if full_output:
        return {
            "command": "decisions build",
            "projectFetch": fetch_report,
            "reportPath": str(report_path) if report_path else None,
            "report": report,
        }
    summary = _decision_report_summary(report)
    return {
        "command": "decisions build",
        "ok": bool(summary["ok"]) and not bool(fetch_report.get("pendingCount")),
        "reportPath": str(report_path) if report_path else None,
        "projectFetchSummary": _project_fetch_summary(fetch_report),
        "reportSummary": summary,
    }


def _working_context_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    failed_count = _list_count(report.get("failed"))
    return {
        "ok": failed_count == 0,
        "dryRun": bool(report.get("dryRun")),
        "projectId": report.get("projectId"),
        "force": bool(report.get("force")),
        "allowEdited": bool(report.get("allowEdited")),
        "changed": bool(report.get("changed")),
        "edited": bool(report.get("edited")),
        "selectedCount": int(report.get("selectedCount") or 0),
        "createdCount": int(report.get("createdCount") or 0),
        "updatedCount": int(report.get("updatedCount") or 0),
        "unchangedCount": int(report.get("unchangedCount") or 0),
        "failedCount": failed_count,
        "sourceCounts": report.get("sourceCounts"),
        "workingContext": report.get("workingContext"),
    }


def _working_context_output(
    fetch_report: dict[str, Any],
    report: dict[str, Any],
    report_path: Path | None,
    *,
    full_output: bool,
) -> dict[str, Any]:
    if full_output:
        return {
            "command": "working-context build",
            "projectFetch": fetch_report,
            "reportPath": str(report_path) if report_path else None,
            "report": report,
        }
    summary = _working_context_report_summary(report)
    return {
        "command": "working-context build",
        "ok": bool(summary["ok"]) and not bool(fetch_report.get("pendingCount")),
        "reportPath": str(report_path) if report_path else None,
        "projectFetchSummary": _project_fetch_summary(fetch_report),
        "reportSummary": summary,
    }


def _prepare_projects(
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> tuple[Any, Any, Any, list[Any], dict[str, Any]]:
    config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
    pipeline_config = config.thread_note_pipeline_config(allow_missing_watermark=dry_run)
    app_state = load_codex_app_state(config.app_state_path)
    records, fetch_report = fetch_projects(config, app_state, dry_run=dry_run)
    return (
        config,
        pipeline_config,
        app_state,
        runtime_projects(records, app_state),
        fetch_report,
    )


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    LOGGER.debug("Parsed command: %s", args.command)
    try:
        if args.command == "init":
            if args.adopt_existing:
                LOGGER.info(
                    "%s existing pipeline storage ownership",
                    "Previewing" if args.dry_run else "Adopting",
                )
            else:
                LOGGER.info("%s pipeline storage", "Previewing" if args.dry_run else "Initializing")
            report = initialize_application(
                args.config,
                overrides=_overrides(args),
                force=args.force,
                dry_run=args.dry_run,
                adopt_existing=args.adopt_existing,
            )
            if args.dry_run:
                LOGGER.info("Dry run complete")
            elif args.adopt_existing:
                log_success(LOGGER, "Storage ownership adoption complete")
            else:
                log_success(LOGGER, "Initialization complete")
            _emit({"command": "init", **report})
            return 0

        if args.command == "config":
            if args.config_command == "init":
                if _overrides(args):
                    raise PipelineError(
                        "runtime configuration options cannot be used with config init; "
                        "create the file first, then edit it or use the options at runtime"
                    )
                LOGGER.info("Initializing user configuration")
                report = initialize_user_config(args.config, force=args.force)
                if report["status"] == "unchanged":
                    LOGGER.info("Configuration unchanged: %s", report["configPath"])
                else:
                    log_success(
                        LOGGER,
                        "Configuration %s: %s",
                        report["status"],
                        report["configPath"],
                    )
                if report["backupPath"]:
                    LOGGER.info("Configuration backup: %s", report["backupPath"])
                _emit({"command": "config init", **report})
                return 0
            LOGGER.info("Showing resolved configuration")
            resolution = resolve_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            resolved = resolution.config
            try:
                profile = load_summary_profile()
                decision_profile = load_decision_profile()
                working_context_profile = load_working_context_profile()
            except (RuntimeError, ValueError) as exc:
                raise PipelineError(str(exc)) from exc
            _emit(
                {
                    "command": "config show",
                    "config": config_document(resolved),
                    "configSchema": {
                        "effectiveVersion": resolution.effective_schema_version,
                        "hasInMemoryMigrations": resolution.has_in_memory_migrations,
                    },
                    "sources": resolution.sources,
                    "layers": list(resolution.layers),
                    "summaryProfile": {
                        "name": profile.name,
                        "source": profile.source,
                        "sha256": profile.sha256,
                        "prompt": {
                            "source": profile.prompt.source,
                            "id": profile.prompt.prompt_id,
                            "version": profile.prompt.version,
                            "sha256": profile.prompt.sha256,
                        },
                        "schema": {
                            "source": profile.schema.source,
                            "sha256": profile.schema.sha256,
                        },
                        "template": {
                            "source": profile.template.source,
                            "id": profile.template.template_id,
                            "version": profile.template.version,
                            "sha256": profile.template.sha256,
                        },
                    },
                    "decisionProfile": {
                        "name": decision_profile.name,
                        "source": decision_profile.source,
                        "sha256": decision_profile.sha256,
                        "prompt": {
                            "source": decision_profile.prompt.source,
                            "id": decision_profile.prompt.prompt_id,
                            "version": decision_profile.prompt.version,
                            "sha256": decision_profile.prompt.sha256,
                        },
                        "schema": {
                            "source": decision_profile.schema.source,
                            "sha256": decision_profile.schema.sha256,
                        },
                        "template": {
                            "source": decision_profile.template.source,
                            "id": decision_profile.template.template_id,
                            "version": decision_profile.template.version,
                            "sha256": decision_profile.template.sha256,
                        },
                    },
                    "workingContextProfile": {
                        "name": working_context_profile.name,
                        "source": working_context_profile.source,
                        "sha256": working_context_profile.sha256,
                        "prompt": {
                            "source": working_context_profile.prompt.source,
                            "id": working_context_profile.prompt.prompt_id,
                            "version": working_context_profile.prompt.version,
                            "sha256": working_context_profile.prompt.sha256,
                        },
                        "schema": {
                            "source": working_context_profile.schema.source,
                            "sha256": working_context_profile.schema.sha256,
                        },
                        "template": {
                            "source": working_context_profile.template.source,
                            "id": working_context_profile.template.template_id,
                            "version": working_context_profile.template.version,
                            "sha256": working_context_profile.template.sha256,
                        },
                    },
                }
            )
            return 0

        if args.command == "raw":
            config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
            captured_at = now_iso()
            LOGGER.info("%s Bronze capture", "Planning" if args.dry_run else "Starting")
            try:
                _inputs, report = ingest_raw_sources(
                    config.sessions_root,
                    config.raw_root,
                    config.source_id,
                    dry_run=args.dry_run,
                    captured_at=captured_at,
                )
            except RawCaptureError as exc:
                raise PipelineError(str(exc)) from exc
            report["startedAt"] = captured_at
            report["finishedAt"] = now_iso()
            report_path = None if args.dry_run else write_run_report(config.reports_root, report)
            failed_count = _list_count(report.get("failed"))
            if failed_count:
                LOGGER.error("Bronze capture completed with %s failed source(s)", failed_count)
            else:
                log_success(
                    LOGGER,
                    "Bronze capture complete: %s captured, %s unchanged, %s Bronze-only",
                    report.get("capturedCount", 0),
                    report.get("unchangedCount", 0),
                    report.get("bronzeOnlyCount", 0),
                )
            if report_path:
                LOGGER.info("Run report: %s", report_path)
            _emit(
                {
                    "command": "raw ingest",
                    "ok": failed_count == 0,
                    "reportPath": str(report_path) if report_path else None,
                    **(
                        {"report": report}
                        if args.full_output
                        else {
                            "reportSummary": {
                                "dryRun": report["dryRun"],
                                "discoveredCount": report["discoveredCount"],
                                "availableCaptureCount": report["availableCaptureCount"],
                                "capturedCount": report["capturedCount"],
                                "plannedCaptureCount": report["plannedCaptureCount"],
                                "unchangedCount": report["unchangedCount"],
                                "bronzeOnlyCount": report["bronzeOnlyCount"],
                                "failedCount": failed_count,
                            }
                        }
                    ),
                }
            )
            return 1 if failed_count else 0

        if args.command == "artifacts":
            LOGGER.info("%s artifact ID migration", "Planning" if args.dry_run else "Starting")
            config, _pipeline, _state, projects, fetch_report = _prepare_projects(args, dry_run=True)
            _log_project_fetch(fetch_report)
            selected_projects = (
                projects
                if args.all
                else [resolve_project_selector(projects, args.project_id)]
            )
            report = migrate_artifact_ids(selected_projects, dry_run=args.dry_run)
            report_path = None if args.dry_run else write_run_report(config.reports_root, report)
            if args.dry_run:
                LOGGER.info("Dry run complete: %s artifact change(s) planned", report["plannedCount"])
            else:
                log_success(LOGGER, "Artifact ID migration complete: %s migrated", report["plannedCount"])
                if report_path:
                    LOGGER.info("Run report: %s", report_path)
            summary = {
                "dryRun": report["dryRun"],
                "projectCount": report["projectCount"],
                "artifactCount": report["artifactCount"],
                "plannedCount": report["plannedCount"],
                "assignedCount": report["assignedCount"],
                "schemaUpgradeCount": report["schemaUpgradeCount"],
                "unchangedCount": report["unchangedCount"],
            }
            _emit(
                {
                    "command": "artifacts migrate-ids",
                    "ok": not bool(fetch_report.get("pendingCount")),
                    "reportPath": str(report_path) if report_path else None,
                    "projectFetchSummary": _project_fetch_summary(fetch_report),
                    **({"report": report} if args.full_output else {"reportSummary": summary}),
                }
            )
            return 2 if fetch_report.get("pendingCount") else 0

        if args.command == "validate":
            note = args.thread_note.expanduser().absolute()
            LOGGER.info("Validating Thread Note: %s", note)
            _emit(validate_thread_note(note))
            log_success(LOGGER, "Validation succeeded: %s", note)
            return 0

        if args.command == "projects":
            config = load_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            if args.projects_command == "list":
                LOGGER.info("Listing registered Projects")
                registered_projects = list_registered_projects(config.registry_path)
                if args.json:
                    _emit(
                        {
                            "command": "projects list",
                            "projectCount": len(registered_projects),
                            "projects": registered_projects,
                        }
                    )
                else:
                    _emit_project_table(registered_projects)
                return 0
            LOGGER.info("%s Project metadata from the Codex app", "Previewing" if args.dry_run else "Fetching")
            state = load_codex_app_state(config.app_state_path)
            _records, report = fetch_projects(config, state, dry_run=args.dry_run)
            _log_project_fetch(report)
            _emit({"command": "projects fetch", **report})
            return 2 if report["pendingCount"] else 0

        if args.command == "decisions":
            if args.decisions_command == "validate":
                record = args.decision_record.expanduser().absolute()
                LOGGER.info("Validating Decision Record: %s", record)
                _emit(validate_decision_record(record))
                log_success(LOGGER, "Validation succeeded: %s", record)
                return 0
            dry_run = bool(args.dry_run)
            write = not dry_run
            if args.write:
                LOGGER.warning(
                    "--write is deprecated for decisions build; normal execution already writes. "
                    "Use --dry-run for a read-only preview."
                )
            LOGGER.info(
                "%s decision build for Project %s",
                "Starting" if write else "Planning",
                args.project_id,
            )
            (
                config,
                pipeline_config,
                _state,
                projects,
                fetch_report,
            ) = _prepare_projects(args, dry_run=not write)
            _log_project_fetch(fetch_report)
            decision_project = resolve_project_selector(projects, args.project_id)
            if decision_project.project_id != args.project_id:
                selector_kind = (
                    "Name" if decision_project.title == args.project_id else "CURRENT ROOT"
                )
                LOGGER.info(
                    "Resolved Project %s %r to ID %s",
                    selector_kind,
                    args.project_id,
                    decision_project.project_id,
                )
            decision_generator = (
                ProviderDecisionGenerator(pipeline_config, observer=_progress)
                if write
                else None
            )
            report, report_path = execute_decision_build(
                pipeline_config,
                decision_project,
                generator=decision_generator,
                write=write,
                force=args.force,
                limit=args.limit,
                cache_root=config.reports_root,
                progress=_progress,
            )
            failed = _list_count(report.get("failed"))
            if write:
                message = (
                    "Decision build complete: %s processed, %s created, "
                    "%s updated, %s existing, %s failed"
                )
                values = (
                    _list_count(report.get("processed")),
                    _list_count(report.get("created")),
                    _list_count(report.get("updated")),
                    _list_count(report.get("referencedExisting")),
                    failed,
                )
                if failed:
                    LOGGER.error(message, *values)
                else:
                    log_success(LOGGER, message, *values)
                if report_path:
                    LOGGER.info("Run report: %s", report_path)
            else:
                LOGGER.info(
                    "Dry run complete: %s selected, %s existing decisions, %s failed",
                    report.get("selectedCount", 0),
                    report.get("existingDecisionCount", 0),
                    failed,
                )
            decision_warnings = report.get("warnings", [])
            if decision_warnings:
                LOGGER.warning(
                    "Decision build completed with %s warning%s",
                    len(decision_warnings),
                    "s" if len(decision_warnings) != 1 else "",
                )
            _emit(
                _decision_output(
                    fetch_report,
                    report,
                    report_path,
                    full_output=args.full_output,
                )
            )
            return 1 if failed else 0

        if args.command == "working-context":
            if args.working_context_command == "validate":
                note = args.working_context.expanduser().absolute()
                LOGGER.info("Validating Working Context: %s", note)
                _emit(validate_working_context(note))
                log_success(LOGGER, "Validation succeeded: %s", note)
                return 0
            dry_run = bool(args.dry_run)
            write = not dry_run
            if args.write:
                LOGGER.warning(
                    "--write is deprecated for working-context build; normal execution already writes. "
                    "Use --dry-run for a read-only preview."
                )
            LOGGER.info(
                "%s Working Context build for Project %s",
                "Starting" if write else "Planning",
                args.project_id,
            )
            (
                config,
                pipeline_config,
                _state,
                projects,
                fetch_report,
            ) = _prepare_projects(args, dry_run=not write)
            _log_project_fetch(fetch_report)
            context_project = resolve_project_selector(projects, args.project_id)
            if context_project.project_id != args.project_id:
                selector_kind = "Name" if context_project.title == args.project_id else "CURRENT ROOT"
                LOGGER.info(
                    "Resolved Project %s %r to ID %s",
                    selector_kind,
                    args.project_id,
                    context_project.project_id,
                )
            context_generator = (
                ProviderWorkingContextGenerator(pipeline_config, observer=_progress)
                if write
                else None
            )
            report, report_path = execute_working_context_build(
                pipeline_config,
                context_project,
                generator=context_generator,
                write=write,
                force=args.force,
                allow_edited=args.allow_edited,
                cache_root=config.reports_root,
                progress=_progress,
            )
            failed = _list_count(report.get("failed"))
            if write:
                message = "Working Context build complete: %s created, %s updated, %s unchanged, %s failed"
                context_values = (
                    report.get("createdCount", 0),
                    report.get("updatedCount", 0),
                    report.get("unchangedCount", 0),
                    failed,
                )
                if failed:
                    LOGGER.error(message, *context_values)
                else:
                    log_success(LOGGER, message, *context_values)
                if report_path:
                    LOGGER.info("Run report: %s", report_path)
            else:
                LOGGER.info(
                    "Dry run complete: changed=%s, edited=%s, %s failed",
                    report.get("changed"),
                    report.get("edited"),
                    failed,
                )
            _emit(
                _working_context_output(
                    fetch_report,
                    report,
                    report_path,
                    full_output=args.full_output,
                )
            )
            return 1 if failed else 0

        if args.notes_command == "pull":
            has_backfill_selector = bool(args.project_id or args.all)
            if args.backfill and not has_backfill_selector:
                raise PipelineError("--backfill requires --project-id <projectIdOrNameOrRoot> or --all")
            if not args.backfill and has_backfill_selector:
                raise PipelineError("--project-id and --all require --backfill")

        dry_run = bool(args.dry_run)
        if args.notes_command == "rebuild":
            LOGGER.info(
                "%s Thread Note rebuild for Project %s",
                "Planning" if dry_run else "Starting",
                args.project_id,
            )
        else:
            mode = "backfill" if args.backfill else "pull"
            LOGGER.info("%s Thread Note %s", "Planning" if dry_run else "Starting", mode)
        (
            config,
            pipeline_config,
            _state,
            projects,
            fetch_report,
        ) = _prepare_projects(args, dry_run=dry_run)
        _log_project_fetch(fetch_report)
        selected_project = (
            resolve_project_selector(projects, args.project_id)
            if args.project_id
            else None
        )
        if selected_project is not None and selected_project.project_id != args.project_id:
            selector_kind = (
                "Name"
                if selected_project.title == args.project_id
                else "CURRENT ROOT"
            )
            LOGGER.info(
                "Resolved Project %s %r to ID %s",
                selector_kind,
                args.project_id,
                selected_project.project_id,
            )
        if not dry_run:
            LOGGER.info(
                "Generator: %s / %s (%s reasoning)",
                provider_name(pipeline_config.provider),
                pipeline_config.model,
                pipeline_config.reasoning_effort,
            )
        summarizer = (
            None
            if dry_run
            else ProviderSummarizer(
                pipeline_config,
                observer=_progress,
            )
        )
        if args.notes_command == "pull":
            report, report_path = execute_pipeline(
                pipeline_config,
                projects,
                summarizer=summarizer,
                dry_run=dry_run,
                backfill=args.backfill,
                force=args.force,
                project_ids=(selected_project.project_id,) if selected_project is not None else (),
                limit=args.limit,
                cache_root=config.reports_root,
                work_cache_root=config.cache_root,
                progress=_progress,
            )
        else:
            if selected_project is None:
                raise PipelineError("rebuild requires --project-id")
            report, report_path = execute_rebuild(
                pipeline_config,
                selected_project,
                summarizer=summarizer,
                force=args.force,
                dry_run=dry_run,
                cache_root=config.reports_root,
                work_cache_root=config.cache_root,
                progress=_progress,
            )
        _log_thread_note_report(report, report_path)
        _emit(
            _thread_note_output(
                f"thread-notes {args.notes_command}",
                fetch_report,
                report,
                report_path,
                full_output=args.full_output,
            )
        )
        return 1 if report.get("failed") else 0
    except PipelineError as exc:
        LOGGER.error("%s", exc)
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
