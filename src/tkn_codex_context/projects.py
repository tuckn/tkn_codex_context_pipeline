"""Bind Codex app Projects to the durable context registry."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .app_state import CodexAppProject, CodexAppState
from .chat_logs import normalize_path_text
from .config import AppConfig
from .session_notes import PipelineError, Project, atomic_write_text, now_iso

IDENTITY_KIND = "codexAppLocalProject"
REGISTRY_SCHEMA_VERSION = 2


def read_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PipelineError(f"invalid registry JSON at line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise PipelineError(f"registry line {line_number} must be an object")
            project_id = str(value.get("projectId") or "")
            if not project_id:
                raise PipelineError(f"registry line {line_number} has no projectId")
            if project_id in seen:
                raise PipelineError(f"duplicate projectId in registry: {project_id}")
            seen.add(project_id)
            records.append(value)
    return records


def _root_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("roots", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PipelineError(f"roots must be an array: {record.get('projectId')}")
    return value


def _active_roots(project: CodexAppProject) -> list[dict[str, str]]:
    return [
        {
            "path": str(path.expanduser().absolute()),
            "role": "primary" if index == 0 else "secondary",
            "status": "active",
        }
        for index, path in enumerate(project.root_paths)
    ]


def _new_record(project: CodexAppProject, config: AppConfig) -> dict[str, Any]:
    project_id = project.id
    data = config.projects_data_root / project_id
    state = config.projects_state_root / project_id
    current_root = project.root_paths[0].expanduser().absolute()
    return {
        "schemaVersion": REGISTRY_SCHEMA_VERSION,
        "identityKind": IDENTITY_KIND,
        "projectId": project_id,
        "title": project.name,
        "currentRoot": str(current_root),
        "projectDataPath": str(data),
        "projectStatePath": str(state),
        "sessionsPath": str(data / "sessions"),
        "sensitivity": "private",
        "status": "active",
        "lastSeenAt": now_iso(),
        "roots": _active_roots(project),
    }


def _update_storage_paths(record: dict[str, Any], config: AppConfig) -> None:
    project_id = str(record.get("projectId") or "")
    if not project_id:
        raise PipelineError("cannot assign storage paths without projectId")
    data = config.projects_data_root / project_id
    state = config.projects_state_root / project_id
    record["projectDataPath"] = str(data)
    record["projectStatePath"] = str(state)
    record["sessionsPath"] = str(data / "sessions")
    for legacy_key in (
        "projectContextPath",
        "decisionsPath",
        "memosPath",
        "workingContextPath",
    ):
        record.pop(legacy_key, None)


def _validate_registry_schema(records: Iterable[dict[str, Any]]) -> None:
    unsupported = [
        str(record.get("projectId") or "")
        for record in records
        if record.get("schemaVersion") != REGISTRY_SCHEMA_VERSION
        or record.get("identityKind") != IDENTITY_KIND
    ]
    if unsupported:
        raise PipelineError(
            "unsupported Project registry detected; run `tkn-codex-context init --force` "
            f"to rebuild it: {', '.join(unsupported)}"
        )


def list_registered_projects(path: Path) -> list[dict[str, Any]]:
    """Return registered Projects in a stable, reader-facing order."""

    records = read_registry(path)
    _validate_registry_schema(records)
    projects: list[dict[str, Any]] = [
        {
            "projectId": str(record["projectId"]),
            "name": str(record.get("title") or record["projectId"]),
            "status": str(record.get("status") or "unknown"),
            "currentRoot": str(record.get("currentRoot") or ""),
            "roots": [
                {
                    "path": str(root.get("path") or ""),
                    "role": str(root.get("role") or ""),
                    "status": str(root.get("status") or ""),
                }
                for root in _root_entries(record)
                if root.get("path")
            ],
        }
        for record in records
    ]
    return sorted(
        projects,
        key=lambda item: (
            item["status"] != "active",
            item["name"].casefold(),
            item["projectId"],
        ),
    )


def _update_record(record: dict[str, Any], project: CodexAppProject, config: AppConfig) -> None:
    record["schemaVersion"] = REGISTRY_SCHEMA_VERSION
    record["identityKind"] = IDENTITY_KIND
    record["projectId"] = project.id
    new_active = {normalize_path_text(str(path)) for path in project.root_paths}
    aliases: list[dict[str, Any]] = []
    seen_aliases: set[str] = set()
    for item in _root_entries(record):
        path = str(item.get("path") or "")
        normalized = normalize_path_text(path)
        if not path or normalized in new_active or normalized in seen_aliases:
            continue
        aliases.append({"path": path, "role": "alias", "status": "historical"})
        seen_aliases.add(normalized)
    prior_current = str(record.get("currentRoot") or "")
    if (
        prior_current
        and normalize_path_text(prior_current) not in new_active
        and normalize_path_text(prior_current) not in seen_aliases
    ):
        aliases.append({"path": prior_current, "role": "alias", "status": "historical"})
    record["roots"] = [*_active_roots(project), *aliases]
    record["currentRoot"] = str(project.root_paths[0].expanduser().absolute())
    record["title"] = project.name
    record["status"] = "active"
    record["lastSeenAt"] = now_iso()
    _update_storage_paths(record, config)


def _write_registry(path: Path, records: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records)
    atomic_write_text(path, text)


def create_fresh_projects(
    config: AppConfig,
    app_state: CodexAppState,
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = [_new_record(project, config) for project in app_state.projects]
    if not dry_run:
        for record in records:
            Path(str(record["sessionsPath"])).mkdir(parents=True, exist_ok=True)
            Path(str(record["projectStatePath"])).mkdir(parents=True, exist_ok=True)
        _write_registry(config.registry_path, records)
    results = [
        {
            "sourceProjectId": project.id,
            "projectId": project.id,
            "name": project.name,
            "status": "bound",
            "method": "new",
            "roots": [str(path) for path in project.root_paths],
        }
        for project in app_state.projects
    ]
    return records, {
        "dryRun": dry_run,
        "projectCount": len(records),
        "boundCount": len(records),
        "newCount": len(records),
        "pendingCount": 0,
        "threadAssignmentCount": len(app_state.assignments),
        "projectlessThreadCount": len(app_state.projectless_thread_ids),
        "projects": results,
    }


def fetch_projects(
    config: AppConfig,
    app_state: CodexAppState,
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = read_registry(config.registry_path)
    _validate_registry_schema(records)
    by_id = {str(item["projectId"]): item for item in records}
    current_ids = {project.id for project in app_state.projects}
    results: list[dict[str, Any]] = []
    for app_project in app_state.projects:
        record = by_id.get(app_project.id)
        method = "project-id"
        if record is None:
            record = _new_record(app_project, config)
            records.append(record)
            by_id[str(record["projectId"])] = record
            method = "new"
        else:
            _update_record(record, app_project, config)
        project_id = str(record["projectId"])
        results.append(
            {
                "sourceProjectId": app_project.id,
                "projectId": project_id,
                "name": app_project.name,
                "status": "bound",
                "method": method,
                "roots": [str(path) for path in app_project.root_paths],
            }
        )
    for record in records:
        if str(record["projectId"]) not in current_ids:
            record["status"] = "inactive"
    if not dry_run:
        for project_id in current_ids:
            record = by_id[project_id]
            Path(str(record["sessionsPath"])).mkdir(parents=True, exist_ok=True)
            Path(str(record["projectStatePath"])).mkdir(parents=True, exist_ok=True)
        _write_registry(config.registry_path, records)
    report = {
        "dryRun": dry_run,
        "projectCount": len(app_state.projects),
        "boundCount": sum(item["status"] == "bound" for item in results),
        "newCount": sum(item.get("method") == "new" for item in results),
        "pendingCount": 0,
        "threadAssignmentCount": len(app_state.assignments),
        "projectlessThreadCount": len(app_state.projectless_thread_ids),
        "projects": results,
    }
    return records, report


def runtime_projects(
    records: Iterable[dict[str, Any]],
    app_state: CodexAppState,
) -> list[Project]:
    app_by_id = {item.id: item for item in app_state.projects}
    assigned: dict[str, set[str]] = {item.id: set() for item in app_state.projects}
    for thread_id, assignment in app_state.assignments.items():
        if assignment.project_kind == "local" and assignment.project_id in assigned:
            assigned[assignment.project_id].add(thread_id)
    projects: list[Project] = []
    for record in records:
        if record.get("status") != "active":
            continue
        source_id = str(record.get("projectId") or "")
        app_project = app_by_id.get(source_id)
        if app_project is None:
            continue
        data = str(record.get("projectDataPath") or "")
        project_state = str(record.get("projectStatePath") or "")
        current = str(record.get("currentRoot") or "")
        if not data or not project_state or not current:
            raise PipelineError(f"bound registry record is incomplete: {record.get('projectId')}")
        historical = tuple(
            Path(str(item["path"])).expanduser().absolute()
            for item in _root_entries(record)
            if item.get("status") == "historical" and item.get("path")
        )
        projects.append(
            Project(
                project_id=str(record["projectId"]),
                title=str(record.get("title") or record["projectId"]),
                current_root=Path(current).expanduser().absolute(),
                context_path=Path(data).expanduser().absolute(),
                historical_roots=historical,
                active_roots=tuple(path.expanduser().absolute() for path in app_project.root_paths),
                source_project_id=source_id,
                assigned_thread_ids=frozenset(assigned[source_id]),
                foreign_assigned_thread_ids=frozenset(
                    thread_id
                    for other_source_id, thread_ids in assigned.items()
                    if other_source_id != source_id
                    for thread_id in thread_ids
                ),
                projectless_thread_ids=app_state.projectless_thread_ids,
                state_directory=Path(project_state).expanduser().absolute(),
            )
        )
    return sorted(projects, key=lambda item: item.project_id)
