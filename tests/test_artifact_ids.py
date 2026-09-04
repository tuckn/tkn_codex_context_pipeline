from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

import tkn_codex_context.artifact_ids as artifact_ids
from tkn_codex_context.artifact_ids import migrate_artifact_ids
from tkn_codex_context.frontmatter import parse_simple_frontmatter, split_frontmatter_lines
from tkn_codex_context.thread_notes import PipelineError, Project

FIXTURES = Path(__file__).parent / "fixtures"


def project_with_artifacts(tmp_path: Path) -> tuple[Project, list[Path]]:
    context = tmp_path / "data" / "projects" / "project-1"
    thread_note = context / "thread-notes" / "thread.md"
    decision = context / "decisions" / "DR-0003-use-semantic-schema-migration.md"
    working_context = context / "working-context.md"
    thread_note.parent.mkdir(parents=True)
    decision.parent.mkdir(parents=True)
    thread_note.write_bytes(b"\xef\xbb\xbf" + (FIXTURES / "thread-note-v3.md").read_bytes().replace(b"\n", b"\r\n"))
    decision.write_bytes((FIXTURES / "decision-v4.md").read_bytes())
    working_context.write_bytes((FIXTURES / "working-context-v3.md").read_bytes())
    project = Project(
        project_id="project-1",
        title="Project 1",
        current_root=tmp_path / "repo",
        context_path=context,
        state_directory=tmp_path / "state" / "project-1",
    )
    return project, [thread_note, decision, working_context]


def body(path: Path) -> str:
    return split_frontmatter_lines(path.read_text(encoding="utf-8-sig"))[1]


def test_dry_run_reports_metadata_changes_without_minting_or_writing(tmp_path: Path) -> None:
    project, paths = project_with_artifacts(tmp_path)
    originals = {path: path.read_bytes() for path in paths}

    report = migrate_artifact_ids([project], dry_run=True)

    assert report["plannedCount"] == 3
    assert report["assignedCount"] == 3
    assert report["schemaUpgradeCount"] == 0
    assert all(path.read_bytes() == originals[path] for path in paths)
    assert all("id" not in parse_simple_frontmatter(path.read_text(encoding="utf-8-sig")) for path in paths)


def test_write_assigns_uuid4_preserves_bodies_bom_and_newlines(tmp_path: Path) -> None:
    project, paths = project_with_artifacts(tmp_path)
    original_bodies = {path: body(path) for path in paths}

    report = migrate_artifact_ids([project], dry_run=False)

    assert report["plannedCount"] == 3
    assert report["unchangedCount"] == 0
    assert len({item["id"] for item in report["migrated"]}) == 3
    expected_versions = {"threadNote": "3", "decision": "4", "workingContext": "3"}
    seen: set[str] = set()
    for path in paths:
        metadata = parse_simple_frontmatter(path.read_text(encoding="utf-8-sig"))
        note_id = metadata["id"]
        assert str(UUID(note_id)) == note_id
        assert UUID(note_id).version == 4
        assert note_id not in seen
        seen.add(note_id)
        assert metadata["schemaVersion"] == expected_versions[metadata["type"]]
        assert body(path) == original_bodies[path]
    assert paths[0].read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in paths[0].read_bytes()


def test_existing_uuid_and_legacy_schema_are_preserved(tmp_path: Path) -> None:
    project, paths = project_with_artifacts(tmp_path)
    existing_id = "760b64a4-2e32-483b-8494-028d3b2c8642"
    decision = paths[1]
    decision.write_text(
        decision.read_text(encoding="utf-8").replace("schemaVersion: 4", f"schemaVersion: 4\nid: {existing_id}"),
        encoding="utf-8",
    )

    migrate_artifact_ids([project], dry_run=False)

    metadata = parse_simple_frontmatter(decision.read_text(encoding="utf-8"))
    assert metadata["id"] == existing_id
    assert metadata["schemaVersion"] == "4"


def test_older_legacy_schema_gets_id_without_false_schema_promotion(tmp_path: Path) -> None:
    context = tmp_path / "data" / "project"
    decision = context / "decisions" / "DR-0003-use-semantic-schema-migration.md"
    decision.parent.mkdir(parents=True)
    decision.write_bytes((FIXTURES / "decision-v2.md").read_bytes())
    original_body = body(decision)
    project = Project("project-1", "Project 1", tmp_path / "repo", context)

    migrate_artifact_ids([project], dry_run=False)

    metadata = parse_simple_frontmatter(decision.read_text(encoding="utf-8"))
    assert UUID(metadata["id"]).version == 4
    assert metadata["schemaVersion"] == "2"
    assert body(decision) == original_body


def test_duplicate_existing_id_is_rejected_before_any_write(tmp_path: Path) -> None:
    project, paths = project_with_artifacts(tmp_path)
    duplicate = "760b64a4-2e32-483b-8494-028d3b2c8642"
    for path in paths[:2]:
        text = path.read_text(encoding="utf-8-sig")
        encoded = text.replace("schemaVersion: 3", f"schemaVersion: 3\nid: {duplicate}").replace(
            "schemaVersion: 4", f"schemaVersion: 4\nid: {duplicate}"
        )
        path.write_text(encoded, encoding="utf-8")
    originals = {path: path.read_bytes() for path in paths}

    with pytest.raises(PipelineError, match="duplicate artifact id"):
        migrate_artifact_ids([project], dry_run=False)

    assert all(path.read_bytes() == originals[path] for path in paths)


def test_validation_failure_restores_all_original_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = project_with_artifacts(tmp_path)
    paths[1].write_text(
        paths[1].read_text(encoding="utf-8").replace("schemaVersion: 4", "schemaVersion: 5"),
        encoding="utf-8",
    )
    paths[2].write_text(
        paths[2].read_text(encoding="utf-8").replace("schemaVersion: 3", "schemaVersion: 4"),
        encoding="utf-8",
    )
    originals = {path: path.read_bytes() for path in paths}
    calls = 0

    def fail_second(_target: artifact_ids.ArtifactIdentityTarget) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PipelineError("injected validation failure")

    monkeypatch.setattr(artifact_ids, "_validate_current", fail_second)

    with pytest.raises(PipelineError, match="injected validation failure"):
        migrate_artifact_ids([project], dry_run=False)

    assert all(path.read_bytes() == originals[path] for path in paths)
