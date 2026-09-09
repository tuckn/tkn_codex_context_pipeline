from __future__ import annotations

from dataclasses import replace

import pytest

from tkn_codex_context.summary_resources import (
    REQUIRED_TEMPLATE_FIELDS,
    load_summary_profile,
    load_summary_schema,
    load_summary_template,
    render_summary_template,
    validate_summary_output_schema,
)


def test_packaged_schema_is_strict_and_versioned_by_hash() -> None:
    resource = load_summary_schema()
    schema = resource.value

    assert resource.source.endswith("profiles/summary/default/output.schema.json")
    assert len(resource.sha256) == 64
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    labels = schema["properties"]["timeline"]["items"]["properties"]["label"]["enum"]
    assert "Explicit Decision" in labels


def test_default_summary_profile_loads_one_application_owned_bundle() -> None:
    profile = load_summary_profile()

    assert profile.name == "default"
    assert profile.source.endswith("profiles/summary/default")
    assert profile.prompt.source.endswith("profiles/summary/default/prompt.md")
    assert profile.schema.source.endswith("profiles/summary/default/output.schema.json")
    assert profile.template.source.endswith("profiles/summary/default/template.md")
    assert len(profile.sha256) == 64


def test_external_schema_validator_rejects_missing_and_extra_fields() -> None:
    schema = load_summary_schema().value

    with pytest.raises(ValueError, match="missing fields"):
        validate_summary_output_schema({}, schema)

    valid = {
        "title": "Title",
        "fileSlug": "valid-slug",
        "description": "Description",
        "summaryItems": [{"text": "Summary", "eventIds": ["L000001"]}],
        "timeline": [],
        "evidence": [],
        "lastKnownState": {
            "workState": "done",
            "detail": "Done.",
            "latestUserDirection": "Complete it.",
            "unresolved": [],
            "unverified": [],
            "continuationPoint": "",
            "eventIds": ["L000001"],
        },
        "sourceLimitations": [],
    }
    validate_summary_output_schema(valid, schema)
    valid["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected fields"):
        validate_summary_output_schema(valid, schema)


def test_packaged_markdown_template_controls_heading_order() -> None:
    template = load_summary_template()
    values = {field: field for field in REQUIRED_TEMPLATE_FIELDS}
    values.update(work_state="done", evidence="- Text: Evidence", source_notes="")

    rendered = render_summary_template(template, values)

    assert template.version == "4.0"
    assert rendered.index("# Thread Note") < rendered.index("## Summary")
    assert rendered.index("## Summary") < rendered.index("## Timeline")
    assert rendered.index("## Timeline") < rendered.index(
        "## Last Known State"
    )
    assert rendered.index("## Last Known State") < rendered.index("## Evidence")


@pytest.mark.parametrize("evidence", ["", "- Text: Evidence"])
@pytest.mark.parametrize("source_notes", [" \n", "- Text: Limitation"])
def test_optional_sections_follow_content(evidence: str, source_notes: str) -> None:
    values = dict.fromkeys(REQUIRED_TEMPLATE_FIELDS, "value")
    values.update(evidence=evidence, source_notes=source_notes)
    rendered = render_summary_template(load_summary_template(), values)
    assert ("## Evidence" in rendered) == bool(evidence.strip())
    assert ("## Source Notes" in rendered) == bool(source_notes.strip())
    assert "{{" not in rendered


def test_template_can_reorder_optional_sections() -> None:
    template = load_summary_template()
    start = template.body.index("{{?evidence}}")
    middle = template.body.index("{{?source_notes}}")
    reordered = replace(template, body=(
        template.body[:start] + template.body[middle:] + "\n" + template.body[start:middle]
    ))
    values = dict.fromkeys(REQUIRED_TEMPLATE_FIELDS, "value")
    rendered = render_summary_template(reordered, values)
    assert rendered.index("## Source Notes") < rendered.index("## Evidence")


def test_inserted_source_text_is_not_template_syntax() -> None:
    values = dict.fromkeys(REQUIRED_TEMPLATE_FIELDS, "value")
    literal = "{{evidence}}\n\n\n{{?source_notes}}\nLiteral source\n{{/source_notes}}"
    values.update(summary=literal, evidence="", source_notes="")
    rendered = render_summary_template(load_summary_template(), values)
    assert literal in rendered
    assert "## Evidence" not in rendered
    assert "## Source Notes" not in rendered


@pytest.mark.parametrize(("old", "new", "message"), [
    ("{{/source_notes}}", "", "must close"),
    ("{{/evidence}}", "{{/source_notes}}", "mismatched"),
    ("{{?evidence}}", "{{?unknown}}", "unknown or duplicate"),
    ("{{?evidence}}", "{{?evidence}}\n{{?source_notes}}", "cannot be nested"),
    ("{{?evidence}}", "", "inside its matching block"),
    ("{{?evidence}}", "inline {{?evidence}}", "malformed syntax"),
    ("{{/evidence}}", "{{/evidence}}\n{{?evidence}}\n{{/evidence}}", "unknown or duplicate"),
    ("{{timeline}}", "", "each required placeholder exactly once"),
])
def test_invalid_conditionals_are_rejected(old: str, new: str, message: str) -> None:
    template = load_summary_template()
    invalid = replace(template, body=template.body.replace(old, new))
    with pytest.raises(ValueError, match=message):
        render_summary_template(invalid, dict.fromkeys(REQUIRED_TEMPLATE_FIELDS, "value"))


def test_optional_block_cannot_hide_required_content() -> None:
    template = load_summary_template()
    invalid = replace(template, body=template.body.replace("{{summary}}", "").replace(
        "{{?evidence}}", "{{?evidence}}\n{{summary}}"
    ))
    with pytest.raises(ValueError, match="cannot hide placeholder summary"):
        render_summary_template(invalid, dict.fromkeys(REQUIRED_TEMPLATE_FIELDS, "value"))
