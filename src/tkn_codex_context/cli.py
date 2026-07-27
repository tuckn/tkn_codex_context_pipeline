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
from .initialization import initialize_application
from .projects import fetch_projects, list_registered_projects, runtime_projects
from .session_notes import (
    CodexSummarizer,
    PipelineError,
    execute_pipeline,
    execute_rebuild,
    validate_session_note,
)

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
    output.add_argument("--quiet", action="store_true")
    output.add_argument("--verbose", action="store_true")


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
        help="Pull post-install idle chats into Session Notes",
    )
    pull.add_argument("--dry-run", action="store_true")
    pull.add_argument("--limit", type=int)

    backfill = note_commands.add_parser("backfill", help="Process older chats")
    selector = backfill.add_mutually_exclusive_group(required=True)
    selector.add_argument("--project-id")
    selector.add_argument("--all", action="store_true")
    backfill.add_argument("--limit", type=int)
    backfill.add_argument("--dry-run", action="store_true")

    rebuild = note_commands.add_parser("rebuild", help="Re-evaluate all chats for one Project")
    rebuild.add_argument("--project-id", required=True)
    rebuild.add_argument("--force", action="store_true")
    rebuild.add_argument("--dry-run", action="store_true")

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
    level = logging.WARNING if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")


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


def _progress(value: dict[str, Any]) -> None:
    LOGGER.info("%s", json.dumps(value, ensure_ascii=False, separators=(",", ":")))


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
    try:
        if args.command == "init":
            report = initialize_application(
                args.config,
                overrides=_overrides(args),
                force=args.force,
                dry_run=args.dry_run,
            )
            _emit({"command": "init", **report})
            return 0

        if args.command == "config":
            resolved = load_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            _emit({"command": "config show", "config": config_document(resolved)})
            return 0

        if args.command == "validate":
            _emit(validate_session_note(args.session_note.expanduser().absolute()))
            return 0

        if args.command == "projects":
            config = load_app_config(
                explicit_path=args.config,
                overrides=_overrides(args),
            )
            if args.projects_command == "list":
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
            state = load_codex_app_state(config.app_state_path)
            _records, report = fetch_projects(config, state, dry_run=args.dry_run)
            _emit({"command": "projects fetch", **report})
            return 2 if report["pendingCount"] else 0

        dry_run = bool(args.dry_run)
        (
            config,
            pipeline_config,
            _state,
            projects,
            fetch_report,
        ) = _prepare_projects(args, dry_run=dry_run)
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
                limit=args.limit,
                cache_root=config.reports_root,
                work_cache_root=config.cache_root,
                progress=_progress,
            )
        elif args.notes_command == "backfill":
            project_ids = () if args.all else (args.project_id,)
            report, report_path = execute_pipeline(
                pipeline_config,
                projects,
                summarizer=summarizer,
                dry_run=dry_run,
                backfill=True,
                project_ids=project_ids,
                limit=args.limit,
                cache_root=config.reports_root,
                work_cache_root=config.cache_root,
                progress=_progress,
            )
        else:
            selected = [item for item in projects if item.project_id == args.project_id]
            if len(selected) != 1:
                raise PipelineError(f"unknown or pending projectId: {args.project_id}")
            report, report_path = execute_rebuild(
                pipeline_config,
                selected[0],
                summarizer=summarizer,
                force=args.force,
                dry_run=dry_run,
                cache_root=config.reports_root,
                work_cache_root=config.cache_root,
                progress=_progress,
            )
        _emit(
            {
                "command": f"session-notes {args.notes_command}",
                "projectFetch": fetch_report,
                "reportPath": str(report_path) if report_path else None,
                "report": report,
            }
        )
        return 1 if report.get("failed") else 0
    except PipelineError as exc:
        LOGGER.error("%s", exc)
        _emit({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
