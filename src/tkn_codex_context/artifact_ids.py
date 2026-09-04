"""Assign stable UUIDv4 identities to application-owned Markdown artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .frontmatter import (
    canonical_uuid4,
    frontmatter_key_block,
    parse_simple_frontmatter,
    require_supported_artifact_schema,
    split_frontmatter_lines,
)
from .thread_notes import PipelineError, Project, atomic_write_bytes, now_iso


@dataclass(frozen=True)
class ArtifactIdentitySpec:
    label: str
    artifact_type: str
    current_schema: str


@dataclass(frozen=True)
class ArtifactIdentityTarget:
    project: Project
    path: Path
    spec: ArtifactIdentitySpec


THREAD_NOTE_IDENTITY = ArtifactIdentitySpec("thread note", "threadNote", "4")
DECISION_IDENTITY = ArtifactIdentitySpec("decision record", "decision", "5")
WORKING_CONTEXT_IDENTITY = ArtifactIdentitySpec("working context", "workingContext", "4")


def _targets(project: Project) -> list[ArtifactIdentityTarget]:
    result = [
        ArtifactIdentityTarget(project, path, THREAD_NOTE_IDENTITY)
        for path in sorted(project.thread_notes_path.glob("*.md"))
    ]
    result.extend(
        ArtifactIdentityTarget(project, path, DECISION_IDENTITY)
        for path in sorted((project.context_path / "decisions").glob("DR-*.md"))
    )
    working_context = project.context_path / "working-context.md"
    if working_context.is_file():
        result.append(ArtifactIdentityTarget(project, working_context, WORKING_CONTEXT_IDENTITY))
    return result


def _newline(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
    return "\n"


def _replace_scalar(lines: list[str], key: str, value: str) -> list[str]:
    newline = _newline(lines)
    replacement = [f"{key}: {value}{newline}"]
    block = frontmatter_key_block(lines, key)
    if block is not None:
        start, end = block
        return lines[:start] + replacement + lines[end:]
    schema_block = frontmatter_key_block(lines, "schemaVersion")
    type_block = frontmatter_key_block(lines, "type")
    insert_at = schema_block[1] if schema_block is not None else type_block[1] if type_block is not None else 1
    return lines[:insert_at] + replacement + lines[insert_at:]


def _updated_bytes(target: ArtifactIdentityTarget, original: bytes, note_id: str) -> tuple[bytes, str, str]:
    had_bom = original.startswith(b"\xef\xbb\xbf")
    try:
        text = original.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PipelineError(f"artifact is not UTF-8: {target.path}: {exc}") from exc
    try:
        lines, body = split_frontmatter_lines(text)
    except SystemExit as exc:
        raise PipelineError(f"invalid artifact frontmatter: {target.path}") from exc
    metadata = parse_simple_frontmatter(text)
    if metadata.get("type") != target.spec.artifact_type:
        raise PipelineError(f"invalid {target.spec.label} type: {target.path}")
    try:
        version = require_supported_artifact_schema(metadata, target.spec.label)
    except SystemExit as exc:
        raise PipelineError(str(exc)) from exc
    updated = _replace_scalar(lines, "id", note_id)
    rendered = "".join(updated) + body
    if split_frontmatter_lines(rendered)[1] != body:
        raise PipelineError(f"metadata-only migration changed artifact body: {target.path}")
    encoded = rendered.encode("utf-8")
    if had_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded, version, version


def _validate_current(target: ArtifactIdentityTarget) -> None:
    if target.spec is THREAD_NOTE_IDENTITY:
        from .thread_notes import validate_thread_note

        validate_thread_note(target.path)
    elif target.spec is DECISION_IDENTITY:
        from .decisions import validate_decision_record

        validate_decision_record(target.path)
    else:
        from .working_context import validate_working_context

        validate_working_context(target.path)


def migrate_artifact_ids(
    projects: list[Project],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Plan or transactionally assign UUIDv4 IDs without changing artifact bodies."""

    targets = [target for project in projects for target in _targets(project)]
    seen_ids: dict[str, Path] = {}
    plans: list[tuple[ArtifactIdentityTarget, bytes, str | None, str, str]] = []
    unchanged: list[dict[str, str]] = []
    for target in targets:
        try:
            original = target.path.read_bytes()
            text = original.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise PipelineError(f"cannot read artifact: {target.path}: {exc}") from exc
        try:
            split_frontmatter_lines(text)
        except SystemExit as exc:
            raise PipelineError(f"invalid artifact frontmatter: {target.path}") from exc
        metadata = parse_simple_frontmatter(text)
        if metadata.get("type") != target.spec.artifact_type:
            raise PipelineError(f"invalid {target.spec.label} type: {target.path}")
        existing_id = metadata.get("id") or None
        if existing_id is not None:
            try:
                canonical_uuid4(existing_id)
            except ValueError as exc:
                raise PipelineError(f"artifact has invalid id: {target.path}") from exc
            prior = seen_ids.setdefault(existing_id, target.path)
            if prior != target.path:
                raise PipelineError(f"duplicate artifact id {existing_id}: {prior} and {target.path}")
        try:
            current_version = require_supported_artifact_schema(metadata, target.spec.label)
        except SystemExit as exc:
            raise PipelineError(str(exc)) from exc
        if existing_id is not None:
            unchanged.append(
                {
                    "projectId": target.project.project_id,
                    "artifact": str(target.path),
                    "id": existing_id,
                    "schemaVersion": current_version,
                }
            )
            continue
        assigned_id = existing_id or (str(uuid4()) if not dry_run else "")
        if assigned_id:
            prior = seen_ids.setdefault(assigned_id, target.path)
            if prior != target.path:
                raise PipelineError(f"duplicate artifact id {assigned_id}: {prior} and {target.path}")
        updated = original
        new_version = current_version
        if not dry_run:
            updated, current_version, new_version = _updated_bytes(target, original, assigned_id)
        plans.append((target, updated, existing_id, current_version, new_version))

    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": now_iso(),
        "finishedAt": None,
        "mode": "artifact-id-migration",
        "dryRun": dry_run,
        "projectCount": len(projects),
        "artifactCount": len(targets),
        "plannedCount": len(plans),
        "assignedCount": sum(existing_id is None for _target, _data, existing_id, _old, _new in plans),
        "schemaUpgradeCount": sum(old != new for _target, _data, _id, old, new in plans),
        "unchangedCount": len(unchanged),
        "planned": [
            {
                "projectId": target.project.project_id,
                "artifact": str(target.path),
                "assignId": existing_id is None,
                "fromSchemaVersion": old,
                "toSchemaVersion": new,
            }
            for target, _data, existing_id, old, new in plans
        ],
        "unchanged": unchanged,
    }
    if dry_run:
        report["finishedAt"] = now_iso()
        return report

    originals = {target.path: target.path.read_bytes() for target, _data, _id, _old, _new in plans}
    written: list[ArtifactIdentityTarget] = []
    try:
        for target, updated, _existing_id, _old, new in plans:
            atomic_write_bytes(target.path, updated)
            written.append(target)
            metadata = parse_simple_frontmatter(target.path.read_text(encoding="utf-8-sig"))
            canonical_uuid4(metadata.get("id") or "")
            if new == target.spec.current_schema:
                _validate_current(target)
    except Exception:
        for target in reversed(written):
            atomic_write_bytes(target.path, originals[target.path])
        raise
    report.pop("planned")
    report["migrated"] = [
        {
            "projectId": target.project.project_id,
            "artifact": str(target.path),
            "id": parse_simple_frontmatter(
                target.path.read_text(encoding="utf-8-sig")
            )["id"],
            "fromSchemaVersion": old,
            "toSchemaVersion": new,
        }
        for target, _data, _existing_id, old, new in plans
    ]
    report["finishedAt"] = now_iso()
    return report
