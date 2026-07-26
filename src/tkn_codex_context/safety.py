"""Safety checks shared by context distillation and audits."""

from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
]


REDACTION_MARKER = "[REDACTED]"


def has_secret_like_content(text: str) -> list[str]:
    hits = []
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(pattern.pattern)
    return hits


def redact_secret_like_content(text: str) -> str:
    """Replace common credential shapes while preserving surrounding evidence."""
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted
