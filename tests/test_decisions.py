from __future__ import annotations

import json
from hashlib import sha256
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
    cleanup_session_decision_backrefs,
    execute_decision_build,
    scan_decision_sources,
    validate_decision_output,
    validate_decision_record,
)
from tkn_codex_context.frontmatter import parse_simple_frontmatter
from tkn_codex_context.session_notes import PipelineConfig, PipelineError, Project


def session_note(*, title: str = "Decision source", explicit: bool = True) -> str:
    decision_section = (
        "\n### Explicit Decision\n\n- Session Noteを一次入力としてdecisionを生成する。\n"
        if explicit
        else "\n### Proposal\n\n- Decision生成を検討する。\n"
    )
    return (
        "---\n"
        "type: summary\n"
        "schemaVersion: 2\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        "description: Decision distillation source.\n"
        "generator: Codex\n"
        "status: done\n"
        "reviewStatus: unreviewed\n"
        "distillationStatus: pending\n"
        "distilledTo: []\n"
        "date: 2026-08-03T09:00:00+09:00\n"
        "updated: 2026-08-03T10:00:00+09:00\n"
        "sessionId: 20260803T090000+0900\n"
        "sourceThreadIds:\n"
        "  - thread-1\n"
        "sourceRefs:\n"
        "  - sessions/example.jsonl\n"
        "---\n\n"
        "# Session Note\n\n"
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
                "sourceSessionRefs": source_refs or ["project:/sessions/one.md"],
                "title": "Session Noteを一次入力にする",
                "fileSlug": "use-session-notes-as-primary-input",
                "description": "Decision生成ではSession Noteを一次入力として利用する。",
                "status": "Accepted",
                "scope": "project",
                "implementationStatus": "implemented",
                "context": ["chatを繰り返し読む処理を避ける必要がある。"],
                "decision": "Decision生成ではSession Noteを一次入力として利用する。",
                "rationale": ["既にcuratedされた事実を再利用できる。"],
                "benefits": ["raw chatの再読を通常経路から除外できる。"],
                "costsAndRisks": ["Session Noteにない根拠は補完できない。"],
                "alternativesConsidered": ["毎回raw chatを読み直す。"],
                "appliesWhen": ["Codex chat由来のdecisionを生成するとき。"],
                "doesNotApplyWhen": [],
                "reusablePrinciples": ["curated artifactを下流処理の入力にする。"],
                "projectSpecificDetails": ["Session Note v2を利用する。"],
                "verificationEvidence": ["Session Note pipelineが実装済み。"],
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
        elif key == "sourceSessionRefs":
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
    item["description"] = "複数のSession Noteを統合してDecisionを更新する。"
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
    project.sessions_path.mkdir(parents=True)
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
    assert rendered.index("## Context") < rendered.index("## Decision")
    assert rendered.index("## Decision") < rendered.index("## Rationale")
    assert rendered.index("## Rationale") < rendered.index("## Materialization")


def test_decision_output_requires_known_existing_id() -> None:
    with pytest.raises(PipelineError, match="unknown decisionId"):
        validate_decision_output(
            existing_decision_output("DR-0001"),
            set(),
            {"project:/sessions/one.md"},
        )
    validate_decision_output(
        new_decision_output(),
        set(),
        {"project:/sessions/one.md"},
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
            {"project:/sessions/one.md"},
        )


def test_old_unreviewed_decision_requires_quality_update() -> None:
    with pytest.raises(PipelineError, match="must update decisionId"):
        validate_decision_output(
            existing_decision_output("DR-0001"),
            {"DR-0001"},
            {"project:/sessions/one.md"},
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
            {"project:/sessions/one.md"},
        )


def test_decision_dry_run_selects_only_explicit_decisions(
    decision_project: tuple[Project, PipelineConfig],
) -> None:
    project, config = decision_project
    (project.sessions_path / "one.md").write_text(session_note(), encoding="utf-8")
    (project.sessions_path / "two.md").write_text(session_note(title="Proposal only", explicit=False), encoding="utf-8")

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
    source = project.sessions_path / "one.md"
    original_source = session_note()
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
    completed = next(event for event in progress_events if event["type"] == "decision-batch-complete")
    assert completed["decisionRecordPaths"] == [str(decision.absolute())]
    assert source.read_text(encoding="utf-8") == original_source
    assert report["processed"][0]["decisionRefs"] == [
        "project:/decisions/DR-0001-use-session-notes-as-primary-input.md"
    ]
    state = json.loads(
        (project.state_directory / DECISION_STATE_FILENAME).read_text(encoding="utf-8")  # type: ignore[operator]
    )
    assert state["sources"]["sessions/one.md"]["decisionIds"] == ["DR-0001"]

    candidates, scan, failures = scan_decision_sources(project, config)
    assert candidates == []
    assert failures == []
    assert scan["unchanged"] == 1


def test_existing_decision_is_referenced_without_creating_duplicate(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.sessions_path / "one.md"
    first.write_text(session_note(), encoding="utf-8")
    execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(new_decision_output()),
        write=True,
        cache_root=tmp_path / "reports-one",
    )
    second = project.sessions_path / "two.md"
    second.write_text(session_note(title="Same decision"), encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(
            existing_decision_output(
                "DR-0001",
                ["project:/sessions/two.md"],
            )
        ),
        write=True,
        cache_root=tmp_path / "reports-two",
    )

    assert report["created"] == []
    assert report["referencedExisting"] == ["DR-0001"]
    assert len(list((project.context_path / "decisions").glob("*.md"))) == 1
    assert "project:/decisions/" not in second.read_text(encoding="utf-8")
    decision_text = (project.context_path / "decisions/DR-0001-use-session-notes-as-primary-input.md").read_text(
        encoding="utf-8"
    )
    assert "project:/sessions/one.md" in decision_text
    assert "project:/sessions/two.md" in decision_text


def test_unreviewed_existing_decision_can_be_resynthesized_from_later_session(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.sessions_path / "one.md"
    first.write_text(session_note(), encoding="utf-8")
    execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(new_decision_output()),
        write=True,
        cache_root=tmp_path / "reports-one",
    )
    decision = project.context_path / "decisions/DR-0001-use-session-notes-as-primary-input.md"
    original_date = parse_simple_frontmatter(decision.read_text(encoding="utf-8"))["date"]
    second = project.sessions_path / "two.md"
    second.write_text(session_note(title="Improved evidence"), encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator(
            update_decision_output(
                "DR-0001",
                ["project:/sessions/two.md"],
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
    assert "複数のSession Noteを統合してDecisionを更新する。" in decision_text
    assert "- Limitations: Direct validation remains incomplete." in decision_text
    assert "project:/sessions/one.md" in decision_text
    assert "project:/sessions/two.md" in decision_text


def test_multiple_session_notes_synthesize_one_decision(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    first = project.sessions_path / "one.md"
    second = project.sessions_path / "two.md"
    first.write_text(session_note(title="Initial choice"), encoding="utf-8")
    second.write_text(session_note(title="Later verification"), encoding="utf-8")
    source_refs = ["project:/sessions/one.md", "project:/sessions/two.md"]
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

    assert generator.calls == [["sessions/one.md", "sessions/two.md"]]
    assert len(report["created"]) == 1
    assert report["created"][0]["sourceSessionRefs"] == source_refs
    decision = project.context_path / report["created"][0]["decisionRecord"]
    decision_text = decision.read_text(encoding="utf-8")
    assert decision_text.count("project:/sessions/one.md") == 2
    assert decision_text.count("project:/sessions/two.md") == 2
    assert "- Limitations: Direct endpoint validation was incomplete." in decision_text
    assert 'promotionStatus: "no-action"' in decision_text
    assert "project:/decisions/" not in first.read_text(encoding="utf-8")
    assert "project:/decisions/" not in second.read_text(encoding="utf-8")


def test_session_decision_backref_cleanup_is_explicit_and_updates_state_hash(
    decision_project: tuple[Project, PipelineConfig],
) -> None:
    project, _config = decision_project
    source = project.sessions_path / "one.md"
    legacy = session_note().replace(
        "distillationStatus: pending\ndistilledTo: []\n",
        "distillationStatus: partial\n"
        "distilledTo:\n"
        '  - "project:/decisions/DR-0001-old.md"\n',
    )
    source.write_text(legacy, encoding="utf-8")
    assert project.state_directory is not None
    state_path = project.state_directory / DECISION_STATE_FILENAME
    state_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "projectId": project.project_id,
                "lastBuildAt": "2026-08-03T10:00:00+09:00",
                "sources": {
                    "sessions/one.md": {
                        "sourceSha256": "0" * 64,
                        "generationFingerprint": "fingerprint",
                        "decisionIds": ["DR-0001"],
                        "noAction": False,
                        "processedAt": "2026-08-03T10:00:00+09:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    planned = cleanup_session_decision_backrefs(project, write=False)

    assert planned["plannedSessionCount"] == 1
    assert planned["changedSessionCount"] == 0
    assert source.read_text(encoding="utf-8") == legacy

    changed = cleanup_session_decision_backrefs(project, write=True)

    assert changed["changedSessionCount"] == 1
    cleaned = source.read_text(encoding="utf-8")
    assert "project:/decisions/" not in cleaned
    assert 'distillationStatus: "pending"' in cleaned
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["sources"]["sessions/one.md"]["decisionIds"] == ["DR-0001"]
    assert state["sources"]["sessions/one.md"]["sourceSha256"] == sha256(source.read_bytes()).hexdigest()
    assert state["lastSessionBackrefCleanupAt"]


def test_no_action_is_remembered_without_finalizing_session(
    decision_project: tuple[Project, PipelineConfig],
    tmp_path: Path,
) -> None:
    project, config = decision_project
    source = project.sessions_path / "one.md"
    original = session_note()
    source.write_text(original, encoding="utf-8")

    report, _path = execute_decision_build(
        config,
        project,
        generator=FakeDecisionGenerator({"decisions": [], "sourceLimitations": []}),
        write=True,
        cache_root=tmp_path / "reports",
    )

    assert report["noAction"] == ["sessions/one.md"]
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
    source = project.sessions_path / "one.md"
    original = session_note()
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
