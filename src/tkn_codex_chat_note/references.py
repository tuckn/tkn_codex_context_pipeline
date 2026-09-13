"""Resolve source-store locators without changing durable note or evidence bytes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .session_notes import PipelineError


def _contained(root: Path, relative: str) -> Path:
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise PipelineError(f"invalid portable store reference: {relative}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PipelineError("store reference escapes its root")
    return path


def resolve_store_ref(root: Path, ref: str, descriptor: dict[str, Any], *, kind: str = "data") -> Path:
    if kind == "raw":
        prefix = descriptor.get("rawRefPrefix")
        aliases = descriptor.get("rawRefAliases", [])
        if (
            not isinstance(prefix, str)
            or not isinstance(aliases, list)
            or not all(isinstance(item, str) for item in aliases)
        ):
            raise PipelineError("invalid Raw reference prefixes")
        for candidate in sorted([prefix, *aliases], key=len, reverse=True):
            if not candidate.startswith("raw:/") or not candidate.endswith("/"):
                raise PipelineError("invalid Raw reference prefix")
            if ref.startswith(candidate):
                return _contained(root, ref[len(candidate) :])
        raise PipelineError("Raw reference belongs to another source store")
    if kind != "data" or not ref.startswith("data:/"):
        raise PipelineError(f"unsupported store reference: {ref}")
    relative = ref.removeprefix("data:/")
    _contained(root, relative)
    aliases = descriptor.get("dataRefAliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(old, str) or not isinstance(new, str) or not old.endswith("/") for old, new in aliases.items()
    ):
        raise PipelineError("invalid data reference aliases")
    for old, new in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        if relative.startswith(old):
            relative = new + relative[len(old) :]
            break
    return _contained(root, relative)
