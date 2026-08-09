from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tkn_codex_context.thread_notes import PipelineConfig, Project
from tkn_codex_context.working_context import (
    WorkingContextSource,
    execute_working_context_build,
    validate_working_context,
    validate_working_context_output,
)
from tkn_codex_context.working_context_resources import load_working_context_profile


def pipeline_config(tmp_path: Path) -> PipelineConfig:
    return PipelineConfig(
        installed_at="2026-08-09T09:00:00+09:00",
        sessions_root=tmp_path / "sessions",
        source_id="windows",
        codex_bin="codex",
        model="test-model",
        reasoning_effort="high",
    )


def working_project(tmp_path: Path) -> Project:
    root = tmp_path / "repo"
    data = tmp_path / "data" / "project"
    state = tmp_path / "state" / "project"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Example\n\nA source-backed example project.\n", encoding="utf-8")
    (data / "thread-notes").mkdir(parents=True)
    (data / "decisions").mkdir(parents=True)
    state.mkdir(parents=True)
    project = Project(
        project_id="local-project",
        title="Example Project",
        current_root=root,
        context_path=data,
        state_directory=state,
    )
    write_thread_note(project.thread_notes_path / "source.md")
    write_decision(data / "decisions" / "DR-0001-source-backed-context.md")
    return project


def write_thread_note(path: Path) -> None:
    path.write_text(
        "---\n"
        "type: threadNote\n"
        "schemaVersion: 3\n"
        "title: Current work\n"
        "description: Current factual work.\n"
        "generator: Codex\n"
        "generatorModel: test-model\n"
        "generatorReasoningEffort: high\n"
        "promptId: f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11\n"
        "promptVersion: \"2.0\"\n"
        f"outputSchemaSha256: {'a' * 64}\n"
        "templateId: 4d19c51c-0d02-43a5-b6ad-6d67f9739b75\n"
        "templateVersion: \"2.0\"\n"
        "generatorPromptVersion: 4\n"
        "rendererVersion: 6\n"
        "generatedAt: 2026-08-09T09:00:00+09:00\n"
        "fileSlug: current-work\n"
        "status: in-progress\n"
        "reviewStatus: unreviewed\n"
        "automatedValidation: passed\n"
        "date: 2026-08-09T09:00:00+09:00\n"
        "updated: 2026-08-09T09:00:00+09:00\n"
        "threadNoteId: 20260809T090000+0900\n"
        "sourceType: codexChat\n"
        "sourceThreadIds:\n  - thread-1\n"
        "sourceRefs:\n  - windows/2026/08/09/thread-1.jsonl\n"
        f"sourceFingerprint: {'b' * 64}\n"
        "sourceProjectId: local-project\n"
        "---\n\n"
        "# Thread Note\n\n"
        "## Summary\n\n- Working Context implementation is active.\n\n"
        "## Key Developments\n\n### Request\n\n- Build a Working Context dashboard.\n\n"
        "## Last Known State\n\n"
        "- Work State: in-progress — implementation is active.\n"
        "- Latest User Direction: Implement the approved design.\n",
        encoding="utf-8",
    )


def write_decision(path: Path) -> None:
    path.write_text(
        "---\n"
        "type: decision\n"
        "schemaVersion: 4\n"
        "title: Keep Working Context concise\n"
        "description: Render only source-backed current context.\n"
        "generator: Codex\n"
        "status: Accepted\n"
        "scope: project\n"
        "implementationStatus: implemented\n"
        "promotionStatus: no-action\n"
        "promotedTo: []\n"
        "projectWorkingContextTargets: []\n"
        "repositoryDocumentationTargets: []\n"
        "globalContextTargets: []\n"
        "skillAutomationTargets: []\n"
        "reviewStatus: reviewed\n"
        "automatedValidation: passed\n"
        "sourceThreadNoteRefs:\n  - project:/thread-notes/source.md\n"
        f"sourceThreadNoteSetSha256: {'c' * 64}\n"
        "date: 2026-08-09T09:00:00+09:00\n"
        "updated: 2026-08-09T09:00:00+09:00\n"
        "decisionId: DR-0001\n"
        "---\n\n"
        "# DR-0001: Keep Working Context concise\n\n"
        "## Decision\n\n"
        "Working Context is a concise current-truth dashboard.\n",
        encoding="utf-8",
    )


def generated_output() -> dict[str, Any]:
    thread_ref = "project:/thread-notes/source.md"
    decision_ref = "project:/decisions/DR-0001-source-backed-context.md"
    repo_ref = "repo:/README.md"
    return {
        "title": "Example Project context",
        "description": "Current implementation state and semantic model for the example Project.",
        "projectStatus": "active",
        "currentFocus": "Implement Working Context v3.",
        "blocked": False,
        "mainBlocker": "",
        "exactNextAction": "Run the Working Context tests.",
        "projectOverview": [{"text": "This Project builds a context pipeline.", "sourceRefs": [repo_ref]}],
        "currentTruth": [{"text": "Working Context implementation is active.", "sourceRefs": [thread_ref]}],
        "currentOutcome": [],
        "activeWork": [{"text": "Implement Working Context v3.", "sourceRefs": [thread_ref]}],
        "risksAndConstraints": [],
        "effectiveDecisions": [
            {"decisionRef": decision_ref, "summary": "Keep Working Context concise and source-backed."}
        ],
        "semanticGlossary": [
            {
                "term": "Working Context",
                "definition": "A concise Project current-truth dashboard.",
                "aliases": [],
                "distinctions": ["It is not a chronological Thread Note."],
                "sourceRefs": [decision_ref],
            }
        ],
        "taxonomyItems": [
            {
                "label": "Context artifacts",
                "kind": "category",
                "parent": "",
                "description": "Project context outputs.",
                "sourceRefs": [decision_ref],
            },
            {
                "label": "Working Context",
                "kind": "artifact",
                "parent": "Context artifacts",
                "description": "Current orientation artifact.",
                "sourceRefs": [decision_ref],
            },
        ],
        "taxonomyRelations": [
            {
                "subject": "Context artifacts",
                "predicate": "contains",
                "object": "Working Context",
                "sourceRefs": [decision_ref],
            }
        ],
        "keyEvidence": [{"ref": decision_ref, "reason": "Defines the dashboard contract."}],
        "resumption": [{"text": "Continue from the active implementation.", "sourceRefs": [thread_ref]}],
        "sourceLimitations": [],
    }


class FakeWorkingContextGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self.last_metrics = {"modelCalls": 1, "transportRetries": 0, "semanticRetries": 0}

    def generate(
        self,
        project: Project,
        source_batches: Sequence[Sequence[WorkingContextSource]],
    ) -> dict[str, Any]:
        self.calls += 1
        assert project.project_id == "local-project"
        assert source_batches
        return generated_output()


def test_profile_is_strict_and_application_owned() -> None:
    profile = load_working_context_profile()

    assert profile.prompt.version == "1.0"
    assert profile.template.version == "1.0"
    assert profile.schema.value["additionalProperties"] is False
    assert set(profile.schema.value["required"]) == set(profile.schema.value["properties"])


def test_output_validation_rejects_unknown_evidence() -> None:
    output = generated_output()
    output["currentTruth"][0]["sourceRefs"] = ["project:/thread-notes/missing.md"]

    try:
        validate_working_context_output(
            output,
            {
                "project:/thread-notes/source.md",
                "project:/decisions/DR-0001-source-backed-context.md",
                "repo:/README.md",
            },
            {"project:/decisions/DR-0001-source-backed-context.md"},
        )
    except Exception as exc:
        assert "unknown sources" in str(exc)
    else:
        raise AssertionError("unknown evidence was accepted")


def test_build_is_read_only_by_default_and_write_creates_artifact(tmp_path: Path) -> None:
    project = working_project(tmp_path)
    config = pipeline_config(tmp_path)

    planned, planned_report_path = execute_working_context_build(
        config,
        project,
        generator=None,
    )

    assert planned["dryRun"] is True
    assert planned["selectedCount"] == 1
    assert planned_report_path is None
    assert not (project.context_path / "working-context.md").exists()

    generator = FakeWorkingContextGenerator()
    report, report_path = execute_working_context_build(
        config,
        project,
        generator=generator,
        write=True,
        cache_root=tmp_path / "reports",
    )

    context_path = project.context_path / "working-context.md"
    assert report["createdCount"] == 1
    assert report["failed"] == []
    assert generator.calls == 1
    assert report_path is not None and report_path.is_file()
    validation = validate_working_context(context_path)
    assert validation["schemaVersion"] == 3
    text = context_path.read_text(encoding="utf-8")
    assert "## Semantic Context" in text
    assert "### Semantic Glossary" in text
    assert "### Taxonomy" in text
    assert "## Current Outcome" not in text
    assert "None." not in text
    state = json.loads(
        (project.state_directory / "working-context-build-state.json").read_text(encoding="utf-8")  # type: ignore[operator]
    )
    assert state["workingContextSha256"] == report["workingContextSha256"]


def test_unchanged_build_skips_generation(tmp_path: Path) -> None:
    project = working_project(tmp_path)
    config = pipeline_config(tmp_path)
    first = FakeWorkingContextGenerator()
    execute_working_context_build(config, project, generator=first, write=True, cache_root=tmp_path / "reports")
    second = FakeWorkingContextGenerator()

    report, _report_path = execute_working_context_build(
        config,
        project,
        generator=second,
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert report["unchangedCount"] == 1
    assert second.calls == 0


def test_edited_artifact_requires_explicit_replacement(tmp_path: Path) -> None:
    project = working_project(tmp_path)
    config = pipeline_config(tmp_path)
    execute_working_context_build(
        config,
        project,
        generator=FakeWorkingContextGenerator(),
        write=True,
        cache_root=tmp_path / "reports",
    )
    context_path = project.context_path / "working-context.md"
    context_path.write_text(context_path.read_text(encoding="utf-8") + "\nManual edit.\n", encoding="utf-8")
    (project.current_root / "README.md").write_text("# Changed\n", encoding="utf-8")
    blocked_generator = FakeWorkingContextGenerator()

    blocked, _ = execute_working_context_build(
        config,
        project,
        generator=blocked_generator,
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert blocked["edited"] is True
    assert blocked["failed"]
    assert blocked_generator.calls == 0

    replacement_generator = FakeWorkingContextGenerator()
    replaced, _ = execute_working_context_build(
        config,
        project,
        generator=replacement_generator,
        write=True,
        allow_edited=True,
        cache_root=tmp_path / "reports",
    )

    assert replaced["updatedCount"] == 1
    assert replaced["failed"] == []
    assert replacement_generator.calls == 1
    assert "Manual edit." not in context_path.read_text(encoding="utf-8")
