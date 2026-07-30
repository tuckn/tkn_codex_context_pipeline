"""Strict layered YAML configuration for the standalone pipeline."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .session_notes import (
    DEFAULT_IDLE_MINUTES,
    DEFAULT_MODEL,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_RUNTIME_MINUTES,
    DEFAULT_SOURCE_ID,
    PipelineConfig,
    PipelineError,
    atomic_write_text,
)

CONFIG_SCHEMA_VERSION = 1
APP_DIRECTORY_NAME = "codex_context_pipeline"


def default_app_root() -> Path:
    return Path.home() / ".tkn" / APP_DIRECTORY_NAME


def default_user_cache_root() -> Path:
    configured = os.getenv("XDG_CACHE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base / APP_DIRECTORY_NAME


class AppConfig(BaseModel):
    """Resolved application configuration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    installed_at: datetime | None = None
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    data_root: Path = Field(default_factory=lambda: default_app_root() / "data")
    state_root: Path = Field(default_factory=lambda: default_app_root() / "state")
    cache_root: Path = Field(default_factory=default_user_cache_root)
    provider: Literal["codex"] = "codex"
    codex_executable: str = "codex"
    model: str = DEFAULT_MODEL
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=0)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)
    source_id: str = DEFAULT_SOURCE_ID
    summary_prompt: Path | None = None

    @field_validator("model")
    @classmethod
    def require_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_id must not be empty")
        return value.strip()

    @property
    def sessions_root(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def app_state_path(self) -> Path:
        return self.codex_home / ".codex-global-state.json"

    @property
    def registry_path(self) -> Path:
        return self.data_root / "project-registry.jsonl"

    @property
    def projects_data_root(self) -> Path:
        return self.data_root / "projects"

    @property
    def projects_state_root(self) -> Path:
        return self.state_root / "projects"

    @property
    def reports_root(self) -> Path:
        return self.state_root / "reports"

    def session_pipeline_config(self, *, allow_missing_watermark: bool = False) -> PipelineConfig:
        installed_at = self.installed_at
        if installed_at is None:
            if not allow_missing_watermark:
                raise PipelineError("installed_at is missing; run `tkn-codex-context init` first")
            installed_at = datetime.now().astimezone()
        return PipelineConfig(
            installed_at=installed_at.astimezone().isoformat(timespec="seconds"),
            sessions_root=self.sessions_root,
            source_id=self.source_id,
            codex_bin=self.codex_executable,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            idle_minutes=self.idle_minutes,
            runtime_minutes=self.runtime_minutes,
            model_timeout_seconds=self.model_timeout_seconds,
            summary_prompt=self.summary_prompt,
        )


def global_config_path() -> Path:
    return default_app_root() / "config.yaml"


def project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / ".tkn" / "config.yaml"


def _read_layer(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, yaml.YAMLError) as exc:
        raise PipelineError(f"cannot read config: {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PipelineError(f"config must contain a YAML mapping: {path}")
    return {str(key): item for key, item in value.items()}


def _resolve_paths(value: dict[str, Any], base: Path) -> dict[str, Any]:
    result = dict(value)
    for key in ("codex_home", "data_root", "state_root", "cache_root"):
        raw = result.get(key)
        if raw is None:
            continue
        expanded = Path(os.path.expandvars(str(raw))).expanduser()
        result[key] = expanded if expanded.is_absolute() else (base / expanded).absolute()
    raw_prompt = result.get("summary_prompt")
    if raw_prompt is not None:
        prompt_text = os.path.expandvars(str(raw_prompt))
        prompt_path = Path(prompt_text).expanduser()
        if prompt_path.is_absolute():
            result["summary_prompt"] = prompt_path
        elif Path(prompt_text).name == prompt_text:
            result["summary_prompt"] = default_app_root() / "prompts" / prompt_path
        else:
            raise PipelineError(
                "summary_prompt must be a filename in the user prompts directory "
                "or an absolute path"
            )
    return result


def load_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load defaults, global, project, explicit, then CLI overrides."""

    working = (cwd or Path.cwd()).absolute()
    merged: dict[str, Any] = {}
    layers = [
        global_config_path(),
        project_config_path(working),
        explicit_path.expanduser().absolute() if explicit_path else None,
    ]
    for path in layers:
        if path is not None and path.is_file():
            merged.update(_resolve_paths(_read_layer(path), path.parent))
    merged.update(_resolve_paths(overrides or {}, working))
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    return config.model_copy(
        update={
            "codex_home": config.codex_home.expanduser().absolute(),
            "data_root": config.data_root.expanduser().absolute(),
            "state_root": config.state_root.expanduser().absolute(),
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )


def config_document(config: AppConfig) -> dict[str, Any]:
    value = config.model_dump(mode="json")
    value["installed_at"] = (
        config.installed_at.astimezone().isoformat(timespec="seconds") if config.installed_at else None
    )
    return value


def initialization_config(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> tuple[AppConfig, Path, tuple[str, ...]]:
    """Load the target config for init, tolerating only retired init-owned keys."""

    target = (path or global_config_path()).expanduser().absolute()
    raw: dict[str, Any] = {}
    removed: list[str] = []
    if target.is_file():
        raw = _read_layer(target)
        for key in ("context_store_root",):
            if key in raw:
                raw.pop(key)
                removed.append(key)
        raw = _resolve_paths(raw, target.parent)
    raw.update(_resolve_paths(overrides or {}, Path.cwd().absolute()))
    raw["installed_at"] = datetime.now().astimezone()
    try:
        config = AppConfig.model_validate(raw)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    return (
        config.model_copy(
            update={
                "codex_home": config.codex_home.expanduser().absolute(),
                "data_root": config.data_root.expanduser().absolute(),
                "state_root": config.state_root.expanduser().absolute(),
                "cache_root": config.cache_root.expanduser().absolute(),
            }
        ),
        target,
        tuple(removed),
    )


def write_config(config: AppConfig, target: Path) -> None:
    text = yaml.safe_dump(
        config_document(config),
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(target, text)
