from __future__ import annotations

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
    labels = schema["properties"]["workItems"]["items"]["properties"][
        "developments"
    ]["items"]["properties"]["label"]["enum"]
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
        "workItems": [],
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
    values = {
        field: {
            "frontmatter": "---\ntype: threadNote\n---",
            "summary": "- Summary",
            "key_developments": "- Development",
            "last_known_state": "- Work State: done",
            "evidence_section": "\n\n## Evidence\n\n- Evidence",
            "source_notes_section": "",
        }[field]
        for field in REQUIRED_TEMPLATE_FIELDS
    }

    rendered = render_summary_template(template, values)

    assert template.version == "2.0"
    assert rendered.index("# Thread Note") < rendered.index("## Summary")
    assert rendered.index("## Summary") < rendered.index("## Key Developments")
    assert rendered.index("## Key Developments") < rendered.index(
        "## Last Known State"
    )
    assert rendered.index("## Last Known State") < rendered.index("## Evidence")
