"""Lossless, bounded source input and source-scoped repair regressions."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from test_thread_note_pipeline import note_data
from test_thread_timeline import candidate, config, entry, event

from tkn_genai_chat_note.safety import redact_secret_like_content
from tkn_genai_chat_note.thread_notes import PipelineError, ProviderSummarizer, chunk_events, prepare_events


def payload(prompt: str) -> dict:
    return json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])


@pytest.mark.parametrize("text", ["", "短い発言", "前" * 6000 + "重要な訂正" + "後" * 6000,
                                  ('日本語😀\n\t"\\' * 500), "a" * 240000],
                         ids=["empty", "short", "middle", "escaped", "oversized"])
def test_parts_reconstruct_the_entire_redacted_event_and_obey_json_budget(text: str) -> None:
    source = replace(event("L000123"), text=text)
    prepared = prepare_events([source])
    chunks = chunk_events(prepared, 1200)
    parts = [e for chunk in chunks for e in chunk]
    assert "".join(e.text for e in parts) == text
    assert all(len(json.dumps([e.as_dict() for e in chunk], ensure_ascii=False)) <= 1200 for chunk in chunks)
    assert all((e.id, e.actor, e.timestamp, e.turn_id) ==
               (source.id, source.actor, source.timestamp, source.turn_id) for e in parts)
    if len(parts) > 1:
        offset = 0
        for index, part in enumerate(parts, 1):
            assert part.text_part == index and part.text_part_count == len(parts)
            assert part.text_start == offset
            offset += len(part.text)
            assert part.text_end == offset
            assert part.full_text_characters == len(text)
        assert all(len(chunk) == 1 for chunk in chunks)
    else:
        assert "textPart" not in parts[0].as_dict()


def test_redaction_precedes_splitting_and_never_leaks_a_boundary_spanning_secret() -> None:
    text = "前" * 1000 + "\napi_key=" + "a" * 64 + "\n" + "後" * 1000
    prepared = prepare_events([replace(event("secret"), text=text)])
    parts = [e for c in chunk_events(prepared, 1200) for e in c]
    assert "".join(e.text for e in parts) == redact_secret_like_content(text)
    assert "[REDACTED]" in "".join(e.text for e in parts)
    assert all("a" * 16 not in e.text for e in parts)


def test_event_order_and_boundaries_survive_an_oversized_event() -> None:
    sources = [event("before"), replace(event("large"), text="中" * 3000), event("after")]
    chunks = chunk_events(prepare_events(sources), 1200)
    ids = [e.id for chunk in chunks for e in chunk]
    assert ids[0] == "before" and ids[-1] == "after"
    assert all(id == "large" for id in ids[1:-1])
    assert "".join(e.text for c in chunks for e in c if e.id == "large") == sources[1].text


@pytest.mark.parametrize("budget", [0, -1, 10])
def test_impossible_budget_fails_explicitly(budget: int) -> None:
    with pytest.raises(PipelineError, match="budget"):
        chunk_events(prepare_events([event("source")]), budget)


def test_split_user_middle_and_repairs_keep_only_the_current_source_piece(tmp_path: Path) -> None:
    source = replace(event("L000123"), text="最初。" * 200 + "中央の訂正。" + "最後。" * 200)
    case = candidate(tmp_path, (source,))
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=650)
    inputs, repaired, facts = [], [], []
    rejected = False

    def invoke(prompt: str, *, overview_only: bool = False) -> dict:
        nonlocal rejected
        data = note_data(case)
        if overview_only:
            data.pop("timeline")
            return data
        current = payload(prompt)
        piece = current["events"][0]
        if "validationError" in current:
            repaired.append(piece)
        else:
            inputs.append(piece)
        if not rejected and piece["textPart"]["index"] == 2:
            rejected = True
            data["timeline"][0]["startEventId"] = "invented"
            return data
        fact = "中央の訂正を記録。" if "中央の訂正" in piece["text"] else f"内容{piece['textPart']['index']}。"
        data["timeline"] = [entry(source, fact)]
        facts.append(fact)
        return data

    with patch.object(runner, "_invoke", side_effect=invoke):
        result = runner.generate(case)
    assert len(inputs) > 2
    assert repaired == [inputs[1]]  # No full source is reintroduced during semantic repair.
    assert "".join(e["text"] for e in inputs) == source.text
    assert [item["text"] for item in result["timeline"]] == facts
    assert "中央の訂正を記録。" in facts
    assert all(item["eventIds"] == [source.id] for item in result["timeline"])
    assert runner.last_metrics["splitEventCount"] == 1
    assert runner.last_metrics["preparedTextCharacters"] == runner.last_metrics["submittedTextCharacters"]
    assert result["sourceLimitations"] == []


def test_fragment_validation_cannot_borrow_a_literal_from_an_unseen_piece(tmp_path: Path) -> None:
    source = replace(event("L000123"), text="Read `C:/example/first.py`.\n" + "later text.\n" * 200)
    case = candidate(tmp_path, (source,))
    part = chunk_events(prepare_events([source]), 650)[1][0]
    assert "C:/example/first.py" not in part.text
    invalid, valid = note_data(case), note_data(case)
    invalid["timeline"] = [entry(source, "`C:/example/first.py`を確認した。")]
    runner = ProviderSummarizer(config(tmp_path), chunk_characters=650)
    with patch.object(runner, "_invoke", side_effect=[invalid, valid]) as invoke:
        result = runner._validated_invoke(
            "source", {source.id}, case.thread_id,
            events=(replace(source, text=part.text),), input_events=(part,),
        )
    repair = payload(invoke.call_args_list[1].args[0])
    assert "literal path is absent" in repair["validationError"]
    assert repair["events"] == [part.as_dict()]
    assert result == valid
