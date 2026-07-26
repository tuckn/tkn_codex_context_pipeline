#!/usr/bin/env python3
"""Read Codex JSONL chat logs without modifying the source files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APPROVAL_REVIEW_PREFIX = "The following is the Codex agent history"
KNOWN_INTERNAL_THREAD_SOURCES = {"approval_review", "subagent"}


@dataclass(frozen=True)
class ChatMessage:
    role: str
    source: str
    text: str
    timestamp: str
    turn_id: str
    cwd: str


@dataclass(frozen=True)
class ChatEvent:
    id: str
    kind: str
    actor: str
    name: str
    text: str
    timestamp: str
    turn_id: str
    cwd: str


@dataclass(frozen=True)
class SessionLog:
    id: str
    timestamp: str
    cwd: str
    path: str
    originator: str
    source: str
    thread_source: str
    repository_url: str
    messages: tuple[ChatMessage, ...]

    @property
    def user_messages(self) -> tuple[ChatMessage, ...]:
        return tuple(message for message in self.messages if message.role == "user")

    @property
    def assistant_messages(self) -> tuple[ChatMessage, ...]:
        return tuple(message for message in self.messages if message.role == "assistant")


def default_sessions_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def normalize_message_text(value: str) -> str:
    return " ".join(value.split())


def normalize_path_text(value: str) -> str:
    normalized = value.replace("/", "\\").rstrip("\\")
    wsl_mount = re.match(r"^\\mnt\\([A-Za-z])(?:\\(.*))?$", normalized)
    if wsl_mount:
        drive = wsl_mount.group(1)
        remainder = wsl_mount.group(2) or ""
        normalized = f"{drive}:\\{remainder}".rstrip("\\")
    return normalized.casefold()


def path_is_within(value: str, root: str) -> bool:
    child = normalize_path_text(value)
    parent = normalize_path_text(root)
    if not child or not parent:
        return False
    return child == parent or child.startswith(parent + "\\")


def normalize_repository_url(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    normalized = re.sub(r"^git@([^:]+):", r"https://\1/", normalized)
    normalized = normalized.removesuffix(".git").rstrip("/")
    return normalized.casefold()


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("input_text") or ""
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def clean_user_text(text: str) -> str:
    for marker in ("## My request for Codex:", "## My request for Codex"):
        if marker in text:
            return text.split(marker, 1)[1].strip()

    if text.lstrip().startswith("# AGENTS.md instructions for"):
        return ""

    if "<INSTRUCTIONS>" in text and "</INSTRUCTIONS>" in text:
        text = text.split("</INSTRUCTIONS>", 1)[-1]

    if "<environment_context>" in text:
        text = text.split("<environment_context>", 1)[0]

    return text.strip()


def iter_json_line_records(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"warning: {path}:{line_number}: {exc}", file=sys.stderr)
                continue
            if isinstance(value, dict):
                yield line_number, value


def iter_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    for _line_number, value in iter_json_line_records(path):
        yield value


def event_payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def read_session_events(path: Path) -> tuple[ChatEvent, ...]:
    """Extract user, assistant, tool, and validation evidence in source order."""
    events: list[ChatEvent] = []
    seen_messages: set[tuple[str, str, str, str]] = set()
    session_cwd = ""
    turn_cwd = ""
    turn_id = ""

    def append_event(
        line_number: int,
        kind: str,
        actor: str,
        name: str,
        text: str,
        timestamp: str,
        *,
        dedupe_message: bool = False,
    ) -> None:
        cleaned = text.strip()
        if actor == "user":
            cleaned = clean_user_text(cleaned)
        if not cleaned:
            return
        if dedupe_message:
            key = (kind, actor, turn_id, normalize_message_text(cleaned))
            if key in seen_messages:
                return
            seen_messages.add(key)
        events.append(
            ChatEvent(
                id=f"L{line_number:06d}",
                kind=kind,
                actor=actor,
                name=name,
                text=cleaned,
                timestamp=timestamp,
                turn_id=turn_id,
                cwd=turn_cwd or session_cwd,
            )
        )

    ignored_event_messages = {
        "agent_reasoning",
        "token_count",
        "task_started",
        "thread_settings_applied",
        "context_compacted",
    }

    for line_number, obj in iter_json_line_records(path):
        event_type = str(obj.get("type") or "")
        payload_value = obj.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        timestamp = str(obj.get("timestamp") or "")

        if event_type == "session_meta":
            session_cwd = str(payload.get("cwd") or session_cwd)
            turn_cwd = session_cwd
            continue
        if event_type == "turn_context":
            turn_id = str(payload.get("turn_id") or "")
            turn_cwd = str(payload.get("cwd") or session_cwd)
            continue
        if not event_type and obj.get("id") and "instructions" in obj:
            session_cwd = str(obj.get("cwd") or session_cwd)
            turn_cwd = session_cwd
            continue

        if event_type == "response_item":
            payload_type = str(payload.get("type") or "")
            if payload_type in {"", "message"}:
                role = str(payload.get("role") or "")
                if role in {"user", "assistant"}:
                    append_event(
                        line_number,
                        f"{role}_message",
                        role,
                        str(payload.get("phase") or ""),
                        content_to_text(payload.get("content")),
                        timestamp,
                        dedupe_message=True,
                    )
                continue
            if payload_type in {"custom_tool_call", "function_call", "tool_call"}:
                append_event(
                    line_number,
                    "tool_call",
                    "assistant",
                    str(payload.get("name") or payload_type),
                    event_payload_text(payload.get("input", payload.get("arguments"))),
                    timestamp,
                )
                continue
            if payload_type in {
                "custom_tool_call_output",
                "function_call_output",
                "tool_call_output",
            }:
                append_event(
                    line_number,
                    "tool_result",
                    "tool",
                    str(payload.get("name") or payload.get("call_id") or payload_type),
                    event_payload_text(payload.get("output")),
                    timestamp,
                )
                continue
            if payload_type in {"web_search_call", "tool_search_call"}:
                append_event(
                    line_number,
                    "tool_call",
                    "assistant",
                    payload_type,
                    event_payload_text(payload),
                    timestamp,
                )
                continue

        if event_type == "message":
            role = str(obj.get("role") or "")
            if role in {"user", "assistant"}:
                append_event(
                    line_number,
                    f"{role}_message",
                    role,
                    "legacy",
                    content_to_text(obj.get("content")),
                    timestamp,
                    dedupe_message=True,
                )
            continue

        if event_type == "event_msg":
            payload_type = str(payload.get("type") or "")
            if payload_type == "user_message":
                append_event(
                    line_number,
                    "user_message",
                    "user",
                    "",
                    str(payload.get("message") or ""),
                    timestamp,
                    dedupe_message=True,
                )
            elif payload_type == "agent_message":
                append_event(
                    line_number,
                    "assistant_message",
                    "assistant",
                    str(payload.get("phase") or ""),
                    str(payload.get("message") or ""),
                    timestamp,
                    dedupe_message=True,
                )
            elif payload_type and payload_type not in ignored_event_messages:
                evidence_fields = {
                    key: value for key, value in payload.items() if key not in {"type", "last_agent_message"}
                }
                if evidence_fields:
                    kind = "tool_result" if payload_type.endswith(("_end", "_complete")) else "event"
                    append_event(
                        line_number,
                        kind,
                        "tool",
                        payload_type,
                        event_payload_text(evidence_fields),
                        timestamp,
                    )

    return tuple(events)


def select_events_for_roots(
    events: Sequence[ChatEvent],
    roots: Sequence[str],
) -> tuple[ChatEvent, ...]:
    return tuple(event for event in events if any(path_is_within(event.cwd, root) for root in roots))


def fingerprint_events(
    session_id: str,
    events: Sequence[ChatEvent],
    relative_source_ref: str,
) -> str:
    payload = {
        "id": session_id,
        "sourceRef": relative_source_ref,
        "events": [
            {
                "id": event.id,
                "kind": event.kind,
                "actor": event.actor,
                "name": event.name,
                "text": normalize_message_text(event.text),
                "turnId": event.turn_id,
                "cwd": normalize_path_text(event.cwd),
                "timestamp": event.timestamp,
            }
            for event in events
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_session(path: Path) -> SessionLog | None:
    meta: dict[str, Any] | None = None
    messages: list[ChatMessage] = []
    seen: set[tuple[str, str, str]] = set()
    turn_id = ""
    turn_cwd = ""

    def append_message(role: str, source: str, text: str, timestamp: str) -> None:
        if role == "user":
            text = clean_user_text(text)
        text = text.strip()
        if not text:
            return
        effective_cwd = turn_cwd or str((meta or {}).get("cwd") or "")
        key = (role, turn_id, normalize_message_text(text))
        if key in seen:
            return
        seen.add(key)
        messages.append(
            ChatMessage(
                role=role,
                source=source,
                text=text,
                timestamp=timestamp,
                turn_id=turn_id,
                cwd=effective_cwd,
            )
        )

    for obj in iter_json_lines(path):
        event_type = obj.get("type")
        payload_value = obj.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        timestamp = str(obj.get("timestamp") or "")

        if meta is None and not event_type and obj.get("id") and obj.get("timestamp") and "instructions" in obj:
            # Legacy Codex JSONL placed session metadata directly in the first object.
            meta = obj
            turn_cwd = str(obj.get("cwd") or "")
            continue

        if event_type == "session_meta" and isinstance(payload, dict):
            meta = payload
            turn_cwd = str(payload.get("cwd") or "")
            continue

        if event_type == "turn_context" and isinstance(payload, dict):
            turn_id = str(payload.get("turn_id") or "")
            turn_cwd = str(payload.get("cwd") or (meta or {}).get("cwd") or "")
            continue

        if event_type == "response_item" and isinstance(payload, dict):
            role = payload.get("role")
            if role in {"user", "assistant"}:
                append_message(role, "response_item", content_to_text(payload.get("content")), timestamp)
            continue

        if event_type == "message":
            role = obj.get("role")
            if role in {"user", "assistant"}:
                append_message(
                    role,
                    "legacy_message",
                    content_to_text(obj.get("content")),
                    timestamp or str((meta or {}).get("timestamp") or ""),
                )
            continue

        if event_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "user_message":
                append_message("user", "event_msg", str(payload.get("message") or ""), timestamp)
            elif payload_type == "agent_message":
                append_message("assistant", "event_msg", str(payload.get("message") or ""), timestamp)

    if not meta:
        return None

    git_value = meta.get("git")
    git: dict[str, Any] = git_value if isinstance(git_value, dict) else {}
    return SessionLog(
        id=str(meta.get("id") or meta.get("session_id") or ""),
        timestamp=str(meta.get("timestamp") or ""),
        cwd=str(meta.get("cwd") or ""),
        path=str(path),
        originator=str(meta.get("originator") or ""),
        source=str(meta.get("source") or ""),
        thread_source=str(meta.get("thread_source") or ""),
        repository_url=str(git.get("repository_url") or ""),
        messages=tuple(messages),
    )


def is_approval_review(session: SessionLog) -> bool:
    return any(message.text.lstrip().startswith(APPROVAL_REVIEW_PREFIX) for message in session.user_messages)


def is_known_internal_session(session: SessionLog) -> bool:
    return session.thread_source.casefold() in KNOWN_INTERNAL_THREAD_SOURCES


def has_clean_user_message(session: SessionLog) -> bool:
    return bool(session.user_messages)


def select_messages_for_roots(
    session: SessionLog,
    roots: Sequence[str],
) -> tuple[ChatMessage, ...]:
    return tuple(
        message
        for message in session.messages
        if any(path_is_within(message.cwd or session.cwd, root) for root in roots)
    )


def source_ref(path: Path, sessions_root: Path) -> str:
    return path.resolve().relative_to(sessions_root.resolve()).as_posix()


def fingerprint_session(
    session: SessionLog,
    messages: Sequence[ChatMessage],
    relative_source_ref: str,
) -> str:
    payload = {
        "id": session.id,
        "timestamp": session.timestamp,
        "repositoryUrl": normalize_repository_url(session.repository_url),
        "sourceRef": relative_source_ref,
        "messages": [
            {
                "role": message.role,
                "text": normalize_message_text(message.text),
                "turnId": message.turn_id,
                "cwd": normalize_path_text(message.cwd or session.cwd),
                "timestamp": message.timestamp,
            }
            for message in messages
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
