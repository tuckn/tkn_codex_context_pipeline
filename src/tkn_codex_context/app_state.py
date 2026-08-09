"""Fail-closed reader for the local Codex app project state."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .thread_notes import PipelineError


class CodexAppProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    root_paths: list[Path] = Field(alias="rootPaths", min_length=1)
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime | None = Field(default=None, alias="updatedAt")

    @field_validator("id")
    @classmethod
    def safe_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        stripped = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", stripped):
            raise ValueError("must be a safe path segment")
        return stripped

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class ThreadAssignment(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_kind: str = Field(alias="projectKind")
    project_id: str = Field(alias="projectId")
    cwd: Path | None = None


class CodexAppState(BaseModel):
    model_config = ConfigDict(extra="allow")

    projects: tuple[CodexAppProject, ...]
    assignments: dict[str, ThreadAssignment]
    projectless_thread_ids: frozenset[str]


def load_codex_app_state(path: Path) -> CodexAppState:
    if not path.is_file():
        raise PipelineError(f"Codex app state not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read Codex app state: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PipelineError("Codex app state must be a JSON object")
    raw_projects = raw.get("local-projects")
    raw_assignments = raw.get("thread-project-assignments")
    raw_projectless = raw.get("projectless-thread-ids", [])
    if not isinstance(raw_projects, dict):
        raise PipelineError("Codex app state local-projects must be an object")
    if not isinstance(raw_assignments, dict):
        raise PipelineError("Codex app state thread-project-assignments must be an object")
    if not isinstance(raw_projectless, list) or not all(isinstance(item, str) for item in raw_projectless):
        raise PipelineError("Codex app state projectless-thread-ids must be a string array")
    projects: list[CodexAppProject] = []
    try:
        for key, value in raw_projects.items():
            if not isinstance(value, dict):
                raise PipelineError(f"Codex app project is not an object: {key}")
            project = CodexAppProject.model_validate(value)
            if project.id != key:
                raise PipelineError(f"Codex app project key/id mismatch: {key}")
            projects.append(project)
        assignments = {
            str(thread_id): ThreadAssignment.model_validate(value) for thread_id, value in raw_assignments.items()
        }
    except ValidationError as exc:
        raise PipelineError(f"unsupported Codex app state format: {exc}") from exc
    project_ids = {item.id for item in projects}
    for thread_id, assignment in assignments.items():
        if assignment.project_kind == "local" and assignment.project_id not in project_ids:
            raise PipelineError(f"thread assignment {thread_id} refers to an unknown local Project")
    return CodexAppState(
        projects=tuple(sorted(projects, key=lambda item: item.id)),
        assignments=assignments,
        projectless_thread_ids=frozenset(raw_projectless),
    )
