"""Preserve distinct histories for one thread without choosing a winning branch."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Any

from .chat_logs import ChatEvent, ChatMessage, ThreadSource, iter_json_line_records
from .raw_capture import RawSourceInput


@dataclass(frozen=True)
class ThreadHistory:
    source: ThreadSource
    captures: tuple[RawSourceInput, ...]
    branches: tuple[dict[str, Any], ...] = ()

    @property
    def source_set_sha256(self) -> str:
        if not self.branches:
            return ""
        # Path aliases and identical archived copies do not alter generation inputs.
        hashes = sorted(item.capture_sha256 for item in self.captures)
        return sha256(json.dumps(hashes, separators=(",", ":")).encode()).hexdigest()


def _time_key(value: str) -> tuple[int, float, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return (0, parsed.timestamp(), "")
    except ValueError:
        pass
    return (1, 0, value)


def combine_histories(versions: list[tuple[RawSourceInput, ThreadSource]]) -> ThreadHistory:
    """Collapse exact copies/byte prefixes; retain every divergent history in order.

    This never reconstructs an assumed active branch from history_base or ordinal.
    Each retained history has its own event namespace and exact Raw locators.
    """
    unique: dict[str, tuple[RawSourceInput, ThreadSource]] = {}
    for item, source in sorted(versions, key=lambda pair: pair[0].source_ref):
        unique.setdefault(item.capture_sha256, (item, source))
    ordered = sorted(unique.values(), key=lambda pair: (-pair[0].byte_count, pair[0].capture_sha256))
    if len(ordered) == 1:
        item, source = ordered[0]
        return ThreadHistory(source, (item,))
    retained: list[tuple[RawSourceInput, ThreadSource]] = []
    contents: list[bytes] = []
    for item, source in ordered:
        content = item.source_path.read_bytes()
        if not any(previous.startswith(content) for previous in contents):
            retained.append((item, source))
            contents.append(content)
    if len(retained) == 1:
        item, source = retained[0]
        return ThreadHistory(source, (item,))

    retained.sort(key=lambda pair: (
        _time_key(pair[1].thread_log.timestamp if pair[1].thread_log else ""), pair[0].capture_sha256,
    ))
    branch_ids = ["H" + item.capture_sha256[:16] for item, _ in retained]
    if len(set(branch_ids)) != len(branch_ids):
        branch_ids = ["H" + item.capture_sha256 for item, _ in retained]
    events: list[ChatEvent] = []
    messages: list[ChatMessage] = []
    branches: list[dict[str, Any]] = []
    for branch_id, (item, source) in zip(branch_ids, retained, strict=True):
        log = source.thread_log
        assert log is not None
        metadata: dict[str, Any] = next((row.get("payload", {}) for _, row in iter_json_line_records(item.source_path)
                         if row.get("type") == "session_meta"), {})
        branches.append({
            "branchId": branch_id,
            "sourceRef": item.source_ref,
            "captureRef": item.capture_ref,
            "captureSha256": item.capture_sha256,
            "startedAt": log.timestamp,
            "lastEventAt": source.last_event_at,
            "historyBase": metadata.get("history_base"),
        })
        messages.extend(log.messages)
        events.extend(replace(event, id=f"{branch_id}-{event.id}", branch_id=branch_id,
                              raw_ref=f"{item.capture_ref}#{event.id}") for event in source.events)
    first_log = retained[0][1].thread_log
    assert first_log is not None
    times = [source.last_event_at for _, source in retained if source.last_event_at]
    valid_times = [value for value in times if _time_key(value)[0] == 0]
    last = max(valid_times, key=_time_key) if valid_times else (times[-1] if times else "")
    return ThreadHistory(
        ThreadSource(replace(first_log, messages=tuple(messages)), tuple(events), last),
        tuple(item for item, _ in retained), tuple(branches),
    )
