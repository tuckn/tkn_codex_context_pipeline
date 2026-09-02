from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tkn_codex_context.config import (
    config_example_text,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from tkn_codex_context.thread_notes import PipelineError


def write_yaml(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_packaged_example_config_uses_portable_home_paths() -> None:
    value = yaml.safe_load(config_example_text())

    for key in ("codex_home", "data_root", "state_root", "cache_root"):
        assert "\\" not in value[key]
        assert value[key].startswith("~/")
    assert value["installed_at"] is None
    assert value["schema_version"] == 2
    assert value["generation"] == {
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
        home / ".tkn/codex_context_pipeline/config.yaml",
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
    write_yaml(explicit, {"idle_minutes": 25, "state_root": "state"})

    config = load_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"idle_minutes": 40},
    )

    assert config.idle_minutes == 40
    assert config.model == "local"
    assert config.state_root == (explicit.parent / "state").absolute()
    assert config.registry_path == config.data_root / "project-registry.jsonl"
    assert config.projects_data_root == config.data_root / "projects"
    assert config.projects_state_root == config.state_root / "projects"


def test_config_resolution_reports_the_winning_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/codex_context_pipeline/config.yaml",
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
    assert resolution.sources["schema_version"] == "built-in defaults"
    assert resolution.sources["generation.providers.codex.model"].startswith("project:")
    assert resolution.sources["idle_minutes"] == "CLI option"
    assert [layer["kind"] for layer in resolution.layers] == [
        "global",
        "project",
        "explicit",
    ]


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
    with pytest.raises(PipelineError, match="extra"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_schema_v1_flat_generation_settings_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"schema_version": 1, "provider": "codex", "model": "gpt-5.6-sol"})

    with pytest.raises(PipelineError, match="schema v1 is no longer supported"):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_active_provider_requires_matching_provider_settings(tmp_path: Path) -> None:
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
            "schema_version": 2,
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
    assert config.thread_note_pipeline_config(allow_missing_watermark=True).provider == provider


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
