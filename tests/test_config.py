from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tkn_codex_context.config import load_app_config
from tkn_codex_context.session_notes import PipelineError


def write_yaml(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_example_config_uses_readable_forward_slash_paths() -> None:
    example = Path(__file__).parents[1] / ".tkn" / "config.example.yaml"
    value = yaml.safe_load(example.read_text(encoding="utf-8"))

    for key in ("codex_home", "data_root", "state_root", "cache_root"):
        assert "\\" not in value[key]
        assert value[key].startswith("C:/")
    assert "summary_prompt" not in value


def test_precedence_and_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/codex_context_pipeline/config.yaml",
        {"idle_minutes": 10, "model": "global"},
    )
    write_yaml(cwd / ".tkn/config.yaml", {"idle_minutes": 20, "model": "local"})
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
