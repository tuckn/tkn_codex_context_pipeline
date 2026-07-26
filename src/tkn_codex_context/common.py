"""Small rendering primitives used by Session Note v2."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "session"


def yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def yaml_string_list(values: Iterable[str]) -> str:
    items = [value for value in values if value]
    if not items:
        return "[]"
    return "\n".join(f"  - {yaml_string(value)}" for value in items)


def frontmatter(fields: list[tuple[str, str | bool | int | list[str]]]) -> str:
    lines = ["---"]
    for key, value in fields:
        if isinstance(value, list):
            rendered = yaml_string_list(value)
            if rendered == "[]":
                lines.append(f"{key}: []")
            else:
                lines.extend((f"{key}:", rendered))
        elif type(value) is bool:
            lines.append(f"{key}: {str(value).lower()}")
        elif type(value) is int:
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {yaml_string(str(value))}")
    lines.append("---")
    return "\n".join(lines)
