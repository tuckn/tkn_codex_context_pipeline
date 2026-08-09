"""Load and validate application-owned Working Context profile bundles."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import yaml

from .prompting import SummaryPrompt, parse_summary_prompt

DEFAULT_WORKING_CONTEXT_PROFILE = "default"
WORKING_CONTEXT_PROFILES_ROOT = "profiles/working_context"
PROMPT_FILENAME = "prompt.md"
SCHEMA_FILENAME = "output.schema.json"
TEMPLATE_FILENAME = "template.md"
ALWAYS_TEMPLATE_FIELDS = frozenset({"frontmatter", "project_overview", "current_truth"})
OPTIONAL_TEMPLATE_FIELDS = frozenset(
    {
        "current_outcome",
        "active_work",
        "risks_and_constraints",
        "effective_decisions",
        "semantic_context",
        "key_evidence",
        "resumption",
        "source_limitations",
    }
)
REQUIRED_TEMPLATE_FIELDS = ALWAYS_TEMPLATE_FIELDS | OPTIONAL_TEMPLATE_FIELDS
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_OPTIONAL_BLOCK_START = re.compile(r"\{\{\?([a-z][a-z0-9_]*)\}\}")
_OPTIONAL_BLOCK_END = re.compile(r"\{\{/([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True)
class WorkingContextSchema:
    value: dict[str, Any]
    source: str
    sha256: str


@dataclass(frozen=True)
class WorkingContextTemplate:
    template_id: str
    version: str
    body: str
    source: str
    sha256: str


@dataclass(frozen=True)
class WorkingContextProfile:
    name: str
    source: str
    sha256: str
    prompt: SummaryPrompt
    schema: WorkingContextSchema
    template: WorkingContextTemplate


def _profile_resource_name(profile_name: str, filename: str) -> str:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile_name) is None:
        raise RuntimeError(f"invalid application-owned Working Context profile name: {profile_name}")
    return f"{WORKING_CONTEXT_PROFILES_ROOT}/{profile_name}/{filename}"


def _resource_bytes(resource_name: str, label: str) -> bytes:
    resource = files("tkn_codex_context").joinpath(resource_name)
    try:
        return resource.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeError(f"built-in {label} is unavailable: {resource_name}: {exc}") from exc


def load_working_context_prompt(
    profile_name: str = DEFAULT_WORKING_CONTEXT_PROFILE,
) -> SummaryPrompt:
    resource_name = _profile_resource_name(profile_name, PROMPT_FILENAME)
    payload = _resource_bytes(resource_name, "Working Context profile prompt")
    source = f"package:tkn_codex_context/{resource_name}"
    return parse_summary_prompt(payload, source)


def load_working_context_schema(
    profile_name: str = DEFAULT_WORKING_CONTEXT_PROFILE,
) -> WorkingContextSchema:
    resource_name = _profile_resource_name(profile_name, SCHEMA_FILENAME)
    payload = _resource_bytes(resource_name, "Working Context profile schema")
    source = f"package:tkn_codex_context/{resource_name}"
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid built-in Working Context schema {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"built-in Working Context schema must be an object: {source}")
    properties = value.get("properties")
    required = value.get("required")
    if (
        value.get("type") != "object"
        or value.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or set(required) != set(properties)
    ):
        raise RuntimeError("built-in Working Context schema must be a strict object with every property required")
    return WorkingContextSchema(
        value=value,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_working_context_template(
    profile_name: str = DEFAULT_WORKING_CONTEXT_PROFILE,
) -> WorkingContextTemplate:
    resource_name = _profile_resource_name(profile_name, TEMPLATE_FILENAME)
    payload = _resource_bytes(resource_name, "Working Context profile template")
    source = f"package:tkn_codex_context/{resource_name}"
    try:
        text = payload.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Working Context template must be UTF-8: {source}: {exc}") from exc
    if not text.startswith("---\n"):
        raise RuntimeError(f"Working Context template must start with YAML frontmatter: {source}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError(f"Working Context template frontmatter closing delimiter is missing: {source}")
    try:
        metadata = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid Working Context template frontmatter {source}: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("type") != "template":
        raise RuntimeError(f"Working Context template type must be 'template': {source}")
    try:
        template_id = str(uuid.UUID(str(metadata.get("id"))))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"Working Context template id must be a UUID: {source}") from exc
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"Working Context template version must be a non-empty quoted string: {source}")
    body = text[end + 5 :].strip()
    placeholders = _PLACEHOLDER.findall(body)
    if set(placeholders) != REQUIRED_TEMPLATE_FIELDS or any(
        placeholders.count(field) != 1 for field in REQUIRED_TEMPLATE_FIELDS
    ):
        expected = ", ".join(sorted(REQUIRED_TEMPLATE_FIELDS))
        raise RuntimeError(
            f"Working Context template must contain each required placeholder exactly once ({expected}): {source}"
        )
    optional_starts = _OPTIONAL_BLOCK_START.findall(body)
    optional_ends = _OPTIONAL_BLOCK_END.findall(body)
    if (
        set(optional_starts) != OPTIONAL_TEMPLATE_FIELDS
        or set(optional_ends) != OPTIONAL_TEMPLATE_FIELDS
        or any(optional_starts.count(field) != 1 for field in OPTIONAL_TEMPLATE_FIELDS)
        or any(optional_ends.count(field) != 1 for field in OPTIONAL_TEMPLATE_FIELDS)
    ):
        expected = ", ".join(sorted(OPTIONAL_TEMPLATE_FIELDS))
        raise RuntimeError(
            f"Working Context template must wrap each optional placeholder in one matching block ({expected}): {source}"
        )
    return WorkingContextTemplate(
        template_id=template_id,
        version=version.strip(),
        body=body,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_working_context_profile(
    profile_name: str = DEFAULT_WORKING_CONTEXT_PROFILE,
) -> WorkingContextProfile:
    prompt = load_working_context_prompt(profile_name)
    schema = load_working_context_schema(profile_name)
    template = load_working_context_template(profile_name)
    source = f"package:tkn_codex_context/{WORKING_CONTEXT_PROFILES_ROOT}/{profile_name}"
    identity = json.dumps(
        {
            "name": profile_name,
            "promptSha256": prompt.sha256,
            "schemaSha256": schema.sha256,
            "templateSha256": template.sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return WorkingContextProfile(
        name=profile_name,
        source=source,
        sha256=hashlib.sha256(identity).hexdigest(),
        prompt=prompt,
        schema=schema,
        template=template,
    )


def render_working_context_template(
    template: WorkingContextTemplate,
    values: dict[str, str],
) -> str:
    if set(values) != REQUIRED_TEMPLATE_FIELDS:
        missing = sorted(REQUIRED_TEMPLATE_FIELDS - set(values))
        extra = sorted(set(values) - REQUIRED_TEMPLATE_FIELDS)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError("invalid Working Context template values: " + "; ".join(details))
    rendered = template.body
    for field in OPTIONAL_TEMPLATE_FIELDS:
        pattern = re.compile(
            rf"\{{\{{\?{re.escape(field)}\}}\}}(.*?)\{{\{{/{re.escape(field)}\}}\}}",
            re.DOTALL,
        )
        match = pattern.search(rendered)
        if match is None:
            raise ValueError(f"Working Context template is missing optional block: {field}")
        replacement = match.group(1) if values[field].strip() else ""
        rendered = rendered[: match.start()] + replacement + rendered[match.end() :]
    for field in REQUIRED_TEMPLATE_FIELDS:
        rendered = rendered.replace(f"{{{{{field}}}}}", values[field])
    if _PLACEHOLDER.search(rendered) or _OPTIONAL_BLOCK_START.search(rendered) or _OPTIONAL_BLOCK_END.search(rendered):
        raise ValueError("Working Context template contains unresolved placeholders")
    return re.sub(r"\n{3,}", "\n\n", rendered).rstrip() + "\n"
