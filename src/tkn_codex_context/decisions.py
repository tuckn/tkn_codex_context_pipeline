"""Distill durable decision records from generated Session Notes."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .common import frontmatter
from .decision_prompting import render_decision_prompt, render_decision_repair_prompt
from .decision_resources import (
    DecisionProfile,
    DecisionTemplate,
    load_decision_profile,
    render_decision_template,
)
from .frontmatter import (
    frontmatter_list_value,
    parse_simple_frontmatter,
    replace_frontmatter_list,
    replace_frontmatter_scalar,
    split_frontmatter_lines,
    unique_ordered,
)
from .safety import has_secret_like_content
from .session_notes import (
    PipelineConfig,
    PipelineError,
    Project,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    now_local,
    session_note_metadata,
    write_run_report,
)
from .summary_resources import validate_summary_output_schema

DECISION_SCHEMA_VERSION = 2
DECISION_STATE_SCHEMA_VERSION = 1
DECISION_RENDERER_VERSION = 1
DECISION_STATE_FILENAME = "decision-build-state.json"
MAX_EXISTING_DECISION_INDEX = 200
MAX_SOURCE_NOTE_CHARACTERS = 20_000
IN_FLIGHT_GRACE_MINUTES = 9
DECISION_PROFILE = load_decision_profile()
DECISION_OUTPUT_SCHEMA = DECISION_PROFILE.schema.value
_DECISION_FILENAME = re.compile(r"^DR-([0-9]{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
_EXPLICIT_DECISION_HEADING = re.compile(r"(?m)^#{3,4} Explicit Decision\s*$")


@dataclass(frozen=True)
class DecisionSource:
    project: Project
    path: Path
    relative_path: str
    source_ref: str
    source_sha256: str
    session_id: str
    thread_id: str
    text: str


@dataclass(frozen=True)
class ExistingDecision:
    decision_id: str
    path: Path
    title: str
    status: str
    decision: str

    def as_prompt_dict(self) -> dict[str, str]:
        return {
            "decisionId": self.decision_id,
            "title": self.title,
            "status": self.status,
            "decision": self.decision[:600],
        }


class DecisionGenerator(Protocol):
    last_metrics: dict[str, int]

    def generate(
        self,
        source: DecisionSource,
        existing_decisions: Sequence[ExistingDecision],
    ) -> dict[str, Any]: ...


def decision_state_path(project: Project) -> Path:
    return (project.state_directory or project.context_path) / DECISION_STATE_FILENAME


def decisions_path(project: Project) -> Path:
    return project.context_path / "decisions"


def decision_generation_fingerprint(
    config: PipelineConfig,
    profile: DecisionProfile = DECISION_PROFILE,
) -> str:
    payload = json.dumps(
        {
            "model": config.model,
            "reasoningEffort": config.reasoning_effort,
            "profileSha256": profile.sha256,
            "rendererVersion": DECISION_RENDERER_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def empty_decision_state(project_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": DECISION_STATE_SCHEMA_VERSION,
        "projectId": project_id,
        "lastBuildAt": None,
        "sources": {},
    }


def load_decision_state(project: Project) -> dict[str, Any]:
    path = decision_state_path(project)
    if not path.exists():
        return empty_decision_state(project.project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid decision build state: {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != DECISION_STATE_SCHEMA_VERSION
        or value.get("projectId") != project.project_id
        or not isinstance(value.get("sources"), dict)
    ):
        raise PipelineError(f"unsupported decision build state: {path}")
    return value


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*\n+(.*?)(?=^## |\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def load_existing_decisions(project: Project) -> list[ExistingDecision]:
    result: list[ExistingDecision] = []
    seen: set[str] = set()
    directory = decisions_path(project)
    if not directory.exists():
        return result
    for path in sorted(directory.glob("DR-*.md")):
        match = _DECISION_FILENAME.fullmatch(path.name)
        if match is None:
            raise PipelineError(f"invalid decision filename: {path}")
        text = path.read_text(encoding="utf-8-sig")
        metadata = parse_simple_frontmatter(text)
        version = metadata.get("schemaVersion") or "1"
        if version not in {"1", "2"}:
            raise PipelineError(f"unsupported decision schemaVersion {version}: {path.name}")
        decision_id = metadata.get("decisionId") or f"DR-{match.group(1)}"
        if decision_id != f"DR-{match.group(1)}":
            raise PipelineError(f"decisionId does not match filename: {path.name}")
        if decision_id in seen:
            raise PipelineError(f"duplicate decisionId: {decision_id}")
        seen.add(decision_id)
        result.append(
            ExistingDecision(
                decision_id=decision_id,
                path=path,
                title=metadata.get("title") or path.stem,
                status=metadata.get("status") or "unknown",
                decision=_section(text, "Decision"),
            )
        )
    return result


def _session_source(path: Path, project: Project) -> DecisionSource:
    text = path.read_text(encoding="utf-8-sig")
    metadata, thread_ids, _source_refs, version = session_note_metadata(path)
    if version != "2":
        raise PipelineError(f"decision distillation requires Session Note v2: {path.name}")
    if metadata.get("type") not in {"summary", "session"}:
        raise PipelineError(f"invalid Session Note type for decision distillation: {path.name}")
    if len(thread_ids) != 1:
        raise PipelineError(f"Session Note must identify one source thread: {path.name}")
    for heading in ("# Session Note", "## Summary", "## Key Developments", "## Last Known State"):
        if heading not in text:
            raise PipelineError(f"Session Note is missing {heading}: {path.name}")
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"Session Note contains secret-like content ({', '.join(secrets)}): {path.name}")
    if len(text) > MAX_SOURCE_NOTE_CHARACTERS:
        raise PipelineError(f"Session Note exceeds the decision input size limit: {path.name}")
    relative = path.relative_to(project.context_path).as_posix()
    return DecisionSource(
        project=project,
        path=path,
        relative_path=relative,
        source_ref=f"project:/{relative}",
        source_sha256=sha256(path.read_bytes()).hexdigest(),
        session_id=metadata.get("sessionId") or path.stem,
        thread_id=thread_ids[0],
        text=text,
    )


def scan_decision_sources(
    project: Project,
    config: PipelineConfig,
    *,
    force: bool = False,
) -> tuple[list[DecisionSource], dict[str, int], list[dict[str, str]]]:
    state = load_decision_state(project)
    sources_state = state["sources"]
    generation = decision_generation_fingerprint(config)
    candidates: list[DecisionSource] = []
    failures: list[dict[str, str]] = []
    counts = {
        "total": 0,
        "eligible": 0,
        "unchanged": 0,
        "withoutExplicitDecision": 0,
        "alreadyDistilled": 0,
        "invalid": 0,
    }
    for path in sorted(project.sessions_path.glob("*.md")):
        counts["total"] += 1
        try:
            source = _session_source(path, project)
            lines, _body = split_frontmatter_lines(source.text)
        except (OSError, PipelineError, SystemExit) as exc:
            counts["invalid"] += 1
            failures.append({"sessionNote": path.name, "error": str(exc)})
            continue
        if _EXPLICIT_DECISION_HEADING.search(source.text) is None:
            counts["withoutExplicitDecision"] += 1
            continue
        source_state = sources_state.get(source.relative_path)
        if not force and isinstance(source_state, dict):
            if (
                source_state.get("sourceSha256") == source.source_sha256
                and source_state.get("generationFingerprint") == generation
            ):
                counts["unchanged"] += 1
                continue
        distilled_to = frontmatter_list_value(lines, "distilledTo")
        if (
            not force
            and not isinstance(source_state, dict)
            and any(re.match(r"^project:/decisions/DR-[0-9]{4}-", value) for value in distilled_to)
        ):
            counts["alreadyDistilled"] += 1
            continue
        counts["eligible"] += 1
        candidates.append(source)
    return candidates, counts, failures


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    return []


def validate_decision_output(
    value: Any,
    existing_decision_ids: set[str],
) -> dict[str, Any]:
    try:
        validate_summary_output_schema(value, DECISION_OUTPUT_SCHEMA)
    except ValueError as exc:
        raise PipelineError(f"Codex output does not match the decision schema: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("Codex decision output must be a JSON object")
    strings = _all_strings(value)
    if any("\n" in item or "\r" in item for item in strings):
        raise PipelineError("Codex decision output strings must not contain line breaks")
    secrets = has_secret_like_content(json.dumps(value, ensure_ascii=False))
    if secrets:
        raise PipelineError("Codex decision output contains secret-like content: " + ", ".join(secrets))
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise PipelineError("Codex decision output has invalid decisions")
    referenced: set[str] = set()
    new_slugs: set[str] = set()
    new_decisions: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise PipelineError("Codex decision output has an invalid decision item")
        disposition = item.get("disposition")
        if disposition == "existing":
            decision_id = str(item.get("existingDecisionId") or "")
            if decision_id not in existing_decision_ids:
                raise PipelineError(f"Codex output references unknown decisionId: {decision_id}")
            if decision_id in referenced:
                raise PipelineError(f"Codex output repeats existing decisionId: {decision_id}")
            referenced.add(decision_id)
            non_empty = [
                key
                for key, field_value in item.items()
                if key not in {"disposition", "existingDecisionId", "materialization"}
                and field_value != ""
                and field_value != []
            ]
            materialization = item.get("materialization")
            if isinstance(materialization, dict) and any(materialization.values()):
                non_empty.append("materialization")
            if non_empty:
                raise PipelineError(
                    "existing decision mappings must not contain new record fields: " + ", ".join(sorted(non_empty))
                )
            continue
        if disposition != "create":
            raise PipelineError("Codex decision output has invalid disposition")
        if item.get("existingDecisionId"):
            raise PipelineError("new decisions must not set existingDecisionId")
        required_strings = (
            "title",
            "fileSlug",
            "description",
            "status",
            "scope",
            "implementationStatus",
            "decision",
        )
        missing = [key for key in required_strings if not str(item.get(key) or "").strip()]
        if missing:
            raise PipelineError("new decision is missing required content: " + ", ".join(missing))
        slug = str(item["fileSlug"])
        central = " ".join(str(item["decision"]).casefold().split())
        if slug in new_slugs or central in new_decisions:
            raise PipelineError("Codex output repeats a new central decision")
        new_slugs.add(slug)
        new_decisions.add(central)
    return value


class CodexDecisionGenerator:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        observer: Callable[[dict[str, Any]], None] | None = None,
        profile: DecisionProfile = DECISION_PROFILE,
    ) -> None:
        self.config = config
        self.sleeper = sleeper
        self.observer = observer
        self.profile = profile
        self.deadline: datetime | None = None
        self.last_metrics: dict[str, int] = {}

    def set_deadline(self, deadline: datetime) -> None:
        self.deadline = deadline

    def _emit(self, event: dict[str, Any]) -> None:
        if self.observer:
            self.observer(event)

    def _invoke(self, prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="tkn-decision-") as directory:
            temp = Path(directory)
            schema_path = temp / "schema.json"
            output_path = temp / "output.json"
            schema_path.write_text(
                json.dumps(self.profile.schema.value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            command = [
                self.config.codex_bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.config.model,
                "-c",
                f'model_reasoning_effort="{self.config.reasoning_effort}"',
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            last_error = ""
            for attempt in range(3):
                timeout = self.config.model_timeout_seconds
                if self.deadline is not None:
                    remaining = int((self.deadline - now_local()).total_seconds())
                    if remaining <= 0:
                        raise PipelineError("decision build deadline reached during model generation")
                    timeout = min(timeout, remaining)
                self.last_metrics["modelCalls"] = self.last_metrics.get("modelCalls", 0) + 1
                if attempt:
                    self.last_metrics["transportRetries"] = self.last_metrics.get("transportRetries", 0) + 1
                self._emit(
                    {
                        "type": "model-attempt",
                        "attempt": attempt + 1,
                        "timeoutSeconds": timeout,
                    }
                )
                try:
                    completed = subprocess.run(
                        command,
                        input=prompt,
                        cwd=temp,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    last_error = f"Codex timed out after {exc.timeout} seconds"
                else:
                    if completed.returncode == 0 and output_path.is_file():
                        try:
                            value = json.loads(output_path.read_text(encoding="utf-8-sig"))
                        except json.JSONDecodeError as exc:
                            last_error = f"Codex returned invalid JSON: {exc}"
                        else:
                            if isinstance(value, dict):
                                return value
                            last_error = "Codex output was not a JSON object"
                    else:
                        stderr = completed.stderr.strip()
                        last_error = f"Codex exited with {completed.returncode}: {stderr[-2000:]}"
                if attempt < 2:
                    self.sleeper(2**attempt)
            raise PipelineError(last_error or "Codex decision generation failed")

    def generate(
        self,
        source: DecisionSource,
        existing_decisions: Sequence[ExistingDecision],
    ) -> dict[str, Any]:
        self.last_metrics = {
            "modelCalls": 0,
            "transportRetries": 0,
            "semanticRetries": 0,
        }
        prompt_index = [item.as_prompt_dict() for item in list(existing_decisions)[-MAX_EXISTING_DECISION_INDEX:]]
        current_prompt = render_decision_prompt(
            self.profile.prompt,
            project_id=source.project.project_id,
            session_ref=source.source_ref,
            session_note=source.text,
            existing_decisions=prompt_index,
        )
        existing_ids = {item.decision_id for item in existing_decisions}
        for semantic_attempt in range(2):
            value = self._invoke(current_prompt)
            try:
                return validate_decision_output(value, existing_ids)
            except PipelineError as exc:
                if semantic_attempt:
                    raise
                self.last_metrics["semanticRetries"] += 1
                current_prompt = render_decision_repair_prompt(
                    self.profile.prompt,
                    project_id=source.project.project_id,
                    session_ref=source.source_ref,
                    validation_error=str(exc),
                    draft=value,
                    existing_decisions=prompt_index,
                )
        raise PipelineError("Codex semantic validation did not produce valid decisions")


def _bullets(values: Sequence[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    return "\n".join(f"- {value}" for value in items) if items else "None."


def _labeled_value(label: str, values: Sequence[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    return f"- {label}: {' / '.join(items) if items else 'None.'}"


def render_decision(
    source: DecisionSource,
    data: dict[str, Any],
    decision_id: str,
    *,
    generated_at: str | None = None,
    config: PipelineConfig,
    template: DecisionTemplate = DECISION_PROFILE.template,
) -> str:
    timestamp = generated_at or now_iso()
    source_refs = [source.source_ref]
    related_evidence = unique_ordered([source.source_ref, *[str(value) for value in data["relatedEvidence"]]])
    fields: list[tuple[str, str | int | list[str]]] = [
        ("type", "decision"),
        ("schemaVersion", DECISION_SCHEMA_VERSION),
        ("title", str(data["title"])),
        ("description", str(data["description"])),
        ("generator", "Codex"),
        ("status", str(data["status"])),
        ("scope", str(data["scope"])),
        ("implementationStatus", str(data["implementationStatus"])),
        ("promotionStatus", "pending"),
        ("promotedTo", []),
        ("generatorModel", config.model),
        ("generatorReasoningEffort", config.reasoning_effort),
        ("promptId", DECISION_PROFILE.prompt.prompt_id),
        ("promptVersion", DECISION_PROFILE.prompt.version),
        ("outputSchemaSha256", DECISION_PROFILE.schema.sha256),
        ("templateId", template.template_id),
        ("templateVersion", template.version),
        ("rendererVersion", DECISION_RENDERER_VERSION),
        ("generatedAt", timestamp),
        ("reviewStatus", "unreviewed"),
        ("automatedValidation", "passed"),
        ("sourceSessionRefs", source_refs),
        ("sourceSessionSha256", source.source_sha256),
        ("date", timestamp),
        ("updated", timestamp),
        ("decisionId", decision_id),
    ]
    verification = "\n".join(
        (
            _labeled_value("Evidence", data["verificationEvidence"]),
            f"- Validation Date: {data['validationDate'] or 'None.'}",
        )
    )
    materialization = data["materialization"]
    materialization_text = "\n".join(
        (
            _labeled_value("Project Working Context", materialization["projectWorkingContext"]),
            _labeled_value("Repository Documentation", materialization["repositoryDocumentation"]),
            _labeled_value("Global Context", materialization["globalContext"]),
            _labeled_value("Skill / Automation", materialization["skillAutomation"]),
            _labeled_value("Follow-up", materialization["followUp"]),
        )
    )
    supersession = "\n".join(
        (
            _labeled_value("Supersedes", data["supersedes"]),
            _labeled_value("Superseded By", data["supersededBy"]),
        )
    )
    return render_decision_template(
        template,
        {
            "frontmatter": frontmatter(fields),
            "title": f"{decision_id}: {data['title']}",
            "context": _bullets(data["context"]),
            "decision": str(data["decision"]).strip(),
            "rationale": _bullets(data["rationale"]),
            "benefits": _bullets(data["benefits"]),
            "costs_and_risks": _bullets(data["costsAndRisks"]),
            "alternatives_considered": _bullets(data["alternativesConsidered"]),
            "applies_when": _bullets(data["appliesWhen"]),
            "does_not_apply_when": _bullets(data["doesNotApplyWhen"]),
            "reusable_principle": _bullets(data["reusablePrinciples"]),
            "project_specific_details": _bullets(data["projectSpecificDetails"]),
            "verification": verification,
            "related_evidence": _bullets(related_evidence),
            "materialization": materialization_text,
            "supersession": supersession,
        },
    )


def validate_decision_record(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PipelineError(f"decision record not found: {path}")
    match = _DECISION_FILENAME.fullmatch(path.name)
    if match is None:
        raise PipelineError(f"invalid decision filename: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    metadata = parse_simple_frontmatter(text)
    required = {
        "type": "decision",
        "schemaVersion": str(DECISION_SCHEMA_VERSION),
        "generator": "Codex",
        "automatedValidation": "passed",
    }
    for key, expected in required.items():
        if metadata.get(key) != expected:
            raise PipelineError(f"decision record has invalid {key}: {path}")
    if metadata.get("reviewStatus") not in {"unreviewed", "reviewed"}:
        raise PipelineError(f"decision record has invalid reviewStatus: {path}")
    decision_id = f"DR-{match.group(1)}"
    if metadata.get("decisionId") != decision_id:
        raise PipelineError(f"decisionId does not match filename: {path.name}")
    if metadata.get("status") not in {
        "Proposed",
        "Accepted",
        "Rejected",
        "Deprecated",
        "Superseded",
    }:
        raise PipelineError(f"decision record has invalid status: {path}")
    if metadata.get("scope") not in {"project", "global", "user", "mixed"}:
        raise PipelineError(f"decision record has invalid scope: {path}")
    if metadata.get("implementationStatus") not in {
        "not-started",
        "partial",
        "implemented",
        "verified",
    }:
        raise PipelineError(f"decision record has invalid implementationStatus: {path}")
    lines, _body = split_frontmatter_lines(text)
    if not frontmatter_list_value(lines, "sourceSessionRefs"):
        raise PipelineError(f"decision record has no sourceSessionRefs: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("sourceSessionSha256") or ""):
        raise PipelineError(f"decision record has invalid sourceSessionSha256: {path}")
    title = metadata.get("title") or ""
    if f"# {decision_id}: {title}" not in text:
        raise PipelineError(f"decision record title does not match frontmatter: {path}")
    headings = (
        "## Context",
        "## Decision",
        "## Rationale",
        "## Consequences",
        "### Benefits",
        "### Costs And Risks",
        "## Alternatives Considered",
        "## Applicability",
        "### Applies When",
        "### Does Not Apply When",
        "### Reusable Principle",
        "### Project-Specific Details",
        "## Verification",
        "## Related Evidence",
        "## Materialization",
        "## Supersession",
    )
    missing = [heading for heading in headings if heading not in text]
    if missing:
        raise PipelineError(f"decision record is missing headings ({', '.join(missing)}): {path}")
    for label in (
        "Project Working Context",
        "Repository Documentation",
        "Global Context",
        "Skill / Automation",
        "Follow-up",
        "Supersedes",
        "Superseded By",
    ):
        if f"- {label}:" not in text:
            raise PipelineError(f"decision record is missing {label}: {path}")
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"decision record contains secret-like content ({', '.join(secrets)}): {path}")
    return {
        "valid": True,
        "path": str(path.absolute()),
        "schemaVersion": DECISION_SCHEMA_VERSION,
        "decisionId": decision_id,
        "status": metadata["status"],
        "implementationStatus": metadata["implementationStatus"],
    }


def _update_session_distillation(path: Path, decision_refs: Sequence[str]) -> str:
    original = path.read_text(encoding="utf-8-sig")
    lines, body = split_frontmatter_lines(original)
    existing = frontmatter_list_value(lines, "distilledTo")
    merged = unique_ordered([*existing, *decision_refs])
    updated_lines = replace_frontmatter_scalar(lines, "distillationStatus", "partial")
    updated_lines = replace_frontmatter_list(updated_lines, "distilledTo", merged)
    updated_lines = replace_frontmatter_scalar(updated_lines, "updated", now_iso())
    rendered = "".join(updated_lines) + body
    atomic_write_text(path, rendered)
    return sha256(path.read_bytes()).hexdigest()


def _next_decision_number(existing: Sequence[ExistingDecision]) -> int:
    numbers = [int(item.decision_id.removeprefix("DR-")) for item in existing]
    return max(numbers, default=0) + 1


def _commit_source(
    source: DecisionSource,
    config: PipelineConfig,
    state: dict[str, Any],
    existing: Sequence[ExistingDecision],
    output: dict[str, Any],
) -> tuple[list[ExistingDecision], list[str], list[str]]:
    if sha256(source.path.read_bytes()).hexdigest() != source.source_sha256:
        raise PipelineError(f"Session Note changed during decision generation: {source.path.name}")
    timestamp = now_iso()
    next_number = _next_decision_number(existing)
    created: list[ExistingDecision] = []
    created_text: dict[Path, str] = {}
    referenced: list[str] = []
    for item in output["decisions"]:
        if item["disposition"] == "existing":
            referenced.append(str(item["existingDecisionId"]))
            continue
        decision_id = f"DR-{next_number:04d}"
        next_number += 1
        filename = f"{decision_id}-{item['fileSlug']}.md"
        path = decisions_path(source.project) / filename
        if path.exists():
            raise PipelineError(f"refusing to overwrite existing decision record: {path}")
        rendered = render_decision(
            source,
            item,
            decision_id,
            generated_at=timestamp,
            config=config,
        )
        created_text[path] = rendered
        created.append(
            ExistingDecision(
                decision_id=decision_id,
                path=path,
                title=str(item["title"]),
                status=str(item["status"]),
                decision=str(item["decision"]),
            )
        )
    decision_ids = unique_ordered([*referenced, *[item.decision_id for item in created]])
    decision_refs = [
        f"project:/decisions/{item.path.name}"
        for item in [
            *[entry for entry in existing if entry.decision_id in referenced],
            *created,
        ]
    ]
    state_path = decision_state_path(source.project)
    old_session = source.path.read_bytes()
    old_state = state_path.read_bytes() if state_path.exists() else None
    previous_state = deepcopy(state)
    written_paths: list[Path] = []
    try:
        for path, rendered in created_text.items():
            atomic_write_text(path, rendered)
            written_paths.append(path)
            validate_decision_record(path)
        updated_source_hash = source.source_sha256
        if decision_refs:
            updated_source_hash = _update_session_distillation(source.path, decision_refs)
        state["sources"][source.relative_path] = {
            "sourceSha256": updated_source_hash,
            "generationFingerprint": decision_generation_fingerprint(config),
            "decisionIds": decision_ids,
            "noAction": not decision_ids,
            "processedAt": timestamp,
        }
        state["lastBuildAt"] = timestamp
        atomic_write_json(state_path, state)
    except Exception:
        for path in written_paths:
            if path.exists():
                path.unlink()
        atomic_write_text(source.path, old_session.decode("utf-8-sig"))
        if old_state is None:
            if state_path.exists():
                state_path.unlink()
        else:
            atomic_write_text(state_path, old_state.decode("utf-8-sig"))
        state.clear()
        state.update(previous_state)
        raise
    return created, referenced, decision_refs


def execute_decision_build(
    config: PipelineConfig,
    project: Project,
    *,
    generator: DecisionGenerator | None,
    write: bool = False,
    force: bool = False,
    limit: int | None = None,
    cache_root: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    started = now_local()
    start_deadline = started + timedelta(minutes=config.runtime_minutes)
    hard_deadline = start_deadline + timedelta(minutes=IN_FLIGHT_GRACE_MINUTES)
    candidates, scan, scan_failures = scan_decision_sources(project, config, force=force)
    if limit is not None:
        if limit <= 0:
            raise PipelineError("limit must be positive")
        candidates = candidates[:limit]
    existing = load_existing_decisions(project)
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": None,
        "mode": "decision-build",
        "projectId": project.project_id,
        "dryRun": not write,
        "force": force,
        "scan": scan,
        "existingDecisionCount": len(existing),
        "selectedCount": len(candidates),
        "processed": [],
        "created": [],
        "referencedExisting": [],
        "noAction": [],
        "failed": list(scan_failures),
        "deferred": [],
    }
    if not write:
        report["selected"] = [
            {
                "sessionNote": source.relative_path,
                "sessionId": source.session_id,
                "threadId": source.thread_id,
                "sourceSha256": source.source_sha256,
            }
            for source in candidates
        ]
        report["finishedAt"] = now_iso()
        return report, None
    if generator is None:
        raise PipelineError("a decision generator is required for a write run")
    if hasattr(generator, "set_deadline"):
        generator.set_deadline(hard_deadline)
    state = load_decision_state(project)
    for index, source in enumerate(candidates):
        if now_local() >= start_deadline:
            report["deferred"].extend(
                {
                    "sessionNote": item.relative_path,
                    "reason": "runtime-deadline",
                }
                for item in candidates[index:]
            )
            break
        source_started = time.monotonic()
        if progress:
            progress(
                {
                    "type": "decision-source-start",
                    "index": index + 1,
                    "total": len(candidates),
                    "sessionNote": source.relative_path,
                }
            )
        try:
            output = generator.generate(source, existing)
            validate_decision_output(output, {item.decision_id for item in existing})
            created, referenced, refs = _commit_source(
                source,
                config,
                state,
                existing,
                output,
            )
        except Exception as exc:  # Per-source isolation is intentional.
            report["failed"].append({"sessionNote": source.relative_path, "error": str(exc)})
            if progress:
                progress(
                    {
                        "type": "decision-source-failed",
                        "index": index + 1,
                        "total": len(candidates),
                        "sessionNote": source.relative_path,
                        "error": str(exc),
                    }
                )
            continue
        existing.extend(created)
        duration = round(time.monotonic() - source_started, 3)
        metrics = deepcopy(getattr(generator, "last_metrics", {}))
        created_values = [
            {
                "decisionId": item.decision_id,
                "decisionRecord": item.path.relative_to(project.context_path).as_posix(),
            }
            for item in created
        ]
        report["created"].extend(created_values)
        report["referencedExisting"].extend(referenced)
        if not created and not referenced:
            report["noAction"].append(source.relative_path)
        report["processed"].append(
            {
                "sessionNote": source.relative_path,
                "created": created_values,
                "referencedExisting": referenced,
                "distilledTo": refs,
                "sourceLimitations": output["sourceLimitations"],
                "durationSeconds": duration,
                **metrics,
            }
        )
        if progress:
            progress(
                {
                    "type": "decision-source-complete",
                    "index": index + 1,
                    "total": len(candidates),
                    "sessionNote": source.relative_path,
                    "createdCount": len(created),
                    "referencedCount": len(referenced),
                    "durationSeconds": duration,
                    **metrics,
                }
            )
    report["finishedAt"] = now_iso()
    report_path = write_run_report(cache_root or project.context_path, report)
    return report, report_path
