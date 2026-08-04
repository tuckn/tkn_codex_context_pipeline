"""Load and validate application-owned decision profile bundles."""

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

DEFAULT_DECISION_PROFILE = "default"
DECISION_PROFILES_ROOT = "profiles/decision"
PROMPT_FILENAME = "prompt.md"
SCHEMA_FILENAME = "output.schema.json"
TEMPLATE_FILENAME = "template.md"
REQUIRED_TEMPLATE_FIELDS = frozenset(
    {
        "frontmatter",
        "title",
        "context",
        "decision",
        "rationale",
        "benefits",
        "costs_and_risks",
        "alternatives_considered",
        "applies_when",
        "does_not_apply_when",
        "reusable_principle",
        "project_specific_details",
        "verification",
        "related_evidence",
        "materialization",
        "supersession",
    }
)
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True)
class DecisionSchema:
    value: dict[str, Any]
    source: str
    sha256: str


@dataclass(frozen=True)
class DecisionTemplate:
    template_id: str
    version: str
    body: str
    source: str
    sha256: str


@dataclass(frozen=True)
class DecisionProfile:
    name: str
    source: str
    sha256: str
    prompt: SummaryPrompt
    schema: DecisionSchema
    template: DecisionTemplate


def _profile_resource_name(profile_name: str, filename: str) -> str:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile_name) is None:
        raise RuntimeError(f"invalid application-owned decision profile name: {profile_name}")
    return f"{DECISION_PROFILES_ROOT}/{profile_name}/{filename}"


def _resource_bytes(resource_name: str, label: str) -> bytes:
    resource = files("tkn_codex_context").joinpath(resource_name)
    try:
        return resource.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeError(f"built-in {label} is unavailable: {resource_name}: {exc}") from exc


def load_decision_prompt(profile_name: str = DEFAULT_DECISION_PROFILE) -> SummaryPrompt:
    resource_name = _profile_resource_name(profile_name, PROMPT_FILENAME)
    payload = _resource_bytes(resource_name, "decision profile prompt")
    source = f"package:tkn_codex_context/{resource_name}"
    return parse_summary_prompt(payload, source)


def load_decision_schema(profile_name: str = DEFAULT_DECISION_PROFILE) -> DecisionSchema:
    resource_name = _profile_resource_name(profile_name, SCHEMA_FILENAME)
    payload = _resource_bytes(resource_name, "decision profile schema")
    source = f"package:tkn_codex_context/{resource_name}"
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid built-in decision schema {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"built-in decision schema must be an object: {source}")
    properties = value.get("properties")
    required = value.get("required")
    if (
        value.get("type") != "object"
        or value.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or set(required) != set(properties)
    ):
        raise RuntimeError("built-in decision schema must be a strict object with every property required")
    return DecisionSchema(
        value=value,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_decision_template(profile_name: str = DEFAULT_DECISION_PROFILE) -> DecisionTemplate:
    resource_name = _profile_resource_name(profile_name, TEMPLATE_FILENAME)
    payload = _resource_bytes(resource_name, "decision profile template")
    source = f"package:tkn_codex_context/{resource_name}"
    try:
        text = payload.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"decision template must be UTF-8: {source}: {exc}") from exc
    if not text.startswith("---\n"):
        raise RuntimeError(f"decision template must start with YAML frontmatter: {source}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError(f"decision template frontmatter closing delimiter is missing: {source}")
    try:
        metadata = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid decision template frontmatter {source}: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("type") != "template":
        raise RuntimeError(f"decision template type must be 'template': {source}")
    try:
        template_id = str(uuid.UUID(str(metadata.get("id"))))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"decision template id must be a UUID: {source}") from exc
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"decision template version must be a non-empty quoted string: {source}")
    body = text[end + 5 :].strip()
    placeholders = _PLACEHOLDER.findall(body)
    if set(placeholders) != REQUIRED_TEMPLATE_FIELDS or any(
        placeholders.count(field) != 1 for field in REQUIRED_TEMPLATE_FIELDS
    ):
        expected = ", ".join(sorted(REQUIRED_TEMPLATE_FIELDS))
        raise RuntimeError(
            f"decision template must contain each required placeholder exactly once ({expected}): {source}"
        )
    return DecisionTemplate(
        template_id=template_id,
        version=version.strip(),
        body=body,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_decision_profile(profile_name: str = DEFAULT_DECISION_PROFILE) -> DecisionProfile:
    prompt = load_decision_prompt(profile_name)
    schema = load_decision_schema(profile_name)
    template = load_decision_template(profile_name)
    source = f"package:tkn_codex_context/{DECISION_PROFILES_ROOT}/{profile_name}"
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
    return DecisionProfile(
        name=profile_name,
        source=source,
        sha256=hashlib.sha256(identity).hexdigest(),
        prompt=prompt,
        schema=schema,
        template=template,
    )


def render_decision_template(template: DecisionTemplate, values: dict[str, str]) -> str:
    if set(values) != REQUIRED_TEMPLATE_FIELDS:
        missing = sorted(REQUIRED_TEMPLATE_FIELDS - set(values))
        extra = sorted(set(values) - REQUIRED_TEMPLATE_FIELDS)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError("invalid decision template values: " + "; ".join(details))
    rendered = template.body
    for field in REQUIRED_TEMPLATE_FIELDS:
        rendered = rendered.replace(f"{{{{{field}}}}}", values[field])
    if _PLACEHOLDER.search(rendered):
        raise ValueError("decision template contains unresolved placeholders")
    return rendered.rstrip() + "\n"
