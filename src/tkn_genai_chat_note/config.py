"""Strict layered YAML configuration for the standalone pipeline."""

from __future__ import annotations

import os
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config_validation import validate_config_layer
from .inference import InferenceProvider, validate_ollama_base_url
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

CONFIG_SCHEMA_VERSION: Literal["4.0.0"] = "4.0.0"
_CONFIG_SCHEMA_VERSION_PARTS = (4, 0, 0)
_CONFIG_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
APP_DIRECTORY_NAME = "genai_chat_note_pipeline"
CONFIG_EXAMPLE_RESOURCE = "resources/config.example.yaml"
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
LEGACY_GENERATION_KEYS = frozenset(
    {
        "provider",
        "model",
        "reasoning_effort",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    }
)
PROVIDER_TRANSPORT_DEFAULTS: dict[InferenceProvider, tuple[str, str]] = {
    "codex": ("executable", "codex"),
    "claude-code": ("executable", "claude"),
    "github-copilot": ("executable", "copilot"),
    "ollama": ("base_url", "http://127.0.0.1:11434"),
}


@dataclass(frozen=True)
class ConfigResolution:
    """Resolved configuration together with its inspectable provenance."""

    config: AppConfig
    sources: dict[str, str]
    layers: tuple[dict[str, Any], ...]
    effective_schema_version: str
    has_in_memory_migrations: bool


def default_app_root() -> Path:
    return Path.home() / ".tkn" / APP_DIRECTORY_NAME


def default_user_cache_root() -> Path:
    configured = os.getenv("XDG_CACHE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return base / APP_DIRECTORY_NAME


class ProviderConfig(BaseModel):
    """Configuration for one inference provider."""

    model_config = ConfigDict(extra="forbid")

    model: str
    reasoning_effort: ReasoningEffort = "high"
    executable: str | None = None
    base_url: str | None = None

    @field_validator("model")
    @classmethod
    def require_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be empty")
        return value.strip()

    @field_validator("executable")
    @classmethod
    def require_executable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("provider executable must not be empty")
        return value.strip()


def _default_providers() -> dict[InferenceProvider, ProviderConfig]:
    return {
        "codex": ProviderConfig(
            model=DEFAULT_MODEL,
            reasoning_effort="high",
            executable="codex",
        )
    }


class GenerationConfig(BaseModel):
    """Active inference provider and provider-specific generation settings."""

    model_config = ConfigDict(extra="forbid")

    active_provider: InferenceProvider = "codex"
    providers: dict[InferenceProvider, ProviderConfig] = Field(default_factory=_default_providers)

    @model_validator(mode="after")
    def validate_provider_settings(self) -> Self:
        if self.active_provider not in self.providers:
            raise ValueError(f"active_provider {self.active_provider!r} must have a matching entry under providers")
        for provider, settings in self.providers.items():
            if provider == "ollama":
                if settings.executable is not None:
                    raise ValueError("generation.providers.ollama does not support executable")
                if settings.base_url is None:
                    raise ValueError("generation.providers.ollama.base_url is required")
                settings.base_url = validate_ollama_base_url(settings.base_url)
                continue
            if settings.base_url is not None:
                raise ValueError(f"generation.providers.{provider}.base_url is not supported")
            if settings.executable is None:
                raise ValueError(f"generation.providers.{provider}.executable is required")
        return self


class ChatSourceConfig(BaseModel):
    """A source installation, independent of the inference backend."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    home: Path
    source_id: str

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", normalized):
            raise ValueError("source_id must use only letters, digits, dot, underscore, or hyphen")
        return normalized

    @field_validator("home", mode="before")
    @classmethod
    def require_home(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("home must not be empty")
        return value


class CodexChatSourceConfig(ChatSourceConfig):
    enabled: bool = True
    home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    source_id: str = DEFAULT_SOURCE_ID
    include_archived: bool = True


class ChatProvidersConfig(BaseModel):
    """Provider-specific fields; future adapters are configured but disabled."""

    model_config = ConfigDict(extra="forbid")

    codex: CodexChatSourceConfig = Field(default_factory=CodexChatSourceConfig)
    claude_code: ChatSourceConfig = Field(
        alias="claude-code",
        default_factory=lambda: ChatSourceConfig(
            home=Path.home() / ".claude", source_id="windows-claude-code"
        ),
    )
    github_copilot: ChatSourceConfig = Field(
        alias="github-copilot",
        default_factory=lambda: ChatSourceConfig(
            home=Path.home() / ".copilot", source_id="windows-github-copilot"
        ),
    )

    def entries(self) -> dict[str, ChatSourceConfig]:
        return {"codex": self.codex, "claude-code": self.claude_code, "github-copilot": self.github_copilot}



class ChatConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: ChatProvidersConfig = Field(default_factory=ChatProvidersConfig)


class AppConfig(BaseModel):
    """Resolved application configuration and inference backend selection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["4.0.0"] = CONFIG_SCHEMA_VERSION
    installed_at: datetime | None = None
    chat: ChatConfig = Field(default_factory=ChatConfig)
    raw_root: Path = Field(default_factory=lambda: default_app_root() / "raw")
    data_root: Path = Field(default_factory=lambda: default_app_root() / "data")
    state_root: Path = Field(default_factory=lambda: default_app_root() / "state")
    cache_root: Path = Field(default_factory=default_user_cache_root)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=0)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)

    @property
    def codex_home(self) -> Path:
        return self.chat.providers.codex.home

    @property
    def source_id(self) -> str:
        return self.chat.providers.codex.source_id

    @property
    def source_provider(self) -> str:
        return "codex"

    def source_storage_paths(self, provider: str) -> dict[str, Path]:
        settings = self.chat.providers.entries()[provider]
        return {
            kind: root / provider / settings.source_id
            for kind, root in (
                ("raw", self.raw_root), ("data", self.data_root),
                ("state", self.state_root), ("cache", self.cache_root),
            )
        }

    @property
    def source_raw_root(self) -> Path:
        return self.source_storage_paths(self.source_provider)["raw"]

    @property
    def source_data_root(self) -> Path:
        return self.source_storage_paths(self.source_provider)["data"]

    @property
    def source_state_root(self) -> Path:
        return self.source_storage_paths(self.source_provider)["state"]

    @property
    def source_cache_root(self) -> Path:
        return self.source_storage_paths(self.source_provider)["cache"]

    @property
    def include_archived(self) -> bool:
        return self.chat.providers.codex.include_archived

    def require_supported_chat_sources(self) -> None:
        unsupported = [
            provider for provider, settings in self.chat.providers.entries().items()
            if settings.enabled and provider != "codex"
        ]
        if unsupported:
            raise PipelineError(
                "chat acquisition is not implemented for " + ", ".join(unsupported)
                + "; set chat.providers.<provider>.enabled to false; generation providers are independent"
            )
        if not self.chat.providers.codex.enabled:
            raise PipelineError("no supported chat source is enabled; set chat.providers.codex.enabled to true")

    @property
    def sessions_root(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def app_state_path(self) -> Path:
        return self.codex_home / ".codex-global-state.json"

    @property
    def registry_path(self) -> Path:
        return self.source_data_root / "project-registry.jsonl"

    @property
    def projects_data_root(self) -> Path:
        return self.source_data_root / "projects"

    @property
    def projects_state_root(self) -> Path:
        return self.source_state_root / "projects"

    @property
    def reports_root(self) -> Path:
        return self.source_state_root / "reports"

    @property
    def provider(self) -> InferenceProvider:
        return self.generation.active_provider

    @property
    def active_provider_config(self) -> ProviderConfig:
        return self.generation.providers[self.provider]

    @property
    def model(self) -> str:
        return self.active_provider_config.model

    @property
    def reasoning_effort(self) -> ReasoningEffort:
        return self.active_provider_config.reasoning_effort

    def _provider_executable(self, provider: InferenceProvider, default: str) -> str:
        settings = self.generation.providers.get(provider)
        return settings.executable if settings is not None and settings.executable is not None else default

    @property
    def codex_executable(self) -> str:
        return self._provider_executable("codex", "codex")

    @property
    def claude_executable(self) -> str:
        return self._provider_executable("claude-code", "claude")

    @property
    def copilot_executable(self) -> str:
        return self._provider_executable("github-copilot", "copilot")

    @property
    def ollama_base_url(self) -> str:
        settings = self.generation.providers.get("ollama")
        if settings is not None and settings.base_url is not None:
            return settings.base_url
        return "http://127.0.0.1:11434"

    def session_note_pipeline_config(self, *, allow_missing_watermark: bool = False) -> PipelineConfig:
        installed_at = self.installed_at
        if installed_at is None:
            if not allow_missing_watermark:
                raise PipelineError("installed_at is missing; run `tkn-genai-chat-note init` first")
            installed_at = datetime.now().astimezone()
        return PipelineConfig(
            installed_at=installed_at.astimezone().isoformat(timespec="seconds"),
            sessions_root=self.sessions_root,
            raw_root=self.raw_root,
            source_id=self.source_id,
            provider=self.provider,
            codex_bin=self.codex_executable,
            claude_bin=self.claude_executable,
            copilot_bin=self.copilot_executable,
            ollama_base_url=self.ollama_base_url,
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


def _leaf_paths(value: Any, prefix: str = "") -> tuple[str, ...]:
    if isinstance(value, dict) and value:
        return tuple(
            child
            for key, item in value.items()
            for child in _leaf_paths(item, f"{prefix}.{key}" if prefix else str(key))
        )
    return (prefix,)


def _mark_sources(sources: dict[str, str], value: dict[str, Any], label: str) -> None:
    for path in _leaf_paths(value):
        if path:
            sources[path] = label


def _deep_merge(target: dict[str, Any], update: dict[str, Any]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            _deep_merge(current, value)
        else:
            target[key] = value


def _without_null_provider_settings(value: dict[str, Any]) -> dict[str, Any]:
    generation = value.get("generation")
    providers = generation.get("providers") if isinstance(generation, dict) else None
    if isinstance(providers, dict):
        for settings in providers.values():
            if isinstance(settings, dict):
                for key in tuple(settings):
                    if settings[key] is None:
                        settings.pop(key)
    return value


def _default_config_document() -> dict[str, Any]:
    return _without_null_provider_settings(AppConfig().model_dump(mode="python", by_alias=True))


def _reject_legacy_generation_config(value: dict[str, Any], path: Path) -> None:
    legacy_keys = sorted(LEGACY_GENERATION_KEYS.intersection(value))
    schema_version = value.get("schema_version")
    schema_v1 = schema_version == 1 or (isinstance(schema_version, str) and schema_version.partition(".")[0] == "1")
    if schema_v1 or legacy_keys:
        detail = f"; retired keys: {', '.join(legacy_keys)}" if legacy_keys else ""
        raise PipelineError(
            f"configuration schema v1 is no longer supported: {path}{detail}; "
            f'set schema_version: "{CONFIG_SCHEMA_VERSION}" and move generation settings under '
            "generation.active_provider and generation.providers"
        )


def _inspect_config_schema(value: dict[str, Any], path: Path) -> dict[str, Any]:
    if "schema_version" not in value:
        raise PipelineError(
            f'configuration schema_version is required: {path}; set schema_version: "{CONFIG_SCHEMA_VERSION}"'
        )
    raw_version = value["schema_version"]
    if (
        type(raw_version) is int
        and raw_version == 2
        or isinstance(raw_version, str)
        and re.fullmatch(r"2\.[0-2]\.[0-9]+", raw_version)
    ):
        if value.get("scopes"):
            raise PipelineError(
                "scopes moved to tkn-genai-context-curation; move their definitions before using this config"
            )
        return {
            "schemaVersion": raw_version,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
            "migration": {
                "kind": "repository-split",
                "fromVersion": raw_version,
                "toVersion": CONFIG_SCHEMA_VERSION,
                "persistentConfigUpdated": False,
            },
        }

    if not isinstance(raw_version, str) or not _CONFIG_SCHEMA_VERSION_PATTERN.fullmatch(raw_version):
        raise PipelineError(
            f"invalid configuration schema_version {raw_version!r}: {path}; "
            f'expected a quoted MAJOR.MINOR.PATCH value such as "{CONFIG_SCHEMA_VERSION}"'
        )
    parts = tuple(int(part) for part in raw_version.split("."))
    if parts[0] == 3 and parts[1] == 0:
        return {
            "schemaVersion": raw_version,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
            "migration": {
                "kind": "chat-provider-hierarchy",
                "fromVersion": raw_version,
                "toVersion": CONFIG_SCHEMA_VERSION,
                "persistentConfigUpdated": False,
            },
        }
    current_major, current_minor, _current_patch = _CONFIG_SCHEMA_VERSION_PARTS
    major, minor, _patch = parts
    if major != current_major:
        direction = "newer" if major > current_major else "older"
        action = (
            "upgrade tkn-genai-chat-note"
            if major > current_major
            else "migrate the configuration explicitly; no migration path is available"
        )
        raise PipelineError(
            f"unsupported {direction} configuration schema_version {raw_version!r}: {path}; "
            f"this application supports schema versions through {CONFIG_SCHEMA_VERSION} "
            f"within major {current_major}; {action}"
        )
    if minor > current_minor:
        raise PipelineError(
            f"unsupported newer configuration schema_version {raw_version!r}: {path}; "
            f"this application supports schema versions through {CONFIG_SCHEMA_VERSION}; "
            "upgrade tkn-genai-chat-note"
        )
    migration: dict[str, Any] | None = None
    if minor < current_minor:
        migration = {
            "kind": "compatible-version-normalization",
            "fromVersion": raw_version,
            "toVersion": CONFIG_SCHEMA_VERSION,
            "persistentConfigUpdated": False,
        }
    return {
        "schemaVersion": raw_version,
        "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
        "migration": migration,
    }


def _config_properties(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("schema_version", None)
    major = str(value.get("schema_version", "")).split(".")[0]
    if major in {"2", "3"}:
        if "chat" in result:
            raise PipelineError('chat requires schema_version: "4.0.0"; do not mix legacy and nested chat settings')
        codex = {}
        for old, new in (("codex_home", "home"), ("source_id", "source_id"), ("include_archived", "include_archived")):
            if old in result:
                codex[new] = result.pop(old)
        if codex:
            result["chat"] = {"providers": {"codex": codex}}
    elif any(key in result for key in ("codex_home", "source_id", "include_archived")):
        raise PipelineError("move codex_home, source_id, and include_archived under chat.providers.codex (home)")
    if major == "2" and result.get("scopes") == {}:
        result.pop("scopes")
    return result


def _resolve_paths(value: dict[str, Any], base: Path) -> dict[str, Any]:
    result = deepcopy(value)

    def resolve(raw: Any) -> Path:
        expanded_text = os.path.expandvars(str(raw))
        if expanded_text == "~":
            expanded = Path.home()
        elif expanded_text.startswith(("~/", "~\\")):
            expanded = Path.home() / expanded_text[2:]
        else:
            expanded = Path(expanded_text).expanduser()
        return expanded if expanded.is_absolute() else (base / expanded).absolute()

    for key in ("raw_root", "data_root", "state_root", "cache_root"):
        if result.get(key) is not None:
            result[key] = resolve(result[key])
    chat = result.get("chat")
    providers = chat.get("providers") if isinstance(chat, dict) else None
    if isinstance(providers, dict):
        for settings in providers.values():
            if isinstance(settings, dict) and settings.get("home") is not None:
                if isinstance(settings["home"], str) and not settings["home"].strip():
                    raise PipelineError("chat provider home must not be empty")
                settings["home"] = resolve(settings["home"])
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


def _new_provider_override(provider: InferenceProvider, model: str, effort: str | None) -> dict[str, Any]:
    transport_key, transport_value = PROVIDER_TRANSPORT_DEFAULTS[provider]
    return {
        "model": model,
        "reasoning_effort": effort or "high",
        transport_key: transport_value,
    }


def _apply_runtime_overrides(
    merged: dict[str, Any],
    overrides: dict[str, Any],
    *,
    working: Path,
    sources: dict[str, str],
) -> None:
    if "schema_version" in overrides:
        raise PipelineError("schema_version is configuration-source metadata and cannot be a CLI override")
    resolved = _resolve_paths(overrides, working)
    generation_options = {key: resolved.pop(key) for key in tuple(resolved) if key in LEGACY_GENERATION_KEYS}
    _deep_merge(merged, resolved)
    _mark_sources(sources, resolved, "CLI option")
    if not generation_options:
        return

    generation = merged.get("generation")
    if not isinstance(generation, dict):
        raise PipelineError("generation must be a YAML mapping")
    providers = generation.get("providers")
    if not isinstance(providers, dict):
        raise PipelineError("generation.providers must be a YAML mapping")

    requested_provider = generation_options.get("provider")
    active_provider = str(requested_provider or generation.get("active_provider", "codex"))
    if active_provider not in PROVIDER_TRANSPORT_DEFAULTS:
        raise PipelineError(f"unsupported inference provider: {active_provider}")
    if requested_provider is not None:
        generation["active_provider"] = active_provider
        sources["generation.active_provider"] = "CLI option"

    provider_settings = providers.get(active_provider)
    model_override = generation_options.get("model")
    effort_override = generation_options.get("reasoning_effort")
    if provider_settings is None:
        if model_override is None:
            raise PipelineError(
                f"provider {active_provider!r} is not configured under generation.providers; "
                "add its settings or pass --model"
            )
        provider_settings = _new_provider_override(
            active_provider,
            str(model_override),
            str(effort_override) if effort_override is not None else None,
        )
        providers[active_provider] = provider_settings
        _mark_sources(
            sources,
            {"generation": {"providers": {active_provider: provider_settings}}},
            "CLI option",
        )
    elif not isinstance(provider_settings, dict):
        raise PipelineError(f"generation.providers.{active_provider} must be a YAML mapping")

    if model_override is not None:
        provider_settings["model"] = model_override
        sources[f"generation.providers.{active_provider}.model"] = "CLI option"
    if effort_override is not None:
        provider_settings["reasoning_effort"] = effort_override
        sources[f"generation.providers.{active_provider}.reasoning_effort"] = "CLI option"

    transport_options = {
        "codex_executable": ("codex", "executable"),
        "claude_executable": ("claude-code", "executable"),
        "copilot_executable": ("github-copilot", "executable"),
        "ollama_base_url": ("ollama", "base_url"),
    }
    for option, (provider, setting) in transport_options.items():
        if option not in generation_options:
            continue
        target = providers.get(provider)
        if not isinstance(target, dict):
            raise PipelineError(
                f"provider {provider!r} is not configured under generation.providers; "
                f"select it with --provider and supply --model before using --{option.replace('_', '-')}"
            )
        target[setting] = generation_options[option]
        sources[f"generation.providers.{provider}.{setting}"] = "CLI option"


def resolve_app_config(
    *,
    explicit_path: Path | None = None,
    cwd: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> ConfigResolution:
    """Load defaults, global, project, explicit, then CLI overrides."""

    working = (cwd or Path.cwd()).absolute()
    merged = _default_config_document()
    sources: dict[str, str] = {}
    _mark_sources(sources, _config_properties(merged), "built-in defaults")
    layer_specs = [
        ("global", global_config_path()),
        ("project", project_config_path(working)),
        (
            "explicit",
            explicit_path.expanduser().absolute() if explicit_path else None,
        ),
    ]
    layer_report: list[dict[str, Any]] = [
        {
            "kind": "built-in",
            "path": None,
            "exists": True,
            "schemaVersion": CONFIG_SCHEMA_VERSION,
            "effectiveSchemaVersion": CONFIG_SCHEMA_VERSION,
            "migration": None,
        }
    ]
    for kind, path in layer_specs:
        if path is None:
            continue
        report: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "exists": path.is_file(),
            "schemaVersion": None,
            "effectiveSchemaVersion": None,
            "migration": None,
        }
        layer_report.append(report)
        if kind == "explicit" and not report["exists"]:
            raise PipelineError(f"explicit configuration file not found: {path}")
        if report["exists"]:
            raw_layer = _read_layer(path)
            _reject_legacy_generation_config(raw_layer, path)
            schema_report = _inspect_config_schema(raw_layer, path)
            report.update(schema_report)
            layer, _removed = _without_retired_user_prompt(
                _config_properties(raw_layer),
                remove_configured=False,
            )
            try:
                validate_config_layer(AppConfig, layer)
            except ValueError as exc:
                raise PipelineError(f"invalid configuration layer {path}: {exc}") from exc
            resolved_layer = _resolve_paths(layer, path.parent)
            _deep_merge(merged, resolved_layer)
            _mark_sources(sources, resolved_layer, f"{kind}: {path}")
    _apply_runtime_overrides(
        merged,
        overrides or {},
        working=working,
        sources=sources,
    )
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    resolved = config.model_copy(
        update={
            "raw_root": config.raw_root.expanduser().absolute(),
            "data_root": config.data_root.expanduser().absolute(),
            "state_root": config.state_root.expanduser().absolute(),
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )
    return ConfigResolution(
        config=resolved,
        sources=sources,
        layers=tuple(layer_report),
        effective_schema_version=CONFIG_SCHEMA_VERSION,
        has_in_memory_migrations=any(layer["migration"] is not None for layer in layer_report),
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
        return files("tkn_genai_chat_note").joinpath(CONFIG_EXAMPLE_RESOURCE).read_text(encoding="utf-8")
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
    value = _without_null_provider_settings(config.model_dump(mode="json", by_alias=True))
    value["installed_at"] = (
        config.installed_at.astimezone().isoformat(timespec="seconds") if config.installed_at else None
    )
    if config.installed_at is None:
        value.pop("installed_at", None)
    return value


def initialization_config(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    refresh_installed_at: bool = True,
) -> tuple[AppConfig, Path, tuple[str, ...]]:
    """Load the target config for init, tolerating only retired init-owned keys."""

    target = (path or global_config_path()).expanduser().absolute()
    if not target.is_file():
        raise PipelineError(f"config not found: {target}; run `tkn-genai-chat-note config init` first")
    raw: dict[str, Any] = {}
    removed: list[str] = []
    if target.is_file():
        raw = _read_layer(target)
        _reject_legacy_generation_config(raw, target)
        _inspect_config_schema(raw, target)
        raw = _config_properties(raw)
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
    merged = _default_config_document()
    _deep_merge(merged, raw)
    _apply_runtime_overrides(
        merged,
        overrides or {},
        working=Path.cwd().absolute(),
        sources={},
    )
    if refresh_installed_at:
        merged["installed_at"] = datetime.now().astimezone()
    try:
        config = AppConfig.model_validate(merged)
    except Exception as exc:
        raise PipelineError(f"invalid configuration: {exc}") from exc
    return (
        config.model_copy(
            update={
                "raw_root": config.raw_root.expanduser().absolute(),
                "data_root": config.data_root.expanduser().absolute(),
                "state_root": config.state_root.expanduser().absolute(),
                "cache_root": config.cache_root.expanduser().absolute(),
            }
        ),
        target,
        tuple(removed),
    )


def write_config(config: AppConfig, target: Path) -> None:
    document = config_document(config)
    schema_version = document.pop("schema_version")
    text = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(target, f'schema_version: "{schema_version}"\n{text}')
