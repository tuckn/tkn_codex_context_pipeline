"""Build a source-backed Working Context v3 dashboard for one Project."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .common import frontmatter
from .frontmatter import frontmatter_list_value, parse_simple_frontmatter, split_frontmatter_lines
from .safety import has_secret_like_content
from .summary_resources import validate_summary_output_schema
from .thread_notes import (
    PipelineConfig,
    PipelineError,
    Project,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    now_local,
    validate_thread_note,
    write_run_report,
)
from .working_context_prompting import (
    render_working_context_merge_prompt,
    render_working_context_prompt,
    render_working_context_repair_prompt,
)
from .working_context_resources import (
    WorkingContextProfile,
    WorkingContextTemplate,
    load_working_context_profile,
    render_working_context_template,
)

WORKING_CONTEXT_SCHEMA_VERSION = 3
WORKING_CONTEXT_STATE_SCHEMA_VERSION = 1
WORKING_CONTEXT_RENDERER_VERSION = 1
WORKING_CONTEXT_FILENAME = "working-context.md"
WORKING_CONTEXT_STATE_FILENAME = "working-context-build-state.json"
MAX_SOURCE_CHARACTERS = 30_000
MAX_BATCH_SOURCES = 40
MAX_BATCH_CHARACTERS = 180_000
IN_FLIGHT_GRACE_MINUTES = 9
WORKING_CONTEXT_PROFILE = load_working_context_profile()
WORKING_CONTEXT_OUTPUT_SCHEMA = WORKING_CONTEXT_PROFILE.schema.value
_ALLOWED_HEADINGS = (
    "Project Overview",
    "Current Truth",
    "Current Outcome",
    "Active Work",
    "Risks And Constraints",
    "Effective Decisions",
    "Semantic Context",
    "Key Evidence",
    "Resumption",
    "Source Limitations",
)
_REPOSITORY_SOURCE_NAMES = (
    "AGENTS.md",
    "README.md",
    "README_ja.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
)


@dataclass(frozen=True)
class WorkingContextSource:
    kind: str
    source_ref: str
    sha256: str
    text: str

    def as_prompt_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "sourceRef": self.source_ref,
            "sha256": self.sha256,
            "content": self.text,
        }


class WorkingContextGenerator(Protocol):
    last_metrics: dict[str, int]

    def generate(
        self,
        project: Project,
        source_batches: Sequence[Sequence[WorkingContextSource]],
    ) -> dict[str, Any]: ...


def working_context_path(project: Project) -> Path:
    return project.context_path / WORKING_CONTEXT_FILENAME


def working_context_state_path(project: Project) -> Path:
    return (project.state_directory or project.context_path) / WORKING_CONTEXT_STATE_FILENAME


def working_context_generation_fingerprint(
    config: PipelineConfig,
    profile: WorkingContextProfile = WORKING_CONTEXT_PROFILE,
) -> str:
    payload = json.dumps(
        {
            "model": config.model,
            "reasoningEffort": config.reasoning_effort,
            "profileSha256": profile.sha256,
            "rendererVersion": WORKING_CONTEXT_RENDERER_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def empty_working_context_state(project_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": WORKING_CONTEXT_STATE_SCHEMA_VERSION,
        "projectId": project_id,
        "lastBuildAt": None,
        "sourceSetSha256": None,
        "generationFingerprint": None,
        "workingContextSha256": None,
        "sources": {},
    }


def load_working_context_state(project: Project) -> dict[str, Any]:
    path = working_context_state_path(project)
    if not path.exists():
        return empty_working_context_state(project.project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid Working Context build state: {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != WORKING_CONTEXT_STATE_SCHEMA_VERSION
        or value.get("projectId") != project.project_id
        or not isinstance(value.get("sources"), dict)
    ):
        raise PipelineError(f"unsupported Working Context build state: {path}")
    return value


def _source_set_sha256(sources: Sequence[WorkingContextSource]) -> str:
    payload = json.dumps(
        [
            {"sourceRef": source.source_ref, "sha256": source.sha256}
            for source in sorted(sources, key=lambda item: item.source_ref)
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _read_bounded(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    if len(text) > MAX_SOURCE_CHARACTERS:
        raise PipelineError(f"Working Context source exceeds the per-file size limit: {path}")
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"Working Context source contains secret-like content ({', '.join(secrets)}): {path}")
    return text


def _artifact_sources(
    project: Project,
) -> tuple[list[WorkingContextSource], list[dict[str, str]]]:
    sources: list[WorkingContextSource] = []
    failures: list[dict[str, str]] = []
    for path in sorted(project.thread_notes_path.glob("*.md")):
        try:
            validate_thread_note(path)
            text = _read_bounded(path)
        except (OSError, PipelineError, SystemExit) as exc:
            failures.append({"source": path.name, "error": str(exc)})
            continue
        relative = path.relative_to(project.context_path).as_posix()
        sources.append(
            WorkingContextSource(
                kind="threadNote",
                source_ref=f"project:/{relative}",
                sha256=sha256(path.read_bytes()).hexdigest(),
                text=text,
            )
        )
    decisions_directory = project.context_path / "decisions"
    for path in sorted(decisions_directory.glob("DR-*.md")):
        try:
            from .decisions import validate_decision_record

            validate_decision_record(path)
            text = _read_bounded(path)
        except (OSError, PipelineError, SystemExit) as exc:
            failures.append({"source": path.name, "error": str(exc)})
            continue
        relative = path.relative_to(project.context_path).as_posix()
        sources.append(
            WorkingContextSource(
                kind="decisionRecord",
                source_ref=f"project:/{relative}",
                sha256=sha256(path.read_bytes()).hexdigest(),
                text=text,
            )
        )
    return sources, failures


def _git_snapshot(root: Path) -> str:
    commands = (
        ("branch", ["git", "-C", str(root), "branch", "--show-current"]),
        ("status", ["git", "-C", str(root), "status", "--short", "--branch"]),
        ("head", ["git", "-C", str(root), "log", "-1", "--format=%H%n%cI%n%s"]),
    )
    sections: list[str] = []
    for label, command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode:
            return ""
        sections.append(f"[{label}]\n{completed.stdout.strip() or '(empty)'}")
    return "\n\n".join(sections)


def _repository_sources(project: Project) -> list[WorkingContextSource]:
    root = project.current_root
    if not root.is_dir():
        return []
    sources: list[WorkingContextSource] = []
    for name in _REPOSITORY_SOURCE_NAMES:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
            secrets = has_secret_like_content(text)
            if secrets:
                raise PipelineError(
                    f"repository source contains secret-like content ({', '.join(secrets)}): {path}"
                )
            if len(text) > MAX_SOURCE_CHARACTERS:
                text = text[:MAX_SOURCE_CHARACTERS].rstrip() + "\n\n[TRUNCATED BY WORKING CONTEXT INPUT LIMIT]\n"
        except (OSError, PipelineError):
            continue
        sources.append(
            WorkingContextSource(
                kind="repositoryFile",
                source_ref=f"repo:/{name}",
                sha256=sha256(path.read_bytes()).hexdigest(),
                text=text,
            )
        )
    snapshot = _git_snapshot(root)
    if snapshot:
        sources.append(
            WorkingContextSource(
                kind="gitSnapshot",
                source_ref="repo:/git-state",
                sha256=sha256(snapshot.encode("utf-8")).hexdigest(),
                text=snapshot,
            )
        )
    return sources


def collect_working_context_sources(
    project: Project,
) -> tuple[list[WorkingContextSource], list[dict[str, str]]]:
    artifacts, failures = _artifact_sources(project)
    sources = [*artifacts, *_repository_sources(project)]
    refs = [source.source_ref for source in sources]
    if len(refs) != len(set(refs)):
        raise PipelineError("Working Context input contains duplicate source references")
    if not sources:
        failures.append({"source": project.project_id, "error": "no Working Context sources were found"})
    return sources, failures


def working_context_source_batches(
    sources: Sequence[WorkingContextSource],
) -> list[list[WorkingContextSource]]:
    batches: list[list[WorkingContextSource]] = []
    current: list[WorkingContextSource] = []
    characters = 0
    for source in sources:
        if current and (
            len(current) >= MAX_BATCH_SOURCES
            or characters + len(source.text) > MAX_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = []
            characters = 0
        current.append(source)
        characters += len(source.text)
    if current:
        batches.append(current)
    return batches


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    return []


def _item_source_refs(value: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for field in (
        "projectOverview",
        "currentTruth",
        "currentOutcome",
        "activeWork",
        "risksAndConstraints",
        "semanticGlossary",
        "taxonomyItems",
        "taxonomyRelations",
        "resumption",
    ):
        for item in value[field]:
            refs.extend(str(ref) for ref in item["sourceRefs"])
    refs.extend(str(item["decisionRef"]) for item in value["effectiveDecisions"])
    refs.extend(str(item["ref"]) for item in value["keyEvidence"])
    return refs


def validate_working_context_output(
    value: Any,
    source_refs: set[str],
    decision_refs: set[str],
) -> dict[str, Any]:
    try:
        validate_summary_output_schema(value, WORKING_CONTEXT_OUTPUT_SCHEMA)
    except ValueError as exc:
        raise PipelineError(f"Codex output does not match the Working Context schema: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("Codex Working Context output must be a JSON object")
    strings = _all_strings(value)
    if any("\n" in item or "\r" in item for item in strings):
        raise PipelineError("Codex Working Context output strings must not contain line breaks")
    secrets = has_secret_like_content(json.dumps(value, ensure_ascii=False))
    if secrets:
        raise PipelineError("Codex Working Context output contains secret-like content: " + ", ".join(secrets))
    unknown = sorted(set(_item_source_refs(value)) - source_refs)
    if unknown:
        raise PipelineError("Codex Working Context output references unknown sources: " + ", ".join(unknown))
    invalid_decisions = sorted(
        str(item["decisionRef"])
        for item in value["effectiveDecisions"]
        if item["decisionRef"] not in decision_refs
    )
    if invalid_decisions:
        raise PipelineError(
            "effectiveDecisions must reference supplied Decision Records: " + ", ".join(invalid_decisions)
        )
    if bool(value["blocked"]) != (value["projectStatus"] == "blocked"):
        raise PipelineError("blocked must be true exactly when projectStatus is blocked")
    if value["blocked"] and not str(value["mainBlocker"]).strip():
        raise PipelineError("a blocked Working Context must identify mainBlocker")
    if not value["blocked"] and value["mainBlocker"]:
        raise PipelineError("an unblocked Working Context must leave mainBlocker empty")
    labels = [str(item["label"]) for item in value["taxonomyItems"]]
    if len(labels) != len(set(labels)):
        raise PipelineError("taxonomyItems must have unique labels")
    label_set = set(labels)
    invalid_parents = sorted(
        str(item["parent"])
        for item in value["taxonomyItems"]
        if item["parent"] and item["parent"] not in label_set
    )
    if invalid_parents:
        raise PipelineError("taxonomyItems reference unknown parents: " + ", ".join(invalid_parents))
    invalid_relations = [
        f"{item['subject']} -> {item['object']}"
        for item in value["taxonomyRelations"]
        if item["subject"] not in label_set or item["object"] not in label_set
    ]
    if invalid_relations:
        raise PipelineError("taxonomyRelations reference unknown labels: " + ", ".join(invalid_relations))
    for field, key in (
        ("projectOverview", "text"),
        ("currentTruth", "text"),
        ("currentOutcome", "text"),
        ("activeWork", "text"),
        ("risksAndConstraints", "text"),
        ("semanticGlossary", "term"),
        ("resumption", "text"),
    ):
        normalized = [" ".join(str(item[key]).casefold().split()) for item in value[field]]
        if len(normalized) != len(set(normalized)):
            raise PipelineError(f"Codex Working Context output repeats {field} items")
    return value


class CodexWorkingContextGenerator:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        observer: Callable[[dict[str, Any]], None] | None = None,
        profile: WorkingContextProfile = WORKING_CONTEXT_PROFILE,
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
        with tempfile.TemporaryDirectory(prefix="tkn-working-context-") as directory:
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
                        raise PipelineError("Working Context build deadline reached during model generation")
                    timeout = min(timeout, remaining)
                self.last_metrics["modelCalls"] = self.last_metrics.get("modelCalls", 0) + 1
                if attempt:
                    self.last_metrics["transportRetries"] = self.last_metrics.get("transportRetries", 0) + 1
                self._emit({"type": "model-attempt", "attempt": attempt + 1, "timeoutSeconds": timeout})
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
                        last_error = f"Codex exited with {completed.returncode}: {completed.stderr.strip()[-2000:]}"
                if attempt < 2:
                    self.sleeper(2**attempt)
            raise PipelineError(last_error or "Codex Working Context generation failed")

    def _validated_invoke(
        self,
        prompt: str,
        *,
        project_id: str,
        source_refs: set[str],
        decision_refs: set[str],
    ) -> dict[str, Any]:
        current_prompt = prompt
        for semantic_attempt in range(2):
            value = self._invoke(current_prompt)
            try:
                return validate_working_context_output(value, source_refs, decision_refs)
            except PipelineError as exc:
                if semantic_attempt:
                    raise
                self.last_metrics["semanticRetries"] = self.last_metrics.get("semanticRetries", 0) + 1
                current_prompt = render_working_context_repair_prompt(
                    self.profile.prompt,
                    project_id=project_id,
                    source_refs=sorted(source_refs),
                    validation_error=str(exc),
                    draft=value,
                )
        raise PipelineError("Codex semantic validation did not produce a valid Working Context")

    def generate(
        self,
        project: Project,
        source_batches: Sequence[Sequence[WorkingContextSource]],
    ) -> dict[str, Any]:
        if not source_batches:
            raise PipelineError("Working Context generation requires at least one source")
        self.last_metrics = {"modelCalls": 0, "transportRetries": 0, "semanticRetries": 0}
        drafts: list[dict[str, Any]] = []
        all_sources = [source for batch in source_batches for source in batch]
        for batch in source_batches:
            refs = {source.source_ref for source in batch}
            decision_refs = {source.source_ref for source in batch if source.kind == "decisionRecord"}
            prompt = render_working_context_prompt(
                self.profile.prompt,
                project_id=project.project_id,
                project_title=project.title,
                sources=[source.as_prompt_dict() for source in batch],
            )
            drafts.append(
                self._validated_invoke(
                    prompt,
                    project_id=project.project_id,
                    source_refs=refs,
                    decision_refs=decision_refs,
                )
            )
        if len(drafts) == 1:
            return drafts[0]
        refs = {source.source_ref for source in all_sources}
        decision_refs = {source.source_ref for source in all_sources if source.kind == "decisionRecord"}
        merge_prompt = render_working_context_merge_prompt(
            self.profile.prompt,
            project_id=project.project_id,
            project_title=project.title,
            source_refs=sorted(refs),
            drafts=drafts,
        )
        return self._validated_invoke(
            merge_prompt,
            project_id=project.project_id,
            source_refs=refs,
            decision_refs=decision_refs,
        )


def _source_suffix(refs: Sequence[str]) -> str:
    return " (Sources: " + ", ".join(f"`{ref}`" for ref in refs) + ")"


def _evidence_bullets(items: Sequence[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {str(item['text']).strip()}{_source_suffix(item['sourceRefs'])}"
        for item in items
    )


def _plain_bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {str(value).strip()}" for value in values if str(value).strip())


def _semantic_context(data: dict[str, Any]) -> str:
    blocks: list[str] = []
    if data["semanticGlossary"]:
        lines = ["### Semantic Glossary", ""]
        for item in data["semanticGlossary"]:
            details = []
            if item["aliases"]:
                details.append("Aliases: " + ", ".join(f"`{value}`" for value in item["aliases"]))
            if item["distinctions"]:
                details.append("Distinctions: " + "; ".join(item["distinctions"]))
            suffix = (" " + " ".join(details)) if details else ""
            lines.append(
                f"- **{item['term']}** — {item['definition']}{suffix}{_source_suffix(item['sourceRefs'])}"
            )
        blocks.append("\n".join(lines))
    if data["taxonomyItems"]:
        lines = ["### Taxonomy", ""]
        for item in data["taxonomyItems"]:
            parent = f"; parent: **{item['parent']}**" if item["parent"] else ""
            lines.append(
                f"- **{item['label']}** (`{item['kind']}`{parent}) — "
                f"{item['description']}{_source_suffix(item['sourceRefs'])}"
            )
        blocks.append("\n".join(lines))
    if data["taxonomyRelations"]:
        lines = ["### Relations", ""]
        lines.extend(
            f"- **{item['subject']}** — `{item['predicate']}` → **{item['object']}**"
            f"{_source_suffix(item['sourceRefs'])}"
            for item in data["taxonomyRelations"]
        )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_working_context(
    project: Project,
    sources: Sequence[WorkingContextSource],
    data: dict[str, Any],
    *,
    config: PipelineConfig,
    generated_at: str | None = None,
    record_date: str | None = None,
    template: WorkingContextTemplate = WORKING_CONTEXT_PROFILE.template,
) -> str:
    timestamp = generated_at or now_iso()
    thread_refs = [source.source_ref for source in sources if source.kind == "threadNote"]
    decision_refs = [source.source_ref for source in sources if source.kind == "decisionRecord"]
    repository_refs = [
        source.source_ref
        for source in sources
        if source.kind in {"repositoryFile", "gitSnapshot"}
    ]
    fields: list[tuple[str, str | int | bool | list[str]]] = [
        ("type", "workingContext"),
        ("schemaVersion", WORKING_CONTEXT_SCHEMA_VERSION),
        ("title", str(data["title"])),
        ("description", str(data["description"])),
        ("projectId", project.project_id),
        ("generator", "Codex"),
        ("status", "active"),
        ("projectStatus", str(data["projectStatus"])),
        ("currentFocus", str(data["currentFocus"])),
        ("blocked", bool(data["blocked"])),
        ("mainBlocker", str(data["mainBlocker"])),
        ("exactNextAction", str(data["exactNextAction"])),
        ("generatorModel", config.model),
        ("generatorReasoningEffort", config.reasoning_effort),
        ("promptId", WORKING_CONTEXT_PROFILE.prompt.prompt_id),
        ("promptVersion", WORKING_CONTEXT_PROFILE.prompt.version),
        ("outputSchemaSha256", WORKING_CONTEXT_PROFILE.schema.sha256),
        ("templateId", template.template_id),
        ("templateVersion", template.version),
        ("rendererVersion", WORKING_CONTEXT_RENDERER_VERSION),
        ("generatedAt", timestamp),
        ("reviewStatus", "unreviewed"),
        ("automatedValidation", "passed"),
        ("sourceThreadNoteRefs", thread_refs),
        ("sourceDecisionRefs", decision_refs),
        ("sourceRepositoryRefs", repository_refs),
        ("sourceSetSha256", _source_set_sha256(sources)),
        ("date", record_date or timestamp),
        ("updated", timestamp),
    ]
    effective_decisions = "\n".join(
        f"- `{item['decisionRef']}` — {item['summary']}"
        for item in data["effectiveDecisions"]
    )
    key_evidence = "\n".join(
        f"- `{item['ref']}` — {item['reason']}"
        for item in data["keyEvidence"]
    )
    resumption = _evidence_bullets(data["resumption"])
    if data["exactNextAction"]:
        next_action = f"**Exact Next Action:** {data['exactNextAction']}"
        resumption = f"{resumption}\n\n{next_action}" if resumption else next_action
    return render_working_context_template(
        template,
        {
            "frontmatter": frontmatter(fields),
            "project_overview": _evidence_bullets(data["projectOverview"]),
            "current_truth": _evidence_bullets(data["currentTruth"]),
            "current_outcome": _evidence_bullets(data["currentOutcome"]),
            "active_work": _evidence_bullets(data["activeWork"]),
            "risks_and_constraints": _evidence_bullets(data["risksAndConstraints"]),
            "effective_decisions": effective_decisions,
            "semantic_context": _semantic_context(data),
            "key_evidence": key_evidence,
            "resumption": resumption,
            "source_limitations": _plain_bullets(data["sourceLimitations"]),
        },
    )


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n+(.*?)(?=^## |\Z)", text)
    return match.group(1).strip() if match else ""


def validate_working_context(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PipelineError(f"Working Context not found: {path}")
    if path.name != WORKING_CONTEXT_FILENAME:
        raise PipelineError(f"invalid Working Context filename: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    metadata = parse_simple_frontmatter(text)
    required = {
        "type": "workingContext",
        "schemaVersion": str(WORKING_CONTEXT_SCHEMA_VERSION),
        "generator": "Codex",
        "status": "active",
        "automatedValidation": "passed",
    }
    for key, expected in required.items():
        if metadata.get(key) != expected:
            raise PipelineError(f"Working Context has invalid {key}: {path}")
    required_values = (
        "title",
        "description",
        "projectId",
        "generatorModel",
        "generatorReasoningEffort",
        "promptVersion",
        "templateVersion",
        "generatedAt",
        "date",
        "updated",
    )
    missing_values = [key for key in required_values if not metadata.get(key)]
    if missing_values:
        raise PipelineError(f"Working Context is missing metadata ({', '.join(missing_values)}): {path}")
    for key in ("promptId", "templateId"):
        try:
            uuid.UUID(metadata.get(key) or "")
        except ValueError as exc:
            raise PipelineError(f"Working Context has invalid {key}: {path}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("outputSchemaSha256") or ""):
        raise PipelineError(f"Working Context has invalid outputSchemaSha256: {path}")
    if not str(metadata.get("rendererVersion") or "").isdigit():
        raise PipelineError(f"Working Context has invalid rendererVersion: {path}")
    if metadata.get("reviewStatus") not in {"unreviewed", "reviewed"}:
        raise PipelineError(f"Working Context has invalid reviewStatus: {path}")
    if metadata.get("projectStatus") not in {
        "active", "paused", "blocked", "completed", "archived", "unknown"
    }:
        raise PipelineError(f"Working Context has invalid projectStatus: {path}")
    if metadata.get("blocked") not in {"true", "false"}:
        raise PipelineError(f"Working Context has invalid blocked: {path}")
    if (metadata.get("blocked") == "true") != (metadata.get("projectStatus") == "blocked"):
        raise PipelineError(f"Working Context blocked fields are inconsistent: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("sourceSetSha256") or ""):
        raise PipelineError(f"Working Context has invalid sourceSetSha256: {path}")
    if "# Working Context" not in text:
        raise PipelineError(f"Working Context has no title heading: {path}")
    headings = re.findall(r"(?m)^## ([^\r\n]+?)\s*$", text)
    if headings[:2] != ["Project Overview", "Current Truth"]:
        raise PipelineError(f"Working Context must start with Project Overview and Current Truth: {path}")
    unknown = [heading for heading in headings if heading not in _ALLOWED_HEADINGS]
    if unknown:
        raise PipelineError(f"Working Context has unsupported headings ({', '.join(unknown)}): {path}")
    duplicates = sorted({heading for heading in headings if headings.count(heading) > 1})
    if duplicates:
        raise PipelineError(f"Working Context repeats headings ({', '.join(duplicates)}): {path}")
    expected_order = [heading for heading in _ALLOWED_HEADINGS if heading in headings]
    if headings != expected_order:
        raise PipelineError(f"Working Context headings are out of order: {path}")
    empty = [heading for heading in headings if not _section(text, heading)]
    if empty:
        raise PipelineError(f"Working Context has empty sections ({', '.join(empty)}): {path}")
    lines, body = split_frontmatter_lines(text)
    declared_refs = {
        ref
        for key in ("sourceThreadNoteRefs", "sourceDecisionRefs", "sourceRepositoryRefs")
        for ref in frontmatter_list_value(lines, key)
    }
    if not declared_refs:
        raise PipelineError(f"Working Context has no source references: {path}")
    invalid_refs = sorted(
        ref
        for ref in declared_refs
        if not (ref.startswith("project:/") or ref.startswith("repo:/"))
    )
    if invalid_refs:
        raise PipelineError(f"Working Context has invalid source references ({', '.join(invalid_refs)}): {path}")
    body_refs = set(re.findall(r"`((?:project|repo):/[^`]+)`", body))
    undeclared_refs = sorted(body_refs - declared_refs)
    if undeclared_refs:
        raise PipelineError(
            f"Working Context body references undeclared sources ({', '.join(undeclared_refs)}): {path}"
        )
    if re.search(r"(?m)(?:^|:\s+)None\.$", body):
        raise PipelineError(f"Working Context v3 must omit empty values instead of rendering None: {path}")
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"Working Context contains secret-like content ({', '.join(secrets)}): {path}")
    return {
        "valid": True,
        "path": str(path.absolute()),
        "schemaVersion": WORKING_CONTEXT_SCHEMA_VERSION,
        "projectId": metadata["projectId"],
        "projectStatus": metadata["projectStatus"],
        "reviewStatus": metadata["reviewStatus"],
    }


def _existing_record_date(path: Path) -> str | None:
    if not path.is_file():
        return None
    metadata = parse_simple_frontmatter(path.read_text(encoding="utf-8-sig"))
    return metadata.get("date") or None


def execute_working_context_build(
    config: PipelineConfig,
    project: Project,
    *,
    generator: WorkingContextGenerator | None,
    write: bool = False,
    force: bool = False,
    allow_edited: bool = False,
    cache_root: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    started = now_local()
    output_path = working_context_path(project)
    state_path = working_context_state_path(project)
    state = load_working_context_state(project)
    sources, source_failures = collect_working_context_sources(project)
    source_set = _source_set_sha256(sources) if sources else ""
    generation = working_context_generation_fingerprint(config)
    current_hash = sha256(output_path.read_bytes()).hexdigest() if output_path.is_file() else None
    tracked_hash = state.get("workingContextSha256")
    edited = bool(output_path.is_file() and current_hash != tracked_hash)
    changed = bool(
        force
        or not output_path.is_file()
        or state.get("sourceSetSha256") != source_set
        or state.get("generationFingerprint") != generation
    )
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": None,
        "mode": "working-context-build",
        "projectId": project.project_id,
        "dryRun": not write,
        "force": force,
        "allowEdited": allow_edited,
        "workingContext": str(output_path),
        "sourceCounts": {
            "threadNotes": sum(source.kind == "threadNote" for source in sources),
            "decisions": sum(source.kind == "decisionRecord" for source in sources),
            "repositoryFiles": sum(source.kind == "repositoryFile" for source in sources),
            "gitSnapshots": sum(source.kind == "gitSnapshot" for source in sources),
        },
        "sourceSetSha256": source_set or None,
        "generationFingerprint": generation,
        "changed": changed,
        "edited": edited,
        "selectedCount": 1 if changed and not source_failures else 0,
        "createdCount": 0,
        "updatedCount": 0,
        "unchangedCount": 1 if not changed else 0,
        "failed": list(source_failures),
        "sourceRefs": [source.source_ref for source in sources],
    }
    if edited and changed and not allow_edited:
        report["failed"].append(
            {
                "source": WORKING_CONTEXT_FILENAME,
                "error": "existing Working Context differs from its recorded hash; use --allow-edited to replace it",
            }
        )
        report["selectedCount"] = 0
    if not write or not changed or report["failed"]:
        report["finishedAt"] = now_iso()
        return report, None
    if generator is None:
        raise PipelineError("a Working Context generator is required for a write run")
    deadline = started + timedelta(minutes=config.runtime_minutes + IN_FLIGHT_GRACE_MINUTES)
    if hasattr(generator, "set_deadline"):
        generator.set_deadline(deadline)
    if progress:
        progress({"type": "working-context-start", "projectId": project.project_id, "sourceCount": len(sources)})
    build_started = time.monotonic()
    try:
        data = generator.generate(project, working_context_source_batches(sources))
        validate_working_context_output(
            data,
            {source.source_ref for source in sources},
            {source.source_ref for source in sources if source.kind == "decisionRecord"},
        )
        current_sources, current_failures = collect_working_context_sources(project)
        if current_failures or _source_set_sha256(current_sources) != source_set:
            raise PipelineError("Working Context sources changed during generation")
        timestamp = now_iso()
        rendered = render_working_context(
            project,
            sources,
            data,
            config=config,
            generated_at=timestamp,
            record_date=_existing_record_date(output_path),
        )
        old_output = output_path.read_bytes() if output_path.exists() else None
        old_state = state_path.read_bytes() if state_path.exists() else None
        previous_state = deepcopy(state)
        try:
            atomic_write_text(output_path, rendered)
            validate_working_context(output_path)
            rendered_hash = sha256(output_path.read_bytes()).hexdigest()
            state.update(
                {
                    "lastBuildAt": timestamp,
                    "sourceSetSha256": source_set,
                    "generationFingerprint": generation,
                    "workingContextSha256": rendered_hash,
                    "sources": {source.source_ref: source.sha256 for source in sources},
                }
            )
            atomic_write_json(state_path, state)
        except Exception:
            if old_output is None:
                if output_path.exists():
                    output_path.unlink()
            else:
                atomic_write_text(output_path, old_output.decode("utf-8-sig"))
            if old_state is None:
                if state_path.exists():
                    state_path.unlink()
            else:
                atomic_write_text(state_path, old_state.decode("utf-8-sig"))
            state.clear()
            state.update(previous_state)
            raise
    except Exception as exc:
        report["failed"].append({"source": project.project_id, "error": str(exc)})
        if progress:
            progress({"type": "working-context-failed", "projectId": project.project_id, "error": str(exc)})
    else:
        report["createdCount"] = 0 if old_output is not None else 1
        report["updatedCount"] = 1 if old_output is not None else 0
        report["workingContextSha256"] = sha256(output_path.read_bytes()).hexdigest()
        report.update(deepcopy(getattr(generator, "last_metrics", {})))
        if progress:
            progress(
                {
                    "type": "working-context-complete",
                    "projectId": project.project_id,
                    "workingContextPath": str(output_path.absolute()),
                    "durationSeconds": round(time.monotonic() - build_started, 3),
                    **deepcopy(getattr(generator, "last_metrics", {})),
                }
            )
    report["finishedAt"] = now_iso()
    report_path = write_run_report(cache_root or project.context_path, report)
    return report, report_path
