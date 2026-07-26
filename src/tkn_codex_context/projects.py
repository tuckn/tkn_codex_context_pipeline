"""Bind Codex app Projects to the durable context registry."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any

from .app_state import CodexAppProject, CodexAppState
from .chat_logs import normalize_path_text
from .config import AppConfig
from .session_notes import PipelineError, Project, atomic_write_text, now_iso

SOURCE_KIND = "codexAppLocalProject"


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


def _bindings(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("sourceBindings", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PipelineError(f"sourceBindings must be an array: {record.get('projectId')}")
    return value


def _root_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("roots", [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PipelineError(f"roots must be an array: {record.get('projectId')}")
    return value


def bound_source_id(record: dict[str, Any]) -> str:
    matches = [str(item.get("sourceProjectId") or "") for item in _bindings(record) if item.get("kind") == SOURCE_KIND]
    matches = [item for item in matches if item]
    if len(matches) > 1:
        raise PipelineError(f"multiple Codex app bindings: {record.get('projectId')}")
    return matches[0] if matches else ""


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return text[:40] or "project"


def deterministic_project_id(project: CodexAppProject) -> str:
    date = project.created_at.astimezone().strftime("%Y%m%d")
    digest = sha256(project.id.encode("utf-8")).hexdigest()[:10]
    return f"{date}_{_slug(project.name)}_{digest}"


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
    project_id = deterministic_project_id(project)
    context = config.context_store_root / "state" / project_id
    current_root = project.root_paths[0].expanduser().absolute()
    return {
        "projectId": project_id,
        "workspaceId": f"codex-app-{sha256(project.id.encode()).hexdigest()[:16]}",
        "repoId": "",
        "title": project.name,
        "currentRoot": str(current_root),
        "projectContextPath": str(context),
        "sessionsPath": str(context / "sessions"),
        "decisionsPath": str(context / "decisions"),
        "memosPath": str(context / "memos"),
        "workingContextPath": str(context / "working-context.md"),
        "sensitivity": "private",
        "status": "active",
        "lastSeenAt": now_iso(),
        "sourceBindings": [{"kind": SOURCE_KIND, "sourceProjectId": project.id}],
        "roots": _active_roots(project),
    }


def _record_root(record: dict[str, Any]) -> str:
    return normalize_path_text(str(record.get("currentRoot") or ""))


def _select_match(
    project: CodexAppProject,
    records: list[dict[str, Any]],
    claimed: set[str],
) -> tuple[dict[str, Any] | None, str, list[str]]:
    saved = [item for item in records if bound_source_id(item) == project.id]
    if len(saved) == 1:
        return saved[0], "saved-binding", []
    if len(saved) > 1:
        return None, "pending", [str(item["projectId"]) for item in saved]
    roots = {normalize_path_text(str(path)) for path in project.root_paths}
    root_matches = [item for item in records if str(item["projectId"]) not in claimed and _record_root(item) in roots]
    if len(root_matches) == 1:
        return root_matches[0], "root", []
    if len(root_matches) > 1:
        return None, "pending", [str(item["projectId"]) for item in root_matches]
    title_matches = [
        item
        for item in records
        if str(item["projectId"]) not in claimed and str(item.get("title") or "").casefold() == project.name.casefold()
    ]
    if len(title_matches) == 1:
        return title_matches[0], "name", []
    if len(title_matches) > 1:
        return None, "pending", [str(item["projectId"]) for item in title_matches]
    return None, "new", []


def _update_record(record: dict[str, Any], project: CodexAppProject) -> None:
    other_bindings = [item for item in _bindings(record) if item.get("kind") != SOURCE_KIND]
    record["sourceBindings"] = [
        *other_bindings,
        {"kind": SOURCE_KIND, "sourceProjectId": project.id},
    ]
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
    record["lastSeenAt"] = now_iso()


def _write_registry(path: Path, records: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records)
    atomic_write_text(path, text)


def sync_projects(
    config: AppConfig,
    app_state: CodexAppState,
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = read_registry(config.registry_path)
    by_id = {str(item["projectId"]): item for item in records}
    claimed: set[str] = set()
    results: list[dict[str, Any]] = []
    created_ids: set[str] = set()
    for app_project in app_state.projects:
        record, method, candidates = _select_match(app_project, records, claimed)
        if method == "pending":
            results.append(
                {
                    "sourceProjectId": app_project.id,
                    "name": app_project.name,
                    "status": "pending",
                    "method": "ambiguous",
                    "candidateProjectIds": candidates,
                }
            )
            continue
        if record is None:
            record = _new_record(app_project, config)
            if record["projectId"] in by_id:
                raise PipelineError(f"deterministic projectId collides with an existing record: {record['projectId']}")
            records.append(record)
            by_id[str(record["projectId"])] = record
            created_ids.add(str(record["projectId"]))
        else:
            _update_record(record, app_project)
        project_id = str(record["projectId"])
        claimed.add(project_id)
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
    if not dry_run:
        for project_id in created_ids:
            record = by_id[project_id]
            Path(str(record["sessionsPath"])).mkdir(parents=True, exist_ok=True)
        _write_registry(config.registry_path, records)
    report = {
        "dryRun": dry_run,
        "projectCount": len(app_state.projects),
        "boundCount": sum(item["status"] == "bound" for item in results),
        "newCount": sum(item.get("method") == "new" for item in results),
        "pendingCount": sum(item["status"] == "pending" for item in results),
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
        source_id = bound_source_id(record)
        app_project = app_by_id.get(source_id)
        if app_project is None:
            continue
        context = str(record.get("projectContextPath") or "")
        current = str(record.get("currentRoot") or "")
        if not context or not current:
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
                context_path=Path(context).expanduser().absolute(),
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
            )
        )
    return sorted(projects, key=lambda item: item.project_id)
