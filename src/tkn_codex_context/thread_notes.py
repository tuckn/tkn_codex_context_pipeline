"""Background Codex chat-to-Thread-Note pipeline."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .chat_logs import (
    ChatEvent,
    default_sessions_root,
    fingerprint_events,
    has_clean_user_message,
    is_approval_review,
    is_known_internal_thread,
    normalize_path_text,
    path_is_within,
    read_thread_events,
    read_thread_log,
    source_ref,
)
from .common import frontmatter, slugify
from .frontmatter import (
    frontmatter_list_value,
    parse_simple_frontmatter,
    split_frontmatter_lines,
)
from .prompting import (
    render_chunk_prompt,
    render_reduction_prompt,
    render_repair_prompt,
)
from .safety import redact_secret_like_content
from .summary_resources import (
    SummaryTemplate,
    load_summary_profile,
    render_summary_template,
    validate_summary_output_schema,
)

CONFIG_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 2
LEGACY_STATE_SCHEMA_VERSION = 1
THREAD_NOTE_SCHEMA_VERSION = 3
CONFIG_FILENAME = "thread-note-pipeline.json"
STATE_FILENAME = "chat-refresh-state.json"
DEFAULT_SOURCE_ID = "windows" if os.name == "nt" else "local"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_IDLE_MINUTES = 30
DEFAULT_RUNTIME_MINUTES = 230
DEFAULT_MODEL_TIMEOUT_SECONDS = 1800
DEFAULT_CHUNK_CHARACTERS = 120_000
MAX_EVENT_TEXT_CHARACTERS = 8_000
GENERATOR_PROMPT_VERSION = 4
RENDERER_VERSION = 6
REBUILD_WORK_SCHEMA_VERSION = 1
IN_FLIGHT_GRACE_MINUTES = 9
MAX_NOTE_NARRATIVE_CHARACTERS = 9_000
AVOIDABLE_ENGLISH_PHRASES = {
    "actual execution",
    "supplied events",
}
SUMMARY_PROFILE = load_summary_profile()
SUMMARY_PROMPT_RESOURCE = SUMMARY_PROFILE.prompt
SUMMARY_SCHEMA_RESOURCE = SUMMARY_PROFILE.schema
SUMMARY_TEMPLATE_RESOURCE = SUMMARY_PROFILE.template
NOTE_SCHEMA = SUMMARY_SCHEMA_RESOURCE.value
MAX_SUMMARY_ITEMS = int(NOTE_SCHEMA["properties"]["summaryItems"]["maxItems"])
MAX_WORK_ITEMS = int(NOTE_SCHEMA["properties"]["workItems"]["maxItems"])
MAX_DEVELOPMENTS_PER_WORK_ITEM = int(
    NOTE_SCHEMA["properties"]["workItems"]["items"]["properties"]["developments"][
        "maxItems"
    ]
)
MAX_EVIDENCE_ITEMS = int(NOTE_SCHEMA["properties"]["evidence"]["maxItems"])
MAX_SOURCE_LIMITATIONS = int(
    NOTE_SCHEMA["properties"]["sourceLimitations"]["maxItems"]
)
MAX_SUMMARY_TEXT_CHARACTERS = int(
    NOTE_SCHEMA["properties"]["summaryItems"]["items"]["properties"]["text"]["maxLength"]
)
MAX_DEVELOPMENT_TEXT_CHARACTERS = int(
    NOTE_SCHEMA["properties"]["workItems"]["items"]["properties"]["developments"][
        "items"
    ]["properties"]["text"]["maxLength"]
)
MAX_EVIDENCE_TEXT_CHARACTERS = int(
    NOTE_SCHEMA["properties"]["evidence"]["items"]["properties"]["text"]["maxLength"]
)
ALLOWED_STATUS = set(
    NOTE_SCHEMA["properties"]["lastKnownState"]["properties"]["workState"]["enum"]
)
ALLOWED_LABELS = set(
    NOTE_SCHEMA["properties"]["workItems"]["items"]["properties"]["developments"][
        "items"
    ]["properties"]["label"]["enum"]
)


class PipelineError(RuntimeError):
    pass


class PartialPipelineError(PipelineError):
    pass


@dataclass(frozen=True)
class PipelineConfig:
    installed_at: str
    sessions_root: Path
    source_id: str
    codex_bin: str
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    idle_minutes: int = DEFAULT_IDLE_MINUTES
    runtime_minutes: int = DEFAULT_RUNTIME_MINUTES
    model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Project:
    project_id: str
    title: str
    current_root: Path
    context_path: Path
    historical_roots: tuple[Path, ...] = ()
    active_roots: tuple[Path, ...] = ()
    source_project_id: str = ""
    assigned_thread_ids: frozenset[str] = frozenset()
    foreign_assigned_thread_ids: frozenset[str] = frozenset()
    projectless_thread_ids: frozenset[str] = frozenset()
    state_directory: Path | None = None

    @property
    def thread_notes_path(self) -> Path:
        return self.context_path / "thread-notes"

    @property
    def state_path(self) -> Path:
        return (self.state_directory or self.context_path) / STATE_FILENAME


@dataclass(frozen=True)
class Candidate:
    project: Project
    thread_id: str
    started_at: str
    source_path: Path
    source_ref: str
    source_relative_ref: str
    fingerprint: str
    events: tuple[ChatEvent, ...]
    source_mtime_ns: int
    source_size: int


@dataclass(frozen=True)
class PreparedEvent:
    id: str
    kind: str
    actor: str
    name: str
    text: str
    timestamp: str
    turn_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "kind": self.kind,
            "actor": self.actor,
            "name": self.name,
            "text": self.text,
            "timestamp": self.timestamp,
            "turnId": self.turn_id,
        }


class Summarizer(Protocol):
    def generate(self, candidate: Candidate) -> dict[str, Any]: ...


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def default_store_root() -> Path:
    return Path.home() / ".tkn" / "codex_context_pipeline"


def default_config_path() -> Path:
    return default_store_root() / "config.yaml"


def default_registry_path() -> Path:
    return default_store_root() / "data" / "project-registry.jsonl"


def default_cache_root() -> Path:
    override = os.environ.get("XDG_CACHE_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".cache"
    return base / "codex_context_pipeline"


def parse_datetime(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PipelineError(f"invalid ISO 8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp-{uuid.uuid4().hex[:12]}"
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def resolve_codex_bin(value: str = "") -> str:
    candidate = value.strip() or shutil.which("codex") or ""
    if not candidate:
        raise PipelineError("standalone Codex CLI was not found")
    resolved = str(Path(candidate).expanduser().absolute())
    if "\\windowsapps\\" in resolved.casefold():
        raise PipelineError("the Codex App WindowsApps executable cannot be used for automation")
    if not Path(resolved).is_file():
        raise PipelineError(f"Codex CLI executable not found: {resolved}")
    return resolved


def make_config(
    *,
    existing: PipelineConfig | None = None,
    sessions_root: Path | None = None,
    codex_bin: str = "",
    installed_at: str | None = None,
) -> PipelineConfig:
    return PipelineConfig(
        installed_at=installed_at or (existing.installed_at if existing else now_iso()),
        sessions_root=(sessions_root or (existing.sessions_root if existing else default_sessions_root()))
        .expanduser()
        .absolute(),
        source_id=existing.source_id if existing else DEFAULT_SOURCE_ID,
        codex_bin=resolve_codex_bin(codex_bin or (existing.codex_bin if existing else "")),
        model=DEFAULT_MODEL,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        idle_minutes=existing.idle_minutes if existing else DEFAULT_IDLE_MINUTES,
        runtime_minutes=existing.runtime_minutes if existing else DEFAULT_RUNTIME_MINUTES,
        model_timeout_seconds=(existing.model_timeout_seconds if existing else DEFAULT_MODEL_TIMEOUT_SECONDS),
    )


def config_json(config: PipelineConfig) -> dict[str, Any]:
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "installedAt": config.installed_at,
        "sessionsRoot": str(config.sessions_root),
        "sourceId": config.source_id,
        "codexBin": config.codex_bin,
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
        "idleMinutes": config.idle_minutes,
        "runtimeMinutes": config.runtime_minutes,
        "modelTimeoutSeconds": config.model_timeout_seconds,
    }


def load_config(path: Path | None = None) -> PipelineConfig:
    resolved = path or default_config_path()
    if not resolved.is_file():
        raise PipelineError(f"pipeline config not found; run configure first: {resolved}")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read pipeline config: {resolved}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != CONFIG_SCHEMA_VERSION:
        raise PipelineError("unsupported thread-note pipeline config schemaVersion")
    if value.get("model") != DEFAULT_MODEL or value.get("reasoningEffort") != DEFAULT_REASONING_EFFORT:
        raise PipelineError(
            f"pipeline model must remain fixed at {DEFAULT_MODEL} with {DEFAULT_REASONING_EFFORT} reasoning"
        )
    config = PipelineConfig(
        installed_at=str(value.get("installedAt") or ""),
        sessions_root=Path(str(value.get("sessionsRoot") or "")).expanduser().absolute(),
        source_id=str(value.get("sourceId") or DEFAULT_SOURCE_ID),
        codex_bin=str(value.get("codexBin") or ""),
        model=str(value["model"]),
        reasoning_effort=str(value["reasoningEffort"]),
        idle_minutes=int(value.get("idleMinutes", DEFAULT_IDLE_MINUTES)),
        runtime_minutes=int(value.get("runtimeMinutes", DEFAULT_RUNTIME_MINUTES)),
        model_timeout_seconds=int(value.get("modelTimeoutSeconds", DEFAULT_MODEL_TIMEOUT_SECONDS)),
    )
    parse_datetime(config.installed_at)
    if not config.sessions_root.is_dir():
        raise PipelineError(f"sessions root not found: {config.sessions_root}")
    if config.idle_minutes < 0 or config.runtime_minutes <= 0 or config.model_timeout_seconds <= 0:
        raise PipelineError("pipeline timing values must be positive")
    return config


def write_config(path: Path, config: PipelineConfig) -> None:
    atomic_write_json(path, config_json(config))


def load_active_projects(registry_path: Path | None = None) -> list[Project]:
    path = registry_path or default_registry_path()
    if not path.is_file():
        raise PipelineError(f"project registry not found: {path}")
    projects: list[Project] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PipelineError(f"invalid registry JSON at line {line_number}: {exc}") from exc
            if not isinstance(value, dict) or value.get("status") != "active":
                continue
            project_id = str(value.get("projectId") or "")
            current_root = str(value.get("currentRoot") or "")
            data_path = str(value.get("projectDataPath") or "")
            state_path = str(value.get("projectStatePath") or "")
            if not project_id or not current_root or not data_path or not state_path:
                raise PipelineError(f"active registry record at line {line_number} is incomplete")
            if project_id in seen:
                raise PipelineError(f"duplicate active projectId in registry: {project_id}")
            seen.add(project_id)
            projects.append(
                Project(
                    project_id=project_id,
                    title=str(value.get("title") or project_id),
                    current_root=Path(current_root).expanduser().absolute(),
                    context_path=Path(data_path).expanduser().absolute(),
                    state_directory=Path(state_path).expanduser().absolute(),
                )
            )
    return sorted(projects, key=lambda item: item.project_id)


def empty_refresh_state(project_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "projectId": project_id,
        "approvedHistoricalRoots": [],
        "rejectedHistoricalRoots": [],
        "lastRefreshAt": None,
        "sources": {},
    }


def load_refresh_state(project: Project, config: PipelineConfig) -> dict[str, Any]:
    path = project.state_path
    if not path.exists():
        value = empty_refresh_state(project.project_id)
    else:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(f"cannot read refresh state: {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise PipelineError(f"refresh state must be a JSON object: {path}")
        if value.get("schemaVersion") == LEGACY_STATE_SCHEMA_VERSION:
            legacy_root = str(value.get("sourceRoot") or config.sessions_root)
            value = {
                "schemaVersion": STATE_SCHEMA_VERSION,
                "projectId": value.get("projectId"),
                "approvedHistoricalRoots": value.get("approvedHistoricalRoots", []),
                "rejectedHistoricalRoots": value.get("rejectedHistoricalRoots", []),
                "lastRefreshAt": value.get("lastRefreshAt"),
                "sources": {
                    config.source_id: {
                        "sourceRoot": legacy_root,
                        "lastRefreshAt": value.get("lastRefreshAt"),
                        "threads": value.get("threads", {}),
                    }
                },
            }
    if value.get("schemaVersion") != STATE_SCHEMA_VERSION:
        raise PipelineError(f"unsupported refresh state schemaVersion: {path}")
    if value.get("projectId") != project.project_id:
        raise PipelineError(f"refresh state projectId mismatch: {path}")
    sources = value.get("sources")
    if not isinstance(sources, dict):
        raise PipelineError(f"refresh state sources must be an object: {path}")
    source = sources.setdefault(
        config.source_id,
        {"sourceRoot": str(config.sessions_root), "lastRefreshAt": None, "threads": {}},
    )
    if not isinstance(source, dict) or not isinstance(source.get("threads"), dict):
        raise PipelineError(f"refresh state source is invalid: {path}")
    existing_root = str(source.get("sourceRoot") or "")
    if existing_root and normalize_path_text(existing_root) != normalize_path_text(str(config.sessions_root)):
        raise PipelineError(f"source id {config.source_id} is already bound to another sessions root")
    source["sourceRoot"] = str(config.sessions_root)
    return value


def update_refresh_state(
    project: Project,
    config: PipelineConfig,
    candidate: Candidate,
    note_path: Path,
) -> None:
    state = load_refresh_state(project, config)
    prompt = SUMMARY_PROMPT_RESOURCE
    source = state["sources"][config.source_id]
    relative_note = note_path.relative_to(project.context_path).as_posix()
    processed_at = now_iso()
    source["threads"][candidate.thread_id] = {
        "fingerprint": candidate.fingerprint,
        "generationFingerprint": generation_fingerprint(config, candidate),
        "threadNoteSchemaVersion": THREAD_NOTE_SCHEMA_VERSION,
        "generatorModel": config.model,
        "generatorReasoningEffort": config.reasoning_effort,
        "generatorPromptVersion": GENERATOR_PROMPT_VERSION,
        "summaryPromptId": prompt.prompt_id,
        "summaryPromptVersion": prompt.version,
        "summaryPromptSha256": prompt.sha256,
        "outputSchemaSha256": SUMMARY_SCHEMA_RESOURCE.sha256,
        "templateId": SUMMARY_TEMPLATE_RESOURCE.template_id,
        "templateVersion": SUMMARY_TEMPLATE_RESOURCE.version,
        "templateSha256": SUMMARY_TEMPLATE_RESOURCE.sha256,
        "rendererVersion": RENDERER_VERSION,
        "noteHash": sha256(note_path.read_bytes()).hexdigest(),
        "sourceRefs": [candidate.source_ref],
        "threadNotes": [relative_note],
        "processedAt": processed_at,
    }
    source["lastRefreshAt"] = processed_at
    state["lastRefreshAt"] = processed_at
    atomic_write_json(project.state_path, state)


def path_variants(value: str | Path) -> tuple[str, ...]:
    path = Path(value).expanduser().absolute()
    values: list[str] = []
    values.append(str(path))
    try:
        values.append(str(path.resolve(strict=False)))
    except OSError:
        pass
    return tuple(dict.fromkeys(values))


def project_roots(project: Project) -> tuple[str, ...]:
    values: list[str] = []
    for root in (
        project.current_root,
        *project.active_roots,
        *project.historical_roots,
    ):
        values.extend(path_variants(root))
    return tuple(dict.fromkeys(values))


def project_with_state_roots(
    project: Project,
    state: dict[str, Any],
) -> Project:
    raw_roots = state.get("approvedHistoricalRoots", [])
    if not isinstance(raw_roots, list):
        raise PipelineError(f"approvedHistoricalRoots must be an array: {project.state_path}")
    state_roots = tuple(Path(str(value)).expanduser().absolute() for value in raw_roots if str(value).strip())
    roots = tuple(dict.fromkeys((*project.historical_roots, *state_roots)))
    return replace(project, historical_roots=roots)


def event_matches_project(event: ChatEvent, project: Project) -> bool:
    return any(
        path_is_within(cwd, root)
        for cwd in path_variants(event.cwd)
        for root in project_roots(project)
    )


def events_for_project(
    thread_id: str,
    events: Sequence[ChatEvent],
    project: Project,
) -> tuple[ChatEvent, ...]:
    """Use explicit Codex app assignment before cwd-based attribution."""

    if thread_id in project.assigned_thread_ids:
        return tuple(events)
    return tuple(event for event in events if event_matches_project(event, project))


def candidate_for_project(
    path: Path,
    project: Project,
    config: PipelineConfig,
    *,
    backfill: bool,
) -> Candidate | None:
    stat = path.stat()
    if not backfill and datetime.fromtimestamp(stat.st_mtime, tz=now_local().tzinfo) < parse_datetime(
        config.installed_at
    ):
        return None
    idle_cutoff = now_local() - timedelta(minutes=config.idle_minutes)
    if datetime.fromtimestamp(stat.st_mtime, tz=idle_cutoff.tzinfo) > idle_cutoff:
        return None
    thread_log = read_thread_log(path)
    if not thread_log or is_approval_review(thread_log) or is_known_internal_thread(thread_log):
        return None
    if not has_clean_user_message(thread_log):
        return None
    events = events_for_project(thread_log.id, read_thread_events(path), project)
    if not any(event.kind == "user_message" for event in events):
        return None
    relative_ref = source_ref(path, config.sessions_root)
    qualified_ref = f"{config.source_id}/{relative_ref}"
    return Candidate(
        project=project,
        thread_id=thread_log.id,
        started_at=thread_log.timestamp,
        source_path=path,
        source_ref=qualified_ref,
        source_relative_ref=relative_ref,
        fingerprint=fingerprint_events(thread_log.id, events, qualified_ref),
        events=events,
        source_mtime_ns=stat.st_mtime_ns,
        source_size=stat.st_size,
    )


def round_robin(groups: dict[str, list[Candidate]]) -> list[Candidate]:
    ordered: list[Candidate] = []
    keys = sorted(groups)
    indexes = {key: 0 for key in keys}
    while True:
        added = False
        for key in keys:
            index = indexes[key]
            if index < len(groups[key]):
                ordered.append(groups[key][index])
                indexes[key] += 1
                added = True
        if not added:
            return ordered


def scan_candidates(
    config: PipelineConfig,
    projects: Sequence[Project],
    *,
    backfill: bool = False,
    project_ids: Sequence[str] = (),
    thread_ids: Sequence[str] = (),
    ignore_fingerprints: bool = False,
) -> tuple[list[Candidate], dict[str, int]]:
    selected_projects = [project for project in projects if not project_ids or project.project_id in set(project_ids)]
    missing = set(project_ids) - {project.project_id for project in selected_projects}
    if missing:
        raise PipelineError(f"unknown or inactive projectId: {', '.join(sorted(missing))}")
    states = {project.project_id: load_refresh_state(project, config) for project in selected_projects}
    selected_projects = [project_with_state_roots(project, states[project.project_id]) for project in selected_projects]
    groups: dict[str, list[Candidate]] = {project.project_id: [] for project in selected_projects}
    counts = {
        "files": 0,
        "eligible": 0,
        "unchanged": 0,
        "staleGenerator": 0,
        "ignoredFiles": 0,
        "excludedApprovalOrInternal": 0,
        "excludedWithoutUserMessage": 0,
        "assigned": 0,
        "ambiguous": 0,
        "projectless": 0,
        "unmatched": 0,
    }
    assigned_to: dict[str, str] = {}
    projectless: set[str] = set()
    for project in selected_projects:
        projectless.update(project.projectless_thread_ids)
        for thread_id in project.assigned_thread_ids:
            prior = assigned_to.setdefault(thread_id, project.project_id)
            if prior != project.project_id:
                raise PipelineError(f"thread {thread_id} is assigned to multiple context projects")
    thread_filter = set(thread_ids)
    for path in sorted(config.sessions_root.rglob("*.jsonl")):
        counts["files"] += 1
        stat = path.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=now_local().tzinfo)
        watermark = parse_datetime(config.installed_at)
        if (not backfill and modified_at < watermark) or (backfill and modified_at >= watermark):
            counts["ignoredFiles"] += 1
            continue
        idle_cutoff = now_local() - timedelta(minutes=config.idle_minutes)
        if datetime.fromtimestamp(stat.st_mtime, tz=idle_cutoff.tzinfo) > idle_cutoff:
            counts["ignoredFiles"] += 1
            continue
        thread_log = read_thread_log(path)
        if not thread_log:
            counts["ignoredFiles"] += 1
            continue
        if is_approval_review(thread_log) or is_known_internal_thread(thread_log):
            counts["ignoredFiles"] += 1
            counts["excludedApprovalOrInternal"] += 1
            continue
        if not has_clean_user_message(thread_log):
            counts["ignoredFiles"] += 1
            counts["excludedWithoutUserMessage"] += 1
            continue
        all_events = read_thread_events(path)
        if thread_log.id in projectless:
            counts["projectless"] += 1
            counts["ignoredFiles"] += 1
            continue
        if thread_log.id not in assigned_to and any(
            thread_log.id in project.foreign_assigned_thread_ids for project in selected_projects
        ):
            counts["unmatched"] += 1
            counts["ignoredFiles"] += 1
            continue
        explicit_project_id = assigned_to.get(thread_log.id)
        if explicit_project_id:
            matched_projects = [project for project in selected_projects if project.project_id == explicit_project_id]
            counts["assigned"] += 1
        else:
            matched_projects = [
                project
                for project in selected_projects
                if any(event_matches_project(event, project) for event in all_events)
            ]
        if len(matched_projects) > 1:
            counts["ambiguous"] += 1
            counts["ignoredFiles"] += 1
            continue
        if not matched_projects:
            counts["unmatched"] += 1
            counts["ignoredFiles"] += 1
            continue
        project = matched_projects[0]
        events = events_for_project(thread_log.id, all_events, project)
        if not any(event.kind == "user_message" for event in events):
            counts["ignoredFiles"] += 1
            continue
        matched_file = False
        for project in matched_projects:
            relative_ref = source_ref(path, config.sessions_root)
            qualified_ref = f"{config.source_id}/{relative_ref}"
            candidate = Candidate(
                project=project,
                thread_id=thread_log.id,
                started_at=thread_log.timestamp,
                source_path=path,
                source_ref=qualified_ref,
                source_relative_ref=relative_ref,
                fingerprint=fingerprint_events(thread_log.id, events, qualified_ref),
                events=events,
                source_mtime_ns=stat.st_mtime_ns,
                source_size=stat.st_size,
            )
            if thread_filter and candidate.thread_id not in thread_filter:
                continue
            matched_file = True
            source = states[project.project_id]["sources"][config.source_id]
            prior = source["threads"].get(candidate.thread_id, {})
            if not ignore_fingerprints and prior.get("fingerprint") == candidate.fingerprint:
                if (
                    prior.get("generationFingerprint") == generation_fingerprint(config, candidate)
                    and current_note_matches_generation(candidate, config)
                ):
                    counts["unchanged"] += 1
                    continue
                counts["staleGenerator"] += 1
            groups[project.project_id].append(candidate)
            counts["eligible"] += 1
        if not matched_file:
            counts["ignoredFiles"] += 1
    for candidates in groups.values():
        candidates.sort(key=lambda item: (item.started_at, item.thread_id))
    return round_robin(groups), counts


def truncate_text(text: str, limit: int = MAX_EVENT_TEXT_CHARACTERS) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n[TRUNCATED {len(text) - limit} CHARACTERS]\n"
    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]


def prepare_events(events: Sequence[ChatEvent]) -> list[PreparedEvent]:
    return [
        PreparedEvent(
            id=event.id,
            kind=event.kind,
            actor=event.actor,
            name=event.name,
            text=truncate_text(redact_secret_like_content(event.text)),
            timestamp=event.timestamp,
            turn_id=event.turn_id,
        )
        for event in events
    ]


def chunk_events(
    events: Sequence[PreparedEvent],
    target_characters: int = DEFAULT_CHUNK_CHARACTERS,
) -> list[list[PreparedEvent]]:
    chunks: list[list[PreparedEvent]] = []
    current: list[PreparedEvent] = []
    current_size = 0
    for event in events:
        rendered_size = len(json.dumps(event.as_dict(), ensure_ascii=False)) + 1
        if current and current_size + rendered_size > target_characters:
            chunks.append(current)
            current = []
            current_size = 0
        current.append(event)
        current_size += rendered_size
    if current:
        chunks.append(current)
    return chunks


def validate_note_data(value: Any, allowed_event_ids: set[str]) -> dict[str, Any]:
    try:
        validate_summary_output_schema(value, NOTE_SCHEMA)
    except ValueError as exc:
        raise PipelineError(f"Codex output does not match the summary schema: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError("Codex output must be a JSON object")
    required = set(NOTE_SCHEMA["required"])
    if not required.issubset(value):
        raise PipelineError(f"Codex output is missing fields: {', '.join(sorted(required - set(value)))}")
    if not all(isinstance(value.get(key), str) for key in ("title", "fileSlug", "description")):
        raise PipelineError("Codex output title, fileSlug, and description must be strings")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value["fileSlug"]):
        raise PipelineError("Codex output has invalid fileSlug")
    summary_items = value.get("summaryItems")
    work_items = value.get("workItems")
    last_state = value.get("lastKnownState")
    if (
        not isinstance(summary_items, list)
        or not 1 <= len(summary_items) <= MAX_SUMMARY_ITEMS
        or not isinstance(work_items, list)
        or not isinstance(last_state, dict)
    ):
        raise PipelineError("Codex output has invalid summaryItems, workItems, or lastKnownState")
    if len(work_items) > MAX_WORK_ITEMS:
        raise PipelineError("Codex output has too many work items")
    if last_state.get("workState") not in ALLOWED_STATUS:
        raise PipelineError("Codex output has invalid workState")
    if not isinstance(last_state.get("unresolved"), list) or not isinstance(last_state.get("unverified"), list):
        raise PipelineError("Codex output has invalid unresolved or unverified items")
    if last_state["workState"] == "done" and (
        last_state["unresolved"] or str(last_state.get("continuationPoint") or "").strip()
    ):
        raise PipelineError(
            "done work cannot contain unresolved items or a continuation point; "
            "use unverified for checks outside the completed request"
        )
    cited: list[str] = list(last_state.get("eventIds") or [])
    for item in summary_items:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            raise PipelineError("Codex output has an invalid summary item")
        if len(str(item["text"])) > MAX_SUMMARY_TEXT_CHARACTERS:
            raise PipelineError("Codex output summary item is too long")
        cited.extend(item.get("eventIds") or [])
    for work_item in work_items:
        if not isinstance(work_item, dict) or not isinstance(work_item.get("developments"), list):
            raise PipelineError("Codex output has an invalid work item")
        if len(work_item["developments"]) > MAX_DEVELOPMENTS_PER_WORK_ITEM:
            raise PipelineError("Codex output has too many developments in a work item")
        for item in work_item["developments"]:
            if not isinstance(item, dict) or item.get("label") not in ALLOWED_LABELS:
                raise PipelineError("Codex output has an invalid key development")
            if (
                not str(item.get("text") or "").strip()
                or len(str(item["text"])) > MAX_DEVELOPMENT_TEXT_CHARACTERS
            ):
                raise PipelineError("Codex output has an empty or overly long development")
            cited.extend(item.get("eventIds") or [])
    evidence = value.get("evidence")
    if not isinstance(evidence, list):
        raise PipelineError("Codex output has invalid evidence")
    for item in evidence:
        if not isinstance(item, dict):
            raise PipelineError("Codex output has invalid evidence")
        if (
            not str(item.get("text") or "").strip()
            or len(str(item["text"])) > MAX_EVIDENCE_TEXT_CHARACTERS
        ):
            raise PipelineError("Codex output has an empty or overly long evidence item")
        cited.extend(item.get("eventIds") or [])
    if len(evidence) > MAX_EVIDENCE_ITEMS:
        raise PipelineError("Codex output has too many evidence items")
    source_limitations = value.get("sourceLimitations")
    if not isinstance(source_limitations, list) or len(source_limitations) > MAX_SOURCE_LIMITATIONS:
        raise PipelineError("Codex output has invalid sourceLimitations")
    invalid = {str(item) for item in cited if str(item) not in allowed_event_ids}
    if invalid:
        raise PipelineError(f"Codex output cited unknown event ids: {', '.join(sorted(invalid))}")
    narrative = json.dumps(value, ensure_ascii=False)
    if len(narrative) > MAX_NOTE_NARRATIVE_CHARACTERS:
        raise PipelineError("Codex output exceeds the thread-note narrative size limit")
    avoidable = sorted(phrase for phrase in AVOIDABLE_ENGLISH_PHRASES if phrase in narrative.casefold())
    if avoidable:
        raise PipelineError("Codex output contains avoidable English prose: " + ", ".join(avoidable))
    return value


class CodexSummarizer:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        chunk_characters: int = DEFAULT_CHUNK_CHARACTERS,
        sleeper: Callable[[float], None] = time.sleep,
        observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.chunk_characters = chunk_characters
        self.sleeper = sleeper
        self.observer = observer
        self.prompt = SUMMARY_PROMPT_RESOURCE
        self.deadline: datetime | None = None
        self.last_metrics: dict[str, int] = {}

    def set_deadline(self, deadline: datetime) -> None:
        self.deadline = deadline

    def _emit(self, event: dict[str, Any]) -> None:
        if self.observer:
            self.observer(event)

    def _invoke(self, prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="tkn-thread-note-") as directory:
            temp = Path(directory)
            schema_path = temp / "schema.json"
            output_path = temp / "output.json"
            schema_path.write_text(json.dumps(NOTE_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")
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
                        raise PartialPipelineError("rebuild deadline reached during model generation")
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
            raise PipelineError(last_error or "Codex generation failed")

    def _validated_invoke(
        self,
        prompt: str,
        allowed_event_ids: set[str],
        thread_id: str,
    ) -> dict[str, Any]:
        current_prompt = prompt
        for semantic_attempt in range(2):
            value = self._invoke(current_prompt)
            try:
                return validate_note_data(value, allowed_event_ids)
            except PipelineError as exc:
                if semantic_attempt:
                    raise
                self.last_metrics["semanticRetries"] = self.last_metrics.get("semanticRetries", 0) + 1
                current_prompt = render_repair_prompt(
                    self.prompt,
                    thread_id=thread_id,
                    validation_error=str(exc),
                    draft=value,
                )
        raise PipelineError("Codex semantic validation did not produce a valid note")

    def generate(self, candidate: Candidate) -> dict[str, Any]:
        self.last_metrics = {
            "chunkCount": 0,
            "modelCalls": 0,
            "transportRetries": 0,
            "semanticRetries": 0,
        }
        prepared = prepare_events(candidate.events)
        allowed_ids = {event.id for event in prepared}
        chunks = chunk_events(prepared, self.chunk_characters)
        self.last_metrics["chunkCount"] = len(chunks)
        partials: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, 1):
            self._emit(
                {
                    "type": "chunk-start",
                    "threadId": candidate.thread_id,
                    "chunk": index,
                    "chunkCount": len(chunks),
                }
            )
            prompt = render_chunk_prompt(
                self.prompt,
                thread_id=candidate.thread_id,
                part=index,
                part_count=len(chunks),
                events=[event.as_dict() for event in chunk],
            )
            partials.append(
                self._validated_invoke(prompt, allowed_ids, candidate.thread_id)
            )
        if len(partials) == 1:
            return partials[0]
        reduction_prompt = render_reduction_prompt(
            self.prompt,
            thread_id=candidate.thread_id,
            partials=partials,
        )
        return self._validated_invoke(
            reduction_prompt,
            allowed_ids,
            candidate.thread_id,
        )


def source_timestamp(value: str) -> datetime:
    try:
        return parse_datetime(value).astimezone()
    except PipelineError:
        return now_local()


def generator_fingerprint(config: PipelineConfig) -> str:
    prompt = SUMMARY_PROMPT_RESOURCE
    value = {
        "model": config.model,
        "reasoningEffort": config.reasoning_effort,
        "generatorPromptVersion": GENERATOR_PROMPT_VERSION,
        "summaryPromptId": prompt.prompt_id,
        "summaryPromptVersion": prompt.version,
        "summaryPromptSha256": prompt.sha256,
        "outputSchemaSha256": SUMMARY_SCHEMA_RESOURCE.sha256,
        "templateId": SUMMARY_TEMPLATE_RESOURCE.template_id,
        "templateVersion": SUMMARY_TEMPLATE_RESOURCE.version,
        "templateSha256": SUMMARY_TEMPLATE_RESOURCE.sha256,
        "rendererVersion": RENDERER_VERSION,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def generation_fingerprint(config: PipelineConfig, candidate: Candidate) -> str:
    return sha256(f"{candidate.fingerprint}:{generator_fingerprint(config)}".encode()).hexdigest()


def event_citation(event_ids: Sequence[str]) -> str:
    values = [str(value) for value in event_ids if str(value)]
    return f" 〔{', '.join(values)}〕" if values else ""


def file_slug_from_note_path(candidate: Candidate, note_path: Path) -> str:
    thread_note_id = source_timestamp(candidate.started_at).strftime("%Y%m%dT%H%M%S%z")
    prefix = f"{thread_note_id}-"
    if not note_path.stem.startswith(prefix):
        raise PipelineError(f"thread note filename has an invalid timestamp prefix: {note_path}")
    return note_path.stem[len(prefix) :]


def find_note_matches(project: Project, thread_id: str) -> list[Path]:
    if not project.thread_notes_path.is_dir():
        return []
    matches: list[Path] = []
    for path in sorted(project.thread_notes_path.glob("*.md")):
        try:
            lines, _body = split_frontmatter_lines(path.read_text(encoding="utf-8-sig"))
        except (OSError, SystemExit):
            continue
        if frontmatter_list_value(lines, "sourceThreadIds") == [thread_id]:
            matches.append(path)
    return matches


def choose_note_path(
    candidate: Candidate,
    title: str,
    *,
    thread_notes_path: Path | None = None,
    match_existing: bool = True,
) -> tuple[Path, dict[str, str]]:
    target = thread_notes_path or candidate.project.thread_notes_path
    matches = find_note_matches(candidate.project, candidate.thread_id) if match_existing else []
    if len(matches) > 1:
        raise PipelineError(
            f"multiple thread notes match thread {candidate.thread_id}: " + ", ".join(str(path) for path in matches)
        )
    if matches:
        existing_text = matches[0].read_text(encoding="utf-8-sig")
        return matches[0], parse_simple_frontmatter(existing_text)
    started = source_timestamp(candidate.started_at)
    thread_note_id = started.strftime("%Y%m%dT%H%M%S%z")
    file_slug = title if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", title) else slugify(title)
    base = target / f"{thread_note_id}-{file_slug}.md"
    if not base.exists():
        return base, {}
    suffix = re.sub(r"[^A-Za-z0-9]", "", candidate.thread_id)[:8] or "thread"
    return base.with_name(f"{base.stem}-{suffix}{base.suffix}"), {}


def render_note(
    candidate: Candidate,
    data: dict[str, Any],
    existing: dict[str, str],
    *,
    template: SummaryTemplate = SUMMARY_TEMPLATE_RESOURCE,
) -> str:
    default_prompt = SUMMARY_PROMPT_RESOURCE
    started = source_timestamp(candidate.started_at)
    created = existing.get("date") or started.isoformat(timespec="seconds")
    thread_note_id = existing.get("threadNoteId") or started.strftime("%Y%m%dT%H%M%S%z")
    last_state = data["lastKnownState"]
    rendered_at = now_iso()
    fields: list[tuple[str, str | int | list[str]]] = [
        ("type", "threadNote"),
        ("schemaVersion", THREAD_NOTE_SCHEMA_VERSION),
        ("title", str(data["title"]).strip() or "Codex thread"),
        ("description", str(data["description"]).strip()),
        ("generator", "Codex"),
        ("generatorModel", data.get("_generatorModel", DEFAULT_MODEL)),
        (
            "generatorReasoningEffort",
            data.get("_generatorReasoningEffort", DEFAULT_REASONING_EFFORT),
        ),
        ("promptId", data.get("_summaryPromptId", default_prompt.prompt_id)),
        ("promptVersion", data.get("_summaryPromptVersion", default_prompt.version)),
        ("outputSchemaSha256", SUMMARY_SCHEMA_RESOURCE.sha256),
        ("templateId", template.template_id),
        ("templateVersion", template.version),
        ("generatorPromptVersion", GENERATOR_PROMPT_VERSION),
        ("rendererVersion", RENDERER_VERSION),
        ("generatedAt", rendered_at),
        ("fileSlug", str(data["fileSlug"])),
        ("status", str(last_state["workState"])),
        ("reviewStatus", "unreviewed"),
        ("automatedValidation", "passed"),
        ("date", created),
        ("updated", rendered_at),
        ("threadNoteId", thread_note_id),
        ("sourceType", "codexChat"),
        ("sourceThreadIds", [candidate.thread_id]),
        ("sourceRefs", [candidate.source_ref]),
        ("sourceFingerprint", candidate.fingerprint),
    ]
    if candidate.project.source_project_id:
        fields.append(("sourceProjectId", candidate.project.source_project_id))
    summary_lines = [
        f"- {str(item['text']).strip()}{event_citation(item.get('eventIds', []))}"
        for item in data.get("summaryItems", [])
    ]
    development_lines: list[str] = []
    work_items = [item for item in data.get("workItems", []) if item.get("developments")]
    if not work_items:
        development_lines.append("- 確認できる記録なし。")
    for work_index, work_item in enumerate(work_items, 1):
        multiple = len(work_items) > 1
        if multiple:
            title = str(work_item.get("title") or f"Work item {work_index}").strip()
            development_lines.extend([f"### WI-{work_index:02d}: {title}", ""])
        grouped: dict[str, list[dict[str, Any]]] = {}
        for development in work_item["developments"]:
            grouped.setdefault(str(development["label"]), []).append(development)
        for label, developments in grouped.items():
            heading = "####" if multiple else "###"
            development_lines.extend([f"{heading} {label}", ""])
            for item in developments:
                development_lines.append(
                    f"- {str(item['text']).strip()}"
                    f"{event_citation(item.get('eventIds', []))}"
                )
            development_lines.append("")
        while development_lines and not development_lines[-1]:
            development_lines.pop()
    last_state_lines = [
        f"- Work State: {last_state['workState']} — "
        f"{str(last_state['detail']).strip()}"
        f"{event_citation(last_state.get('eventIds', []))}",
        "- Latest User Direction: "
        + (str(last_state["latestUserDirection"]).strip() or "追加指示なし。"),
    ]
    for unresolved in last_state.get("unresolved", []):
        last_state_lines.append(f"- Unresolved: {str(unresolved).strip()}")
    for unverified in last_state.get("unverified", []):
        last_state_lines.append(f"- Unverified: {str(unverified).strip()}")
    continuation = str(last_state.get("continuationPoint") or "").strip()
    if continuation:
        last_state_lines.append(f"- Continuation Point: {continuation}")
    evidence = [item for item in data.get("evidence", []) if str(item.get("text") or "").strip()]
    evidence_section = ""
    if evidence:
        evidence_lines = [
            f"- {str(item['text']).strip()}{event_citation(item.get('eventIds', []))}"
            for item in evidence
        ]
        evidence_section = "\n\n## Evidence\n\n" + "\n".join(evidence_lines)
    limitations = [str(value).strip() for value in data.get("sourceLimitations", []) if str(value).strip()]
    source_notes_section = ""
    if limitations:
        source_notes_section = "\n\n## Source Notes\n\n" + "\n".join(
            f"- {value}" for value in limitations
        )
    return render_summary_template(
        template,
        {
            "frontmatter": frontmatter(fields),
            "summary": "\n".join(summary_lines),
            "key_developments": "\n".join(development_lines),
            "last_known_state": "\n".join(last_state_lines),
            "evidence_section": evidence_section,
            "source_notes_section": source_notes_section,
        },
    )


def revalidate_candidate(candidate: Candidate, config: PipelineConfig) -> None:
    try:
        candidate.source_path.stat()
    except OSError as exc:
        raise PipelineError(f"source log disappeared: {candidate.source_relative_ref}") from exc
    thread_log = read_thread_log(candidate.source_path)
    if not thread_log:
        raise PipelineError(f"source log no longer has metadata: {candidate.source_relative_ref}")
    events = events_for_project(
        thread_log.id,
        read_thread_events(candidate.source_path),
        candidate.project,
    )
    current = fingerprint_events(thread_log.id, events, candidate.source_ref)
    if current != candidate.fingerprint:
        raise PipelineError(f"source log changed during generation: {candidate.source_relative_ref}")


def write_candidate_note(
    candidate: Candidate,
    config: PipelineConfig,
    summarizer: Summarizer,
    *,
    work_cache_root: Path,
    force: bool = False,
) -> Path:
    project_key = sha256(candidate.project.project_id.encode()).hexdigest()[:16]
    thread_key = sha256(candidate.thread_id.encode()).hexdigest()[:16]
    pending_parent = work_cache_root.expanduser().absolute() / "pending" / project_key
    work_root = pending_parent / thread_key
    manifest_path = work_root / "manifest.json"
    staged_note: Path | None = None
    note_path: Path | None = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            manifest = None
        if not force and isinstance(manifest, dict):
            filename = str(manifest.get("file") or "")
            proposed = work_root / filename
            final = candidate.project.thread_notes_path / filename
            matches = find_note_matches(candidate.project, candidate.thread_id)
            if (
                manifest.get("sourceFingerprint") == candidate.fingerprint
                and manifest.get("generationFingerprint") == generation_fingerprint(config, candidate)
                and filename
                and Path(filename).name == filename
                and proposed.is_file()
                and (not matches or matches == [final])
            ):
                validate_thread_note(proposed)
                staged_note = proposed
                note_path = final
    if staged_note is None or note_path is None:
        if work_root.exists():
            if work_root.parent != pending_parent:
                raise PipelineError(f"refusing to remove unmanaged pending cache: {work_root}")
            shutil.rmtree(work_root)
        data = summarizer.generate(candidate)
        allowed_ids = {event.id for event in candidate.events}
        validate_note_data(data, allowed_ids)
        revalidate_candidate(candidate, config)
        note_path, existing = choose_note_path(
            candidate,
            str(data["fileSlug"]),
        )
        data["fileSlug"] = file_slug_from_note_path(candidate, note_path)
        data["_generatorModel"] = config.model
        data["_generatorReasoningEffort"] = config.reasoning_effort
        prompt = SUMMARY_PROMPT_RESOURCE
        data["_summaryPromptId"] = prompt.prompt_id
        data["_summaryPromptVersion"] = prompt.version
        rendered = render_note(candidate, data, existing)
        staged_note = work_root / note_path.name
        atomic_write_text(staged_note, rendered)
        validate_thread_note(staged_note)
        atomic_write_json(
            manifest_path,
            {
                "sourceFingerprint": candidate.fingerprint,
                "generationFingerprint": generation_fingerprint(config, candidate),
                "file": note_path.name,
                "noteHash": sha256(staged_note.read_bytes()).hexdigest(),
                "completedAt": now_iso(),
            },
        )
    rendered = staged_note.read_text(encoding="utf-8-sig")
    revalidate_candidate(candidate, config)
    old_note = note_path.read_bytes() if note_path.exists() else None
    old_state = candidate.project.state_path.read_bytes() if candidate.project.state_path.exists() else None
    try:
        atomic_write_text(note_path, rendered)
        update_refresh_state(candidate.project, config, candidate, note_path)
    except Exception:
        if old_note is None:
            if note_path.exists():
                note_path.unlink()
        else:
            atomic_write_text(note_path, old_note.decode("utf-8-sig"))
        if old_state is None:
            if candidate.project.state_path.exists():
                candidate.project.state_path.unlink()
        else:
            atomic_write_text(
                candidate.project.state_path,
                old_state.decode("utf-8-sig"),
            )
        raise
    if work_root.parent != pending_parent:
        raise PipelineError(f"refusing to remove unmanaged pending cache: {work_root}")
    shutil.rmtree(work_root)
    return note_path


def write_run_report(cache_root: Path, report: dict[str, Any]) -> Path:
    run_id = now_local().strftime("%Y%m%dT%H%M%S%z") + f"-{uuid.uuid4().hex[:8]}"
    path = cache_root / "runs" / f"{run_id}.json"
    atomic_write_json(path, report)
    return path


def normalize_git_remote(value: str) -> str:
    remote = value.strip().replace("\\", "/")
    if remote.startswith("git@") and ":" in remote:
        host, path = remote[4:].split(":", 1)
        remote = f"https://{host}/{path}"
    remote = re.sub(r"^ssh://git@", "https://", remote, flags=re.IGNORECASE)
    remote = remote.rstrip("/")
    if remote.casefold().endswith(".git"):
        remote = remote[:-4]
    return remote.casefold()


def git_remote(root: Path) -> str:
    if not root.is_dir():
        raise PipelineError(f"project root not found: {root}")
    completed = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    remote = normalize_git_remote(completed.stdout)
    if completed.returncode or not remote:
        raise PipelineError(f"cannot resolve Git origin for project root: {root}")
    return remote


def verify_historical_root(project: Project, historical_root: Path) -> Path:
    root = historical_root.expanduser().absolute()
    if normalize_path_text(str(root)) == normalize_path_text(str(project.current_root)):
        raise PipelineError(f"historical root is the current project root: {root}")
    if git_remote(project.current_root) != git_remote(root):
        raise PipelineError(f"historical root does not have the same Git origin as the current project: {root}")
    return root


def scan_rebuild_candidates(
    config: PipelineConfig,
    project: Project,
) -> tuple[list[Candidate], dict[str, int]]:
    candidates: list[Candidate] = []
    counts = {
        "files": 0,
        "matchedProject": 0,
        "eligible": 0,
        "excludedApprovalOrInternal": 0,
        "excludedWithoutUserMessage": 0,
        "unmatchedProject": 0,
    }
    for path in sorted(config.sessions_root.rglob("*.jsonl")):
        counts["files"] += 1
        thread_log = read_thread_log(path)
        if not thread_log:
            counts["unmatchedProject"] += 1
            continue
        if (
            thread_log.id in project.projectless_thread_ids
            or thread_log.id in project.foreign_assigned_thread_ids
        ):
            counts["unmatchedProject"] += 1
            continue
        all_events = read_thread_events(path)
        project_events = events_for_project(thread_log.id, all_events, project)
        if not project_events:
            counts["unmatchedProject"] += 1
            continue
        counts["matchedProject"] += 1
        if is_approval_review(thread_log) or is_known_internal_thread(thread_log):
            counts["excludedApprovalOrInternal"] += 1
            continue
        if not has_clean_user_message(thread_log) or not any(
            event.kind == "user_message" for event in project_events
        ):
            counts["excludedWithoutUserMessage"] += 1
            continue
        stat = path.stat()
        relative_ref = source_ref(path, config.sessions_root)
        qualified_ref = f"{config.source_id}/{relative_ref}"
        candidates.append(
            Candidate(
                project=project,
                thread_id=thread_log.id,
                started_at=thread_log.timestamp,
                source_path=path,
                source_ref=qualified_ref,
                source_relative_ref=relative_ref,
                fingerprint=fingerprint_events(thread_log.id, project_events, qualified_ref),
                events=project_events,
                source_mtime_ns=stat.st_mtime_ns,
                source_size=stat.st_size,
            )
        )
        counts["eligible"] += 1
    candidates.sort(key=lambda item: (item.started_at, item.thread_id))
    duplicate_threads = sorted(
        thread_id
        for thread_id in {item.thread_id for item in candidates}
        if sum(item.thread_id == thread_id for item in candidates) > 1
    )
    if duplicate_threads:
        raise PipelineError("duplicate source thread ids in rebuild input: " + ", ".join(duplicate_threads))
    return candidates, counts


def thread_note_metadata(path: Path) -> tuple[dict[str, str], list[str], list[str], str]:
    text = path.read_text(encoding="utf-8-sig")
    try:
        lines, _body = split_frontmatter_lines(text)
    except SystemExit as exc:
        raise PipelineError(f"invalid thread note frontmatter: {path}") from exc
    metadata = parse_simple_frontmatter(text)
    version = metadata.get("schemaVersion") or "1"
    return (
        metadata,
        frontmatter_list_value(lines, "sourceThreadIds"),
        frontmatter_list_value(lines, "sourceRefs"),
        version,
    )


def parse_thread_note_schema_version(version: str, path: Path) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise PipelineError(f"unsupported Thread Note schemaVersion {version}: {path.name}")
    return int(version)


def current_note_matches_generation(
    candidate: Candidate,
    config: PipelineConfig,
) -> bool:
    prompt = SUMMARY_PROMPT_RESOURCE
    matches = find_note_matches(candidate.project, candidate.thread_id)
    if len(matches) > 1:
        raise PipelineError(
            f"multiple thread notes match thread {candidate.thread_id}: "
            + ", ".join(str(path) for path in matches)
        )
    if not matches:
        return False
    path = matches[0]
    metadata, thread_ids, source_refs, version = thread_note_metadata(path)
    parsed_version = parse_thread_note_schema_version(version, path)
    if parsed_version > THREAD_NOTE_SCHEMA_VERSION:
        raise PipelineError(f"unsupported Thread Note schemaVersion {version}: {path.name}")
    return (
        parsed_version == THREAD_NOTE_SCHEMA_VERSION
        and metadata.get("type") == "threadNote"
        and thread_ids == [candidate.thread_id]
        and source_refs == [candidate.source_ref]
        and metadata.get("sourceFingerprint") == candidate.fingerprint
        and metadata.get("generatorModel") == config.model
        and metadata.get("generatorReasoningEffort") == config.reasoning_effort
        and metadata.get("promptId") == prompt.prompt_id
        and metadata.get("promptVersion") == prompt.version
        and metadata.get("outputSchemaSha256") == SUMMARY_SCHEMA_RESOURCE.sha256
        and metadata.get("templateId") == SUMMARY_TEMPLATE_RESOURCE.template_id
        and metadata.get("templateVersion") == SUMMARY_TEMPLATE_RESOURCE.version
        and metadata.get("generatorPromptVersion") == str(GENERATOR_PROMPT_VERSION)
        and metadata.get("rendererVersion") == str(RENDERER_VERSION)
    )


def validate_staged_thread_notes(
    thread_notes_path: Path,
    candidates: Sequence[Candidate],
    config: PipelineConfig,
    *,
    strict_threads: set[str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    prompt = SUMMARY_PROMPT_RESOURCE
    by_thread: dict[str, str] = {}
    note_hashes: dict[str, str] = {}
    candidates_by_thread = {candidate.thread_id: candidate for candidate in candidates}
    strict = strict_threads or set()
    for path in sorted(thread_notes_path.glob("*.md")):
        metadata, thread_ids, source_refs, version = thread_note_metadata(path)
        if parse_thread_note_schema_version(version, path) != THREAD_NOTE_SCHEMA_VERSION:
            raise PipelineError(
                f"staged thread note is not schemaVersion {THREAD_NOTE_SCHEMA_VERSION}: {path.name}"
            )
        if thread_ids:
            if metadata.get("type") != "threadNote":
                raise PipelineError(f"staged thread note has invalid type: {path.name}")
            if metadata.get("reviewStatus") != "unreviewed":
                raise PipelineError(f"chat-backed note is not unreviewed: {path.name}")
            if metadata.get("sourceType") != "codexChat" or not source_refs:
                raise PipelineError(f"chat-backed note has incomplete provenance: {path.name}")
            for thread_id in thread_ids:
                if thread_id in by_thread:
                    raise PipelineError(
                        f"multiple staged notes match thread {thread_id}: {by_thread[thread_id]}, {path.name}"
                    )
                by_thread[thread_id] = path.name
                note_hashes[thread_id] = sha256(path.read_bytes()).hexdigest()
        elif metadata.get("type") != "threadNote":
            raise PipelineError(f"staged thread note has invalid type: {path.name}")
        body = path.read_text(encoding="utf-8-sig")
        for heading in (
            "# Thread Note",
            "## Summary",
            "## Key Developments",
            "## Last Known State",
        ):
            if heading not in body:
                raise PipelineError(f"staged thread note is missing {heading}: {path.name}")
        status_match = re.search(
            r"(?m)^- Work State: (in-progress|blocked|waiting-for-user|done)\b",
            body,
        )
        if status_match and metadata.get("status") != status_match.group(1):
            raise PipelineError(f"frontmatter status and Last Known State differ: {path.name}")
        for thread_id in thread_ids:
            candidate = candidates_by_thread.get(thread_id)
            if candidate is None:
                continue
            if source_refs != [candidate.source_ref]:
                raise PipelineError(f"sourceRefs do not match source thread {thread_id}: {path.name}")
            expected_thread_note_id = source_timestamp(candidate.started_at).strftime("%Y%m%dT%H%M%S%z")
            expected_date = source_timestamp(candidate.started_at).isoformat(timespec="seconds")
            if metadata.get("threadNoteId") != expected_thread_note_id or not path.name.startswith(
                f"{expected_thread_note_id}-"
            ):
                raise PipelineError(f"filename or threadNoteId does not match source time: {path.name}")
            if metadata.get("date") != expected_date:
                raise PipelineError(f"date does not match source time: {path.name}")
            if thread_id in strict:
                expected_scalars = {
                    "generatorModel": config.model,
                    "generatorReasoningEffort": config.reasoning_effort,
                    "promptId": prompt.prompt_id,
                    "promptVersion": prompt.version,
                    "outputSchemaSha256": SUMMARY_SCHEMA_RESOURCE.sha256,
                    "templateId": SUMMARY_TEMPLATE_RESOURCE.template_id,
                    "templateVersion": SUMMARY_TEMPLATE_RESOURCE.version,
                    "generatorPromptVersion": str(GENERATOR_PROMPT_VERSION),
                    "rendererVersion": str(RENDERER_VERSION),
                    "automatedValidation": "passed",
                    "sourceFingerprint": candidate.fingerprint,
                }
                for key, expected_value in expected_scalars.items():
                    if metadata.get(key) != expected_value:
                        raise PipelineError(f"generated note has invalid {key}: {path.name}")
                file_slug = metadata.get("fileSlug") or ""
                if (
                    not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", file_slug)
                    or path.name != f"{expected_thread_note_id}-{file_slug}.md"
                ):
                    raise PipelineError(f"generated note filename does not match fileSlug: {path.name}")
    expected = {candidate.thread_id for candidate in candidates}
    missing = expected - set(by_thread)
    if missing:
        raise PipelineError("staged rebuild is missing source threads: " + ", ".join(sorted(missing)))
    return by_thread, note_hashes


def validate_thread_note(path: Path) -> dict[str, Any]:
    """Validate one standalone Thread Note v3 without touching project state."""

    if not path.is_file():
        raise PipelineError(f"thread note not found: {path}")
    metadata, thread_ids, source_refs, version = thread_note_metadata(path)
    if parse_thread_note_schema_version(version, path) != THREAD_NOTE_SCHEMA_VERSION:
        raise PipelineError(f"thread note is not schemaVersion {THREAD_NOTE_SCHEMA_VERSION}: {path}")
    required = {
        "type": "threadNote",
        "reviewStatus": "unreviewed",
        "sourceType": "codexChat",
        "automatedValidation": "passed",
    }
    for key, expected in required.items():
        if metadata.get(key) != expected:
            raise PipelineError(f"thread note has invalid {key}: {path}")
    try:
        uuid.UUID(metadata.get("promptId") or "")
    except ValueError as exc:
        raise PipelineError(f"thread note has invalid promptId: {path}") from exc
    if not metadata.get("promptVersion"):
        raise PipelineError(f"thread note has no promptVersion: {path}")
    try:
        uuid.UUID(metadata.get("templateId") or "")
    except ValueError as exc:
        raise PipelineError(f"thread note has invalid templateId: {path}") from exc
    if not metadata.get("templateVersion"):
        raise PipelineError(f"thread note has no templateVersion: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("outputSchemaSha256") or ""):
        raise PipelineError(f"thread note has invalid outputSchemaSha256: {path}")
    if len(thread_ids) != 1 or len(source_refs) != 1:
        raise PipelineError(f"thread note must have one source thread and source ref: {path}")
    if not metadata.get("sourceFingerprint"):
        raise PipelineError(f"thread note has no sourceFingerprint: {path}")
    body = path.read_text(encoding="utf-8-sig")
    for heading in (
        "# Thread Note",
        "## Summary",
        "## Key Developments",
        "## Last Known State",
    ):
        if heading not in body:
            raise PipelineError(f"thread note is missing {heading}: {path}")
    status = re.search(
        r"(?m)^- Work State: (in-progress|blocked|waiting-for-user|done)\b",
        body,
    )
    if status is None:
        raise PipelineError(f"thread note has no valid Work State: {path}")
    if metadata.get("status") != status.group(1):
        raise PipelineError(f"frontmatter status and Work State differ: {path}")
    return {
        "valid": True,
        "path": str(path.absolute()),
        "schemaVersion": THREAD_NOTE_SCHEMA_VERSION,
        "threadId": thread_ids[0],
        "sourceRef": source_refs[0],
        "status": status.group(1),
    }


def rebuild_state(
    project: Project,
    config: PipelineConfig,
    previous: dict[str, Any],
    candidates: Sequence[Candidate],
    note_by_thread: dict[str, str],
    note_hash_by_thread: dict[str, str],
) -> dict[str, Any]:
    state = deepcopy(previous)
    processed_at = now_iso()
    prompt = SUMMARY_PROMPT_RESOURCE
    source = state["sources"][config.source_id]
    threads = deepcopy(source.get("threads", {}))
    for candidate in candidates:
        threads[candidate.thread_id] = {
            "fingerprint": candidate.fingerprint,
            "generationFingerprint": generation_fingerprint(config, candidate),
            "threadNoteSchemaVersion": THREAD_NOTE_SCHEMA_VERSION,
            "generatorModel": config.model,
            "generatorReasoningEffort": config.reasoning_effort,
            "generatorPromptVersion": GENERATOR_PROMPT_VERSION,
            "summaryPromptId": prompt.prompt_id,
            "summaryPromptVersion": prompt.version,
            "summaryPromptSha256": prompt.sha256,
            "outputSchemaSha256": SUMMARY_SCHEMA_RESOURCE.sha256,
            "templateId": SUMMARY_TEMPLATE_RESOURCE.template_id,
            "templateVersion": SUMMARY_TEMPLATE_RESOURCE.version,
            "templateSha256": SUMMARY_TEMPLATE_RESOURCE.sha256,
            "rendererVersion": RENDERER_VERSION,
            "noteHash": note_hash_by_thread[candidate.thread_id],
            "sourceRefs": [candidate.source_ref],
            "threadNotes": [f"thread-notes/{note_by_thread[candidate.thread_id]}"],
            "processedAt": processed_at,
        }
    source["threads"] = threads
    source["lastRefreshAt"] = processed_at
    state["lastRefreshAt"] = processed_at
    state["approvedHistoricalRoots"] = [str(root) for root in project.historical_roots]
    return state


def remove_rebuild_tree(project: Project, path: Path) -> None:
    context = project.context_path.absolute()
    target = path.absolute()
    if target.parent != context or not target.name.startswith(
        (".thread-notes-rebuild-", ".thread-notes-rebuild-backup-")
    ):
        raise PipelineError(f"refusing to remove unmanaged rebuild path: {target}")
    if target.exists():
        shutil.rmtree(target)


def rebuild_work_signature(
    project: Project,
    config: PipelineConfig,
    candidates: Sequence[Candidate],
    *,
    force: bool,
) -> dict[str, Any]:
    return {
        "schemaVersion": REBUILD_WORK_SCHEMA_VERSION,
        "projectId": project.project_id,
        "sourceId": config.source_id,
        "sourceRoot": str(config.sessions_root),
        "generatorFingerprint": generator_fingerprint(config),
        "force": force,
        "approvedHistoricalRoots": [str(root) for root in project.historical_roots],
        "candidates": {
            candidate.thread_id: {
                "sourceFingerprint": candidate.fingerprint,
                "generationFingerprint": generation_fingerprint(config, candidate),
            }
            for candidate in candidates
        },
    }


def prepare_rebuild_work(
    project: Project,
    signature: dict[str, Any],
    cache_root: Path,
) -> tuple[Path, dict[str, Any], bool]:
    project_cache = cache_root.expanduser().absolute() / "rebuild" / project.project_id
    work_root = project_cache / "work"
    manifest_path = work_root / "manifest.json"
    reused = False
    manifest: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            comparable = {key: loaded.get(key) for key in signature}
            if comparable == signature:
                manifest = loaded
                reused = True
    if manifest is None:
        if work_root.exists():
            if work_root.parent != project_cache:
                raise PipelineError(f"refusing to remove unmanaged rebuild cache: {work_root}")
            shutil.rmtree(work_root)
        (work_root / "generated").mkdir(parents=True)
        manifest = {
            **signature,
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "completed": {},
        }
        atomic_write_json(manifest_path, manifest)
    else:
        (work_root / "generated").mkdir(parents=True, exist_ok=True)
        if not isinstance(manifest.get("completed"), dict):
            manifest["completed"] = {}
    return work_root, manifest, reused


def save_rebuild_work(work_root: Path, manifest: dict[str, Any]) -> None:
    manifest["updatedAt"] = now_iso()
    atomic_write_json(work_root / "manifest.json", manifest)


def reusable_generated_note(
    work_root: Path,
    manifest: dict[str, Any],
    candidate: Candidate,
    config: PipelineConfig,
) -> Path | None:
    completed = manifest.get("completed", {}).get(candidate.thread_id)
    if not isinstance(completed, dict):
        return None
    if completed.get("sourceFingerprint") != candidate.fingerprint or completed.get(
        "generationFingerprint"
    ) != generation_fingerprint(config, candidate):
        return None
    filename = str(completed.get("file") or "")
    if not filename or Path(filename).name != filename:
        return None
    path = work_root / "generated" / filename
    if not path.is_file():
        return None
    if sha256(path.read_bytes()).hexdigest() != completed.get("noteHash"):
        return None
    _metadata, thread_ids, source_refs, version = thread_note_metadata(path)
    if (
        version != str(THREAD_NOTE_SCHEMA_VERSION)
        or thread_ids != [candidate.thread_id]
        or source_refs != [candidate.source_ref]
    ):
        return None
    return path


def replace_thread_notes_and_state(
    project: Project,
    staged_thread_notes: Path,
    state: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    context = project.context_path.absolute()
    live = project.thread_notes_path.absolute()
    backup = context / f".thread-notes-rebuild-backup-{uuid.uuid4().hex}"
    if live.parent != context or staged_thread_notes.parent.parent != context:
        raise PipelineError("refusing rebuild outside the project context folder")
    original_state = project.state_path.read_bytes() if project.state_path.exists() else None
    moved_live = False
    installed_staged = False
    try:
        if live.exists():
            os.replace(live, backup)
            moved_live = True
        os.replace(staged_thread_notes, live)
        installed_staged = True
        atomic_write_json(project.state_path, state)
    except Exception:
        if installed_staged and live.exists():
            failed = staged_thread_notes.parent / "failed-thread-notes"
            os.replace(live, failed)
        if moved_live and backup.exists():
            os.replace(backup, live)
        if original_state is None:
            if project.state_path.exists():
                project.state_path.unlink()
        else:
            atomic_write_text(
                project.state_path,
                original_state.decode("utf-8-sig"),
            )
        raise
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            warnings.append(
                f"new Thread Notes and state were committed, but backup cleanup failed: {backup}: {exc}"
            )
    return warnings


def execute_rebuild(
    config: PipelineConfig,
    project: Project,
    *,
    summarizer: Summarizer | None,
    approve_roots: Sequence[Path] = (),
    force: bool = False,
    dry_run: bool = False,
    cache_root: Path | None = None,
    work_cache_root: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    started = now_local()
    start_deadline = started + timedelta(minutes=config.runtime_minutes)
    hard_deadline = start_deadline + timedelta(minutes=IN_FLIGHT_GRACE_MINUTES)
    previous_state = load_refresh_state(project, config)
    approved = [Path(str(value)).expanduser().absolute() for value in previous_state.get("approvedHistoricalRoots", [])]
    for requested in approve_roots:
        verified = verify_historical_root(project, requested)
        if normalize_path_text(str(verified)) not in {normalize_path_text(str(value)) for value in approved}:
            approved.append(verified)
    project = replace(
        project,
        historical_roots=tuple(dict.fromkeys((*project.historical_roots, *approved))),
    )
    candidates, scan_counts = scan_rebuild_candidates(config, project)
    prompt = SUMMARY_PROMPT_RESOURCE
    candidates_by_thread = {item.thread_id: item for item in candidates}

    preserve: list[Path] = []
    legacy: list[Path] = []
    replaced_current: list[Path] = []
    matched_current: dict[str, Path] = {}
    reusable_threads: set[str] = set()
    generator_versions = {"current": 0, "older": 0, "unknown": 0}
    if project.thread_notes_path.is_dir():
        for path in sorted(project.thread_notes_path.glob("*.md")):
            metadata, thread_ids, _source_refs, version = thread_note_metadata(path)
            parsed_version = parse_thread_note_schema_version(version, path)
            if parsed_version < THREAD_NOTE_SCHEMA_VERSION:
                legacy.append(path)
                continue
            if parsed_version > THREAD_NOTE_SCHEMA_VERSION:
                raise PipelineError(f"unsupported Thread Note schemaVersion {version}: {path.name}")
            if len(thread_ids) == 1 and thread_ids[0] in candidates_by_thread:
                if thread_ids[0] in matched_current:
                    raise PipelineError(f"multiple current Thread Notes match thread {thread_ids[0]}")
                matched_current[thread_ids[0]] = path
                prompt_version = metadata.get("generatorPromptVersion")
                renderer_version = metadata.get("rendererVersion")
                current_generator = (
                    prompt_version == str(GENERATOR_PROMPT_VERSION)
                    and renderer_version == str(RENDERER_VERSION)
                    and metadata.get("generatorModel") == config.model
                    and metadata.get("generatorReasoningEffort") == config.reasoning_effort
                    and metadata.get("type") == "threadNote"
                    and metadata.get("promptId") == prompt.prompt_id
                    and metadata.get("promptVersion") == prompt.version
                    and metadata.get("outputSchemaSha256")
                    == SUMMARY_SCHEMA_RESOURCE.sha256
                    and metadata.get("templateId")
                    == SUMMARY_TEMPLATE_RESOURCE.template_id
                    and metadata.get("templateVersion")
                    == SUMMARY_TEMPLATE_RESOURCE.version
                )
                if not prompt_version or not renderer_version:
                    generator_versions["unknown"] += 1
                elif current_generator:
                    generator_versions["current"] += 1
                else:
                    generator_versions["older"] += 1
                current_source = metadata.get("sourceFingerprint") == candidates_by_thread[thread_ids[0]].fingerprint
                if force or not (current_generator and current_source):
                    replaced_current.append(path)
                    continue
                reusable_threads.add(thread_ids[0])
            preserve.append(path)

    generate = [candidate for candidate in candidates if force or candidate.thread_id not in reusable_threads]
    deleted = [{"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()} for path in legacy]
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": None,
        "mode": "rebuild",
        "dryRun": dry_run,
        "projectId": project.project_id,
        "force": force,
        "generator": {
            "model": config.model,
            "reasoningEffort": config.reasoning_effort,
            "promptVersion": GENERATOR_PROMPT_VERSION,
            "summaryPromptId": prompt.prompt_id,
            "summaryPromptVersion": prompt.version,
            "summaryPromptSource": prompt.source,
            "summaryPromptSha256": prompt.sha256,
            "outputSchemaSource": SUMMARY_SCHEMA_RESOURCE.source,
            "outputSchemaSha256": SUMMARY_SCHEMA_RESOURCE.sha256,
            "templateId": SUMMARY_TEMPLATE_RESOURCE.template_id,
            "templateVersion": SUMMARY_TEMPLATE_RESOURCE.version,
            "templateSource": SUMMARY_TEMPLATE_RESOURCE.source,
            "templateSha256": SUMMARY_TEMPLATE_RESOURCE.sha256,
            "rendererVersion": RENDERER_VERSION,
            "fingerprint": generator_fingerprint(config),
        },
        "approvedHistoricalRoots": [str(root) for root in approved],
        "scan": scan_counts,
        "selectedCount": len(candidates),
        "preservedCurrent": [path.name for path in preserve],
        "generatorVersions": generator_versions,
        "generationCount": len(generate),
        "generationThreadIds": [item.thread_id for item in generate],
        "deletedLegacy": deleted,
        "replacedCurrent": [
            {"file": path.name, "sha256": sha256(path.read_bytes()).hexdigest()}
            for path in replaced_current
        ],
        "processed": [],
        "failed": [],
        "deferred": [],
        "warnings": [],
        "resumedCount": 0,
        "resumeAvailable": 0,
        "existingTotalBytes": sum(path.stat().st_size for path in project.thread_notes_path.glob("*.md"))
        if project.thread_notes_path.is_dir()
        else 0,
    }
    if dry_run:
        report["finishedAt"] = now_iso()
        return report, None
    if summarizer is None:
        raise PipelineError("a summarizer is required for a rebuild")

    project.context_path.mkdir(parents=True, exist_ok=True)
    signature = rebuild_work_signature(project, config, candidates, force=force)
    rebuild_cache = (work_cache_root or cache_root or default_cache_root()).expanduser().absolute()
    work_root, work_manifest, reused_work = prepare_rebuild_work(
        project,
        signature,
        rebuild_cache,
    )
    report["reusedWorkArea"] = reused_work
    if hasattr(summarizer, "set_deadline"):
        summarizer.set_deadline(hard_deadline)
    stage_root = project.context_path / f".thread-notes-rebuild-{uuid.uuid4().hex}"
    staged_thread_notes = stage_root / "thread-notes"
    generated_by_thread: dict[str, Path] = {}
    for index, candidate in enumerate(generate):
        reusable = reusable_generated_note(work_root, work_manifest, candidate, config)
        if reusable is not None:
            generated_by_thread[candidate.thread_id] = reusable
            final_note_path = (candidate.project.thread_notes_path / reusable.name).absolute()
            report["resumedCount"] += 1
            report["processed"].append(
                {
                    "threadId": candidate.thread_id,
                    "threadNote": reusable.name,
                    "resumed": True,
                    "noteHash": sha256(reusable.read_bytes()).hexdigest(),
                    "generationFingerprint": generation_fingerprint(config, candidate),
                }
            )
            if progress:
                progress(
                    {
                        "type": "thread-resumed",
                        "index": index + 1,
                        "total": len(generate),
                        "threadId": candidate.thread_id,
                        "threadNotePath": str(final_note_path),
                    }
                )
            continue
        if now_local() >= start_deadline:
            report["deferred"].extend(
                {
                    "threadId": item.thread_id,
                    "reason": "runtime-deadline",
                }
                for item in generate[index:]
                if item.thread_id not in generated_by_thread
            )
            break
        thread_started = time.monotonic()
        if progress:
            progress(
                {
                    "type": "thread-start",
                    "index": index + 1,
                    "total": len(generate),
                    "threadId": candidate.thread_id,
                }
            )
        try:
            data = summarizer.generate(candidate)
            validate_note_data(data, {event.id for event in candidate.events})
            revalidate_candidate(candidate, config)
            note_path, _existing = choose_note_path(
                candidate,
                str(data["fileSlug"]),
                thread_notes_path=work_root / "generated",
                match_existing=False,
            )
            data["fileSlug"] = file_slug_from_note_path(candidate, note_path)
            data["_generatorModel"] = config.model
            data["_generatorReasoningEffort"] = config.reasoning_effort
            prompt = SUMMARY_PROMPT_RESOURCE
            data["_summaryPromptId"] = prompt.prompt_id
            data["_summaryPromptVersion"] = prompt.version
            atomic_write_text(note_path, render_note(candidate, data, {}))
            note_hash = sha256(note_path.read_bytes()).hexdigest()
            work_manifest["completed"][candidate.thread_id] = {
                "file": note_path.name,
                "noteHash": note_hash,
                "sourceFingerprint": candidate.fingerprint,
                "generationFingerprint": generation_fingerprint(config, candidate),
                "completedAt": now_iso(),
            }
            save_rebuild_work(work_root, work_manifest)
            generated_by_thread[candidate.thread_id] = note_path
            final_note_path = (candidate.project.thread_notes_path / note_path.name).absolute()
            duration = round(time.monotonic() - thread_started, 3)
            metrics = deepcopy(getattr(summarizer, "last_metrics", {}))
            report["processed"].append(
                {
                    "threadId": candidate.thread_id,
                    "threadNote": note_path.name,
                    "resumed": False,
                    "durationSeconds": duration,
                    "noteHash": note_hash,
                    "generationFingerprint": generation_fingerprint(config, candidate),
                    **metrics,
                }
            )
            if progress:
                progress(
                    {
                        "type": "thread-complete",
                        "index": index + 1,
                        "total": len(generate),
                        "threadId": candidate.thread_id,
                        "threadNotePath": str(final_note_path),
                        "durationSeconds": duration,
                        **metrics,
                    }
                )
        except Exception as exc:
            report["failed"].append(
                {
                    "threadId": candidate.thread_id,
                    "error": str(exc),
                }
            )
            if progress:
                progress(
                    {
                        "type": "thread-failed",
                        "index": index + 1,
                        "total": len(generate),
                        "threadId": candidate.thread_id,
                        "error": str(exc),
                    }
                )
    report["resumeAvailable"] = len(work_manifest.get("completed", {}))
    if report["failed"] or report["deferred"]:
        report["finishedAt"] = now_iso()
        report_path = write_run_report(cache_root or default_cache_root(), report)
        return report, report_path

    staged_thread_notes.mkdir(parents=True)
    try:
        for path in preserve:
            shutil.copy2(path, staged_thread_notes / path.name)
        for candidate in generate:
            generated = generated_by_thread.get(candidate.thread_id)
            if generated is None:
                raise PipelineError(f"generated note missing from rebuild work area: {candidate.thread_id}")
            destination = staged_thread_notes / generated.name
            if destination.exists():
                raise PipelineError(
                    f"generated filename conflicts with preserved current Thread Note: {generated.name}"
                )
            shutil.copy2(generated, destination)
        for candidate in candidates:
            revalidate_candidate(candidate, config)
        note_by_thread, note_hash_by_thread = validate_staged_thread_notes(
            staged_thread_notes,
            candidates,
            config,
            strict_threads={candidate.thread_id for candidate in generate},
        )
        state = rebuild_state(
            project,
            config,
            previous_state,
            candidates,
            note_by_thread,
            note_hash_by_thread,
        )
        report["warnings"].extend(replace_thread_notes_and_state(project, staged_thread_notes, state))
    except Exception as exc:
        report["failed"].append({"error": str(exc)})
        report["finishedAt"] = now_iso()
        report_path = write_run_report(cache_root or default_cache_root(), report)
        return report, report_path
    finally:
        if stage_root.exists():
            remove_rebuild_tree(project, stage_root)
    try:
        expected_parent = rebuild_cache / "rebuild" / project.project_id
        if work_root.parent != expected_parent:
            raise PipelineError(f"refusing to remove unmanaged rebuild cache: {work_root}")
        shutil.rmtree(work_root)
    except OSError as exc:
        report["warnings"].append(f"rebuild succeeded, but reusable work cleanup failed: {work_root}: {exc}")
    report["newTotalBytes"] = sum(path.stat().st_size for path in project.thread_notes_path.glob("*.md"))
    report["finishedAt"] = now_iso()
    report_path = write_run_report(cache_root or default_cache_root(), report)
    return report, report_path


def execute_pipeline(
    config: PipelineConfig,
    projects: Sequence[Project],
    *,
    summarizer: Summarizer | None,
    dry_run: bool = False,
    backfill: bool = False,
    force: bool = False,
    project_ids: Sequence[str] = (),
    thread_ids: Sequence[str] = (),
    limit: int | None = None,
    cache_root: Path | None = None,
    work_cache_root: Path | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], Path | None]:
    started = now_local()
    start_deadline = started + timedelta(minutes=config.runtime_minutes)
    hard_deadline = start_deadline + timedelta(minutes=IN_FLIGHT_GRACE_MINUTES)
    candidates, scan_counts = scan_candidates(
        config,
        projects,
        backfill=backfill,
        project_ids=project_ids,
        thread_ids=thread_ids,
        ignore_fingerprints=force,
    )
    if limit is not None:
        if limit <= 0:
            raise PipelineError("limit must be positive")
        candidates = candidates[-limit:] if backfill else candidates[:limit]
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": None,
        "mode": "backfill" if backfill else "daily",
        "dryRun": dry_run,
        "force": force,
        "scan": scan_counts,
        "selectedCount": len(candidates),
        "processed": [],
        "failed": [],
        "deferred": [],
    }
    if dry_run:
        report["selected"] = [
            {
                "projectId": item.project.project_id,
                "threadId": item.thread_id,
                "sourceRef": item.source_ref,
                "startedAt": item.started_at,
            }
            for item in candidates
        ]
    else:
        if summarizer is None:
            raise PipelineError("a summarizer is required for a write run")
        if hasattr(summarizer, "set_deadline"):
            summarizer.set_deadline(hard_deadline)
        for index, candidate in enumerate(candidates):
            if now_local() >= start_deadline:
                report["deferred"].extend(
                    {
                        "projectId": item.project.project_id,
                        "threadId": item.thread_id,
                        "reason": "runtime-deadline",
                    }
                    for item in candidates[index:]
                )
                break
            thread_started = time.monotonic()
            if progress:
                progress(
                    {
                        "type": "thread-start",
                        "index": index + 1,
                        "total": len(candidates),
                        "threadId": candidate.thread_id,
                    }
                )
            try:
                note_path = write_candidate_note(
                    candidate,
                    config,
                    summarizer,
                    work_cache_root=work_cache_root or cache_root or default_cache_root(),
                    force=force,
                )
            except Exception as exc:  # Per-thread isolation is intentional.
                report["failed"].append(
                    {
                        "projectId": candidate.project.project_id,
                        "threadId": candidate.thread_id,
                        "error": str(exc),
                    }
                )
                if progress:
                    progress(
                        {
                            "type": "thread-failed",
                            "index": index + 1,
                            "total": len(candidates),
                            "threadId": candidate.thread_id,
                            "error": str(exc),
                        }
                    )
                continue
            duration = round(time.monotonic() - thread_started, 3)
            metrics = deepcopy(getattr(summarizer, "last_metrics", {}))
            report["processed"].append(
                {
                    "projectId": candidate.project.project_id,
                    "threadId": candidate.thread_id,
                    "threadNote": note_path.relative_to(candidate.project.context_path).as_posix(),
                    "durationSeconds": duration,
                    **metrics,
                }
            )
            if progress:
                progress(
                    {
                        "type": "thread-complete",
                        "index": index + 1,
                        "total": len(candidates),
                        "threadId": candidate.thread_id,
                        "threadNotePath": str(note_path.absolute()),
                        "durationSeconds": duration,
                        **metrics,
                    }
                )
    report["finishedAt"] = now_iso()
    if dry_run:
        return report, None
    report_path = write_run_report(cache_root or default_cache_root(), report)
    return report, report_path
