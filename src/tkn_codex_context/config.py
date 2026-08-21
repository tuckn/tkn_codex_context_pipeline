"""Strict layered YAML configuration for the standalone pipeline."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .thread_notes import (
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
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"


@dataclass(frozen=True)
class ConfigResolution:
    """Resolved configuration together with its inspectable provenance."""

    config: AppConfig
    sources: dict[str, str]
    layers: tuple[dict[str, Any], ...]


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

    def thread_note_pipeline_config(self, *, allow_missing_watermark: bool = False) -> PipelineConfig:
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
        expanded_text = os.path.expandvars(str(raw))
        if expanded_text == "~":
            expanded = Path.home()
        elif expanded_text.startswith(("~/", "~\\")):
            expanded = Path.home() / expanded_text[2:]
        else:
            expanded = Path(expanded_text).expanduser()
        result[key] = expanded if expanded.is_absolute() else (base / expanded).absolute()
    return result


def _without_retired_user_prompt(
    value: dict[str, Any],
    *,
    remove_configured: bool,
) -> tuple[dict[str, Any], bool]:
    result = dict(value)
    if "summary_prompt" not in result:
        return result, False
    configured = result.pop("summary_prompt")
    if configured is not None and not remove_configured:
        raise PipelineError(
            "summary_prompt is no longer supported; remove it from config because "
            "summary profiles are application-owned"
        )
    return result, True


def resolve_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> ConfigResolution:
    """Load defaults, global, project, explicit, then CLI overrides."""

    working = (cwd or Path.cwd()).absolute()
    merged: dict[str, Any] = {}
    sources = {name: "built-in defaults" for name in AppConfig.model_fields}
    layer_specs = [
        ("global", global_config_path()),
        ("project", project_config_path(working)),
        (
            "explicit",
            explicit_path.expanduser().absolute() if explicit_path else None,
        ),
    ]
    layer_report: list[dict[str, Any]] = []
    for kind, path in layer_specs:
        if path is not None:
            layer_report.append(
                {
                    "kind": kind,
                    "path": str(path),
                    "exists": path.is_file(),
                }
            )
        if path is not None and path.is_file():
            layer, _removed = _without_retired_user_prompt(
                _read_layer(path),
                remove_configured=False,
            )
            resolved_layer = _resolve_paths(layer, path.parent)
            merged.update(resolved_layer)
            for key in resolved_layer:
                sources[key] = f"{kind}: {path}"
    resolved_overrides = _resolve_paths(overrides or {}, working)
    merged.update(resolved_overrides)
    for key in resolved_overrides:
        sources[key] = "CLI option"
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    resolved = config.model_copy(
        update={
            "codex_home": config.codex_home.expanduser().absolute(),
            "data_root": config.data_root.expanduser().absolute(),
            "state_root": config.state_root.expanduser().absolute(),
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )
    return ConfigResolution(
        config=resolved,
        sources=sources,
        layers=tuple(layer_report),
    )


def load_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load and return the effective application configuration."""

    return resolve_app_config(
        explicit_path=explicit_path,
        cwd=cwd,
        overrides=overrides,
    ).config


def config_example_text() -> str:
    """Read the application-owned example distributed in the package."""

    try:
        return files("tkn_codex_context").joinpath(CONFIG_EXAMPLE_RESOURCE).read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, OSError) as exc:
        raise PipelineError(f"packaged config example is unavailable: {exc}") from exc


def initialize_user_config(
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create the user config safely from the packaged example."""

    target = (path or global_config_path()).expanduser().absolute()
    example = config_example_text()
    expected = example.encode("utf-8")
    backup_path: Path | None = None

    if target.exists() and not target.is_file():
        raise PipelineError(f"config target is not a file: {target}")
    if target.is_file():
        try:
            current = target.read_bytes()
        except OSError as exc:
            raise PipelineError(f"cannot read config: {target}: {exc}") from exc
        if current == expected:
            return {
                "status": "unchanged",
                "configPath": str(target),
                "backupPath": None,
            }
        if not force:
            raise PipelineError(
                f"config already exists with different content: {target}; "
                "review it or use `config init --force` to back it up and replace it"
            )
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        backup_path = target.with_name(f"{target.name}.{stamp}.bak")
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            raise PipelineError(f"cannot back up config: {target}: {exc}") from exc

    try:
        atomic_write_text(target, example)
    except OSError as exc:
        raise PipelineError(f"cannot write config: {target}: {exc}") from exc
    return {
        "status": "replaced" if backup_path else "created",
        "configPath": str(target),
        "backupPath": str(backup_path) if backup_path else None,
    }


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
    if not target.is_file():
        raise PipelineError(
            f"config not found: {target}; run `tkn-codex-context config init` first"
        )
    raw: dict[str, Any] = {}
    removed: list[str] = []
    if target.is_file():
        raw = _read_layer(target)
        raw, removed_summary_prompt = _without_retired_user_prompt(
            raw,
            remove_configured=True,
        )
        if removed_summary_prompt:
            removed.append("summary_prompt")
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
