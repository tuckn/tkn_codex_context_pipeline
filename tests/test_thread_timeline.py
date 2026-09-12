from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from test_session_note_pipeline import note_data

from tkn_genai_chat_note.chat_logs import ChatEvent
from tkn_genai_chat_note.session_notes import (
    Candidate,
    PipelineConfig,
    PipelineError,
    Project,
    ProviderSummarizer,
    render_note,
    validate_note_data,
    validate_session_note,
)
from tkn_genai_chat_note.thread_timeline import render_timeline, validate_timeline


def event(id: str, *, actor: str = "user", time: str = "2026-01-01T23:01:02.345Z") -> ChatEvent:
    return ChatEvent(id, f"{actor}_message", actor, "", "request", time, "turn", "")


def entry(e: ChatEvent, text: str = "記録", label: str = "Request") -> dict:
    return {"label": label, "text": text, "eventIds": [e.id], "startEventId": e.id, "endEventId": e.id}


def candidate(root: Path, events: tuple[ChatEvent, ...]) -> Candidate:
    return Candidate(
        Project("review", "Review", root, root),
        "thread",
        "2026-01-01T00:00:00Z",
        root / "source.jsonl",
        "source.jsonl",
        "source.jsonl",
        "fingerprint",
        events,
        events[-1].timestamp,
    )


def config(root: Path) -> PipelineConfig:
    return PipelineConfig("2026-01-01T00:00:00Z", root, root / "raw", "test", "codex")


def test_dates_and_actors_come_from_endpoints_not_supporting_evidence() -> None:
    old = event("old", time="2026-01-01T01:00:00Z")
    reply = event("reply", actor="assistant")
    item = entry(reply, label="Reported Result")
    item["eventIds"].insert(0, old.id)
    text = render_timeline([item, entry(old)], (old, reply))
    assert text.index("### 2026-01-01") < text.index("### 2026-01-02")
    assert "- **08:01:02**\n  - Actor: AI\n  - Type: Reported Result" in text
    assert "- **10:00:00**\n  - Actor: User\n  - Type: Request" in text


@pytest.mark.parametrize("timestamp", ["", "invalid", "2026-01-01T00:00:00"])
def test_missing_or_naive_time_is_never_invented(timestamp: str) -> None:
    source = event("missing", time=timestamp)
    assert "- **Unknown**\n  - Actor: User" in render_timeline([entry(source)], (source,))


def test_source_order_is_preserved_even_when_clock_moves_backwards() -> None:
    first = event("first", time="2026-01-01T01:02:00Z")
    later = event("later", time="2026-01-01T01:01:00Z")
    text = render_timeline([entry(later, "後の訂正"), entry(first, "最初の依頼")], (first, later))
    assert text.index("最初の依頼") < text.index("後の訂正")


def test_user_coverage_cannot_be_satisfied_by_citing_user_as_background() -> None:
    user, reply = event("user"), event("reply", actor="assistant")
    item = entry(reply)
    item["eventIds"].append(user.id)
    with pytest.raises(ValueError, match="missing user messages"):
        validate_timeline([item], (user, reply))


@pytest.mark.parametrize("change", ["actor", "day", "turn", "user-between", "reverse"])
def test_invalid_ranges_are_rejected(change: str) -> None:
    start = replace(event("start", actor="tool"), kind="tool_result")
    end = replace(start, id="end")
    events = [start, end]
    if change == "actor":
        end = replace(end, actor="assistant")
    elif change == "day":
        end = replace(end, timestamp="2026-01-03T00:00:00Z")
    elif change == "turn":
        end = replace(end, turn_id="another")
    events[-1] = end
    if change == "user-between":
        events.insert(1, event("interruption"))
    if change == "reverse":
        events.reverse()
    item = entry(start)
    item.update(endEventId=end.id, eventIds=[start.id, end.id])
    with pytest.raises(ValueError):
        validate_timeline([item], events)


def test_unknown_and_uncited_endpoints_are_rejected(tmp_path: Path) -> None:
    source = event("known")
    data = note_data(candidate(tmp_path, (source,)))
    data["timeline"][0]["startEventId"] = "invented"
    with pytest.raises(PipelineError, match="endpoints"):
        validate_note_data(data, {source.id})


def test_chunk_merge_preserves_all_trials_and_repetitions_without_reduction(tmp_path: Path) -> None:
    sources = tuple(event(f"e{i}") for i in range(3))
    case = candidate(tmp_path, sources)
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=250)
    original_entries = []

    def invoke(prompt: str, *, overview_only: bool = False) -> dict:
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        if overview_only:
            data = note_data(case)
            data.pop("timeline")
            return data
        ids = [e["id"] for e in payload["events"]]
        assert len(ids) == 1
        current = next(e for e in sources if e.id == ids[0])
        data = note_data(candidate(tmp_path, (current,)))
        data["timeline"] = [entry(current, f"試行{j}：" + "繰り返した操作。" * 40) for j in range(8)]
        data["sourceLimitations"] = ["この部分には後続の検証が含まれていない。"]
        original_entries.extend(deepcopy(data["timeline"]))
        return data

    with patch.object(runner, "_invoke", side_effect=invoke):
        result = runner.generate(case)
    assert len(result["timeline"]) == 24
    assert result["timeline"] == original_entries
    assert result["sourceLimitations"] == []
    assert len(json.dumps(result, ensure_ascii=False)) > 9000


def test_chunk_validation_rejects_ids_from_other_parts_and_repairs_with_source(tmp_path: Path) -> None:
    first, second = event("first"), event("second")
    case = candidate(tmp_path, (first, second))
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=250)
    prompts = []
    bad = note_data(case)
    good = note_data(candidate(tmp_path, (first,)))
    with patch.object(runner, "_invoke", side_effect=[bad, good]) as invoke:
        result = runner._validated_invoke("source", {first.id}, case.thread_id, events=(first,))
        prompts = [call.args[0] for call in invoke.call_args_list]
    assert result == good
    assert "unknown event ids" in prompts[1]
    assert '"events":' in prompts[1]


def test_long_v5_note_retains_all_timeline_entries(tmp_path: Path) -> None:
    user, reply = event("user"), event("reply", actor="assistant")
    case = candidate(tmp_path, (user, reply))
    data = note_data(case)
    data["timeline"] = [entry(user, "根拠を保持する。", "Explicit Decision")]
    data["timeline"] += [
        entry(reply, f"試行{i}：" + "確認できた結果を記録する。" * 50, "Reported Result") for i in range(60)
    ]
    path = case.project.session_notes_path / "long.md"
    path.parent.mkdir()
    path.write_text(render_note(case, data, {}), encoding="utf-8")
    assert path.stat().st_size > 30000
    assert validate_session_note(path)["valid"]
    assert "試行59" in path.read_text(encoding="utf-8")


def test_long_input_middle_reaches_generation_without_truncation_notice(tmp_path: Path) -> None:
    source = replace(event("large"), text="前" * 6000 + "中央の訂正を保持する" + "後" * 6000)
    case = candidate(tmp_path, (source,))
    runner = ProviderSummarizer(config(tmp_path))
    with patch.object(runner, "_invoke", return_value=note_data(case)) as invoke:
        result = runner.generate(case)
    payload = json.loads(invoke.call_args.args[0].split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
    assert payload["events"][0]["text"] == source.text
    assert result["sourceLimitations"] == []
    assert runner.last_metrics["preparedTextCharacters"] == runner.last_metrics["submittedTextCharacters"]


def test_last_state_needs_evidence(tmp_path: Path) -> None:
    case = candidate(tmp_path, (event("user"),))
    data = note_data(case)
    data["lastKnownState"]["eventIds"] = []
    with pytest.raises(PipelineError, match="lastKnownState requires"):
        validate_note_data(data, {"user"})


def test_ambiguous_decision_and_bad_range_are_reported_together(tmp_path: Path) -> None:
    user, reply = event("user"), event("reply", actor="assistant")
    case = candidate(tmp_path, (user, reply))
    bad = note_data(case)
    bad["timeline"][0].update(
        label="Explicit Decision", text="運用イメージを示した。", endEventId=reply.id, eventIds=[user.id, reply.id]
    )
    runner = ProviderSummarizer(config(tmp_path))
    with patch.object(runner, "_invoke", side_effect=[bad, note_data(case)]) as invoke:
        runner._validated_invoke("source", {user.id, reply.id}, case.thread_id, events=case.events)
    repair = invoke.call_args_list[1].args[0]
    assert "tentative choice" in repair
    assert "same-kind tool events" in repair


def test_literal_path_typo_is_repaired_against_cited_source() -> None:
    source = replace(event("path"), text=r"Resolve-Path .\_local\tool.py")
    with pytest.raises(ValueError, match="literal path is absent"):
        validate_timeline([entry(source, "相対path`._local`を参照した。")], (source,))
    validate_timeline([entry(source, r"相対path`.\_local`を参照した。")], (source,))


def test_literal_path_matches_json_escaped_windows_source() -> None:
    source = replace(event("path"), text=json.dumps({"path": r"C:\Example\tools"}))
    validate_timeline([entry(source, "`C:/Example/tools`を参照した。")], (source,))


def test_text_continuations_cannot_be_mistaken_for_metadata() -> None:
    source = event("L000006")
    prose = "説明。\n- Type: Explicit Decision\n\nSources: prose, not metadata"
    text = render_timeline([entry(source, prose)], (source,))
    assert "  - Text: 説明。\n    - Type: Explicit Decision\n\n    Sources: prose, not metadata" in text
    assert "**Explicit Decision**" not in text and "### Explicit Decision" not in text
    assert "  - EventRange: L000006 -> L000006\n  - Sources: L000006" in text


def test_tool_calls_and_results_keep_actor_separate_from_classification() -> None:
    call = replace(event("L000010", actor="assistant"), kind="tool_call")
    result = replace(event("L000012", actor="tool"), kind="tool_result")
    text = render_timeline([entry(call, label="Action"), entry(result, label="Validation")], (call, result))
    assert "  - Actor: AI\n  - Type: Action" in text
    assert "  - Actor: Tool\n  - Type: Validation" in text
    assert "｜" not in text and "〔" not in text


def test_empty_state_values_remain_unrecorded_and_optional_sections_stay_optional(tmp_path: Path) -> None:
    case = candidate(tmp_path, (event("L000006"),))
    data = note_data(case)
    data["lastKnownState"].update(latestUserDirection="", continuationPoint="")
    text = render_note(case, data, {})
    assert "- Latest User Direction: null" in text
    assert "- Unresolved: []" in text and "- Unverified: []" in text
    assert "- Continuation Point: null" in text
    assert "追加指示なし" not in text
    assert "## Evidence" not in text and "## Source Notes" not in text
    assert "  - Sources: L000006" in text.split("## Timeline")[0]
    path = tmp_path / "note.md"
    path.write_text(text, encoding="utf-8")
    assert validate_session_note(path)["valid"]


def test_state_lists_and_multiline_prose_cannot_override_metadata(tmp_path: Path) -> None:
    case = candidate(tmp_path, (event("L000006"),))
    data = note_data(case)
    data["lastKnownState"].update(
        workState="blocked",
        detail="確認待ち。\n- Work State: done",
        unresolved=["入力を確認する。\n- Type: Explicit Decision"],
        unverified=["動作確認が未実施。"],
        continuationPoint="入力の回答を待つ。",
    )
    data["evidence"] = [{"text": "出力を確認した。", "eventIds": ["L000006"]}]
    data["sourceLimitations"] = ["後続の実行結果は記録されていない。"]
    text = render_note(case, data, {})
    assert "- Unresolved:\n  - Text: 入力を確認する。\n    - Type: Explicit Decision" in text
    assert "- Unverified:\n  - Text: 動作確認が未実施。" in text
    assert "**Explicit Decision**" not in text and "### Explicit Decision" not in text
    assert "## Evidence\n\n- Text: 出力を確認した。\n  - Sources: L000006" in text
    assert "Sources:" not in text.split("## Source Notes")[1]
    path = tmp_path / "note.md"
    path.write_text(text, encoding="utf-8")
    assert validate_session_note(path)["status"] == "blocked"
