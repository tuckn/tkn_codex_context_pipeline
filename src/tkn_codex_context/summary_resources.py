"""Load and validate packaged summary schema and Markdown template resources."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import yaml

DEFAULT_SCHEMA_RESOURCE = "schemas/summary-note-output.schema.json"
DEFAULT_TEMPLATE_RESOURCE = "templates/default-summary-note.md"
REQUIRED_TEMPLATE_FIELDS = frozenset(
    {
        "frontmatter",
        "summary",
        "key_developments",
        "last_known_state",
        "evidence_section",
        "source_notes_section",
    }
)
_PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


@dataclass(frozen=True)
class SummarySchema:
    value: dict[str, Any]
    source: str
    sha256: str


@dataclass(frozen=True)
class SummaryTemplate:
    template_id: str
    version: str
    body: str
    source: str
    sha256: str


def _resource_bytes(resource_name: str, label: str) -> bytes:
    resource = files("tkn_codex_context").joinpath(resource_name)
    try:
        return resource.read_bytes()
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeError(
            f"built-in {label} is unavailable: {resource_name}: {exc}"
        ) from exc


def load_summary_schema() -> SummarySchema:
    payload = _resource_bytes(DEFAULT_SCHEMA_RESOURCE, "summary schema")
    source = f"package:tkn_codex_context/{DEFAULT_SCHEMA_RESOURCE}"
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid built-in summary schema {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"built-in summary schema must be an object: {source}")
    properties = value.get("properties")
    required = value.get("required")
    if (
        value.get("type") != "object"
        or value.get("additionalProperties") is not False
        or not isinstance(properties, dict)
        or not isinstance(required, list)
        or set(required) != set(properties)
    ):
        raise RuntimeError(
            "built-in summary schema must be a strict object with every property required"
        )
    return SummarySchema(
        value=value,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def load_summary_template() -> SummaryTemplate:
    payload = _resource_bytes(DEFAULT_TEMPLATE_RESOURCE, "summary template")
    source = f"package:tkn_codex_context/{DEFAULT_TEMPLATE_RESOURCE}"
    try:
        text = payload.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"summary template must be UTF-8: {source}: {exc}") from exc
    if not text.startswith("---\n"):
        raise RuntimeError(f"summary template must start with YAML frontmatter: {source}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RuntimeError(f"summary template frontmatter closing delimiter is missing: {source}")
    try:
        metadata = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid summary template frontmatter {source}: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("type") != "template":
        raise RuntimeError(f"summary template type must be 'template': {source}")
    try:
        template_id = str(uuid.UUID(str(metadata.get("id"))))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError(f"summary template id must be a UUID: {source}") from exc
    version = metadata.get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(
            f"summary template version must be a non-empty quoted string: {source}"
        )
    body = text[end + 5 :].strip()
    placeholders = _PLACEHOLDER.findall(body)
    if set(placeholders) != REQUIRED_TEMPLATE_FIELDS or any(
        placeholders.count(field) != 1 for field in REQUIRED_TEMPLATE_FIELDS
    ):
        expected = ", ".join(sorted(REQUIRED_TEMPLATE_FIELDS))
        raise RuntimeError(
            f"summary template must contain each required placeholder exactly once "
            f"({expected}): {source}"
        )
    return SummaryTemplate(
        template_id=template_id,
        version=version.strip(),
        body=body,
        source=source,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def render_summary_template(
    template: SummaryTemplate,
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
        raise ValueError("invalid summary template values: " + "; ".join(details))
    rendered = template.body
    for field in REQUIRED_TEMPLATE_FIELDS:
        rendered = rendered.replace(f"{{{{{field}}}}}", values[field])
    if _PLACEHOLDER.search(rendered):
        raise ValueError("summary template contains unresolved placeholders")
    return rendered.rstrip() + "\n"


def validate_summary_output_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
) -> None:
    """Validate the JSON Schema subset used by the packaged output contract."""

    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"{path} has an invalid object schema")
        missing = sorted(set(required) - set(value))
        if missing:
            raise ValueError(f"{path} is missing fields: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValueError(f"{path} has unexpected fields: {', '.join(extra)}")
        for key, item_schema in properties.items():
            if key in value:
                if not isinstance(item_schema, dict):
                    raise ValueError(f"{path}.{key} has an invalid schema")
                validate_summary_output_schema(
                    value[key],
                    item_schema,
                    path=f"{path}.{key}",
                )
        return
    if expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} must contain at least {minimum} items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} must contain at most {maximum} items")
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            raise ValueError(f"{path} has an invalid item schema")
        for index, item in enumerate(value):
            validate_summary_output_schema(
                item,
                item_schema,
                path=f"{path}[{index}]",
            )
        return
    if expected_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        pattern = schema.get("pattern")
        enum = schema.get("enum")
        if isinstance(minimum, int) and len(value) < minimum:
            raise ValueError(f"{path} must contain at least {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            raise ValueError(f"{path} must contain at most {maximum} characters")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            raise ValueError(f"{path} does not match the required pattern")
        if isinstance(enum, list) and value not in enum:
            raise ValueError(f"{path} must be one of the permitted values")
        return
    raise ValueError(f"{path} uses unsupported schema type: {expected_type}")
