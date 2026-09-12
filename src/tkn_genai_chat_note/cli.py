"""Command-line interface for Tkn GenAI Chat Note Pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import (
    config_document,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from .console_logging import ColorFormatter, log_success, supports_color
from .raw_capture import RawCaptureError
from .session_notes import (
    PipelineError,
    validate_session_note,
)
from .summary_resources import load_summary_profile

LOGGER = logging.getLogger("tkn_genai_chat_note")


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
        help="Generation provider (generation.active_provider); chat sources use chat.providers",
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
    parser = argparse.ArgumentParser(
        prog="tkn-genai-chat-note",
        description="Preserve local AI chat evidence and generate reusable Session Notes.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.11.0")
    _add_runtime_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Create or inspect configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    init = sub.add_parser("init", help="Create config.yaml; protect existing edits")
    init.add_argument("--force", action="store_true", help="Back up and replace an edited config")
    sub.add_parser("show", help="Show effective values and their sources")
    for name, help_text in (
        ("clone", "Initialize and process all available history; resume safely when repeated"),
        ("pull", "Capture new/changed logs and resume unfinished Session Notes"),
    ):
        command = commands.add_parser(
            name, help=help_text, description=help_text + ". Generates and writes by default."
        )
        _add_build_options(command)
        command.add_argument("--limit", type=int, help="Maximum Session Note generations; remaining work is deferred")
    storage = commands.add_parser("storage", help="Inspect or migrate storage layout")
    migrate = storage.add_subparsers(dest="storage_command", required=True).add_parser(
        "migrate", help="Copy legacy data into provider/source folders; retain original evidence"
    )
    migrate.add_argument("--dry-run", action="store_true", help="Show exact migration files without writing")
    commands.add_parser("status", help="Show last-run coverage and pending work without a live source scan")
    provenance = commands.add_parser("provenance", help="Inspect downstream evidence integrity")
    provenance.add_subparsers(dest="provenance_command", required=True).add_parser(
        "validate",
        help="Read-only verification of artifact identities, snapshots, and activity relations",
    )
    raw = commands.add_parser("raw", help="Capture source bytes independently of inference")
    ingest = raw.add_subparsers(dest="raw_command", required=True).add_parser(
        "ingest", help="Write source copies and manifests (writes by default)"
    )
    ingest.add_argument("--dry-run", action="store_true", help="Inspect without writing")
    ingest.add_argument("--full-output", action="store_true")
    for name, dest, label in (("session-notes", "notes_command", "Session Notes"),):
        group = commands.add_parser(name, help="Build or validate " + label)
        sub = group.add_subparsers(dest=dest, required=True)
        build = sub.add_parser("build", help="Re-evaluate " + label + " (generates and writes by default)")
        _add_build_options(build)
        build.add_argument("--thread-id", help="Select one source conversation; defaults to every conversation")
        build.add_argument("--limit", type=int)
        validate = sub.add_parser("validate", help="Validate an existing artifact")
        validate.add_argument("artifact", type=Path)
    return parser


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without inference or file writes")
    parser.add_argument(
        "--force", action="store_true", help="Re-evaluate unchanged inputs; does not override reviewed or edited files"
    )
    parser.add_argument("--allow-edited", action="store_true", help="Explicitly replace edited, unreviewed outputs")
    parser.add_argument(
        "--full-output", action="store_true", help="Include per-thread and per-stage details in JSON output"
    )


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
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(3)]
    print(f"{headers[0]:<{widths[0]}}  {headers[1]:<{widths[1]}}  {headers[2]:<{widths[2]}}  {headers[3]}")
    for status, name, project_id, current_root in rows:
        print(f"{status:<{widths[0]}}  {name:<{widths[1]}}  {project_id:<{widths[2]}}  {current_root}")


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


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    _configure_logging(args)
    try:
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
            except (RuntimeError, ValueError) as exc:
                raise PipelineError(str(exc)) from exc
            _emit(
                {
                    "command": "config show",
                    "config": config_document(resolved),
                    "storage": {
                        "layoutVersion": 3,
                        "sourceRoots": {
                            provider: {
                                kind: str(path) for kind, path in resolved.source_storage_paths(provider).items()
                            }
                            for provider in resolved.chat.providers.entries()
                        },
                        "sharedCatalog": str(resolved.data_root / "catalog"),
                        "sharedProvenance": str(resolved.data_root / "provenance"),
                    },
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
                }
            )
            return 0

        from .pipeline import pipeline_status, run_pipeline

        config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
        if args.command == "provenance":
            from .provenance import validate_provenance

            _emit(validate_provenance(config.data_root))
            return 0
        if args.command == "storage":
            from .storage_migration import migrate_storage

            _emit(migrate_storage(config, dry_run=args.dry_run, config_path=args.config))
            return 0
        if args.command == "status":
            _emit(pipeline_status(config))
            return 0
        if getattr(args, "artifact", None) is not None:
            validators = {
                "session-notes": validate_session_note,
            }
            _emit(validators[args.command](args.artifact.expanduser().absolute()))
            log_success(LOGGER, "Validation succeeded: %s", args.artifact)
            return 0
        LOGGER.info("%s %s", "Planning" if args.dry_run else "Starting", args.command)
        report = run_pipeline(
            config,
            mode=args.command,
            dry_run=args.dry_run,
            force=getattr(args, "force", False),
            allow_edited=getattr(args, "allow_edited", False),
            thread_id=getattr(args, "thread_id", None),
            limit=getattr(args, "limit", None),
            config_path=args.config,
            progress=_progress,
        )
        if report.get("reportPath"):
            LOGGER.info("Run report: %s", report["reportPath"])
        LOGGER.info("Thread states: %s", report["threadCounts"])
        for warning in report.get("warnings", []):
            LOGGER.warning("%s", warning)
        _emit(
            report
            if args.full_output
            else {key: value for key, value in report.items() if key not in {"threads", "rawIngest"}}
        )
        failed = bool(report["failed"] or report["threadCounts"].get("failed"))
        if failed:
            LOGGER.error("Pipeline has failures; inspect the report and retry after resolving them")
            return 1
        if not report["ok"] or (not args.dry_run and args.command in {"clone", "pull"} and not report["complete"]):
            LOGGER.warning("Pipeline is incomplete; the next pull resumes pending work")
            return 2
        log_success(LOGGER, "Plan validated" if args.dry_run else "Run completed")
        return 0
    except (PipelineError, RawCaptureError, OSError, ValueError, SystemExit) as exc:
        LOGGER.error("%s", exc)
        _emit({"ok": False, "error": str(exc)})
        return 1
