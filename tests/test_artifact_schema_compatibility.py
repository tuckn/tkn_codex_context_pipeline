from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIB_ROOT = PLUGIN_ROOT / "lib"
sys.path.insert(0, str(LIB_ROOT))

from tkn_codex_context.frontmatter import (  # noqa: E402
    ensure_artifact_schema_version,
    parse_simple_frontmatter,
    require_supported_artifact_schema,
    split_frontmatter_lines,
)


class ArtifactSchemaCompatibilityTests(unittest.TestCase):
    artifact_cases = (
        ("decision", "decision record"),
        ("working-context", "working context"),
    )

    def fixture_text(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_fixture_matrix_classifies_supported_versions(self) -> None:
        for prefix, label in self.artifact_cases:
            with self.subTest(artifact=prefix, version="unversioned"):
                metadata = parse_simple_frontmatter(self.fixture_text(f"{prefix}-v1-unversioned.md"))
                self.assertEqual(
                    "1",
                    require_supported_artifact_schema(metadata, label),
                )

            with self.subTest(artifact=prefix, version="1"):
                metadata = parse_simple_frontmatter(self.fixture_text(f"{prefix}-v1.md"))
                self.assertEqual(
                    "1",
                    require_supported_artifact_schema(metadata, label),
                )

            with self.subTest(artifact=prefix, version="2"):
                metadata = parse_simple_frontmatter(self.fixture_text(f"{prefix}-v2.md"))
                self.assertEqual(
                    "2",
                    require_supported_artifact_schema(metadata, label),
                )

        decision_v4 = parse_simple_frontmatter(self.fixture_text("decision-v4.md"))
        self.assertEqual("4", require_supported_artifact_schema(decision_v4, "decision record"))

        working_context_v3 = parse_simple_frontmatter(self.fixture_text("working-context-v3.md"))
        self.assertEqual("3", require_supported_artifact_schema(working_context_v3, "working context"))

        thread_note_v3 = parse_simple_frontmatter(self.fixture_text("thread-note-v3.md"))
        self.assertEqual("3", require_supported_artifact_schema(thread_note_v3, "thread note"))

    def test_unversioned_fixtures_become_explicit_v1_without_body_changes(self) -> None:
        for prefix, label in self.artifact_cases:
            with self.subTest(artifact=prefix):
                original = self.fixture_text(f"{prefix}-v1-unversioned.md")
                header, body = split_frontmatter_lines(original)

                updated_header = ensure_artifact_schema_version(header, label)
                updated = "".join(updated_header) + body

                self.assertIn(
                    f"type: {parse_simple_frontmatter(original)['type']}\nschemaVersion: 1\n",
                    updated,
                )
                self.assertEqual(body, split_frontmatter_lines(updated)[1])

    def test_explicit_supported_fixtures_keep_their_declared_schema(self) -> None:
        for prefix, label in self.artifact_cases:
            versions = ("1", "2")
            for version in versions:
                with self.subTest(artifact=prefix, version=version):
                    original = self.fixture_text(f"{prefix}-v{version}.md")
                    header, body = split_frontmatter_lines(original)

                    updated_header = ensure_artifact_schema_version(header, label)
                    updated = "".join(updated_header) + body

                    self.assertEqual(original, updated)

    def test_unsupported_fixture_versions_are_rejected(self) -> None:
        for prefix, label in self.artifact_cases:
            with self.subTest(artifact=prefix):
                metadata = parse_simple_frontmatter(self.fixture_text(f"{prefix}-v99.md"))
                with self.assertRaisesRegex(
                    SystemExit,
                    rf"Unsupported {label} schemaVersion: 99",
                ):
                    require_supported_artifact_schema(metadata, label)

        thread_note = parse_simple_frontmatter(self.fixture_text("thread-note-v99.md"))
        with self.assertRaisesRegex(SystemExit, r"Unsupported thread note schemaVersion: 99"):
            require_supported_artifact_schema(thread_note, "thread note")

    def test_current_fixtures_contain_the_stable_contract_signals(self) -> None:
        thread_note = self.fixture_text("thread-note-v3.md")
        self.assertIn("## Summary", thread_note)
        self.assertIn("## Key Developments", thread_note)
        self.assertIn("### WI-01:", thread_note)
        self.assertIn("#### Request", thread_note)
        self.assertIn("#### Clarification / Correction", thread_note)
        self.assertIn("#### Explicit Decision", thread_note)
        self.assertIn("## Last Known State", thread_note)
        self.assertIn("## Evidence", thread_note)
        self.assertIn("## Source Notes", thread_note)

        decision = self.fixture_text("decision-v2.md")
        decision_metadata = parse_simple_frontmatter(decision)
        self.assertEqual("verified", decision_metadata["implementationStatus"])
        self.assertIn("## Rationale", decision)
        self.assertIn("## Applicability", decision)
        self.assertIn("## Materialization", decision)
        self.assertIn("## Supersession", decision)

        working_context = self.fixture_text("working-context-v2.md")
        working_metadata = parse_simple_frontmatter(working_context)
        for field in (
            "projectStatus",
            "health",
            "priority",
            "currentFocus",
            "blocked",
            "mainBlocker",
            "exactNextAction",
            "lastMeaningfulActivity",
            "reviewAfter",
            "dependencyProjectIds",
        ):
            self.assertIn(field, working_metadata)
        self.assertIn("## Effective Decisions", working_context)
        self.assertIn("## Key Files And Evidence", working_context)
        self.assertIn("## Resumption", working_context)

    def test_decision_v4_fixture_is_concise_and_omits_empty_sections(self) -> None:
        decision = self.fixture_text("decision-v4.md")
        metadata = parse_simple_frontmatter(decision)
        self.assertEqual("4", metadata["schemaVersion"])
        for field in (
            "projectWorkingContextTargets",
            "repositoryDocumentationTargets",
            "globalContextTargets",
            "skillAutomationTargets",
        ):
            self.assertIn(field, metadata)
        self.assertIn("## Decision", decision)
        self.assertIn("## Why", decision)
        self.assertIn("## Verification", decision)
        self.assertNotIn("## Consequences", decision)
        self.assertNotIn("## Materialization", decision)
        self.assertNotIn("## Supersession", decision)
        self.assertNotIn("None.", decision)

    def test_thread_note_v3_core_fixture_omits_empty_optional_sections(self) -> None:
        thread_note = self.fixture_text("thread-note-v3-core.md")
        metadata = parse_simple_frontmatter(thread_note)
        self.assertEqual("3", require_supported_artifact_schema(metadata, "thread note"))

        for heading in (
            "# Thread Note",
            "## Summary",
            "## Key Developments",
            "## Last Known State",
        ):
            self.assertIn(heading, thread_note)

        for heading in (
            "### Request",
            "### Action",
            "### Reported Result",
        ):
            self.assertIn(heading, thread_note)

        for label in (
            "- Work State:",
            "- Latest User Direction:",
        ):
            self.assertIn(label, thread_note)

        for flat_label in (
            "- Request:",
            "- Action:",
            "- Reported Result:",
        ):
            self.assertNotIn(flat_label, thread_note)

        for heading in (
            "## Evidence",
            "## Source Notes",
        ):
            self.assertNotIn(heading, thread_note)

    def test_working_context_v3_fixture_is_concise_and_semantic(self) -> None:
        working_context = self.fixture_text("working-context-v3.md")
        metadata = parse_simple_frontmatter(working_context)

        self.assertEqual("3", metadata["schemaVersion"])
        self.assertIn("## Project Overview", working_context)
        self.assertIn("## Current Truth", working_context)
        self.assertIn("## Semantic Context", working_context)
        self.assertIn("### Semantic Glossary", working_context)
        self.assertIn("### Taxonomy", working_context)
        self.assertNotIn("## Current Outcome", working_context)
        self.assertNotIn("None.", working_context)


if __name__ == "__main__":
    unittest.main()
