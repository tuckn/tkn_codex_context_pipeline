from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from tkn_codex_context.decision_resources import (
    REQUIRED_TEMPLATE_FIELDS,
    load_decision_profile,
    render_decision_template,
)
from tkn_codex_context.decisions import (
    DECISION_STATE_FILENAME,
    DecisionSource,
    ExistingDecision,
    execute_decision_build,
    scan_decision_sources,
    validate_decision_output,
    validate_decision_record,
)
from tkn_codex_context.frontmatter import parse_simple_frontmatter
from tkn_codex_context.thread_notes import PipelineConfig, PipelineError, Project


def thread_note(*, title: str = "Decision source", explicit: bool = True) -> str:
    decision_section = (
        "\n### Explicit Decision\n\n- Thread Noteを一次入力としてdecisionを生成する。\n"
        if explicit
        else "\n### Proposal\n\n- Decision生成を検討する。\n"
    )
    return (
        "---\n"
        "type: threadNote\n"
        "schemaVersion: 3\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "description: Decision distillation source.\n"
        "generator: Codex\n"
        "status: done\n"
        "reviewStatus: unreviewed\n"
        "date: 2026-08-03T09:00:00+09:00\n"
        "updated: 2026-08-03T10:00:00+09:00\n"
        "threadNoteId: 20260803T090000+0900\n"
        "sourceThreadIds:\n"
        "  - thread-1\n"
        "sourceRefs:\n"
        "  - sessions/example.jsonl\n"
        "---\n\n"
        "# Thread Note\n\n"
        "## Summary\n\n"
        "- Decision生成方針を決定した。\n\n"
        "## Key Developments\n"
        f"{decision_section}\n"
        "## Last Known State\n\n"
        "- Work State: done — 方針が決定した。\n"
    )


def new_decision_output(
    source_refs: list[str] | None = None,
) -> dict:
    return {
        "decisions": [
            {
                "disposition": "create",
                "existingDecisionId": "",
                "sourceThreadNoteRefs": source_refs or ["project:/thread-notes/one.md"],
                "title": "Thread Noteを一次入力にする",
                "fileSlug": "use-thread-notes-as-primary-input",
                "description": "Decision生成ではThread Noteを一次入力として利用する。",
                "status": "Accepted",
                "scope": "project",
                "implementationStatus": "implemented",
                "context": ["chatを繰り返し読む処理を避ける必要がある。"],
                "decision": "Decision生成ではThread Noteを一次入力として利用する。",
                "rationale": ["既にcuratedされた事実を再利用できる。"],
                "benefits": ["raw chatの再読を通常経路から除外できる。"],
                "costsAndRisks": ["Thread Noteにない根拠は補完できない。"],
                "alternativesConsidered": ["毎回raw chatを読み直す。"],
                "appliesWhen": ["Codex chat由来のdecisionを生成するとき。"],
                "doesNotApplyWhen": [],
                "reusablePrinciples": ["curated artifactを下流処理の入力にする。"],
                "projectSpecificDetails": ["Thread Note v3を利用する。"],
                "verificationEvidence": ["Thread Note pipelineが実装済み。"],
                "verificationLimitations": [],
                "validationDate": "2026-08-03",
                "relatedEvidence": ["project:/README_ja.md"],
                "materialization": {
                    "projectWorkingContext": ["Effective Decisionsへ反映する。"],
                    "repositoryDocumentation": [],
                    "globalContext": [],
                    "skillAutomation": [],
                    "followUp": ["working context生成を追加する。"],
                },
                "supersedes": [],
                "supersededBy": [],
            }
        ],
        "sourceLimitations": [],
    }


def existing_decision_output(
    decision_id: str,
    source_refs: list[str] | None = None,
) -> dict:
    item = new_decision_output(source_refs)["decisions"][0]
    for key, value in list(item.items()):
        if key == "disposition":
            item[key] = "existing"
        elif key == "existingDecisionId":
            item[key] = decision_id
        elif key == "sourceThreadNoteRefs":
            continue
        elif key == "materialization":
            item[key] = {name: [] for name in value}
        elif isinstance(value, list):
            item[key] = []
        else:
            item[key] = ""
    return {"decisions": [item], "sourceLimitations": []}


def update_decision_output(
    decision_id: str,
    source_refs: list[str],
) -> dict:
    output = new_decision_output(source_refs)
    item = output["decisions"][0]
    item["disposition"] = "update"
    item["existingDecisionId"] = decision_id
    item["description"] = "複数のThread Noteを統合してDecisionを更新する。"
    item["verificationLimitations"] = ["Direct validation remains incomplete."]
    return output


class FakeDecisionGenerator:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[list[str]] = []
        self.last_metrics = {"modelCalls": 1, "transportRetries": 0, "semanticRetries": 0}

    def generate(
        self,
        sources: list[DecisionSource],
        existing_decisions: list[ExistingDecision],
    ) -> dict:
        self.calls.append([source.relative_path for source in sources])
        return self.output


@pytest.fixture
def decision_project(tmp_path: Path) -> tuple[Project, PipelineConfig]:
    project = Project(
        project_id="project-1",
        title="Project 1",
        current_root=tmp_path / "repo",
        context_path=tmp_path / "data" / "projects" / "project-1",
        state_directory=tmp_path / "state" / "projects" / "project-1",
    )
    project.current_root.mkdir(parents=True)
    project.thread_notes_path.mkdir(parents=True)
    assert project.state_directory is not None
    project.state_directory.mkdir(parents=True)
    config = PipelineConfig(
        installed_at="2026-08-01T00:00:00+09:00",
        sessions_root=tmp_path / "codex-sessions",
        source_id="windows",
        codex_bin=str(tmp_path / "codex.exe"),
    )
    return project, config


def test_decision_profile_is_packaged_and_controls_heading_order() -> None:
    profile = load_decision_profile()
    assert profile.source.endswith("profiles/decision/default")
    assert profile.schema.value["additionalProperties"] is False
    assert set(profile.schema.value["required"]) == set(profile.schema.value["properties"])
    values = {field: field for field in REQUIRED_TEMPLATE_FIELDS}
    rendered = render_decision_template(profile.template, values)
    assert rendered.index("## Decision") < rendered.index("## Why")
    assert rendered.index("## Why") < rendered.index("## Consequences")
    assert rendered.index("## Consequences") < rendered.index("## Follow-up")


def test_decision_template_omits_empty_optional_sections() -> None:
    profile = load_decision_profile()
    values = {field: "" for field in REQUIRED_TEMPLATE_FIELDS}
    values.update(
        {
            "frontmatter": "---\ntype: decision\n---",
            "title": "DR-0001: Concise decision",
            "decision": "Keep only sections with content.",
        }
    )

    rendered = render_decision_template(profile.template, values)

    assert "## Decision" in rendered
    assert "## Why" not in rendered
    assert "## Consequences" not in rendered
    assert "## Follow-up" not in rendered
    assert "None." not in rendered


def test_decision_validator_keeps_generated_v2_compatibility(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "decision-v2.md"
    record = tmp_path / "DR-0003-use-semantic-schema-migration.md"
    record.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    validation = validate_decision_record(record)

    assert validation["schemaVersion"] == 2
    assert validation["decisionId"] == "DR-0003"


def test_unreviewed_codex_v2_is_a_v4_quality_upgrade_candidate(tmp_path: Path) -> None:
    decision = ExistingDecision(
        decision_id="DR-0003",
        path=tmp_path / "DR-0003-example.md",
        title="Example",
        status="Accepted",
        decision="Keep the decision.",
        review_status="unreviewed",
        generator="Codex",
        schema_version="2",
        prompt_version="2.1",
    )

    assert decision.update_allowed is True
    assert decision.quality_upgrade_required is True

    reviewed = replace(decision, review_status="reviewed")
    assert reviewed.update_allowed is False
    assert reviewed.quality_upgrade_required is False


def test_unreviewed_v2_can_be_resynthesized_as_v4(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    source = project.thread_notes_path / "one.md"
    source.write_text(thread_note(), encoding="utf-8")
    directory = project.context_path / "decisions"
    directory.mkdir(parents=True)
    fixture = (Path(__file__).parent / "fixtures" / "decision-v2.md").read_text(encoding="utf-8")
    fixture = fixture.replace("reviewStatus: reviewed", "reviewStatus: unreviewed")
    record = directory / "DR-0003-use-semantic-schema-migration.md"
    record.write_text(fixture, encoding="utf-8")
    original_date = parse_simple_frontmatter(fixture)["date"]

    report, _report_path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(
            update_decision_output("DR-0003", ["project:/thread-notes/one.md"])
        ),
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert [item["decisionId"] for item in report["updated"]] == ["DR-0003"]
    metadata = parse_simple_frontmatter(record.read_text(encoding="utf-8"))
    assert metadata["schemaVersion"] == "4"
    assert metadata["date"] == original_date
    assert "None." not in record.read_text(encoding="utf-8")


def test_decision_build_warns_when_existing_index_reaches_limit(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    existing = [
        ExistingDecision(
            decision_id=f"DR-{index:04d}",
            path=tmp_path / f"DR-{index:04d}-decision.md",
            title=f"Decision {index}",
            status="Accepted",
            decision=f"Keep decision {index}.",
            source_refs=(),
            review_status="reviewed",
            generator="Codex",
            schema_version="4",
            prompt_version="4.0",
        )
        for index in range(1, 201)
    ]

    with patch("tkn_codex_context.decisions.load_existing_decisions", return_value=existing):
        report, report_path = execute_decision_build(
            config,
            project,
            generator=None,
            write=False,
            cache_root=tmp_path / "reports",
        )

    assert report_path is None
    assert report["existingDecisionIndexLimit"] == 200
    assert report["existingDecisionIndexOmittedCount"] == 0
    assert len(report["warnings"]) == 1
    assert "has reached its 200-record limit" in report["warnings"][0]


def test_decision_output_requires_known_existing_id() -> None:
    with pytest.raises(PipelineError, match="unknown decisionId"):
        validate_decision_output(
            existing_decision_output("DR-0001"),
            set(),
            {"project:/thread-notes/one.md"},
        )
    validate_decision_output(
        new_decision_output(),
        set(),
        {"project:/thread-notes/one.md"},
    )


def test_decision_output_rejects_context_artifacts_as_repository_documentation() -> None:
    output = new_decision_output()
    output["decisions"][0]["materialization"]["repositoryDocumentation"] = [
        "DR-0011-prior-decision.md"
    ]

    with pytest.raises(PipelineError, match="evidence, not repository documentation"):
        validate_decision_output(
            output,
            set(),
            {"project:/thread-notes/one.md"},
        )


def test_old_unreviewed_decision_requires_quality_update() -> None:
    with pytest.raises(PipelineError, match="must update decisionId"):
        validate_decision_output(
            existing_decision_output("DR-0001"),
            {"DR-0001"},
            {"project:/thread-notes/one.md"},
            {"DR-0001"},
            {"DR-0001"},
        )


def test_decision_output_rejects_unavailable_decision_evidence() -> None:
    output = new_decision_output()
    output["decisions"][0]["relatedEvidence"] = ["DR-0011-legacy-record.md"]

    with pytest.raises(PipelineError, match="unavailable from the current index"):
        validate_decision_output(
            output,
            set(),
            {"project:/thread-notes/one.md"},
        )


def test_decision_dry_run_selects_only_explicit_decisions(
    decision_project: tuple[Project, PipelineConfig],
) -> None:
    project, config = decision_project
    (project.thread_notes_path / "one.md").write_text(thread_note(), encoding="utf-8")
    (project.thread_notes_path / "two.md").write_text(
        thread_note(title="Proposal only", explicit=False),
        encoding="utf-8",
    )

    report, report_path = execute_decision_build(
        config,
        project,
        generator=None,
        write=False,
    )

    assert report_path is None
    assert report["selectedCount"] == 1
    assert report["scan"]["withoutExplicitDecision"] == 1
    assert not (project.context_path / "decisions").exists()
    assert not (project.state_directory / DECISION_STATE_FILENAME).exists()  # type: ignore[operator]


def test_decision_write_creates_record_without_mutating_source_and_becomes_unchanged(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    source = project.thread_notes_path / "one.md"
    original_source = thread_note()
    source.write_text(original_source, encoding="utf-8")
    generator = FakeDecisionGenerator(new_decision_output())
    progress_events: list[dict[str, object]] = []

    report, report_path = execute_decision_build(
        config,
        project,
        generator=generator,
        write=True,
        cache_root=tmp_path / "reports",
        progress=progress_events.append,
    )

    assert report_path is not None and report_path.is_file()
    assert report["selectedCount"] == 1
    assert len(report["created"]) == 1
    decision = project.context_path / report["created"][0]["decisionRecord"]
    validation = validate_decision_record(decision)
    assert validation["decisionId"] == "DR-0001"
    assert validation["schemaVersion"] == 4
    decision_text = decision.read_text(encoding="utf-8")
    assert "schemaVersion: 4" in decision_text
    assert "## Decision" in decision_text
    assert "## Why" in decision_text
    assert "## Follow-up" in decision_text
    assert "## Materialization" not in decision_text
    assert "## Supersession" not in decision_text
    assert "None." not in decision_text
    completed = next(event for event in progress_events if event["type"] == "decision-batch-complete")
    assert completed["decisionRecordPaths"] == [str(decision.absolute())]
    assert source.read_text(encoding="utf-8") == original_source
    assert report["processed"][0]["decisionRefs"] == [
        "project:/decisions/DR-0001-use-thread-notes-as-primary-input.md"
    ]
    state = json.loads(
        (project.state_directory / DECISION_STATE_FILENAME).read_text(encoding="utf-8")  # type: ignore[operator]
    )
    assert state["sources"]["thread-notes/one.md"]["decisionIds"] == ["DR-0001"]

    candidates, scan, failures = scan_decision_sources(project, config)
    assert candidates == []
    assert failures == []
    assert scan["unchanged"] == 1


def test_existing_decision_is_referenced_without_creating_duplicate(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.thread_notes_path / "one.md"
    first.write_text(thread_note(), encoding="utf-8")
    execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(new_decision_output()),
        write=True,
        cache_root=tmp_path / "reports-one",
    )
    second = project.thread_notes_path / "two.md"
    second.write_text(thread_note(title="Same decision"), encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(
            existing_decision_output(
                "DR-0001",
                ["project:/thread-notes/two.md"],
            )
        ),
        write=True,
        cache_root=tmp_path / "reports-two",
    )

    assert report["created"] == []
    assert report["referencedExisting"] == ["DR-0001"]
    assert len(list((project.context_path / "decisions").glob("*.md"))) == 1
    assert "project:/decisions/" not in second.read_text(encoding="utf-8")
    decision_text = (project.context_path / "decisions/DR-0001-use-thread-notes-as-primary-input.md").read_text(
        encoding="utf-8"
    )
    assert "project:/thread-notes/one.md" in decision_text
    assert "project:/thread-notes/two.md" in decision_text


def test_unreviewed_existing_decision_can_be_resynthesized_from_later_session(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.thread_notes_path / "one.md"
    first.write_text(thread_note(), encoding="utf-8")
    execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(new_decision_output()),
        write=True,
        cache_root=tmp_path / "reports-one",
    )
    decision = project.context_path / "decisions/DR-0001-use-thread-notes-as-primary-input.md"
    original_date = parse_simple_frontmatter(decision.read_text(encoding="utf-8"))["date"]
    second = project.thread_notes_path / "two.md"
    second.write_text(thread_note(title="Improved evidence"), encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(
            update_decision_output(
                "DR-0001",
                ["project:/thread-notes/two.md"],
            )
        ),
        write=True,
        cache_root=tmp_path / "reports-two",
    )

    assert report["created"] == []
    assert [item["decisionId"] for item in report["updated"]] == ["DR-0001"]
    assert len(list((project.context_path / "decisions").glob("*.md"))) == 1
    decision_text = decision.read_text(encoding="utf-8")
    metadata = parse_simple_frontmatter(decision_text)
    assert metadata["date"] == original_date
    assert "複数のThread Noteを統合してDecisionを更新する。" in decision_text
    assert "**Limitations**\n\n- Direct validation remains incomplete." in decision_text
    assert "project:/thread-notes/one.md" in decision_text
    assert "project:/thread-notes/two.md" in decision_text


def test_multiple_thread_notes_synthesize_one_decision(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.thread_notes_path / "one.md"
    second = project.thread_notes_path / "two.md"
    first.write_text(thread_note(title="Initial choice"), encoding="utf-8")
    second.write_text(thread_note(title="Later verification"), encoding="utf-8")
    source_refs = ["project:/thread-notes/one.md", "project:/thread-notes/two.md"]
    output = new_decision_output(source_refs)
    output["decisions"][0]["reusablePrinciples"] = []
    output["decisions"][0]["verificationLimitations"] = ["Direct endpoint validation was incomplete."]
    generator = FakeDecisionGenerator(output)

    report, _path = execute_decision_build(
        config,
        project,
        generator=generator,
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert generator.calls == [["thread-notes/one.md", "thread-notes/two.md"]]
    assert len(report["created"]) == 1
    assert report["created"][0]["sourceThreadNoteRefs"] == source_refs
    decision = project.context_path / report["created"][0]["decisionRecord"]
    decision_text = decision.read_text(encoding="utf-8")
    assert decision_text.count("project:/thread-notes/one.md") == 1
    assert decision_text.count("project:/thread-notes/two.md") == 1
    assert "**Limitations**\n\n- Direct endpoint validation was incomplete." in decision_text
    assert 'promotionStatus: "no-action"' in decision_text
    assert "project:/decisions/" not in first.read_text(encoding="utf-8")
    assert "project:/decisions/" not in second.read_text(encoding="utf-8")


def test_no_action_is_remembered_without_finalizing_session(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    source = project.thread_notes_path / "one.md"
    original = thread_note()
    source.write_text(original, encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator({"decisions": [], "sourceLimitations": []}),
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert report["noAction"] == ["thread-notes/one.md"]
    assert source.read_text(encoding="utf-8") == original
    candidates, scan, failures = scan_decision_sources(project, config)
    assert candidates == []
    assert failures == []
    assert scan["unchanged"] == 1


def test_transaction_rolls_back_decision_when_state_write_fails(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    source = project.thread_notes_path / "one.md"
    original = thread_note()
    source.write_text(original, encoding="utf-8")

    with patch(
        "tkn_codex_context.decisions.atomic_write_json",
        side_effect=OSError("simulated state failure"),
    ):
        report, _path = execute_decision_build(
            config,
            project,
            generator=FakeDecisionGenerator(new_decision_output()),
            write=True,
            cache_root=tmp_path / "reports",
        )

    assert len(report["failed"]) == 1
    assert source.read_text(encoding="utf-8") == original
    assert list((project.context_path / "decisions").glob("*.md")) == []
    assert not (project.state_directory / DECISION_STATE_FILENAME).exists()  # type: ignore[operator]
