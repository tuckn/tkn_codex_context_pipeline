"""Small frontmatter helpers shared by project initialization and distillation."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .common import yaml_string

FRONTMATTER_PATTERN = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
ARTIFACT_SCHEMA_VERSION = "2"
LEGACY_ARTIFACT_SCHEMA_VERSION = "1"
SUPPORTED_ARTIFACT_SCHEMA_VERSIONS = {
    LEGACY_ARTIFACT_SCHEMA_VERSION,
    ARTIFACT_SCHEMA_VERSION,
}
DECISION_ARTIFACT_SCHEMA_VERSION = "3"


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_PATTERN.sub("", text, count=1)


def parse_simple_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if value in {"", "[]"}:
            metadata[key.strip()] = ""
        else:
            metadata[key.strip()] = value.strip("'\"")
    return metadata


def split_frontmatter_lines(text: str) -> tuple[list[str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SystemExit("Source file must have YAML frontmatter.")
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return lines[: index + 1], "".join(lines[index + 1 :])
    raise SystemExit("Source file has an opening frontmatter delimiter but no closing delimiter.")


def strip_yaml_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def frontmatter_key_block(lines: list[str], key: str) -> tuple[int, int] | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for index in range(1, len(lines) - 1):
        if pattern.match(lines[index].rstrip("\r\n")):
            end = index + 1
            while end < len(lines) - 1:
                candidate = lines[end]
                if candidate.startswith((" ", "\t")) or candidate.strip() == "":
                    end += 1
                    continue
                break
            return index, end
    return None


def frontmatter_list_value(lines: list[str], key: str) -> list[str]:
    block = frontmatter_key_block(lines, key)
    if not block:
        return []
    start, end = block
    match = re.match(rf"^{re.escape(key)}:\s*(.*)$", lines[start].rstrip("\r\n"))
    if not match:
        return []
    inline = match.group(1).strip()
    if inline == "[]":
        return []
    if inline:
        return [strip_yaml_quotes(inline)]
    values: list[str] = []
    for line in lines[start + 1 : end]:
        item = re.match(r"^\s*-\s*(.*?)\s*$", line.rstrip("\r\n"))
        if item:
            values.append(strip_yaml_quotes(item.group(1)))
    return values


def replace_frontmatter_scalar(lines: list[str], key: str, value: str) -> list[str]:
    replacement = [f"{key}: {yaml_string(value)}\n"]
    block = frontmatter_key_block(lines, key)
    if block:
        start, end = block
        return lines[:start] + replacement + lines[end:]
    return lines[:-1] + replacement + lines[-1:]


def replace_frontmatter_list(lines: list[str], key: str, values: list[str]) -> list[str]:
    if values:
        replacement = [f"{key}:\n", *[f"  - {yaml_string(value)}\n" for value in values]]
    else:
        replacement = [f"{key}: []\n"]
    block = frontmatter_key_block(lines, key)
    if block:
        start, end = block
        return lines[:start] + replacement + lines[end:]
    return lines[:-1] + replacement + lines[-1:]


def require_supported_artifact_schema(
    metadata: dict[str, str],
    artifact_label: str,
) -> str:
    """Treat unversioned artifacts as v1 and reject unknown versions."""
    version = metadata.get("schemaVersion") or LEGACY_ARTIFACT_SCHEMA_VERSION
    supported_versions = set(SUPPORTED_ARTIFACT_SCHEMA_VERSIONS)
    if artifact_label == "decision record":
        supported_versions.add(DECISION_ARTIFACT_SCHEMA_VERSION)
    if version not in supported_versions:
        supported = ", ".join(sorted(supported_versions))
        raise SystemExit(f"Unsupported {artifact_label} schemaVersion: {version}. Supported versions: {supported}.")
    return version


def ensure_artifact_schema_version(
    lines: list[str],
    artifact_label: str,
) -> list[str]:
    """Make the existing schema explicit without relabeling the artifact body."""
    metadata = parse_simple_frontmatter("".join(lines))
    version = require_supported_artifact_schema(metadata, artifact_label)
    if frontmatter_key_block(lines, "schemaVersion"):
        return lines
    type_block = frontmatter_key_block(lines, "type")
    insert_at = type_block[1] if type_block else 1
    return lines[:insert_at] + [f"schemaVersion: {version}\n"] + lines[insert_at:]


def unique_ordered(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
