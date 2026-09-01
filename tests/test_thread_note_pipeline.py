from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tkn_codex_context.chat_logs import ChatEvent, read_thread_events
from tkn_codex_context.thread_notes import (
    Candidate,
    CodexSummarizer,
    PipelineConfig,
    Project,
    chunk_events,
    event_matches_project,
    execute_pipeline,
    execute_rebuild,
    load_config,
    make_config,
    prepare_events,
    render_note,
    scan_candidates,
    validate_note_data,
    write_config,
)


def json_line(event_type: str, payload: dict, timestamp: str) -> str:
    return json.dumps({"timestamp": timestamp, "type": event_type, "payload": payload})


def write_chat(path: Path, *, thread_id: str, cwd: Path, request: str = "do work") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json_line(
            "session_meta",
            {
                "id": thread_id,
                "timestamp": "2026-07-01T00:00:00Z",
                "cwd": str(cwd),
                "thread_source": "user",
            },
            "2026-07-01T00:00:00Z",
        ),
        json_line(
            "turn_context",
            {"turn_id": "turn-1", "cwd": str(cwd)},
            "2026-07-01T00:00:01Z",
        ),
        json_line(
            "response_item",
            {"type": "message", "role": "user", "content": [{"text": request}]},
            "2026-07-01T00:00:02Z",
        ),
        json_line(
            "event_msg",
            {"type": "user_message", "message": request},
            "2026-07-01T00:00:02Z",
        ),
        json_line(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "input": {"cmd": "test"}},
            "2026-07-01T00:00:03Z",
        ),
        json_line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "password=0123456789abcdef and tests passed",
            },
            "2026-07-01T00:00:04Z",
        ),
        json_line(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"text": "completed"}],
            },
            "2026-07-01T00:00:05Z",
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
    os.utime(path, (old, old))


def note_data(candidate: Candidate, *, title: str = "Automated Thread Note") -> dict:
    ids = [event.id for event in candidate.events]
    return {
        "title": title,
        "fileSlug": "automated-thread-note",
        "description": "A factual Thread Note.",
        "summaryItems": [
            {
                "text": "The requested work was completed.",
                "eventIds": ids[:1],
            }
        ],
        "workItems": [
            {
                "title": "Requested work",
                "developments": [
                    {"label": "Request", "text": "The user requested work.", "eventIds": ids[:1]},
                    {
                        "label": "Reported Result",
                        "text": "The work completed.",
                        "eventIds": ids[-1:],
                    },
                ],
            }
        ],
        "evidence": [],
        "lastKnownState": {
            "workState": "done",
            "detail": "The assistant reported completion.",
            "latestUserDirection": "Complete the work.",
            "unresolved": [],
            "unverified": [],
            "continuationPoint": "",
            "eventIds": ids[-1:],
        },
        "sourceLimitations": [],
    }


class FakeSummarizer:
    def __init__(self, *, fail_thread: str = "") -> None:
        self.fail_thread = fail_thread
        self.calls: list[str] = []

    def generate(self, candidate: Candidate) -> dict:
        self.calls.append(candidate.thread_id)
        if candidate.thread_id == self.fail_thread:
            raise RuntimeError("simulated failure")
        return note_data(candidate)


class MutatingSummarizer:
    def generate(self, candidate: Candidate) -> dict:
        with candidate.source_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json_line(
                    "event_msg",
                    {"type": "agent_message", "message": "changed during generation"},
                    "2026-07-01T00:00:06Z",
                )
                + "\n"
            )
        return note_data(candidate)


class ThreadNotePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.context = self.root / "context"
        self.sessions = self.root / "codex-sessions"
        self.sessions.mkdir()
        self.cache = self.root / "cache"
        self.project = Project(
            "project-1",
            "Project 1",
            self.repo,
            self.context,
            state_directory=self.root / "state" / "projects" / "project-1",
        )
        assert self.project.state_directory is not None
        self.project.state_directory.mkdir(parents=True)
        self.config = PipelineConfig(
            installed_at="2026-01-01T00:00:00+00:00",
            sessions_root=self.sessions,
            source_id="windows",
            codex_bin=str(Path(sys.executable)),
            idle_minutes=30,
            runtime_minutes=230,
            model_timeout_seconds=30,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_parser_deduplicates_messages_and_keeps_tool_evidence(self) -> None:
        path = self.sessions / "2026" / "07" / "01" / "chat.jsonl"
        write_chat(path, thread_id="thread-1", cwd=self.repo)
        events = read_thread_events(path)

        self.assertEqual(1, sum(event.kind == "user_message" for event in events))
        self.assertEqual(1, sum(event.kind == "assistant_message" for event in events))
        self.assertEqual(1, sum(event.kind == "tool_call" for event in events))
        self.assertEqual(1, sum(event.kind == "tool_result" for event in events))
        prepared = prepare_events(events)
        tool_result = next(event for event in prepared if event.kind == "tool_result")
        self.assertIn("[REDACTED]", tool_result.text)
        self.assertNotIn("0123456789abcdef", tool_result.text)

    def test_chunking_preserves_event_boundaries(self) -> None:
        path = self.sessions / "chat.jsonl"
        write_chat(path, thread_id="thread-1", cwd=self.repo)
        prepared = prepare_events(read_thread_events(path))
        chunks = chunk_events(prepared, target_characters=150)

        self.assertGreater(len(chunks), 1)
        self.assertEqual([item.id for item in prepared], [item.id for chunk in chunks for item in chunk])

    def test_config_preserves_watermark_and_fixed_model(self) -> None:
        config_path = self.root / "config.json"
        write_config(config_path, self.config)
        loaded = load_config(config_path)
        updated = make_config(existing=loaded, codex_bin=sys.executable)

        self.assertEqual(self.config.installed_at, updated.installed_at)
        self.assertEqual("gpt-5.6-sol", updated.model)
        self.assertEqual("high", updated.reasoning_effort)

    def test_codex_path_preserves_stable_launcher_path(self) -> None:
        launcher = self.root / "codex.exe"
        launcher.write_bytes(Path(sys.executable).read_bytes())
        configured = make_config(existing=self.config, codex_bin=str(launcher))

        self.assertEqual(str(launcher.absolute()), configured.codex_bin)

    def test_daily_scan_skips_preinstallation_history_but_backfill_finds_it(self) -> None:
        path = self.sessions / "2025" / "12" / "31" / "old.jsonl"
        write_chat(path, thread_id="thread-old", cwd=self.repo)
        old = datetime(2025, 12, 31).astimezone().timestamp()
        os.utime(path, (old, old))

        daily, _counts = scan_candidates(self.config, [self.project])
        backfill, _counts = scan_candidates(self.config, [self.project], backfill=True)

        self.assertEqual([], daily)
        self.assertEqual(["thread-old"], [item.thread_id for item in backfill])

    def test_backfill_excludes_post_watermark_chat(self) -> None:
        write_chat(self.sessions / "new.jsonl", thread_id="thread-new", cwd=self.repo)

        candidates, _counts = scan_candidates(
            self.config,
            [self.project],
            backfill=True,
        )

        self.assertEqual([], candidates)

    def test_secondary_root_is_active_and_explicit_assignment_precedes_cwd(self) -> None:
        secondary = self.root / "secondary"
        secondary.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        project = Project(
            "project-1",
            "Project 1",
            self.repo,
            self.context,
            active_roots=(self.repo, secondary),
            assigned_thread_ids=frozenset({"thread-assigned"}),
        )
        write_chat(
            self.sessions / "secondary.jsonl",
            thread_id="thread-secondary",
            cwd=secondary,
        )
        write_chat(
            self.sessions / "assigned.jsonl",
            thread_id="thread-assigned",
            cwd=outside,
        )

        candidates, counts = scan_candidates(self.config, [project])

        self.assertEqual(
            {"thread-secondary", "thread-assigned"},
            {item.thread_id for item in candidates},
        )
        self.assertEqual(1, counts["assigned"])

    def test_cwd_fallback_compares_resolved_symlink_path(self) -> None:
        target = self.root / "target"
        target.mkdir()
        alias = self.root / "alias"
        event = ChatEvent(
            id="event-1",
            kind="user_message",
            actor="user",
            name="",
            text="work",
            timestamp="2026-01-01T00:00:00+00:00",
            turn_id="turn-1",
            cwd=str(alias / "nested"),
        )
        project = Project("project-1", "Project 1", target, self.context)
        original_resolve = Path.resolve

        def resolve_alias(path: Path, strict: bool = False) -> Path:
            try:
                relative = path.relative_to(alias)
            except ValueError:
                return original_resolve(path, strict=strict)
            return target / relative

        with patch.object(Path, "resolve", new=resolve_alias):
            self.assertTrue(event_matches_project(event, project))

    def test_projectless_and_ambiguous_threads_are_excluded(self) -> None:
        nested = self.repo / "nested"
        nested.mkdir()
        first = Project(
            "first",
            "First",
            self.repo,
            self.context / "first",
            projectless_thread_ids=frozenset({"thread-projectless"}),
        )
        second = Project(
            "second",
            "Second",
            nested,
            self.context / "second",
            projectless_thread_ids=frozenset({"thread-projectless"}),
        )
        write_chat(
            self.sessions / "projectless.jsonl",
            thread_id="thread-projectless",
            cwd=self.repo,
        )
        write_chat(
            self.sessions / "ambiguous.jsonl",
            thread_id="thread-ambiguous",
            cwd=nested,
        )

        candidates, counts = scan_candidates(self.config, [first, second])

        self.assertEqual([], candidates)
        self.assertEqual(1, counts["projectless"])
        self.assertEqual(1, counts["ambiguous"])

    def test_daily_scan_requires_idle_source(self) -> None:
        path = self.sessions / "chat.jsonl"
        write_chat(path, thread_id="thread-1", cwd=self.repo)
        current = datetime.now().astimezone().timestamp()
        os.utime(path, (current, current))

        active, _counts = scan_candidates(self.config, [self.project])
        old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
        os.utime(path, (old, old))
        idle, _counts = scan_candidates(self.config, [self.project])

        self.assertEqual([], active)
        self.assertEqual(["thread-1"], [item.thread_id for item in idle])

    def test_scan_has_no_daily_item_cap(self) -> None:
        for index in range(25):
            write_chat(
                self.sessions / "2026" / "07" / "01" / f"chat-{index:02d}.jsonl",
                thread_id=f"thread-{index:02d}",
                cwd=self.repo,
            )

        candidates, _counts = scan_candidates(self.config, [self.project])

        self.assertEqual(25, len(candidates))

    def test_write_run_creates_live_note_then_state_and_updates_exact_match(self) -> None:
        path = self.sessions / "2026" / "07" / "01" / "chat.jsonl"
        write_chat(path, thread_id="thread-1", cwd=self.repo)
        first, _report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual(1, len(first["processed"]))
        notes = list(self.project.thread_notes_path.glob("*.md"))
        self.assertEqual(1, len(notes))
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn('reviewStatus: "unreviewed"', text)
        self.assertIn('sourceThreadIds:\n  - "thread-1"', text)
        self.assertIn("# Thread Note", text)
        self.assertIn("### Request", text)
        self.assertIn("### Reported Result", text)
        state = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        self.assertIn("thread-1", state["sources"]["windows"]["threads"])

        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json_line(
                    "event_msg",
                    {"type": "agent_message", "message": "a later result"},
                    "2026-07-01T00:00:06Z",
                )
                + "\n"
            )
        old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
        os.utime(path, (old, old))
        second, _report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual(1, len(second["processed"]))
        self.assertEqual(1, len(list(self.project.thread_notes_path.glob("*.md"))))
        self.assertIn('reviewStatus: "unreviewed"', notes[0].read_text(encoding="utf-8"))

    def test_stale_generator_fingerprint_is_regenerated(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        state = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        state["sources"]["windows"]["threads"]["thread-1"]["generationFingerprint"] = "old"
        self.project.state_path.write_text(json.dumps(state), encoding="utf-8")

        candidates, counts = scan_candidates(self.config, [self.project])

        self.assertEqual(["thread-1"], [item.thread_id for item in candidates])
        self.assertEqual(1, counts["staleGenerator"])
        self.assertEqual(0, counts["unchanged"])

    def test_unchanged_source_and_generator_do_not_call_model(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        second = FakeSummarizer()

        report, _path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=second,
            cache_root=self.cache,
        )

        self.assertEqual([], second.calls)
        self.assertEqual(1, report["scan"]["unchanged"])
        self.assertEqual(0, report["selectedCount"])

    def test_old_note_schema_is_regenerated_even_when_state_matches(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        note = next(self.project.thread_notes_path.glob("*.md"))
        note.write_text(
            note.read_text(encoding="utf-8").replace("schemaVersion: 3", "schemaVersion: 2"),
            encoding="utf-8",
        )

        candidates, counts = scan_candidates(self.config, [self.project])

        self.assertEqual(["thread-1"], [item.thread_id for item in candidates])
        self.assertEqual(1, counts["staleGenerator"])
        self.assertEqual(0, counts["unchanged"])

    def test_force_regenerates_unchanged_source(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        forced = FakeSummarizer()

        report, _path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=forced,
            force=True,
            cache_root=self.cache,
        )

        self.assertEqual(["thread-1"], forced.calls)
        self.assertTrue(report["force"])
        self.assertEqual(1, report["selectedCount"])
        self.assertEqual(1, len(report["processed"]))

    def test_state_failure_rolls_back_existing_note_and_state(self) -> None:
        source = self.sessions / "chat.jsonl"
        write_chat(source, thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        note = next(self.project.thread_notes_path.glob("*.md"))
        old_note = note.read_bytes()
        old_state = self.project.state_path.read_bytes()
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json_line(
                    "event_msg",
                    {"type": "agent_message", "message": "later"},
                    "2026-07-01T00:00:06Z",
                )
                + "\n"
            )
        old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
        os.utime(source, (old, old))

        with patch(
            "tkn_codex_context.thread_notes.update_refresh_state",
            side_effect=OSError("simulated state failure"),
        ):
            report, _path = execute_pipeline(
                self.config,
                [self.project],
                summarizer=FakeSummarizer(),
                cache_root=self.cache,
            )

        self.assertEqual(1, len(report["failed"]))
        self.assertEqual(old_note, note.read_bytes())
        self.assertEqual(old_state, self.project.state_path.read_bytes())
        retry = FakeSummarizer()
        resumed, _path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=retry,
            cache_root=self.cache,
        )
        self.assertEqual([], resumed["failed"])
        self.assertEqual([], retry.calls)

    def test_failure_is_isolated_and_does_not_commit_failed_thread(self) -> None:
        for thread in ("thread-good", "thread-bad"):
            write_chat(self.sessions / f"{thread}.jsonl", thread_id=thread, cwd=self.repo)
        report, _report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(fail_thread="thread-bad"),
            cache_root=self.cache,
        )

        self.assertEqual(1, len(report["processed"]))
        self.assertEqual(1, len(report["failed"]))
        state = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        self.assertIn("thread-good", state["sources"]["windows"]["threads"])
        self.assertNotIn("thread-bad", state["sources"]["windows"]["threads"])

    def test_source_change_during_generation_is_not_written(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        report, _report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=MutatingSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual([], report["processed"])
        self.assertEqual(1, len(report["failed"]))
        self.assertFalse(self.project.thread_notes_path.exists())
        self.assertFalse(self.project.state_path.exists())

    def test_duplicate_exact_note_matches_fail_without_overwrite(self) -> None:
        source = self.sessions / "chat.jsonl"
        write_chat(source, thread_id="thread-1", cwd=self.repo)
        execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        note = next(self.project.thread_notes_path.glob("*.md"))
        duplicate = note.with_name("duplicate.md")
        shutil.copy2(note, duplicate)
        before = {path.name: path.read_bytes() for path in (note, duplicate)}
        with source.open("a", encoding="utf-8") as handle:
            handle.write(
                json_line(
                    "event_msg",
                    {"type": "agent_message", "message": "later"},
                    "2026-07-01T00:00:06Z",
                )
                + "\n"
            )
        old = (datetime.now().astimezone() - timedelta(hours=1)).timestamp()
        os.utime(source, (old, old))

        report, _report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual(1, len(report["failed"]))
        self.assertEqual(before, {path.name: path.read_bytes() for path in (note, duplicate)})

    def test_dry_run_does_not_create_live_state(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        report, report_path = execute_pipeline(
            self.config,
            [self.project],
            summarizer=None,
            dry_run=True,
            cache_root=self.cache,
        )

        self.assertEqual(1, report["selectedCount"])
        self.assertIsNone(report_path)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.project.thread_notes_path.exists())
        self.assertFalse(self.project.state_path.exists())

    def test_codex_runner_uses_ephemeral_fixed_model_and_schema(self) -> None:
        path = self.sessions / "chat.jsonl"
        write_chat(path, thread_id="thread-1", cwd=self.repo)
        candidate = scan_candidates(self.config, [self.project])[0][0]
        captured: list[str] = []
        prompts: list[str] = []

        def fake_run(command, **kwargs):
            captured.extend(command)
            prompts.append(kwargs["input"])
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(note_data(candidate)), encoding="utf-8")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        runner = CodexSummarizer(self.config, sleeper=lambda _seconds: None)
        with patch("tkn_codex_context.inference.subprocess.run", side_effect=fake_run):
            result = runner.generate(candidate)

        self.assertEqual("Automated Thread Note", result["title"])
        self.assertIn("--ephemeral", captured)
        self.assertIn("--ignore-user-config", captured)
        self.assertEqual("gpt-5.6-sol", captured[captured.index("--model") + 1])
        self.assertIn('model_reasoning_effort="high"', captured)
        self.assertIn("natural Japanese", prompts[0])
        self.assertIn("fileSlug", prompts[0])

    def test_rebuild_success_replaces_legacy_notes_and_is_idempotent(self) -> None:
        for thread in ("thread-1", "thread-2"):
            write_chat(self.sessions / f"{thread}.jsonl", thread_id=thread, cwd=self.repo)
        self.project.thread_notes_path.mkdir(parents=True)
        (self.project.thread_notes_path / "legacy.md").write_text(
            "---\ntype: threadNote\nschemaVersion: 2\ntitle: Legacy\n---\n\n# Legacy\n",
            encoding="utf-8",
        )
        progress_events: list[dict] = []

        report, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
            progress=progress_events.append,
        )

        self.assertEqual([], report["failed"])
        self.assertEqual(2, report["generationCount"])
        self.assertEqual(["legacy.md"], [item["file"] for item in report["deletedLegacy"]])
        notes = sorted(self.project.thread_notes_path.glob("*.md"))
        self.assertEqual(2, len(notes))
        self.assertTrue(all("schemaVersion: 3" in path.read_text(encoding="utf-8") for path in notes))
        completed = [event for event in progress_events if event["type"] == "thread-complete"]
        self.assertEqual(
            [str(path.absolute()) for path in notes],
            sorted(event["threadNotePath"] for event in completed),
        )

        second, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=None,
            dry_run=True,
            cache_root=self.cache,
        )
        self.assertEqual(0, second["generationCount"])
        self.assertEqual(2, len(second["preservedCurrent"]))
        self.assertEqual([], second["deletedLegacy"])

    def test_rebuild_failure_keeps_legacy_notes_and_state(self) -> None:
        for thread in ("thread-1", "thread-2"):
            write_chat(self.sessions / f"{thread}.jsonl", thread_id=thread, cwd=self.repo)
        self.project.thread_notes_path.mkdir(parents=True)
        legacy = self.project.thread_notes_path / "legacy.md"
        legacy.write_text(
            "---\ntype: threadNote\nschemaVersion: 2\n---\n\n# Legacy\n",
            encoding="utf-8",
        )
        before = legacy.read_bytes()

        report, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(fail_thread="thread-2"),
            cache_root=self.cache,
        )

        self.assertEqual(1, len(report["failed"]))
        self.assertEqual(before, legacy.read_bytes())
        self.assertEqual(["legacy.md"], [path.name for path in self.project.thread_notes_path.glob("*.md")])
        self.assertFalse(self.project.state_path.exists())

    def test_rebuild_force_regenerates_existing_current_note(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        first_summarizer = FakeSummarizer()
        execute_rebuild(
            self.config,
            self.project,
            summarizer=first_summarizer,
            cache_root=self.cache,
        )
        dry, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=None,
            dry_run=True,
            cache_root=self.cache,
        )
        forced, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=None,
            force=True,
            dry_run=True,
            cache_root=self.cache,
        )

        self.assertEqual(0, dry["generationCount"])
        self.assertEqual(1, forced["generationCount"])
        self.assertEqual(1, len(forced["replacedCurrent"]))
        self.assertEqual(64, len(forced["replacedCurrent"][0]["sha256"]))

    def test_rebuild_dry_run_does_not_save_approved_root(self) -> None:
        historical = self.root / "old-repo"
        historical.mkdir()
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=historical)
        with patch(
            "tkn_codex_context.thread_notes.verify_historical_root",
            return_value=historical,
        ):
            report, _path = execute_rebuild(
                self.config,
                self.project,
                summarizer=None,
                approve_roots=[historical],
                dry_run=True,
                cache_root=self.cache,
            )

        self.assertEqual(1, report["selectedCount"])
        self.assertFalse(self.project.state_path.exists())

    def test_rebuild_rejects_future_thread_note_schema(self) -> None:
        self.project.thread_notes_path.mkdir(parents=True)
        (self.project.thread_notes_path / "future.md").write_text(
            "---\ntype: threadNote\nschemaVersion: 99\n---\n\n# Thread Note\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Exception, "unsupported Thread Note schemaVersion 99"):
            execute_rebuild(
                self.config,
                self.project,
                summarizer=None,
                dry_run=True,
                cache_root=self.cache,
            )

    def test_rebuild_treats_every_older_schema_as_legacy(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )
        note = next(self.project.thread_notes_path.glob("*.md"))

        with patch("tkn_codex_context.thread_notes.THREAD_NOTE_SCHEMA_VERSION", 4):
            report, _path = execute_rebuild(
                self.config,
                self.project,
                summarizer=None,
                dry_run=True,
                cache_root=self.cache,
            )

        self.assertEqual([note.name], [item["file"] for item in report["deletedLegacy"]])
        self.assertEqual(1, report["generationCount"])

    def test_saved_historical_root_is_used_by_daily_scan(self) -> None:
        historical = self.root / "historical"
        historical.mkdir()
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-old-root", cwd=historical)
        self.context.mkdir()
        self.project.state_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "projectId": self.project.project_id,
                    "approvedHistoricalRoots": [str(historical)],
                    "rejectedHistoricalRoots": [],
                    "lastRefreshAt": None,
                    "sources": {},
                }
            ),
            encoding="utf-8",
        )

        candidates, _counts = scan_candidates(self.config, [self.project])

        self.assertEqual(["thread-old-root"], [item.thread_id for item in candidates])

    def test_rebuild_preserves_current_note_without_source_thread_ids(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        self.project.thread_notes_path.mkdir(parents=True)
        manual = self.project.thread_notes_path / "manual-v3.md"
        manual.write_text(
            "---\ntype: threadNote\nschemaVersion: 3\nstatus: done\n---\n\n"
            "# Thread Note\n\n## Summary\n\n- Manual.\n\n"
            "## Key Developments\n\n### Action\n\n- Manual.\n\n"
            "## Last Known State\n\n- Work State: done — manual.\n"
            "- Latest User Direction: 追加指示なし。\n",
            encoding="utf-8",
        )

        report, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual([], report["failed"])
        self.assertTrue((self.project.thread_notes_path / "manual-v3.md").is_file())
        self.assertEqual(2, len(list(self.project.thread_notes_path.glob("*.md"))))

    def test_multiple_work_items_render_h3_and_h4_labels(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        candidate = scan_candidates(self.config, [self.project])[0][0]
        data = note_data(candidate)
        data["workItems"].append(
            {
                "title": "Second task",
                "developments": [
                    {
                        "label": "Validation",
                        "text": "The second task was checked.",
                        "eventIds": [candidate.events[-1].id],
                    }
                ],
            }
        )

        text = render_note(candidate, data, {})

        self.assertIn("### WI-01: Requested work", text)
        self.assertIn("#### Request", text)
        self.assertIn("### WI-02: Second task", text)
        self.assertIn("#### Validation", text)
        self.assertNotIn("distillationStatus", text)
        self.assertNotIn("distilledTo", text)

    def test_rebuild_state_write_failure_restores_legacy_thread_notes(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        self.project.thread_notes_path.mkdir(parents=True)
        legacy = self.project.thread_notes_path / "legacy.md"
        legacy.write_text(
            "---\ntype: threadNote\nschemaVersion: 2\n---\n\n# Legacy\n",
            encoding="utf-8",
        )
        import tkn_codex_context.thread_notes as module

        original_write = module.atomic_write_json

        def fail_state(path, value):
            if path == self.project.state_path:
                raise OSError("simulated state failure")
            return original_write(path, value)

        with patch(
            "tkn_codex_context.thread_notes.atomic_write_json",
            side_effect=fail_state,
        ):
            report, _path = execute_rebuild(
                self.config,
                self.project,
                summarizer=FakeSummarizer(),
                cache_root=self.cache,
            )

        self.assertEqual(1, len(report["failed"]))
        self.assertTrue(legacy.is_file())
        self.assertEqual(["legacy.md"], [path.name for path in self.project.thread_notes_path.glob("*.md")])

    def test_rebuild_resumes_completed_generation_after_failure(self) -> None:
        for thread in ("thread-1", "thread-2"):
            write_chat(self.sessions / f"{thread}.jsonl", thread_id=thread, cwd=self.repo)
        first = FakeSummarizer(fail_thread="thread-2")

        failed, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=first,
            cache_root=self.cache,
        )

        self.assertEqual(1, len(failed["failed"]))
        self.assertEqual(1, failed["resumeAvailable"])
        self.assertTrue((self.cache / "rebuild" / self.project.project_id / "work").is_dir())

        second = FakeSummarizer()
        resumed, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=second,
            cache_root=self.cache,
        )

        self.assertEqual([], resumed["failed"])
        self.assertEqual(1, resumed["resumedCount"])
        self.assertEqual(["thread-2"], second.calls)
        self.assertFalse((self.cache / "rebuild" / self.project.project_id / "work").exists())
        self.assertEqual(2, len(list(self.project.thread_notes_path.glob("*.md"))))

    def test_rebuild_preserves_state_for_missing_source_thread(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-new", cwd=self.repo)
        self.context.mkdir()
        self.project.state_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "projectId": self.project.project_id,
                    "approvedHistoricalRoots": [],
                    "rejectedHistoricalRoots": [],
                    "lastRefreshAt": None,
                    "sources": {
                        "windows": {
                            "sourceRoot": str(self.sessions),
                            "lastRefreshAt": None,
                            "threads": {
                                "thread-missing": {
                                    "fingerprint": "old",
                                    "threadNotes": ["thread-notes/old.md"],
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        report, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual([], report["failed"])
        state = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        self.assertIn("thread-missing", state["sources"]["windows"]["threads"])
        self.assertIn("thread-new", state["sources"]["windows"]["threads"])

    def test_backup_cleanup_failure_is_warning_after_successful_commit(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        self.project.thread_notes_path.mkdir(parents=True)
        (self.project.thread_notes_path / "legacy.md").write_text(
            "---\ntype: threadNote\nschemaVersion: 2\n---\n\n# Legacy\n",
            encoding="utf-8",
        )
        original_rmtree = shutil.rmtree

        def fail_backup_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".thread-notes-rebuild-backup-"):
                raise OSError("simulated backup cleanup failure")
            return original_rmtree(path, *args, **kwargs)

        with patch(
            "tkn_codex_context.thread_notes.shutil.rmtree",
            side_effect=fail_backup_cleanup,
        ):
            report, _path = execute_rebuild(
                self.config,
                self.project,
                summarizer=FakeSummarizer(),
                cache_root=self.cache,
            )

        self.assertEqual([], report["failed"])
        self.assertEqual(1, len(report["warnings"]))
        self.assertEqual(1, len(list(self.project.thread_notes_path.glob("*.md"))))
        for backup in self.context.glob(".thread-notes-rebuild-backup-*"):
            original_rmtree(backup)

    def test_generated_note_records_generator_and_validation_metadata(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        report, _path = execute_rebuild(
            self.config,
            self.project,
            summarizer=FakeSummarizer(),
            cache_root=self.cache,
        )

        self.assertEqual([], report["failed"])
        note = next(self.project.thread_notes_path.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn('generatorModel: "gpt-5.6-sol"', note)
        self.assertIn('generatorReasoningEffort: "high"', note)
        self.assertIn('type: "threadNote"', note)
        self.assertIn('promptId: "f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11"', note)
        self.assertIn('promptVersion: "2.0"', note)
        self.assertIn(
            'outputSchemaSha256: "3ebffe117e29f76dfca25375a7e96ba0867de31a7ed68022dc6b65d91d651170"',
            note,
        )
        self.assertIn('templateId: "4d19c51c-0d02-43a5-b6ad-6d67f9739b75"', note)
        self.assertIn('templateVersion: "2.0"', note)
        self.assertIn("generatorPromptVersion: 4", note)
        self.assertIn("rendererVersion: 6", note)
        self.assertIn("generatedAt:", note)
        self.assertIn('fileSlug: "automated-thread-note"', note)
        self.assertIn('automatedValidation: "passed"', note)
        state = json.loads(self.project.state_path.read_text(encoding="utf-8"))
        thread = state["sources"]["windows"]["threads"]["thread-1"]
        self.assertTrue(thread["generationFingerprint"])
        self.assertTrue(thread["noteHash"])

    def test_done_note_rejects_unresolved_items(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        candidate = scan_candidates(self.config, [self.project])[0][0]
        data = note_data(candidate)
        data["lastKnownState"]["unresolved"] = ["unfinished request"]

        with self.assertRaisesRegex(Exception, "done work cannot contain unresolved"):
            validate_note_data(data, {event.id for event in candidate.events})

    def test_note_validation_rejects_avoidable_english_prose(self) -> None:
        write_chat(self.sessions / "chat.jsonl", thread_id="thread-1", cwd=self.repo)
        candidate = scan_candidates(self.config, [self.project])[0][0]
        data = note_data(candidate)
        data["summaryItems"][0]["text"] = "Merged from supplied events."

        with self.assertRaisesRegex(Exception, "avoidable English prose"):
            validate_note_data(data, {event.id for event in candidate.events})


if __name__ == "__main__":
    unittest.main()
