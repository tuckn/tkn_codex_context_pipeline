from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tkn_codex_context.config import load_app_config
from tkn_codex_context.session_notes import PipelineError


def write_yaml(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")


def test_precedence_and_relative_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    explicit = tmp_path / "explicit" / "config.yaml"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    write_yaml(
        home / ".tkn/codex-context-pipeline/config.yaml",
        {"idle_minutes": 10, "model": "global"},
    )
    write_yaml(cwd / ".tkn/config.yaml", {"idle_minutes": 20, "model": "local"})
    write_yaml(explicit, {"idle_minutes": 25, "pipeline_root": "runtime"})

    config = load_app_config(
        explicit_path=explicit,
        cwd=cwd,
        overrides={"idle_minutes": 40},
    )

    assert config.idle_minutes == 40
    assert config.model == "local"
    assert config.pipeline_root == (explicit.parent / "runtime").absolute()


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_yaml(path, {"unknown_setting": True})
    with pytest.raises(PipelineError, match="extra"):
        load_app_config(explicit_path=path, cwd=tmp_path)
