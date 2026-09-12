from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from test_decisions import existing_decision_output, new_decision_output
from test_thread_note_pipeline import FakeSummarizer, note_data, write_chat

from tkn_codex_context.catalog import thread_key
from tkn_codex_context.config import AppConfig, ScopeConfig
from tkn_codex_context.frontmatter import parse_simple_frontmatter
from tkn_codex_context.pipeline import pipeline_status, run_pipeline
from tkn_codex_context.provenance import validate_provenance
from tkn_codex_context.storage import pipeline_storage
from tkn_codex_context.thread_notes import Candidate, PipelineError, Project
from tkn_codex_context.working_context import WORKING_CONTEXT_OUTPUT_SCHEMA, WorkingContextSource


def config_for(tmp_path: Path) -> AppConfig:
    return AppConfig(
        codex_home=tmp_path / "codex",
        raw_root=tmp_path / "raw",
        data_root=tmp_path / "data",
        state_root=tmp_path / "state",
        cache_root=tmp_path / "cache",
        idle_minutes=0,
    )


def app_state(
    config: AppConfig, *, projects: dict[str, Path], assignments: dict[str, str], projectless: list[str]
) -> None:
    config.codex_home.mkdir(parents=True, exist_ok=True)
    config.app_state_path.write_text(
        json.dumps(
            {
                "local-projects": {
                    key: {"id": key, "name": key, "rootPaths": [str(path)], "createdAt": "2026-01-01T00:00:00Z"}
                    for key, path in projects.items()
                },
                "thread-project-assignments": {
                    thread: {"projectKind": "local", "projectId": project} for thread, project in assignments.items()
                },
                "projectless-thread-ids": projectless,
            }
        ),
        encoding="utf-8",
    )


class Summary(FakeSummarizer):
    def generate(self, candidate: Candidate) -> dict[str, Any]:
        self.calls.append(candidate.thread_id)
        if candidate.thread_id == self.fail_thread:
            raise RuntimeError("simulated failure")
        value = note_data(candidate)
        value["timeline"].append(
            {
                "label": "Explicit Decision",
                "text": "Keep source evidence.",
                "startEventId": candidate.events[0].id,
                "endEventId": candidate.events[0].id,
                "eventIds": [candidate.events[0].id],
            }
        )
        return value


class Decisions:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.last_metrics: dict[str, int] = {}

    def generate(self, sources: Sequence[Any], existing: Sequence[Any]) -> dict[str, Any]:
        refs = [source.source_ref for source in sources]
        self.calls.append(refs)
        return existing_decision_output(existing[0].decision_id, refs) if existing else new_decision_output(refs)


class Context:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last_metrics: dict[str, int] = {}

    def generate(self, project: Project, batches: Sequence[Sequence[WorkingContextSource]]) -> dict[str, Any]:
        self.calls.append(project.project_id)
        result = {
            key: [] if spec["type"] == "array" else False if spec["type"] == "boolean" else ""
            for key, spec in WORKING_CONTEXT_OUTPUT_SCHEMA["properties"].items()
        }
        ref = batches[0][0].source_ref
        result.update(
            title="Context",
            description="Current source evidence.",
            projectStatus="unknown",
            projectOverview=[{"text": "Source-backed work.", "sourceRefs": [ref]}],
            currentTruth=[{"text": "Evidence is available.", "sourceRefs": [ref]}],
        )
        return result


def execute(config: AppConfig, mode: str = "clone", **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("summarizer", Summary())
    kwargs.setdefault("decision_generator", Decisions())
    kwargs.setdefault("context_generator", Context())
    return run_pipeline(config, mode=mode, **kwargs)


def test_clone_projectless_archived_and_cross_project_scope(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    roots = {"a": tmp_path / "repo-a", "b": tmp_path / "repo-b"}
    app_state(config, projects=roots, assignments={"one": "a", "two": "b"}, projectless=["three"])
    config.scopes["shared"] = ScopeConfig(title="Shared work", project_ids=["a", "b"], thread_ids=["three"])
    for thread, folder in [("one", "sessions"), ("two", "sessions"), ("three", "archived_sessions")]:
        write_chat(config.codex_home / folder / f"{thread}.jsonl", thread_id=thread, cwd=tmp_path / "unknown")
    report = execute(config)
    assert report["complete"], report
    assert report["threadCounts"] == {"current": 3}
    assert report["scopeCounts"] == {"current": 4}
    assert len(list((config.data_root / "thread-notes").rglob("*.md"))) == 3
    shared = next(scope for scope in report["scopeResults"] if scope["id"] == "work:shared")
    assert len(shared["threadKeys"]) == 3
    index = json.loads((config.data_root / "provenance" / "index.json").read_text())
    assert len({item["id"] for item in index["artifacts"]}) == len(index["artifacts"])
    for activity_path in (config.data_root / "provenance" / "activities").glob("*.json"):
        activity = json.loads(activity_path.read_text())
        for entity in activity["used"] + activity["generated"]:
            snapshot = config.data_root / entity["snapshotRef"].removeprefix("data:/")
            assert sha256(snapshot.read_bytes()).hexdigest() == entity["sha256"]


def test_repeat_clone_is_noop_and_late_old_history_is_ingested(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    assert first["complete"], first
    ids = {item["threadId"]: item["noteId"] for item in first["threads"]}
    summary, decisions, context = Summary(), Decisions(), Context()
    second = execute(config, summarizer=summary, decision_generator=decisions, context_generator=context)
    assert second["complete"], second
    assert not summary.calls and not decisions.calls and not context.calls
    write_chat(config.sessions_root / "old.jsonl", thread_id="old", cwd=tmp_path, started_at="2020-01-01T00:00:00Z")
    third = execute(config, "pull", summarizer=summary)
    assert third["complete"], third
    assert summary.calls == ["old"]
    assert next(item for item in third["threads"] if item["threadId"] == "one")["noteId"] == ids["one"]


def test_dry_run_has_no_writes_or_inference(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    summary, decisions, context = Summary(), Decisions(), Context()
    report = execute(config, dry_run=True, summarizer=summary, decision_generator=decisions, context_generator=context)
    assert not report["complete"] and report["reportPath"] is None
    assert not summary.calls and not decisions.calls and not context.calls
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert not config.raw_root.exists() and not config.state_root.exists() and not config.data_root.exists()


def test_retry_failed_thread_keeps_success_and_raw(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for identity in ["one", "two"]:
        write_chat(config.sessions_root / f"{identity}.jsonl", thread_id=identity, cwd=tmp_path)
    first = execute(config, summarizer=Summary(fail_thread="two"))
    assert not first["complete"]
    assert first["rawIngest"]["availableCaptureCount"] == 2
    assert first["threadCounts"] == {"current": 1, "failed": 1}
    assert first["scopeResults"][0]["status"] == "blocked"
    summary = Summary()
    second = execute(config, "pull", summarizer=summary)
    assert second["complete"], second
    assert summary.calls == ["two"]


def test_limit_and_active_conversation_do_not_claim_completion(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    for identity in ["one", "two"]:
        write_chat(config.sessions_root / f"{identity}.jsonl", thread_id=identity, cwd=tmp_path)
    first = execute(config, limit=1)
    assert not first["complete"] and first["threadCounts"]["deferred"] == 1
    assert execute(config, "pull")["complete"]
    config.idle_minutes = 30
    write_chat(
        config.sessions_root / "active.jsonl",
        thread_id="active",
        cwd=tmp_path,
        started_at=datetime.now(UTC).isoformat(),
    )
    active = execute(config, "pull")
    assert not active["complete"]
    assert (
        next(entry for entry in active["threads"] if entry["threadId"] == "active")["reason"] == "active-conversation"
    )


def test_reassignment_keeps_identity_and_whole_thread(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    roots = {"a": tmp_path / "repo-a", "b": tmp_path / "repo-b"}
    app_state(config, projects=roots, assignments={"one": "a"}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path / "outside")
    first = execute(config)
    assert first["complete"], first
    note_id = first["threads"][0]["noteId"]
    app_state(config, projects=roots, assignments={"one": "b"}, projectless=[])
    summary = Summary()
    second = execute(config, "pull", summarizer=summary)
    assert second["threads"][0]["noteId"] == note_id
    assert not summary.calls
    assert second["threads"][0]["membership"]["sourceProjectId"] == "b"


def test_conflicting_logs_are_retained_and_reported(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "a.jsonl", thread_id="same", cwd=tmp_path, request="first")
    write_chat(config.sessions_root / "b.jsonl", thread_id="same", cwd=tmp_path, request="second")
    summary = Summary()
    report = execute(config, summarizer=summary)
    assert report["threads"][0]["reason"] == "conflicting-thread-versions"
    assert report["rawIngest"]["availableCaptureCount"] == 2
    assert not summary.calls and not report["complete"]


def test_archiving_identical_log_does_not_regenerate_and_raw_survives_removal(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    original = config.sessions_root / "one.jsonl"
    write_chat(original, thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    archived = config.codex_home / "archived_sessions" / "one.jsonl"
    archived.parent.mkdir()
    archived.write_bytes(original.read_bytes())
    original.unlink()
    summary = Summary()
    report = execute(config, "pull", summarizer=summary)
    assert report["complete"], report
    assert not summary.calls
    archived.unlink()
    assert execute(config, "pull")["complete"]


def test_edited_and_reviewed_notes_are_protected_even_with_force(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    note = config.data_root / first["threads"][0]["noteRef"].removeprefix("data:/")
    content = note.read_text(encoding="utf-8").replace("reviewStatus: unreviewed", "reviewStatus: reviewed")
    # The frontmatter renderer may quote scalars.
    content = content.replace('reviewStatus: "unreviewed"', 'reviewStatus: "reviewed"')
    note.write_text(content, encoding="utf-8")
    assert parse_simple_frontmatter(content)["reviewStatus"] == "reviewed"
    report = execute(config, "pull", force=True, allow_edited=True)
    assert report["threads"][0]["reason"] == "reviewed-thread-note"
    assert note.read_text(encoding="utf-8") == content


def test_missing_app_state_and_ambiguous_membership_do_not_exclude_notes(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    app_state(config, projects={"a": tmp_path, "b": tmp_path}, assignments={}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    assert first["complete"], first
    assert first["threads"][0]["membership"]["status"] == "ambiguous"
    config.app_state_path.unlink()
    second = execute(config, "pull")
    assert second["threads"][0]["status"] == "current"


def test_os_lock_is_released_and_pull_requires_clone(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    with pytest.raises(PipelineError, match="clone"):
        execute(config, "pull")
    with pipeline_storage(config, initialize=True, dry_run=False):
        with pytest.raises(PipelineError, match="lock"):
            with pipeline_storage(config, initialize=False, dry_run=False):
                pass
    with pipeline_storage(config, initialize=False, dry_run=False):
        pass
    assert pipeline_status(config)["initialized"]
    assert thread_key("one") != thread_key("two")


def append_turn(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-04T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Retain earlier evidence too."},
                }
            )
            + "\n"
        )


def test_append_and_missing_note_preserve_id_and_evidence(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    original = config.sessions_root / "one.jsonl"
    write_chat(original, thread_id="one", cwd=tmp_path)
    first = execute(config)
    identity = first["threads"][0]["noteId"]
    capture = config.raw_root / first["threads"][0]["sourceCaptureRef"].removeprefix("raw:/")
    original_bytes = capture.read_bytes()
    append_turn(original)
    second = execute(config, "pull")
    assert second["complete"], second
    assert second["threads"][0]["noteId"] == identity
    assert second["threads"][0]["sourceCaptureSha256"] != first["threads"][0]["sourceCaptureSha256"]
    assert capture.read_bytes() == original.read_bytes()
    assert capture.read_bytes() != original_bytes
    note = config.data_root / second["threads"][0]["noteRef"].removeprefix("data:/")
    note.unlink()
    third = execute(config, "pull")
    assert third["complete"], third
    assert third["threads"][0]["noteId"] == identity
    assert validate_provenance(config.data_root)["ok"]


def test_context_failure_resumes_only_context(tmp_path: Path) -> None:
    class BrokenContext(Context):
        def generate(self, project: Project, batches: Sequence[Sequence[WorkingContextSource]]) -> dict[str, Any]:
            raise RuntimeError("context service failed")

    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config, context_generator=BrokenContext())
    assert not first["complete"] and first["scopeCounts"] == {"failed": 1}
    summary, decisions, context = Summary(), Decisions(), Context()
    second = execute(config, "pull", summarizer=summary, decision_generator=decisions, context_generator=context)
    assert second["complete"], second
    assert not summary.calls and not decisions.calls and context.calls == ["unassigned"]


def test_partial_decision_batch_retry_does_not_repeat_completed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tkn_codex_context.decisions as decision_module

    monkeypatch.setattr(decision_module, "MAX_DECISION_BATCH_SOURCES", 1)

    class FailSecond(Decisions):
        def generate(self, sources: Sequence[Any], existing: Sequence[Any]) -> dict[str, Any]:
            if self.calls:
                raise RuntimeError("second batch failed")
            return super().generate(sources, existing)

    config = config_for(tmp_path)
    for identity in ["one", "two"]:
        write_chat(config.sessions_root / f"{identity}.jsonl", thread_id=identity, cwd=tmp_path)
    first = execute(config, decision_generator=FailSecond())
    assert first["scopeCounts"] == {"failed": 1}
    blocked = execute(config, "working-context")
    assert blocked["scopeResults"][0]["reason"] == "decisions-need-build"
    summary, decisions = Summary(), Decisions()
    second = execute(config, "pull", summarizer=summary, decision_generator=decisions)
    assert second["complete"], second
    assert not summary.calls and len(decisions.calls) == 1
    assert len(list((config.data_root / "scopes").rglob("DR-*.md"))) == 1


def test_no_decision_is_a_successful_stage(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    decisions, context = Decisions(), Context()
    report = execute(config, summarizer=FakeSummarizer(), decision_generator=decisions, context_generator=context)
    assert report["complete"], report
    assert not decisions.calls and context.calls == ["unassigned"]
    assert not list((config.data_root / "scopes").rglob("DR-*.md"))


def test_raw_capture_does_not_require_scope_or_parser_success(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    path = config.sessions_root / "unknown.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("unrecognized format\n", encoding="utf-8")
    config.scopes["future"] = ScopeConfig(title="Future", thread_ids=["absent"])
    summary = Summary()
    captured = execute(config, "raw", summarizer=summary)
    assert captured["ok"] and not captured["complete"]
    assert not summary.calls and not (config.data_root / "source-aligned").exists()
    assert captured["rawIngest"]["availableCaptureCount"] == 1
    config.scopes.clear()
    normalized = execute(config, "pull", summarizer=summary)
    assert not normalized["ok"] and not summary.calls


def test_scope_validation_precedes_inference(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    config.scopes["missing"] = ScopeConfig(title="Missing", thread_ids=["unknown"])
    summary = Summary()
    with pytest.raises(PipelineError, match="unknown threads"):
        execute(config, summarizer=summary)
    assert not summary.calls
    assert not pipeline_status(config)["complete"]


def test_edited_note_requires_explicit_override(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    note = config.data_root / first["threads"][0]["noteRef"].removeprefix("data:/")
    note.write_bytes(note.read_bytes() + b"\nA manual correction.\n")
    edited = note.read_bytes()
    blocked = execute(config, "pull", force=True)
    assert blocked["threads"][0]["reason"] == "edited-thread-note"
    assert note.read_bytes() == edited
    replaced = execute(config, "pull", allow_edited=True)
    assert replaced["complete"], replaced
    assert replaced["threads"][0]["noteId"] == first["threads"][0]["noteId"]


def test_leaf_decision_build_does_not_advertise_context_as_current(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    result = execute(config, "decisions", force=True)
    assert result["ok"] and not result["complete"]
    index = json.loads((config.data_root / "provenance" / "index.json").read_text())
    assert next(entity for entity in index["artifacts"] if entity["kind"] == "workingContext")["status"] == "pending"
    assert execute(config, "pull")["complete"]


@pytest.mark.parametrize("damage", ["snapshot", "relation", "artifact", "structure"])
def test_provenance_validation_detects_damage(tmp_path: Path, damage: str) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    assert validate_provenance(config.data_root)["ok"]
    index_path = config.data_root / "provenance" / "index.json"
    index = json.loads(index_path.read_text())
    if damage in {"snapshot", "artifact"}:
        ref = index["artifacts"][0]["snapshotRef" if damage == "snapshot" else "ref"]
        (config.data_root / ref.removeprefix("data:/")).write_bytes(b"damaged")
    else:
        path = next((config.data_root / "provenance" / "activities").glob("*.json"))
        activity = json.loads(path.read_text())
        if damage == "relation":
            activity["relations"][0]["to"]["id"] = "missing-entity"
        else:
            activity["used"] = None
        path.write_text(json.dumps(activity), encoding="utf-8")
    with pytest.raises(PipelineError):
        validate_provenance(config.data_root)


def test_repository_bytes_and_bounded_input_are_separate_evidence(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    content = b"# Repository\r\n" + b"ordinary text\r\n" * 3000
    (root / "README.md").write_bytes(content)
    app_state(config, projects={"a": root}, assignments={"one": "a"}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=root)
    assert execute(config)["complete"]
    activities = [
        json.loads(path.read_text()) for path in (config.data_root / "provenance" / "activities").glob("*.json")
    ]
    activity = next(
        item for item in activities if item["stage"] == "prepare-input" and item["used"][0]["kind"] == "repositoryFile"
    )
    assert activity["used"][0]["sha256"] == sha256(content).hexdigest()
    bounded = config.data_root / activity["generated"][0]["snapshotRef"].removeprefix("data:/")
    assert "TRUNCATED" in json.loads(bounded.read_text())["content"]
    assert validate_provenance(config.data_root)["ok"]


def test_working_context_rejects_undeclared_data_reference(tmp_path: Path) -> None:
    from tkn_codex_context.working_context import validate_working_context

    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    assert execute(config)["complete"]
    context = next((config.data_root / "scopes").rglob("working-context.md"))
    with context.open("a", encoding="utf-8") as handle:
        handle.write("\n- Unsupported evidence `data:/missing.md`.\n")
    with pytest.raises(PipelineError, match="undeclared sources"):
        validate_working_context(context)


def test_corrupted_raw_is_repaired_from_available_original(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(config)
    capture = config.raw_root / first["threads"][0]["sourceCaptureRef"].removeprefix("raw:/")
    capture.write_bytes(b"corrupted")
    second = execute(config, "pull")
    assert second["complete"] and second["ok"]
    assert capture.read_bytes() == (config.sessions_root / "one.jsonl").read_bytes()


def test_scope_membership_removal_reconciles_decision_inputs(tmp_path: Path) -> None:
    from tkn_codex_context.frontmatter import frontmatter_list_value, split_frontmatter_lines

    config = config_for(tmp_path)
    config.scopes["shared"] = ScopeConfig(title="Shared", thread_ids=["one", "two"])
    for identity in ["one", "two"]:
        write_chat(config.sessions_root / f"{identity}.jsonl", thread_id=identity, cwd=tmp_path)
    first = execute(config)
    assert first["complete"], first
    scope = next(item for item in first["scopeResults"] if item["id"] == "work:shared")
    decision = next((config.data_root / scope["dataRef"].removeprefix("data:/") / "decisions").glob("DR-*.md"))
    old_id = parse_simple_frontmatter(decision.read_text(encoding="utf-8"))["id"]
    config.scopes["shared"].thread_ids = ["one"]
    summary, decisions = Summary(), Decisions()
    second = execute(config, "pull", summarizer=summary, decision_generator=decisions)
    assert second["complete"], second
    assert not summary.calls and len(decisions.calls) == 1
    current_scope = next(item for item in second["scopeResults"] if item["id"] == "work:shared")
    assert not current_scope["staleDecisionRefs"]
    lines, _body = split_frontmatter_lines(decision.read_text(encoding="utf-8"))
    assert frontmatter_list_value(lines, "sourceThreadNoteRefs") == [
        next(item for item in second["threads"] if item["threadId"] == "one")["noteRef"]
    ]
    assert parse_simple_frontmatter(decision.read_text(encoding="utf-8"))["id"] == old_id


def test_changing_optional_app_metadata_does_not_stop_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tkn_codex_context.catalog as catalog
    from tkn_codex_context.raw_capture import RawCaptureError

    config = config_for(tmp_path)
    app_state(config, projects={}, assignments={}, projectless=[])
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)

    def changing_metadata(path: Path) -> bytes:
        raise RawCaptureError("source changed during raw capture")

    monkeypatch.setattr(catalog, "_stable_source_bytes", changing_metadata)
    report = execute(config)
    assert report["complete"], report
    assert report["warnings"]
    assert report["threads"][0]["membership"]["status"] == "state-unavailable"


def test_non_utf8_source_is_captured_before_normalization_fails(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    path = config.sessions_root / "unreadable.jsonl"
    path.parent.mkdir(parents=True)
    content = b"not utf8: \xff\xfe\x80\n"
    path.write_bytes(content)
    captured = execute(config, "raw")
    assert captured["ok"] and captured["rawIngest"]["availableCaptureCount"] == 1
    assert next(config.raw_root.rglob("*.jsonl")).exists()
    captures = list((config.raw_root / config.source_id / "sessions").rglob("*.jsonl"))
    assert len(captures) == 1 and captures[0].read_bytes() == content
    path.unlink()
    report = execute(config, "pull")
    assert not report["complete"] and report["failed"]
    assert report["rawIngest"]["availableCaptureCount"] == 1


@pytest.mark.parametrize("started_at", ["2025-12-31T23:30:00Z", "2026-01-31T23:30:00Z"])
def test_notes_use_start_month_in_system_timezone(tmp_path: Path, started_at: str) -> None:
    config = config_for(tmp_path)
    for thread_id in ("one", "two"):
        write_chat(config.sessions_root / f"{thread_id}.jsonl", thread_id=thread_id,
                   cwd=tmp_path, started_at=started_at)
    report = execute(config)
    assert report["complete"], report
    local = datetime.fromisoformat(started_at).astimezone()
    notes = list((config.data_root / "thread-notes" / local.strftime("%Y/%m")).glob("*.md"))
    assert len(notes) == 2
    assert all(note.name.startswith(local.strftime("%Y%m%dT%H%M%S%z")) for note in notes)
    assert len({parse_simple_frontmatter(note.read_text(encoding="utf-8"))["id"] for note in notes}) == 2
    summary = Summary()
    again = execute(config, "thread-notes", summarizer=summary)
    assert not summary.calls
    assert {entry["noteRef"] for entry in again["threads"]} == {entry["noteRef"] for entry in report["threads"]}
