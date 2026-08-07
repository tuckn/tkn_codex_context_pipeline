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
from .config import config_document, load_app_config
from .console_logging import ColorFormatter, log_success, supports_color
from .decision_resources import load_decision_profile
from .decisions import (
    CodexDecisionGenerator,
    execute_decision_build,
    validate_decision_record,
)
from .initialization import initialize_application
from .projects import (
    fetch_projects,
    list_registered_projects,
    resolve_project_selector,
    runtime_projects,
)
from .session_notes import (
    CodexSummarizer,
    PipelineError,
    execute_pipeline,
    execute_rebuild,
    validate_session_note,
)
from .summary_resources import load_summary_profile

LOGGER = logging.getLogger("tkn_codex_context")


def _utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, help="Explicit YAML config path")
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--idle-minutes", type=int)
    parser.add_argument("--runtime-minutes", type=int)
    parser.add_argument("--model-timeout-seconds", type=int)
    parser.add_argument("--codex-executable")
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

    init = commands.add_parser("init", help="Initialize or cleanly rebuild pipeline storage")
    init.add_argument("--dry-run", action="store_true")
    init.add_argument("--force", action="store_true")

    config = commands.add_parser("config", help="Manage pipeline configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="Show resolved configuration")

    projects = commands.add_parser("projects", help="Manage Project bindings")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    project_list = project_commands.add_parser("list", help="List registered Projects")
    project_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    fetch = project_commands.add_parser("fetch", help="Fetch Projects from the Codex app")
    fetch.add_argument("--dry-run", action="store_true")

    notes = commands.add_parser("session-notes", help="Generate Session Note v2 artifacts")
    note_commands = notes.add_subparsers(dest="notes_command", required=True)
    pull = note_commands.add_parser(
        "pull",
        help="Pull eligible chats into Session Notes",
    )
    pull.add_argument("--dry-run", action="store_true")
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

    rebuild = note_commands.add_parser("rebuild", help="Re-evaluate all chats for one Project")
    rebuild.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        required=True,
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    rebuild.add_argument("--force", action="store_true")
    rebuild.add_argument("--dry-run", action="store_true")
    rebuild.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and run report JSON",
    )

    decisions = commands.add_parser("decisions", help="Distill durable decision records")
    decision_commands = decisions.add_subparsers(dest="decisions_command", required=True)
    decision_build = decision_commands.add_parser(
        "build",
        help="Build decision records from Session Notes",
    )
    decision_build.add_argument(
        "--project-id",
        metavar="PROJECT_ID_NAME_OR_ROOT",
        required=True,
        help="Select one active Project by ID, exact current Name, or CURRENT ROOT",
    )
    decision_build.add_argument(
        "--write",
        action="store_true",
        help="Generate and write decision records; the default is a read-only plan",
    )
    decision_build.add_argument(
        "--force",
        action="store_true",
        help="Re-evaluate selected Session Notes even when decision state is unchanged",
    )
    decision_build.add_argument("--limit", type=int)
    decision_build.add_argument(
        "--full-output",
        action="store_true",
        help="Emit the full Project fetch and decision build report JSON",
    )
    decision_validate = decision_commands.add_parser(
        "validate",
        help="Validate one Decision Record v2 or v3 file",
    )
    decision_validate.add_argument("decision_record", type=Path)

    validate = commands.add_parser("validate", help="Validate a Session Note v2 file")
    validate.add_argument("session_note", type=Path)
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = (
        "model",
        "reasoning_effort",
        "idle_minutes",
        "runtime_minutes",
        "model_timeout_seconds",
        "codex_executable",
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


def _session_note_path_summary(value: dict[str, Any]) -> str:
    path = value.get("sessionNotePath")
    return f" — Session Note: {path}" if isinstance(path, str) and path else ""


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
            _session_note_path_summary(value),
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
            _session_note_path_summary(value),
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
        session_notes = value.get("sessionNotes")
        source_count = len(session_notes) if isinstance(session_notes, list) else 0
        LOGGER.info(
            "Starting decision synthesis batch %s/%s: %s Session Notes",
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
            value.get("sessionNote", "unknown"),
        )
    elif event_type == "decision-source-complete":
        log_success(
            LOGGER,
            "Completed decision source %s/%s: %s (%s created, %s existing)%s",
            value.get("index", "?"),
            value.get("total", "?"),
            value.get("sessionNote", "unknown"),
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
            value.get("sessionNote", "unknown"),
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


def _log_session_report(report: dict[str, Any], report_path: Path | None) -> None:
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
        message = "Session Note run complete: %s processed, %s failed, %s deferred"
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


def _session_report_summary(report: dict[str, Any]) -> dict[str, Any]:
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
    return summary


def _session_output(
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
    summary = _session_report_summary(report)
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
        "existingDecisionCount": int(report.get("existingDecisionCount") or 0),
        "synthesisBatchCount": int(report.get("synthesisBatchCount") or 0),
    }
    scan = report.get("scan")
    if isinstance(scan, dict):
        summary["scan"] = {
            str(key): value
            for key, value in scan.items()
            if type(value) is int
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


def _prepare_projects(
    args: argparse.Namespace,
    *,
    dry_run: bool,
) -> tuple[Any, Any, Any, list[Any], dict[str, Any]]:
    config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
    pipeline_config = config.session_pipeline_config(allow_missing_watermark=dry_run)
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
            LOGGER.info("%s pipeline storage", "Previewing" if args.dry_run else "Initializing")
            report = initialize_application(
                args.config,
                overrides=_overrides(args),
                force=args.force,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                LOGGER.info("Dry run complete")
            else:
                log_success(LOGGER, "Initialization complete")
            _emit({"command": "init", **report})
            return 0

        if args.command == "config":
            LOGGER.info("Showing resolved configuration")
            resolved = load_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            try:
                profile = load_summary_profile()
                decision_profile = load_decision_profile()
            except (RuntimeError, ValueError) as exc:
                raise PipelineError(str(exc)) from exc
            _emit(
                {
                    "command": "config show",
                    "config": config_document(resolved),
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
                }
            )
            return 0

        if args.command == "validate":
            note = args.session_note.expanduser().absolute()
            LOGGER.info("Validating Session Note: %s", note)
            _emit(validate_session_note(note))
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
            if args.force and not args.write:
                raise PipelineError("--force requires --write for decisions build")
            write = bool(args.write)
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
            generator = (
                CodexDecisionGenerator(pipeline_config, observer=_progress)
                if write
                else None
            )
            report, report_path = execute_decision_build(
                pipeline_config,
                decision_project,
                generator=generator,
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
            _emit(
                _decision_output(
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
                "%s Session Note rebuild for Project %s",
                "Planning" if dry_run else "Starting",
                args.project_id,
            )
        else:
            mode = "backfill" if args.backfill else "pull"
            LOGGER.info("%s Session Note %s", "Planning" if dry_run else "Starting", mode)
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
                "Generator: %s (%s reasoning)",
                pipeline_config.model,
                pipeline_config.reasoning_effort,
            )
        summarizer = (
            None
            if dry_run
            else CodexSummarizer(
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
        _log_session_report(report, report_path)
        _emit(
            _session_output(
                f"session-notes {args.notes_command}",
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
