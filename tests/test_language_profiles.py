from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from test_config import write_yaml
from test_pipeline_workflow import Summary, config_for, execute
from test_session_note_pipeline import note_data, write_chat
from test_thread_timeline import candidate, config, event

from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import CONFIG_SCHEMA_VERSION, load_app_config, resolve_app_config
from tkn_genai_chat_note.frontmatter import parse_simple_frontmatter
from tkn_genai_chat_note.pipeline import _agent
from tkn_genai_chat_note.session_notes import (
    PipelineError,
    ProviderSummarizer,
    generator_fingerprint,
    make_config,
    render_note,
    validate_note_data,
    validate_session_note,
)
from tkn_genai_chat_note.summary_resources import load_summary_profile


def test_profiles_share_the_fixed_structure_and_have_distinct_instructions() -> None:
    japanese, english = (load_summary_profile(name) for name in ("default-jp", "default-en"))
    assert japanese.schema.value == english.schema.value
    assert japanese.template == replace(english.template, source=japanese.template.source)
    assert japanese.prompt.prompt_id != english.prompt.prompt_id
    assert "Write natural Japanese" in japanese.prompt.instructions
    assert "Write natural English" in english.prompt.instructions
    assert japanese.sha256 != english.sha256


@pytest.mark.parametrize("name", ["default", "default-ja", "custom", "../default-en", "C:/profiles/custom"])
def test_custom_and_unknown_profiles_are_rejected_before_loading(name: str, tmp_path: Path) -> None:
    write_yaml(tmp_path / ".tkn/config.yaml", {"generation": {"session_note_profile": name}})
    with pytest.raises(PipelineError):
        load_app_config(cwd=tmp_path)
    with pytest.raises(RuntimeError, match="profile name"):
        load_summary_profile(name)


def test_profile_configuration_precedence_and_old_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    global_path = home / ".tkn/genai_chat_note_pipeline/config.yaml"
    write_yaml(global_path, {"schema_version": "4.0.0", "idle_minutes": 30})
    old_bytes = global_path.read_bytes()
    old = resolve_app_config(cwd=work)
    assert old.config.generation.session_note_profile == "default-jp"
    assert old.effective_schema_version == CONFIG_SCHEMA_VERSION
    assert global_path.read_bytes() == old_bytes
    write_yaml(global_path, {"generation": {"session_note_profile": "default-en"}})
    write_yaml(work / ".tkn/config.yaml", {"generation": {"session_note_profile": "default-jp"}})
    explicit = tmp_path / "explicit.yaml"
    write_yaml(explicit, {"generation": {"session_note_profile": "default-en"}})
    resolved = resolve_app_config(cwd=work, explicit_path=explicit)
    assert (
        resolved.config.session_note_pipeline_config(allow_missing_watermark=True).summary_profile.name == "default-en"
    )
    assert resolved.sources["generation.session_note_profile"].startswith("explicit:")
    overridden = resolve_app_config(
        cwd=work, explicit_path=explicit, overrides={"generation": {"session_note_profile": "default-jp"}}
    )
    assert overridden.config.generation.session_note_profile == "default-jp"
    assert overridden.sources["generation.session_note_profile"] == "CLI option"


def test_config_show_reports_selected_bundle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    explicit = tmp_path / "config.yaml"
    write_yaml(explicit, {"generation": {"session_note_profile": "default-jp"}})
    assert main(["--config", str(explicit), "--session-note-profile", "default-en", "config", "show"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["config"]["generation"]["session_note_profile"] == "default-en"
    assert report["summaryProfile"]["name"] == "default-en"
    assert report["summaryProfile"]["prompt"]["id"] == load_summary_profile("default-en").prompt.prompt_id
    assert report["sources"]["generation.session_note_profile"] == "CLI option"


def test_english_keeps_semantic_guards_and_localizes_renderer(tmp_path: Path) -> None:
    profile = load_summary_profile("default-en")
    case = candidate(tmp_path, (event("known"),))
    data = note_data(case)
    data["summaryItems"][0]["text"] = "Actual execution was confirmed in the supplied events."
    validate_note_data(data, {"known"}, profile=profile)
    with pytest.raises(PipelineError, match="avoidable English"):
        validate_note_data(data, {"known"})
    path = tmp_path / "note.md"
    path.write_text(render_note(case, data, {}, profile=profile), encoding="utf-8")
    validate_session_note(path)
    text = path.read_text(encoding="utf-8")
    assert "Japan Standard Time (Asia/Tokyo)" in text
    assert not re.search("[ぁ-んァ-ヶ一-龯]", text)
    data["lastKnownState"]["unresolved"] = ["Unfinished request"]
    with pytest.raises(PipelineError, match="done work"):
        validate_note_data(data, {"known"}, profile=profile)
    data["lastKnownState"]["unresolved"] = []
    data["summaryItems"][0]["eventIds"] = ["invented"]
    with pytest.raises(PipelineError, match="unknown event ids"):
        validate_note_data(data, {"known"}, profile=profile)


def test_english_chunk_merge_repair_and_unknown_time_notices(tmp_path: Path) -> None:
    sources = (event("first", time=""), event("second"))
    case = candidate(tmp_path, sources)
    runner = ProviderSummarizer(replace(config(tmp_path), session_note_profile="default-en"), chunk_characters=250)
    prompts: list[str] = []
    invalid_returned = False

    def invoke(prompt: str, *, overview_only: bool = False) -> dict:
        nonlocal invalid_returned
        prompts.append(prompt)
        assert "Write natural English" in prompt
        payload = json.loads(prompt.split("BEGIN_INPUT_JSON\n")[1].split("\nEND_INPUT_JSON")[0])
        if overview_only:
            data = note_data(case)
            data.pop("timeline")
            return data
        ids = [item["id"] for item in payload["events"]]
        current = next(source for source in sources if source.id == ids[0])
        data = note_data(candidate(tmp_path, (current,)))
        data["summaryItems"][0]["text"] = "Actual execution was reported."
        if not invalid_returned:
            invalid_returned = True
            data["lastKnownState"]["unresolved"] = ["Incomplete"]
        return data

    with patch.object(runner, "_invoke", side_effect=invoke):
        result = runner.generate(case)
    assert any("MODE: repair-invalid-draft" in prompt for prompt in prompts)
    assert any("MODE: merge-partial-summaries" in prompt for prompt in prompts)
    assert runner.last_metrics["chunkCount"] == 2
    assert len(result["timeline"]) == 4
    assert result["sourceLimitations"] == ["Events with an unknown timestamp or time zone: first"]


def test_language_switch_regenerates_once_preserving_identity_and_dry_run(tmp_path: Path) -> None:
    settings = config_for(tmp_path)
    write_chat(settings.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(settings)
    assert first["complete"], first
    identity = first["threads"][0]["noteId"]
    note = settings.data_root / first["threads"][0]["noteRef"].removeprefix("data:/")
    jp_config = settings.session_note_pipeline_config(allow_missing_watermark=True)
    for name in ("default-en", "default-jp"):
        settings.generation.session_note_profile = name
        selected = settings.session_note_pipeline_config(allow_missing_watermark=True)
        if name == "default-en":
            assert generator_fingerprint(selected) != generator_fingerprint(jp_config)
        assert _agent(selected, "session-note")["profile"] == name
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        fake = Summary()
        execute(settings, "pull", dry_run=True, summarizer=fake)
        assert not fake.calls
        assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        result = execute(settings, "pull", summarizer=fake)
        assert result["complete"], result
        assert fake.calls == ["one"]
        assert result["threads"][0]["noteId"] == identity
        assert (
            parse_simple_frontmatter(note.read_text(encoding="utf-8"))["promptId"]
            == selected.summary_profile.prompt.prompt_id
        )
        repeated = Summary()
        assert execute(settings, "pull", summarizer=repeated)["complete"]
        assert not repeated.calls


@pytest.mark.parametrize("reviewed", [False, True])
def test_language_switch_protects_existing_user_content(tmp_path: Path, reviewed: bool) -> None:
    settings = config_for(tmp_path)
    write_chat(settings.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    first = execute(settings)
    note = settings.data_root / first["threads"][0]["noteRef"].removeprefix("data:/")
    text = note.read_text(encoding="utf-8")
    text = (
        text.replace('reviewStatus: "unreviewed"', 'reviewStatus: "reviewed"') if reviewed else text + "\nUser edit.\n"
    )
    note.write_text(text, encoding="utf-8")
    settings.generation.session_note_profile = "default-en"
    fake = Summary()
    execute(settings, "pull", force=True, summarizer=fake)
    assert not fake.calls
    assert note.read_text(encoding="utf-8") == text


def test_pending_work_from_other_language_is_not_reused(tmp_path: Path) -> None:
    settings = config_for(tmp_path)
    write_chat(settings.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)
    with patch("tkn_genai_chat_note.session_notes.update_refresh_state", side_effect=RuntimeError("interrupted")):
        first = execute(settings)
    assert not first["complete"]
    assert list(settings.cache_root.rglob("manifest.json"))
    settings.generation.session_note_profile = "default-en"
    fake = Summary()
    resumed = execute(settings, "pull", summarizer=fake)
    assert resumed["complete"], resumed
    assert fake.calls == ["one"]
    note = settings.data_root / resumed["threads"][0]["noteRef"].removeprefix("data:/")
    assert (
        parse_simple_frontmatter(note.read_text(encoding="utf-8"))["promptId"]
        == load_summary_profile("default-en").prompt.prompt_id
    )


def test_refreshing_internal_config_preserves_language(tmp_path: Path) -> None:
    original = replace(config(tmp_path), session_note_profile="default-en")
    with patch("tkn_genai_chat_note.session_notes.resolve_codex_bin", return_value="codex"):
        refreshed = make_config(existing=original)
    assert refreshed.summary_profile.name == "default-en"
