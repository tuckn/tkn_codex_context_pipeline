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
        ("session", "session note"),
        ("decision", "decision record"),
        ("working-context", "working context"),
    )

    def fixture_text(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_fixture_matrix_classifies_unversioned_v1_and_v2(self) -> None:
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

    def test_explicit_v1_and_v2_fixtures_keep_their_declared_schema(self) -> None:
        for prefix, label in self.artifact_cases:
            for version in ("1", "2"):
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

    def test_v2_fixtures_contain_the_stable_contract_signals(self) -> None:
        session = self.fixture_text("session-v2.md")
        self.assertIn("## Summary", session)
        self.assertIn("## Key Developments", session)
        self.assertIn("### WI-01:", session)
        self.assertIn("#### Request", session)
        self.assertIn("#### Clarification / Correction", session)
        self.assertIn("#### Explicit Decision", session)
        self.assertIn("## Last Known State", session)
        self.assertIn("## Evidence", session)
        self.assertIn("## Source Notes", session)

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

    def test_session_v2_core_fixture_omits_empty_optional_sections(self) -> None:
        session = self.fixture_text("session-v2-core.md")
        metadata = parse_simple_frontmatter(session)
        self.assertEqual("2", require_supported_artifact_schema(metadata, "session note"))

        for heading in (
            "# Session Note",
            "## Summary",
            "## Key Developments",
            "## Last Known State",
        ):
            self.assertIn(heading, session)

        for heading in (
            "### Request",
            "### Action",
            "### Reported Result",
        ):
            self.assertIn(heading, session)

        for label in (
            "- Work State:",
            "- Latest User Direction:",
        ):
            self.assertIn(label, session)

        for flat_label in (
            "- Request:",
            "- Action:",
            "- Reported Result:",
        ):
            self.assertNotIn(flat_label, session)

        for heading in (
            "## Evidence",
            "## Source Notes",
        ):
            self.assertNotIn(heading, session)


if __name__ == "__main__":
    unittest.main()
