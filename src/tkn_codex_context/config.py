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


class AppConfig(BaseModel):
    """Resolved application configuration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    installed_at: datetime | None = None
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    context_store_root: Path = Field(default_factory=lambda: Path.home() / ".tkn" / "codex-context")
    pipeline_root: Path = Field(default_factory=lambda: Path.home() / ".tkn" / "codex-context-pipeline")
    provider: Literal["codex"] = "codex"
    codex_executable: str = "codex"
    model: str = DEFAULT_MODEL
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] = "high"
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=0)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)
    source_id: str = DEFAULT_SOURCE_ID

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
        return self.context_store_root / "state" / "index.jsonl"

    @property
    def cache_root(self) -> Path:
        return self.pipeline_root / "cache"

    @property
    def reports_root(self) -> Path:
        return self.pipeline_root / "reports"

    def session_pipeline_config(self, *, allow_missing_watermark: bool = False) -> PipelineConfig:
        installed_at = self.installed_at
        if installed_at is None:
            if not allow_missing_watermark:
                raise PipelineError("installed_at is missing; run `config init` first")
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
        )


def global_config_path() -> Path:
    return Path.home() / ".tkn" / "codex-context-pipeline" / "config.yaml"


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
    for key in ("codex_home", "context_store_root", "pipeline_root"):
        raw = result.get(key)
        if raw is None:
            continue
        expanded = Path(os.path.expandvars(str(raw))).expanduser()
        result[key] = expanded if expanded.is_absolute() else (base / expanded).absolute()
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
            "context_store_root": config.context_store_root.expanduser().absolute(),
            "pipeline_root": config.pipeline_root.expanduser().absolute(),
        }
    )


def config_document(config: AppConfig) -> dict[str, Any]:
    value = config.model_dump(mode="json")
    value["installed_at"] = (
        config.installed_at.astimezone().isoformat(timespec="seconds") if config.installed_at else None
    )
    return value


def init_config(path: Path | None = None, *, dry_run: bool = False) -> tuple[AppConfig, Path]:
    target = (path or global_config_path()).expanduser().absolute()
    if target.exists():
        raise PipelineError(f"config already exists: {target}")
    config = AppConfig(installed_at=datetime.now().astimezone())
    if not dry_run:
        text = yaml.safe_dump(
            config_document(config),
            allow_unicode=True,
            sort_keys=False,
        )
        atomic_write_text(target, text)
    return config, target
