"""Distill durable decision records from generated Thread Notes."""

from __future__ import annotations

import json
import re
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
from .decision_prompting import render_decision_prompt, render_decision_repair_prompt
from .decision_resources import (
    DecisionProfile,
    DecisionTemplate,
    load_decision_profile,
    render_decision_template,
)
from .frontmatter import (
    canonical_uuid4,
    frontmatter_list_value,
    parse_simple_frontmatter,
    replace_frontmatter_list,
    replace_frontmatter_scalar,
    split_frontmatter_lines,
    unique_ordered,
)
from .inference import (
    InferenceExecutionError,
    invoke_structured,
    is_supported_generator,
    provider_name,
)
from .safety import has_secret_like_content
from .summary_resources import validate_summary_output_schema
from .thread_notes import (
    PipelineConfig,
    PipelineError,
    Project,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    now_local,
    thread_note_metadata,
    write_run_report,
)

DECISION_SCHEMA_VERSION = 5
DECISION_STATE_SCHEMA_VERSION = 1
DECISION_RENDERER_VERSION = 5
DECISION_STATE_FILENAME = "decision-build-state.json"
MAX_EXISTING_DECISION_INDEX = 200
MAX_SOURCE_NOTE_CHARACTERS = 20_000
MAX_DECISION_BATCH_SOURCES = 50
MAX_DECISION_BATCH_CHARACTERS = 200_000
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
    thread_note_id: str
    thread_id: str
    text: str


@dataclass(frozen=True)
class ExistingDecision:
    decision_id: str
    path: Path
    title: str
    status: str
    decision: str
    source_refs: tuple[str, ...] = ()
    review_status: str = "unknown"
    generator: str = "unknown"
    schema_version: str = "1"
    prompt_version: str = "unknown"
    record_excerpt: str = ""

    @property
    def update_allowed(self) -> bool:
        return (
            self.schema_version in {"2", "3", "4", str(DECISION_SCHEMA_VERSION)}
            and is_supported_generator(self.generator)
            and self.review_status == "unreviewed"
        )

    @property
    def quality_upgrade_required(self) -> bool:
        return self.update_allowed and (
            self.schema_version != str(DECISION_SCHEMA_VERSION)
            or self.prompt_version != DECISION_PROFILE.prompt.version
        )

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "decisionId": self.decision_id,
            "title": self.title,
            "status": self.status,
            "reviewStatus": self.review_status,
            "updateAllowed": self.update_allowed,
            "qualityUpgradeRequired": self.quality_upgrade_required,
            "sourceThreadNoteRefs": list(self.source_refs),
            "decision": self.decision[:600],
            "recordExcerpt": self.record_excerpt[:4000] if self.update_allowed else "",
        }


@dataclass(frozen=True)
class DecisionBatchCommit:
    created: list[ExistingDecision]
    updated: list[ExistingDecision]
    referenced: list[str]
    decision_ids_by_source: dict[str, list[str]]
    decision_refs_by_source: dict[str, list[str]]


class DecisionGenerator(Protocol):
    last_metrics: dict[str, int]

    def generate(
        self,
        sources: Sequence[DecisionSource],
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
            "provider": config.provider,
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
        if version not in {"1", "2", "3", "4", str(DECISION_SCHEMA_VERSION)}:
            raise PipelineError(f"unsupported decision schemaVersion {version}: {path.name}")
        if version == str(DECISION_SCHEMA_VERSION):
            try:
                canonical_uuid4(metadata.get("id") or "")
            except ValueError as exc:
                raise PipelineError(f"decision record has invalid id: {path}") from exc
        decision_id = metadata.get("decisionId") or f"DR-{match.group(1)}"
        if decision_id != f"DR-{match.group(1)}":
            raise PipelineError(f"decisionId does not match filename: {path.name}")
        if decision_id in seen:
            raise PipelineError(f"duplicate decisionId: {decision_id}")
        seen.add(decision_id)
        lines, _body = split_frontmatter_lines(text)
        result.append(
            ExistingDecision(
                decision_id=decision_id,
                path=path,
                title=metadata.get("title") or path.stem,
                status=metadata.get("status") or "unknown",
                decision=_section(text, "Decision"),
                source_refs=tuple(frontmatter_list_value(lines, "sourceThreadNoteRefs")),
                review_status=metadata.get("reviewStatus") or "unknown",
                generator=metadata.get("generator") or "unknown",
                schema_version=version,
                prompt_version=metadata.get("promptVersion") or "unknown",
                record_excerpt=text,
            )
        )
    return result


def _refresh_existing_decision_index_report(report: dict[str, Any], existing_count: int) -> None:
    prefix = "Existing Decision prompt index"
    warnings = [str(value) for value in report.get("warnings", []) if not str(value).startswith(prefix)]
    omitted_count = max(0, existing_count - MAX_EXISTING_DECISION_INDEX)
    report["existingDecisionIndexOmittedCount"] = omitted_count
    if existing_count >= MAX_EXISTING_DECISION_INDEX:
        if omitted_count:
            warnings.append(
                f"{prefix} is limited to the newest {MAX_EXISTING_DECISION_INDEX} records; "
                f"{omitted_count} older record{'s are' if omitted_count != 1 else ' is'} omitted."
            )
        else:
            warnings.append(
                f"{prefix} has reached its {MAX_EXISTING_DECISION_INDEX}-record limit; "
                "older records will be omitted as new Decisions are added."
            )
    report["warnings"] = warnings


def _thread_note_source(path: Path, project: Project) -> DecisionSource:
    text = path.read_text(encoding="utf-8-sig")
    metadata, thread_ids, _source_refs, version = thread_note_metadata(path)
    if version not in {"3", "4"}:
        raise PipelineError(f"decision distillation requires Thread Note v3 or v4: {path.name}")
    if version == "4":
        try:
            canonical_uuid4(metadata.get("id") or "")
        except ValueError as exc:
            raise PipelineError(f"Thread Note v4 has invalid id: {path.name}") from exc
    if metadata.get("type") != "threadNote":
        raise PipelineError(f"invalid Thread Note type for decision distillation: {path.name}")
    if len(thread_ids) != 1:
        raise PipelineError(f"Thread Note must identify one source thread: {path.name}")
    for heading in ("# Thread Note", "## Summary", "## Key Developments", "## Last Known State"):
        if heading not in text:
            raise PipelineError(f"Thread Note is missing {heading}: {path.name}")
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"Thread Note contains secret-like content ({', '.join(secrets)}): {path.name}")
    if len(text) > MAX_SOURCE_NOTE_CHARACTERS:
        raise PipelineError(f"Thread Note exceeds the decision input size limit: {path.name}")
    relative = path.relative_to(project.context_path).as_posix()
    return DecisionSource(
        project=project,
        path=path,
        relative_path=relative,
        source_ref=f"project:/{relative}",
        source_sha256=sha256(path.read_bytes()).hexdigest(),
        thread_note_id=metadata.get("threadNoteId") or path.stem,
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
        "invalid": 0,
    }
    for path in sorted(project.thread_notes_path.glob("*.md")):
        counts["total"] += 1
        try:
            source = _thread_note_source(path, project)
        except (OSError, PipelineError, SystemExit) as exc:
            counts["invalid"] += 1
            failures.append({"threadNote": path.name, "error": str(exc)})
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
        counts["eligible"] += 1
        candidates.append(source)
    return candidates, counts, failures


def decision_source_batches(
    sources: Sequence[DecisionSource],
) -> list[list[DecisionSource]]:
    batches: list[list[DecisionSource]] = []
    current: list[DecisionSource] = []
    current_characters = 0
    for source in sources:
        source_characters = len(source.text)
        if current and (
            len(current) >= MAX_DECISION_BATCH_SOURCES
            or current_characters + source_characters > MAX_DECISION_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = []
            current_characters = 0
        current.append(source)
        current_characters += source_characters
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


def validate_decision_output(
    value: Any,
    existing_decision_ids: set[str],
    source_refs: set[str],
    updateable_decision_ids: set[str] | None = None,
    required_update_decision_ids: set[str] | None = None,
) -> dict[str, Any]:
    updateable_ids = updateable_decision_ids or set()
    required_update_ids = required_update_decision_ids or set()
    try:
        validate_summary_output_schema(value, DECISION_OUTPUT_SCHEMA)
    except ValueError as exc:
        raise PipelineError(f"Inference output does not match the decision schema: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("Inference decision output must be a JSON object")
    strings = _all_strings(value)
    if any("\n" in item or "\r" in item for item in strings):
        raise PipelineError("Inference decision output strings must not contain line breaks")
    secrets = has_secret_like_content(json.dumps(value, ensure_ascii=False))
    if secrets:
        raise PipelineError("Inference decision output contains secret-like content: " + ", ".join(secrets))
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise PipelineError("Inference decision output has invalid decisions")
    referenced: set[str] = set()
    new_slugs: set[str] = set()
    new_decisions: set[str] = set()
    for item in decisions:
        if not isinstance(item, dict):
            raise PipelineError("Inference decision output has an invalid decision item")
        disposition = item.get("disposition")
        item_source_refs = item.get("sourceThreadNoteRefs")
        if not isinstance(item_source_refs, list) or not item_source_refs:
            raise PipelineError("each decision must identify at least one sourceThreadNoteRef")
        normalized_source_refs = [str(ref) for ref in item_source_refs]
        if len(set(normalized_source_refs)) != len(normalized_source_refs):
            raise PipelineError("a decision repeats a sourceThreadNoteRef")
        unknown_source_refs = sorted(set(normalized_source_refs) - source_refs)
        if unknown_source_refs:
            raise PipelineError(
                "Inference output references unknown sourceThreadNoteRefs: " + ", ".join(unknown_source_refs)
            )
        if disposition == "existing":
            decision_id = str(item.get("existingDecisionId") or "")
            if decision_id not in existing_decision_ids:
                raise PipelineError(f"Inference output references unknown decisionId: {decision_id}")
            if decision_id in referenced:
                raise PipelineError(f"Inference output repeats existing decisionId: {decision_id}")
            if decision_id in required_update_ids:
                raise PipelineError(
                    f"Inference output must update decisionId to the current quality profile: {decision_id}"
                )
            referenced.add(decision_id)
            non_empty = [
                key
                for key, field_value in item.items()
                if key not in {
                    "disposition",
                    "existingDecisionId",
                    "sourceThreadNoteRefs",
                    "materialization",
                }
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
        if disposition not in {"create", "update"}:
            raise PipelineError("Inference decision output has invalid disposition")
        if disposition == "update":
            decision_id = str(item.get("existingDecisionId") or "")
            if decision_id not in existing_decision_ids:
                raise PipelineError(f"Inference output references unknown decisionId: {decision_id}")
            if decision_id not in updateable_ids:
                raise PipelineError(f"Inference output cannot update reviewed or legacy decisionId: {decision_id}")
            if decision_id in referenced:
                raise PipelineError(f"Inference output repeats existing decisionId: {decision_id}")
            referenced.add(decision_id)
        elif item.get("existingDecisionId"):
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
            raise PipelineError("Inference output repeats a new central decision")
        new_slugs.add(slug)
        new_decisions.add(central)
        if item["implementationStatus"] == "verified" and not item["verificationEvidence"]:
            raise PipelineError("verified decisions must include verificationEvidence")
        invalid_destinations = [
            destination
            for destination in item["materialization"]["repositoryDocumentation"]
            if re.search(
                r"(?:^|/)(?:thread-notes?/|decisions?/|DR-[0-9]{4})",
                destination,
                re.IGNORECASE,
            )
        ]
        if invalid_destinations:
            raise PipelineError(
                "Thread Notes and Decision Records are evidence, not repository documentation: "
                + ", ".join(invalid_destinations)
            )
        unavailable_decision_refs = []
        for evidence in item["relatedEvidence"]:
            match = re.search(r"(?:^|/)(DR-[0-9]{4})(?:-|\b)", evidence)
            if match and match.group(1) not in existing_decision_ids:
                unavailable_decision_refs.append(evidence)
        if unavailable_decision_refs:
            raise PipelineError(
                "relatedEvidence references Decision Records unavailable from the current index: "
                + ", ".join(unavailable_decision_refs)
            )
    return value


class ProviderDecisionGenerator:
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
                    return invoke_structured(
                        self.config,
                        prompt,
                        self.profile.schema.value,
                        cwd=temp,
                        timeout=timeout,
                    )
                except InferenceExecutionError as exc:
                    last_error = str(exc)
                if attempt < 2:
                    self.sleeper(2**attempt)
            raise PipelineError(last_error or "decision inference generation failed")

    def generate(
        self,
        sources: Sequence[DecisionSource],
        existing_decisions: Sequence[ExistingDecision],
    ) -> dict[str, Any]:
        if not sources:
            raise PipelineError("decision generation requires at least one Thread Note")
        self.last_metrics = {
            "modelCalls": 0,
            "transportRetries": 0,
            "semanticRetries": 0,
        }
        prompt_index = [item.as_prompt_dict() for item in list(existing_decisions)[-MAX_EXISTING_DECISION_INDEX:]]
        thread_notes = [
            {
                "sourceRef": source.source_ref,
                "threadNoteId": source.thread_note_id,
                "threadId": source.thread_id,
                "threadNote": source.text,
            }
            for source in sources
        ]
        current_prompt = render_decision_prompt(
            self.profile.prompt,
            project_id=sources[0].project.project_id,
            thread_notes=thread_notes,
            existing_decisions=prompt_index,
        )
        existing_ids = {item.decision_id for item in existing_decisions}
        updateable_ids = {item.decision_id for item in existing_decisions if item.update_allowed}
        required_update_ids = {
            item.decision_id for item in existing_decisions if item.quality_upgrade_required
        }
        source_refs = {source.source_ref for source in sources}
        for semantic_attempt in range(2):
            value = self._invoke(current_prompt)
            try:
                return validate_decision_output(
                    value,
                    existing_ids,
                    source_refs,
                    updateable_ids,
                    required_update_ids,
                )
            except PipelineError as exc:
                if semantic_attempt:
                    raise
                self.last_metrics["semanticRetries"] += 1
                current_prompt = render_decision_repair_prompt(
                    self.profile.prompt,
                    project_id=sources[0].project.project_id,
                    thread_note_refs=sorted(source_refs),
                    validation_error=str(exc),
                    draft=value,
                    existing_decisions=prompt_index,
                )
        raise PipelineError("Inference semantic validation did not produce valid decisions")


# Backward-compatible import for callers that used the original provider-specific name.
CodexDecisionGenerator = ProviderDecisionGenerator


def _bullets(values: Sequence[str]) -> str:
    items = [str(value).strip() for value in values if str(value).strip()]
    return "\n".join(f"- {value}" for value in items)


def _labeled_block(label: str, values: Sequence[str]) -> str:
    bullets = _bullets(values)
    return f"**{label}**\n\n{bullets}" if bullets else ""


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(block for block in blocks if block.strip())


def _source_set_sha256(sources: Sequence[DecisionSource]) -> str:
    if len(sources) == 1:
        return sources[0].source_sha256
    payload = json.dumps(
        [
            {"sourceRef": source.source_ref, "sha256": source.source_sha256}
            for source in sorted(sources, key=lambda item: item.source_ref)
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def render_decision(
    sources: Sequence[DecisionSource],
    data: dict[str, Any],
    decision_id: str,
    *,
    generated_at: str | None = None,
    record_date: str | None = None,
    note_id: str | None = None,
    source_refs_override: Sequence[str] | None = None,
    config: PipelineConfig,
    template: DecisionTemplate = DECISION_PROFILE.template,
) -> str:
    if not sources:
        raise PipelineError("a Decision Record requires at least one source Thread Note")
    timestamp = generated_at or now_iso()
    source_refs = unique_ordered(
        list(source_refs_override) if source_refs_override is not None else [source.source_ref for source in sources]
    )
    related_evidence = unique_ordered(
        [str(value) for value in data["relatedEvidence"] if str(value) not in source_refs]
    )
    promotion_status = (
        "no-action"
        if data["scope"] == "project"
        and not data["reusablePrinciples"]
        and not data["materialization"]["globalContext"]
        else "pending"
    )
    fields: list[tuple[str, str | int | list[str]]] = [
        ("type", "decision"),
        ("schemaVersion", DECISION_SCHEMA_VERSION),
        ("id", note_id or str(uuid.uuid4())),
        ("title", str(data["title"])),
        ("description", str(data["description"])),
        ("generator", provider_name(config.provider)),
        ("generatorProvider", config.provider),
        ("status", str(data["status"])),
        ("scope", str(data["scope"])),
        ("implementationStatus", str(data["implementationStatus"])),
        ("promotionStatus", promotion_status),
        ("promotedTo", []),
        ("projectWorkingContextTargets", data["materialization"]["projectWorkingContext"]),
        ("repositoryDocumentationTargets", data["materialization"]["repositoryDocumentation"]),
        ("globalContextTargets", data["materialization"]["globalContext"]),
        ("skillAutomationTargets", data["materialization"]["skillAutomation"]),
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
        ("sourceThreadNoteRefs", source_refs),
        (
            "sourceThreadNoteSetSha256",
            _source_set_sha256(sources)
            if source_refs_override is None
            else _source_refs_fingerprint(sources[0].project, source_refs),
        ),
        ("date", record_date or timestamp),
        ("updated", timestamp),
        ("decisionId", decision_id),
    ]
    why = _join_blocks(
        _labeled_block("Context", data["context"]),
        _labeled_block("Rationale", data["rationale"]),
    )
    consequences = _join_blocks(
        _labeled_block("Benefits", data["benefits"]),
        _labeled_block("Costs And Risks", data["costsAndRisks"]),
    )
    scope = _join_blocks(
        _labeled_block("Applies When", data["appliesWhen"]),
        _labeled_block("Does Not Apply When", data["doesNotApplyWhen"]),
        _labeled_block("Reusable Principle", data["reusablePrinciples"]),
        _labeled_block("Project-Specific Details", data["projectSpecificDetails"]),
    )
    verification = _join_blocks(
        _labeled_block("Evidence", data["verificationEvidence"]),
        _labeled_block("Limitations", data["verificationLimitations"]),
        f"**Validation Date:** {data['validationDate']}" if data["validationDate"] else "",
    )
    materialization = data["materialization"]
    supersession = _join_blocks(
        _labeled_block("Supersedes", data["supersedes"]),
        _labeled_block("Superseded By", data["supersededBy"]),
    )
    return render_decision_template(
        template,
        {
            "frontmatter": frontmatter(fields),
            "title": f"{decision_id}: {data['title']}",
            "decision": str(data["decision"]).strip(),
            "why": why,
            "consequences": consequences,
            "alternatives": _bullets(data["alternativesConsidered"]),
            "scope": scope,
            "verification": verification,
            "related_evidence": _bullets(related_evidence),
            "follow_up": _bullets(materialization["followUp"]),
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
    schema_version = metadata.get("schemaVersion") or "1"
    if schema_version not in {"2", "3", "4", str(DECISION_SCHEMA_VERSION)}:
        raise PipelineError(f"unsupported decision schemaVersion {schema_version}: {path.name}")
    required = {
        "type": "decision",
        "schemaVersion": schema_version,
        "automatedValidation": "passed",
    }
    for key, expected in required.items():
        if metadata.get(key) != expected:
            raise PipelineError(f"decision record has invalid {key}: {path}")
    if schema_version == str(DECISION_SCHEMA_VERSION):
        try:
            canonical_uuid4(metadata.get("id") or "")
        except ValueError as exc:
            raise PipelineError(f"decision record has invalid id: {path}") from exc
    if not is_supported_generator(metadata.get("generator") or ""):
        raise PipelineError(f"decision record has invalid generator: {path}")
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
    if metadata.get("promotionStatus") not in {
        "pending",
        "partial",
        "promoted",
        "no-action",
    }:
        raise PipelineError(f"decision record has invalid promotionStatus: {path}")
    lines, body = split_frontmatter_lines(text)
    if not frontmatter_list_value(lines, "sourceThreadNoteRefs"):
        raise PipelineError(f"decision record has no sourceThreadNoteRefs: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("sourceThreadNoteSetSha256") or ""):
        raise PipelineError(f"decision record has invalid sourceThreadNoteSetSha256: {path}")
    title = metadata.get("title") or ""
    if f"# {decision_id}: {title}" not in text:
        raise PipelineError(f"decision record title does not match frontmatter: {path}")
    if schema_version == "2":
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
        required_labels = [
            "Evidence",
            "Validation Date",
            "Project Working Context",
            "Repository Documentation",
            "Global Context",
            "Skill / Automation",
            "Follow-up",
            "Supersedes",
            "Superseded By",
        ]
        if metadata.get("promptVersion") == "2.1":
            required_labels.insert(1, "Limitations")
        for label in required_labels:
            if f"- {label}:" not in text:
                raise PipelineError(f"decision record is missing {label}: {path}")
    else:
        target_fields = (
            "projectWorkingContextTargets",
            "repositoryDocumentationTargets",
            "globalContextTargets",
            "skillAutomationTargets",
        )
        missing_target_fields = [field for field in target_fields if field not in metadata]
        if missing_target_fields:
            raise PipelineError(
                f"decision record is missing materialization targets ({', '.join(missing_target_fields)}): {path}"
            )
        allowed_headings = (
            "Decision",
            "Why",
            "Consequences",
            "Alternatives",
            "Scope",
            "Verification",
            "Related Evidence",
            "Follow-up",
            "Supersession",
        )
        present_headings = re.findall(r"(?m)^## ([^\r\n]+?)\s*$", text)
        if not present_headings or present_headings[0] != "Decision":
            raise PipelineError(f"decision record must start with ## Decision: {path}")
        unknown = [heading for heading in present_headings if heading not in allowed_headings]
        if unknown:
            raise PipelineError(f"decision record has unsupported headings ({', '.join(unknown)}): {path}")
        duplicates = sorted({heading for heading in present_headings if present_headings.count(heading) > 1})
        if duplicates:
            raise PipelineError(f"decision record repeats headings ({', '.join(duplicates)}): {path}")
        expected_order = [heading for heading in allowed_headings if heading in present_headings]
        if present_headings != expected_order:
            raise PipelineError(f"decision record headings are out of order: {path}")
        empty = [heading for heading in present_headings if not _section(text, heading)]
        if empty:
            raise PipelineError(f"decision record has empty sections ({', '.join(empty)}): {path}")
        if re.search(r"(?m)(?:^|:\s+)None\.$", body):
            raise PipelineError(
                f"decision record v3-v4 must omit empty values instead of rendering None: {path}"
            )
    secrets = has_secret_like_content(text)
    if secrets:
        raise PipelineError(f"decision record contains secret-like content ({', '.join(secrets)}): {path}")
    return {
        "valid": True,
        "path": str(path.absolute()),
        "schemaVersion": int(schema_version),
        "id": metadata.get("id"),
        "decisionId": decision_id,
        "status": metadata["status"],
        "implementationStatus": metadata["implementationStatus"],
    }


def _next_decision_number(existing: Sequence[ExistingDecision]) -> int:
    numbers = [int(item.decision_id.removeprefix("DR-")) for item in existing]
    return max(numbers, default=0) + 1


def _source_refs_fingerprint(project: Project, source_refs: Sequence[str]) -> str:
    evidence: list[dict[str, str]] = []
    for source_ref in sorted(set(source_refs)):
        digest = "missing"
        prefix = "project:/"
        if source_ref.startswith(prefix):
            candidate = (project.context_path / source_ref.removeprefix(prefix)).resolve()
            context_root = project.context_path.resolve()
            if candidate == context_root or context_root in candidate.parents:
                if candidate.is_file():
                    digest = sha256(candidate.read_bytes()).hexdigest()
        evidence.append({"sourceRef": source_ref, "sha256": digest})
    if len(evidence) == 1 and evidence[0]["sha256"] != "missing":
        return evidence[0]["sha256"]
    return sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _extend_existing_decision_sources(
    decision: ExistingDecision,
    sources: Sequence[DecisionSource],
    *,
    timestamp: str,
) -> str | None:
    text = decision.path.read_text(encoding="utf-8-sig")
    metadata = parse_simple_frontmatter(text)
    if metadata.get("schemaVersion") != str(DECISION_SCHEMA_VERSION) or not is_supported_generator(
        metadata.get("generator") or ""
    ):
        return None
    lines, body = split_frontmatter_lines(text)
    merged_refs = unique_ordered(
        [
            *frontmatter_list_value(lines, "sourceThreadNoteRefs"),
            *[source.source_ref for source in sources],
        ]
    )
    updated_lines = replace_frontmatter_list(lines, "sourceThreadNoteRefs", merged_refs)
    updated_lines = replace_frontmatter_scalar(
        updated_lines,
        "sourceThreadNoteSetSha256",
        _source_refs_fingerprint(sources[0].project, merged_refs),
    )
    updated_lines = replace_frontmatter_scalar(updated_lines, "updated", timestamp)
    return "".join(updated_lines) + body


def _commit_batch(
    sources: Sequence[DecisionSource],
    config: PipelineConfig,
    state: dict[str, Any],
    existing: Sequence[ExistingDecision],
    output: dict[str, Any],
) -> DecisionBatchCommit:
    if not sources:
        raise PipelineError("cannot commit an empty decision synthesis batch")
    for source in sources:
        if sha256(source.path.read_bytes()).hexdigest() != source.source_sha256:
            raise PipelineError(f"Thread Note changed during decision generation: {source.path.name}")
    timestamp = now_iso()
    source_by_ref = {source.source_ref: source for source in sources}
    existing_by_id = {item.decision_id: item for item in existing}
    next_number = _next_decision_number(existing)
    created: list[ExistingDecision] = []
    updated: list[ExistingDecision] = []
    created_text: dict[Path, str] = {}
    updated_text: dict[Path, str] = {}
    referenced: list[str] = []
    decision_ids_by_source: dict[str, list[str]] = {source.source_ref: [] for source in sources}
    decision_refs_by_source: dict[str, list[str]] = {source.source_ref: [] for source in sources}
    existing_source_updates: dict[str, list[DecisionSource]] = {}

    for item in output["decisions"]:
        item_sources = [source_by_ref[str(ref)] for ref in item["sourceThreadNoteRefs"]]
        disposition = item["disposition"]
        if disposition == "existing":
            decision_id = str(item["existingDecisionId"])
            decision = existing_by_id[decision_id]
            referenced.append(decision_id)
            existing_source_updates.setdefault(decision_id, []).extend(item_sources)
        elif disposition == "update":
            decision_id = str(item["existingDecisionId"])
            previous = existing_by_id[decision_id]
            metadata = parse_simple_frontmatter(previous.path.read_text(encoding="utf-8-sig"))
            merged_source_refs = unique_ordered(
                [*previous.source_refs, *[source.source_ref for source in item_sources]]
            )
            rendered = render_decision(
                item_sources,
                item,
                decision_id,
                generated_at=timestamp,
                record_date=metadata.get("date") or timestamp,
                note_id=metadata.get("id") or None,
                source_refs_override=merged_source_refs,
                config=config,
            )
            updated_text[previous.path] = rendered
            decision = ExistingDecision(
                decision_id=decision_id,
                path=previous.path,
                title=str(item["title"]),
                status=str(item["status"]),
                decision=str(item["decision"]),
                source_refs=tuple(merged_source_refs),
                review_status="unreviewed",
                generator=provider_name(config.provider),
                schema_version=str(DECISION_SCHEMA_VERSION),
                prompt_version=DECISION_PROFILE.prompt.version,
                record_excerpt=rendered,
            )
            updated.append(decision)
        else:
            decision_id = f"DR-{next_number:04d}"
            next_number += 1
            filename = f"{decision_id}-{item['fileSlug']}.md"
            path = decisions_path(sources[0].project) / filename
            if path.exists():
                raise PipelineError(f"refusing to overwrite existing decision record: {path}")
            rendered = render_decision(
                item_sources,
                item,
                decision_id,
                generated_at=timestamp,
                config=config,
            )
            created_text[path] = rendered
            decision = ExistingDecision(
                decision_id=decision_id,
                path=path,
                title=str(item["title"]),
                status=str(item["status"]),
                decision=str(item["decision"]),
                source_refs=tuple(str(ref) for ref in item["sourceThreadNoteRefs"]),
                review_status="unreviewed",
                generator=provider_name(config.provider),
                schema_version=str(DECISION_SCHEMA_VERSION),
                prompt_version=DECISION_PROFILE.prompt.version,
                record_excerpt=rendered,
            )
            created.append(decision)
        decision_ref = f"project:/decisions/{decision.path.name}"
        for source in item_sources:
            decision_ids_by_source[source.source_ref].append(decision_id)
            decision_refs_by_source[source.source_ref].append(decision_ref)

    for source_ref in decision_ids_by_source:
        decision_ids_by_source[source_ref] = unique_ordered(decision_ids_by_source[source_ref])
        decision_refs_by_source[source_ref] = unique_ordered(decision_refs_by_source[source_ref])

    state_path = decision_state_path(sources[0].project)
    old_state = state_path.read_bytes() if state_path.exists() else None
    changed_existing_ids = set(existing_source_updates) | {item.decision_id for item in updated}
    old_decisions = {
        existing_by_id[decision_id].path: existing_by_id[decision_id].path.read_bytes()
        for decision_id in changed_existing_ids
    }
    previous_state = deepcopy(state)
    written_paths: list[Path] = []
    try:
        for path, rendered in created_text.items():
            atomic_write_text(path, rendered)
            written_paths.append(path)
            validate_decision_record(path)
        for path, rendered in updated_text.items():
            atomic_write_text(path, rendered)
            validate_decision_record(path)
        for decision_id, decision_sources in existing_source_updates.items():
            decision = existing_by_id[decision_id]
            updated_decision_text = _extend_existing_decision_sources(
                decision,
                decision_sources,
                timestamp=timestamp,
            )
            if updated_decision_text is not None:
                atomic_write_text(decision.path, updated_decision_text)
                validate_decision_record(decision.path)
        for source in sources:
            decision_ids = decision_ids_by_source[source.source_ref]
            state["sources"][source.relative_path] = {
                "sourceSha256": source.source_sha256,
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
        for path, content in old_decisions.items():
            atomic_write_bytes(path, content)
        if old_state is None:
            if state_path.exists():
                state_path.unlink()
        else:
            atomic_write_bytes(state_path, old_state)
        state.clear()
        state.update(previous_state)
        raise
    return DecisionBatchCommit(
        created=created,
        updated=updated,
        referenced=unique_ordered(referenced),
        decision_ids_by_source=decision_ids_by_source,
        decision_refs_by_source=decision_refs_by_source,
    )


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
    batches = decision_source_batches(candidates)
    existing = load_existing_decisions(project)
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": None,
        "mode": "decision-build",
        "projectId": project.project_id,
        "dryRun": not write,
        "force": force,
        "generator": {
            "provider": config.provider,
            "name": provider_name(config.provider),
            "model": config.model,
            "reasoningEffort": config.reasoning_effort,
        },
        "scan": scan,
        "existingDecisionCount": len(existing),
        "existingDecisionIndexLimit": MAX_EXISTING_DECISION_INDEX,
        "existingDecisionIndexOmittedCount": 0,
        "selectedCount": len(candidates),
        "synthesisBatchCount": len(batches),
        "batches": [],
        "processed": [],
        "created": [],
        "updated": [],
        "referencedExisting": [],
        "noAction": [],
        "failed": list(scan_failures),
        "deferred": [],
        "warnings": [],
    }
    _refresh_existing_decision_index_report(report, len(existing))
    if not write:
        report["selected"] = [
            {
                "threadNote": source.relative_path,
                "threadNoteId": source.thread_note_id,
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
    for batch_index, batch in enumerate(batches):
        if now_local() >= start_deadline:
            report["deferred"].extend(
                {
                    "threadNote": item.relative_path,
                    "reason": "runtime-deadline",
                }
                for remaining_batch in batches[batch_index:]
                for item in remaining_batch
            )
            break
        batch_started = time.monotonic()
        if progress:
            progress(
                {
                    "type": "decision-batch-start",
                    "index": batch_index + 1,
                    "total": len(batches),
                    "threadNotes": [source.relative_path for source in batch],
                }
            )
        try:
            output = generator.generate(batch, existing)
            validate_decision_output(
                output,
                {item.decision_id for item in existing},
                {source.source_ref for source in batch},
                {item.decision_id for item in existing if item.update_allowed},
                {item.decision_id for item in existing if item.quality_upgrade_required},
            )
            committed = _commit_batch(
                batch,
                config,
                state,
                existing,
                output,
            )
        except Exception as exc:  # Per-batch isolation is intentional.
            report["failed"].extend(
                {"threadNote": source.relative_path, "error": str(exc)} for source in batch
            )
            if progress:
                progress(
                    {
                        "type": "decision-batch-failed",
                        "index": batch_index + 1,
                        "total": len(batches),
                        "threadNotes": [source.relative_path for source in batch],
                        "error": str(exc),
                    }
                )
            continue
        if committed.updated:
            updated_by_id = {item.decision_id: item for item in committed.updated}
            existing = [updated_by_id.get(item.decision_id, item) for item in existing]
        existing.extend(committed.created)
        _refresh_existing_decision_index_report(report, len(existing))
        duration = round(time.monotonic() - batch_started, 3)
        metrics = deepcopy(getattr(generator, "last_metrics", {}))
        created_values = [
            {
                "decisionId": item.decision_id,
                "decisionRecord": item.path.relative_to(project.context_path).as_posix(),
                "sourceThreadNoteRefs": list(item.source_refs),
            }
            for item in committed.created
        ]
        updated_values = [
            {
                "decisionId": item.decision_id,
                "decisionRecord": item.path.relative_to(project.context_path).as_posix(),
                "sourceThreadNoteRefs": list(item.source_refs),
            }
            for item in committed.updated
        ]
        report["created"].extend(created_values)
        report["updated"].extend(updated_values)
        report["referencedExisting"].extend(committed.referenced)
        report["batches"].append(
            {
                "batchIndex": batch_index + 1,
                "threadNotes": [source.relative_path for source in batch],
                "created": created_values,
                "updated": updated_values,
                "referencedExisting": committed.referenced,
                "sourceLimitations": output["sourceLimitations"],
                "durationSeconds": duration,
                **metrics,
            }
        )
        for source in batch:
            decision_ids = committed.decision_ids_by_source[source.source_ref]
            source_created = [item for item in created_values if item["decisionId"] in decision_ids]
            source_updated = [item for item in updated_values if item["decisionId"] in decision_ids]
            source_referenced = [item for item in committed.referenced if item in decision_ids]
            if not decision_ids:
                report["noAction"].append(source.relative_path)
            report["processed"].append(
                {
                    "threadNote": source.relative_path,
                    "created": source_created,
                    "updated": source_updated,
                    "referencedExisting": source_referenced,
                    "decisionRefs": committed.decision_refs_by_source[source.source_ref],
                    "sourceLimitations": output["sourceLimitations"],
                    "batchIndex": batch_index + 1,
                }
            )
        if progress:
            progress(
                {
                    "type": "decision-batch-complete",
                    "index": batch_index + 1,
                    "total": len(batches),
                    "threadNotes": [source.relative_path for source in batch],
                    "createdCount": len(committed.created),
                    "updatedCount": len(committed.updated),
                    "decisionRecordPaths": [str(item.path.absolute()) for item in committed.created],
                    "referencedCount": len(committed.referenced),
                    "durationSeconds": duration,
                    **metrics,
                }
            )
    report["finishedAt"] = now_iso()
    report_path = write_run_report(cache_root or project.context_path, report)
    return report, report_path
