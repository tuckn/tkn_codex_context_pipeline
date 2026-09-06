"""Project membership is one view; explicit work scopes can span many projects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .catalog import Discovery
from .config import AppConfig
from .thread_notes import PipelineError, Project


@dataclass
class Scope:
    id: str
    title: str
    kind: str
    thread_keys: tuple[str, ...]
    project: Project

    def document(self, config: AppConfig) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "threadKeys": list(self.thread_keys),
            "dataRef": "data:/" + self.project.context_path.relative_to(config.data_root).as_posix(),
            "repositoryRoots": [str(root) for root in self.project.repository_roots or ()],
        }


def make_scopes(config: AppConfig, discovery: Discovery) -> list[Scope]:
    members = [entry for entry in discovery.entries if entry["status"] != "excluded"]
    definitions: dict[str, tuple[str, str, list[str], tuple[Path, ...]]] = {}
    for project_id, project in discovery.projects.items():
        definitions[f"project:{project_id}"] = (
            project["title"],
            "project",
            [],
            tuple(Path(root) for root in project["roots"]),
        )
    for entry in members:
        assignment = entry.get("membership", {})
        project_id = assignment.get("sourceProjectId")
        kind = assignment.get("projectKind")
        if project_id and kind == "local":
            scope_id = f"project:{project_id}"
            definitions.setdefault(scope_id, (str(project_id), "project", [], ()))
        else:
            scope_id = "unassigned"
            definitions.setdefault(
                scope_id,
                (
                    "Unassigned conversations (a collection of independent activities)",
                    "collection",
                    [],
                    (),
                ),
            )
        definitions[scope_id][2].append(entry["threadKey"])
    known_threads = {entry["threadId"] for entry in members}
    known_projects = set(discovery.projects) | {entry.get("membership", {}).get("sourceProjectId") for entry in members}
    for key, configured in config.scopes.items():
        unknown_threads = set(configured.thread_ids) - known_threads
        unknown_projects = set(configured.project_ids) - known_projects
        if unknown_threads or unknown_projects:
            raise PipelineError(
                f"scope {key} selects unknown threads/projects: "
                f"threads={sorted(unknown_threads)}, projects={sorted(unknown_projects)}"
            )
        selected = [
            entry["threadKey"]
            for entry in members
            if entry["threadId"] in configured.thread_ids
            or entry.get("membership", {}).get("sourceProjectId") in configured.project_ids
        ]
        if not configured.thread_ids and not configured.project_ids:
            raise PipelineError(f"scope {key} must select project_ids or thread_ids")
        definitions[f"work:{key}"] = (configured.title, "work", selected, tuple(configured.repository_roots))
    scopes: list[Scope] = []
    for identity, (title, kind, keys, roots) in sorted(definitions.items()):
        if not keys:
            continue
        storage_key = str(uuid5(NAMESPACE_URL, "codex-scope:" + identity))
        notes = tuple(
            config.data_root / entry["noteRef"].removeprefix("data:/")
            for entry in members
            if entry["threadKey"] in keys and entry.get("noteRef")
        )
        context_project = Project(
            project_id=identity,
            title=title,
            current_root=roots[0] if roots else config.data_root / "scopes" / storage_key,
            context_path=config.data_root / "scopes" / storage_key,
            state_directory=config.state_root / "scopes" / storage_key,
            note_paths=notes,
            data_directory=config.data_root,
            repository_roots=roots,
        )
        scopes.append(Scope(identity, title, kind, tuple(sorted(set(keys))), context_project))
    return scopes
