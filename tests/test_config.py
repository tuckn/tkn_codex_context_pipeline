from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tkn_genai_chat_note.config import (
    CONFIG_SCHEMA_VERSION,
    config_example_text,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from tkn_genai_chat_note.session_notes import PipelineError


def write_yaml(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": CONFIG_SCHEMA_VERSION, **value}
    schema_version = document.pop("schema_version")
    schema_text = json.dumps(schema_version) if isinstance(schema_version, str) else str(schema_version)
    path.write_text(
        f"schema_version: {schema_text}\n{yaml.safe_dump(document, sort_keys=False)}",
        encoding="utf-8",
    )


def test_packaged_example_config_uses_portable_home_paths() -> None:
    value = yaml.safe_load(config_example_text())

    for key in ("cache_root",):
        assert "\\" not in value[key]
        assert value[key].startswith("~/")
    assert "installed_at" not in value
    providers = value["chat"]["providers"]
    assert providers["codex"]["sources"] == {
        "my-windows-note-pc": {
            "enabled": True,
            "source_root": "~/.codex",
            "include_archived": True,
        }
    }
    for provider, home in (("claude-code", "~/.claude"), ("github-copilot", "~/.copilot")):
        assert providers[provider]["sources"] == {"my-windows-note-pc": {"enabled": False, "source_root": home}}
    assert not {"codex_home", "source_id", "include_archived"}.intersection(value)
    assert "scopes" not in value
    assert value["schema_version"] == CONFIG_SCHEMA_VERSION
    assert config_example_text().splitlines()[0] == f'schema_version: "{CONFIG_SCHEMA_VERSION}"'
    assert value["generation"] == {
        "session_note_profile": "default-jp",
        "active_provider": "codex",
        "providers": {
            "codex": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "executable": "codex",
            }
        },
    }
    assert "summary_prompt" not in value


def test_config_init_is_idempotent_and_protects_edited_config(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"

    created = initialize_user_config(target)
    unchanged = initialize_user_config(target)
    target.write_text("schema_version: 1\nmodel: edited\n", encoding="utf-8")

    with pytest.raises(PipelineError, match="different content"):
        initialize_user_config(target)

    replaced = initialize_user_config(target, force=True)

    assert created["status"] == "created"
    assert unchanged["status"] == "unchanged"
    assert replaced["status"] == "replaced"
    assert target.read_text(encoding="utf-8") == config_example_text()
    backup = Path(str(replaced["backupPath"]))
    assert backup.read_text(encoding="utf-8") == "schema_version: 1\nmodel: edited\n"


def test_precedence_and_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/genai_chat_note_pipeline/config.yaml",
        {
            "idle_minutes": 10,
            "generation": {"providers": {"codex": {"model": "global"}}},
        },
    )
    write_yaml(
        cwd / ".tkn/config.yaml",
        {
            "idle_minutes": 20,
            "generation": {"providers": {"codex": {"model": "local"}}},
        },
    )
    write_yaml(
        explicit,
        {"idle_minutes": 25, "chat": {"providers": {"codex": {"sources": {"windows": {"state_root": "state"}}}}}},
    )

    config = load_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"idle_minutes": 40},
    )

    assert config.idle_minutes == 40
    assert config.model == "local"
    assert config.state_root == (explicit.parent / "state").absolute()
    assert config.registry_path == config.source_data_root / "project-registry.jsonl"
    assert config.projects_data_root == config.source_data_root / "projects"
    assert config.projects_state_root == config.source_state_root / "projects"


def test_config_resolution_reports_the_winning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/genai_chat_note_pipeline/config.yaml",
        {"generation": {"providers": {"codex": {"model": "global"}}}},
    )
    write_yaml(
        cwd / ".tkn/config.yaml",
        {
            "generation": {"providers": {"codex": {"model": "project"}}},
            "idle_minutes": 10,
        },
    )
    write_yaml(explicit, {"idle_minutes": 20})

    resolution = resolve_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"idle_minutes": 30},
    )

    assert resolution.config.model == "project"
    assert resolution.config.idle_minutes == 30
    assert "schema_version" not in resolution.sources
    assert resolution.sources["generation.providers.codex.model"].startswith("project:")
    assert resolution.sources["idle_minutes"] == "CLI option"
    assert [layer["kind"] for layer in resolution.layers] == [
        "built-in",
        "global",
        "project",
        "explicit",
    ]
    assert all(layer["effectiveSchemaVersion"] == CONFIG_SCHEMA_VERSION for layer in resolution.layers)
    assert not resolution.has_in_memory_migrations


def test_retired_null_summary_prompt_is_ignored(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "work"
    write_yaml(cwd / ".tkn/config.yaml", {"summary_prompt": None})

    config = load_app_config(cwd=cwd)

    assert "summary_prompt" not in config.model_dump()


def test_retired_configured_summary_prompt_is_rejected(tmp_path: Path) -> None:
    write_yaml(tmp_path / ".tkn/config.yaml", {"summary_prompt": "custom.md"})

    with pytest.raises(PipelineError, match="summary profiles are application-owned"):
        load_app_config(cwd=tmp_path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"unknown_setting": True})
    with pytest.raises(PipelineError, match="unknown configuration key"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_config_file_requires_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("idle_minutes: 10\n", encoding="utf-8")

    with pytest.raises(PipelineError, match="schema_version is required"):
        load_app_config(explicit_path=path, cwd=tmp_path)


@pytest.mark.parametrize(
    ("schema_version", "message"),
    [
        ("3", "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2.0"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"2.0.0-rc1"', "expected a quoted MAJOR.MINOR.PATCH"),
        ('"1.9.0"', "schema v1 is no longer supported"),
        ('"6.1.0"', "unsupported newer configuration schema_version"),
        ('"7.0.0"', "unsupported newer configuration schema_version"),
    ],
)
def test_unsupported_schema_versions_are_rejected(
    tmp_path: Path,
    schema_version: str,
    message: str,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"schema_version: {schema_version}\n", encoding="utf-8")

    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_same_major_minor_newer_patch_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"schema_version": "6.0.7", "idle_minutes": 10})

    resolution = resolve_app_config(explicit_path=path, cwd=tmp_path)

    assert resolution.config.schema_version == CONFIG_SCHEMA_VERSION
    explicit = resolution.layers[-1]
    assert explicit["schemaVersion"] == "6.0.7"
    assert explicit["effectiveSchemaVersion"] == CONFIG_SCHEMA_VERSION
    assert explicit["migration"] is None


def test_legacy_config_requires_explicit_copy_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text('schema_version: "4.1.0"\nraw_root: old-raw\n', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="storage migrate --from-config"):
        load_app_config(explicit_path=path, cwd=tmp_path)
    assert path.read_bytes() == before


def test_source_id_must_be_safe_for_storage(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"chat": {"providers": {"codex": {"sources": {"../outside": {}}}}}})

    with pytest.raises(PipelineError, match="source_id must use only"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_legacy_integer_schema_v2_requires_explicit_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.yaml"
    path.write_text("schema_version: 2\nraw_root: old-raw\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="storage migrate --from-config"):
        load_app_config(explicit_path=path, cwd=tmp_path)
    assert path.read_bytes() == before


def test_schema_version_cannot_be_overridden_as_a_setting(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="cannot be a CLI override"):
        load_app_config(cwd=tmp_path, overrides={"schema_version": "2.0.7"})


def test_schema_v1_flat_generation_settings_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"schema_version": "1.0.0", "provider": "codex", "model": "gpt-5.6-sol"})

    with pytest.raises(PipelineError, match="schema v1 is no longer supported"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_active_provider_requires_matching_provider_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / "config.yaml"
    write_yaml(path, {"generation": {"active_provider": "ollama"}})

    with pytest.raises(PipelineError, match="matching entry under providers"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_cli_can_select_an_already_configured_provider_without_repeating_its_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(
        path,
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "generation": {
                "providers": {
                    "claude-code": {
                        "model": "claude-sonnet-4-6",
                        "reasoning_effort": "medium",
                        "executable": "claude",
                    }
                }
            },
        },
    )

    resolution = resolve_app_config(
        explicit_path=path,
        cwd=tmp_path,
        overrides={"provider": "claude-code"},
    )

    assert resolution.config.provider == "claude-code"
    assert resolution.config.model == "claude-sonnet-4-6"
    assert resolution.config.reasoning_effort == "medium"
    assert resolution.sources["generation.active_provider"] == "CLI option"
    assert resolution.sources["generation.providers.claude-code.model"].startswith("explicit:")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("codex", "gpt-5.6-sol"),
        ("claude-code", "claude-sonnet-4-6"),
        ("github-copilot", "claude-sonnet-4.6"),
        ("ollama", "qwen3.5:9b"),
    ],
)
def test_inference_providers_resolve_without_changing_codex_source(
    tmp_path: Path,
    provider: str,
    model: str,
) -> None:
    config = load_app_config(
        cwd=tmp_path,
        overrides={"provider": provider, "model": model},
    )

    assert config.provider == provider
    assert config.model == model
    assert config.sessions_root == config.codex_home / "sessions"
    assert config.session_note_pipeline_config(allow_missing_watermark=True).provider == provider


def test_remote_ollama_endpoint_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="loopback"):
        load_app_config(
            cwd=tmp_path,
            overrides={
                "provider": "ollama",
                "model": "qwen3.5:9b",
                "ollama_base_url": "https://ollama.example.com",
            },
        )


@pytest.mark.parametrize("schema_version", [2, "2.2.1", "3.0.0", "3.0.7"])
def test_legacy_chat_config_remains_unchanged_when_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: object
) -> None:
    path = tmp_path / "legacy.yaml"
    write_yaml(path, {"schema_version": schema_version, "raw_root": "old-raw"})
    before = path.read_bytes()
    with pytest.raises(PipelineError, match="storage migrate --from-config"):
        load_app_config(explicit_path=path, cwd=tmp_path)
    assert path.read_bytes() == before


def test_chat_layers_resolve_paths_per_file_and_preserve_other_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tkn_genai_chat_note.config import config_document, write_config

    home = tmp_path / "user"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    global_path = home / ".tkn/genai_chat_note_pipeline/config.yaml"
    write_yaml(
        global_path,
        {"chat": {"providers": {"codex": {"sources": {"pc-a-windows-user-a-codex": {"source_root": "logs"}}}}}},
    )
    project = tmp_path / "work/.tkn/config.yaml"
    write_yaml(
        project,
        {
            "chat": {
                "providers": {
                    "codex": {"sources": {"pc-a-windows-user-a-codex": {"include_archived": False}}},
                    "claude-code": {"sources": {"pc-a-windows-user-a-claude": {"source_root": "claude"}}},
                }
            }
        },
    )
    explicit = tmp_path / "explicit/config.yaml"
    write_yaml(
        explicit,
        {
            "chat": {
                "providers": {"github-copilot": {"sources": {"windows-github-copilot": {"source_root": "copilot"}}}}
            }
        },
    )
    resolution = resolve_app_config(
        explicit_path=explicit,
        cwd=tmp_path / "work",
        overrides={
            "chat": {"providers": {"codex": {"sources": {"pc-a-windows-user-a-codex": {"source_root": "cli-codex"}}}}}
        },
    )
    config = resolution.config
    assert config.codex_home == tmp_path / "work/cli-codex"
    assert config.source_id == "pc-a-windows-user-a-codex"
    assert config.include_archived is False
    assert (
        config.chat.providers.claude_code.sources["pc-a-windows-user-a-claude"].source_root == project.parent / "claude"
    )
    assert (
        config.chat.providers.github_copilot.sources["windows-github-copilot"].source_root
        == explicit.parent / "copilot"
    )
    assert resolution.sources["chat.providers.codex.sources.pc-a-windows-user-a-codex.source_root"] == "CLI option"
    assert resolution.sources["chat.providers.codex.sources.pc-a-windows-user-a-codex.raw_root"] == "built-in defaults"
    assert resolution.sources["chat.providers.claude-code.sources.pc-a-windows-user-a-claude.source_root"].startswith(
        "project:"
    )
    assert resolution.sources["chat.providers.github-copilot.sources.windows-github-copilot.source_root"].startswith(
        "explicit:"
    )
    saved = tmp_path / "saved.yaml"
    write_config(config, saved)
    document = yaml.safe_load(saved.read_text(encoding="utf-8"))
    assert document == config_document(config)
    assert "claude-code" in document["chat"]["providers"]
    assert "claude_code" not in document["chat"]["providers"]
    assert config_document(load_app_config(explicit_path=saved, cwd=tmp_path)) == document


@pytest.mark.parametrize("provider", ["codex", "claude-code", "github-copilot"])
def test_chat_home_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CHAT_SOURCE_TEST_ROOT", str(tmp_path / "environment"))
    for raw, expected in [
        ("~/logs", tmp_path / "logs"),
        ("$CHAT_SOURCE_TEST_ROOT/logs", tmp_path / "environment/logs"),
    ]:
        target = tmp_path / "config.yaml"
        write_yaml(target, {"chat": {"providers": {provider: {"sources": {"windows": {"source_root": raw}}}}}})
        config = load_app_config(explicit_path=target, cwd=tmp_path)
        assert config.chat.providers.entries()[provider].sources["windows"].source_root == expected


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"codex": {"sources": {"../outside": {}}}}, "source_id must use only"),
        (
            {
                "github-copilot": {
                    "sources": {"windows-github-copilot": {"include_archived": True, "source_root": "~/.copilot"}}
                }
            },
            "unknown configuration key",
        ),
        ({"ollama": {"sources": {"windows-ollama": {"enabled": False}}}}, "unknown configuration key"),
        ({"claude_code": {"sources": {"windows-claude_code": {"enabled": False}}}}, "unknown configuration key"),
        ({"codex": {"sources": {"windows": {"source_root": "  "}}}}, "paths must not be empty"),
        ({"codex": {"sources": {"windows": {"source_root": None}}}}, "Input is not a valid path"),
    ],
)
def test_invalid_chat_settings_are_rejected_before_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = tmp_path / "config.yaml"
    write_yaml(target, {"chat": {"providers": settings}})
    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=target, cwd=tmp_path)


def test_unknown_chat_key_in_lower_layer_cannot_be_hidden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    write_yaml(
        tmp_path / ".tkn/genai_chat_note_pipeline/config.yaml",
        {"chat": {"providers": {"codex": {"sources": {"windows": {"hom": "typo"}}}}}},
    )
    with pytest.raises(PipelineError, match="unknown configuration key: chat.providers.codex.sources.windows.hom"):
        load_app_config(
            cwd=tmp_path,
            overrides={"chat": {"providers": {"codex": {"sources": {"windows": {"source_root": "correct"}}}}}},
        )


@pytest.mark.parametrize(
    "schema_version,settings,message",
    [
        ("4.0.0", {"codex_home": "old"}, "legacy configuration"),
        ("5.0.0", {"chat": {"providers": {}}}, "config-only update"),
        ("3.0.0", {"chat": {"providers": {}}}, "legacy configuration"),
    ],
)
def test_chat_schema_cannot_mix_old_and_new_forms(
    tmp_path: Path, schema_version: str, settings: dict[str, object], message: str
) -> None:
    target = tmp_path / "config.yaml"
    write_yaml(target, {"schema_version": schema_version, **settings})
    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=target, cwd=tmp_path)


def test_source_id_is_scoped_by_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    path = tmp_path / "config.yaml"
    write_yaml(
        path,
        {
            "chat": {
                "providers": {
                    provider: {"sources": {"pc-a-windows-main": {"source_root": "~/source"}}}
                    for provider in ("codex", "claude-code", "github-copilot")
                }
            }
        },
    )
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    for provider in config.chat.providers.entries():
        assert (
            config.source_storage_paths(provider)["raw"]
            == Path.home() / ".tkn/genai_chat_note_pipeline/raw" / provider / "pc-a-windows-main"
        )
    assert config.source_data_root == config.data_root


def test_source_roots_keep_declaring_layer_and_config_show_reports_final_paths(tmp_path: Path) -> None:
    from tkn_genai_chat_note.config import config_document

    work = tmp_path / "work"
    project = work / ".tkn/config.yaml"
    project.parent.mkdir(parents=True)
    write_yaml(project, {"chat": {"providers": {"codex": {"sources": {"machine": {"data_root": "project-data"}}}}}})
    explicit = tmp_path / "explicit.yaml"
    write_yaml(explicit, {"chat": {"providers": {"codex": {"sources": {"machine": {"raw_root": "raw"}}}}}})
    resolution = resolve_app_config(explicit_path=explicit, cwd=work)
    config = resolution.config
    assert config.data_root == (project.parent / "project-data").absolute()
    assert config.raw_root == (tmp_path / "raw").absolute()
    assert config.state_root == Path.home() / ".tkn/genai_chat_note_pipeline/state/codex/machine"
    document = config_document(config)
    assert "raw_root" not in document and "state_root" not in document
    assert document["chat"]["providers"]["codex"]["sources"]["machine"]["raw_root"] == str(config.raw_root)
    assert resolution.sources["chat.providers.codex.sources.machine.data_root"].startswith("project:")
