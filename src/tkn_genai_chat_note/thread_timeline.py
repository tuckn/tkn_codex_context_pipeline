"""Source-derived ordering, timestamps, actors, and coverage for Thread Notes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .chat_logs import ChatEvent

TIMELINE_TIMEZONE = "Asia/Tokyo"
_JST = timezone(timedelta(hours=9))


def event_time(value: str) -> datetime | None:
    """Do not guess the zone of an absent, invalid, or timezone-naive timestamp."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(_JST) if parsed.tzinfo is not None else None


def validate_timeline(items: list[dict[str, Any]], events: Sequence[ChatEvent]) -> None:
    by_id = {event.id: event for event in events}
    order = {event.id: index for index, event in enumerate(events)}
    covered: set[str] = set()
    for item in items:
        start_id, end_id = item["startEventId"], item["endEventId"]
        if start_id not in by_id or end_id not in by_id:
            raise ValueError("timeline endpoints must refer to supplied events")
        if start_id not in item["eventIds"] or end_id not in item["eventIds"]:
            raise ValueError("timeline endpoints must be included in eventIds")
        source_text = "\n".join(by_id[id].text for id in item["eventIds"] if id in by_id)
        source_text = source_text.replace("\\\\", "\\").replace("\\", "/").casefold()
        for literal in re.findall(r"`([^`\n]+)`", item["text"]):
            if re.match(r"^(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|\._)", literal):
                normalized = literal.replace("\\", "/").casefold()
                if normalized not in source_text:
                    raise ValueError(
                        f"timeline literal path is absent from cited source events: {literal}; "
                        "copy the exact source spelling or use a supported shorter name"
                    )
        start, end = by_id[start_id], by_id[end_id]
        if order[start_id] > order[end_id]:
            raise ValueError("timeline endpoints must follow source order")
        if start_id != end_id:
            first_time, last_time = event_time(start.timestamp), event_time(end.timestamp)
            if (
                start.actor != end.actor or start.kind != end.kind or start.turn_id != end.turn_id
                or start.kind in {"user_message", "assistant_message"}
                or first_time is None or last_time is None
                or first_time.date() != last_time.date() or first_time > last_time
                or any(e.actor == "user" for e in events[order[start_id] + 1:order[end_id] + 1])
            ):
                raise ValueError("timeline ranges must group same-kind tool events within one turn and day")
        if start.actor == "user":
            covered.add(start_id)
    missing = [event.id for event in events if event.kind == "user_message" and event.id not in covered]
    if missing:
        raise ValueError("timeline is missing user messages: " + ", ".join(missing))


def ordered_timeline(items: list[dict[str, Any]], events: Sequence[ChatEvent]) -> list[dict[str, Any]]:
    order = {event.id: index for index, event in enumerate(events)}
    return sorted(items, key=lambda item: order[item["startEventId"]])


def render_timeline(items: list[dict[str, Any]], events: Sequence[ChatEvent]) -> str:
    validate_timeline(items, events)
    by_id = {event.id: event for event in events}
    lines: list[str] = ["日時は日本時間（Asia/Tokyo）。ログの記録時刻であり、作業時間の計測値ではありません。", ""]
    previous_day = ""
    for item in ordered_timeline(items, events):
        start, end = by_id[item["startEventId"]], by_id[item["endEventId"]]
        first_time, last_time = event_time(start.timestamp), event_time(end.timestamp)
        day = first_time.strftime("%Y-%m-%d") if first_time else "Unknown date"
        if day != previous_day:
            lines.extend([f"### {day}", ""])
            previous_day = day
        timestamp = first_time.strftime("%H:%M:%S") if first_time else "Unknown"
        if end.id != start.id and last_time:
            timestamp += " - " + last_time.strftime("%H:%M:%S")
        actor = {"user": "User", "assistant": "AI", "tool": "Tool"}.get(start.actor, start.actor)
        refs = ", ".join(dict.fromkeys(item["eventIds"]))
        # Keep prose continuation lines below Text, never at the metadata-field level.
        text_lines = item["text"].strip().splitlines()
        lines.extend([
            f"- **{timestamp}**",
            f"  - Actor: {actor}",
            f"  - Type: {item['label']}",
            f"  - Text: {text_lines[0]}",
            *(f"    {line}" if line else "" for line in text_lines[1:]),
            f"  - EventRange: {start.id} -> {end.id}",
            f"  - Sources: {refs}",
            "",
        ])
    return "\n".join(lines).rstrip()
