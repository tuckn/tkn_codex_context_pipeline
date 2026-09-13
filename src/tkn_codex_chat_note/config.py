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
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

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

CONFIG_SCHEMA_VERSION: Literal["7.0.0"] = "7.0.0"
_CONFIG_SCHEMA_VERSION_PARTS = (7, 0, 0)
_CONFIG_SCHEMA_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
APP_DIRECTORY_NAME = "codex_chat_note_pipeline"
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

    session_note_profile: Literal["default-jp", "default-en"] = "default-jp"
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


def validate_source_id(value: str) -> str:
    """Preserve exact identity while requiring a portable path component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError(
            "source_id must use only ASCII letters, digits, dot, underscore, or hyphen; "
            "start with a letter or digit; lowercase kebab-case is recommended"
        )
    if value.endswith(".") or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value.split(".")[0]):
        raise ValueError("source_id must not end with a dot or use a Windows reserved device name")
    return value


SourceId = Annotated[str, AfterValidator(validate_source_id)]


class CodexSourceConfig(BaseModel):
    """One local Codex input and its owned output roots; the map key is its ID."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    source_root: Path = Field(default_factory=lambda: Path.home() / ".codex")
    include_archived: bool = True
    raw_root: Path | None = None
    data_root: Path | None = None
    state_root: Path | None = None

    @field_validator("source_root", "raw_root", "data_root", "state_root", mode="before")
    @classmethod
    def require_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("source paths must not be empty")
        return value


class AppConfig(BaseModel):
    """Codex acquisition sources and independent inference backend selection."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["7.0.0"] = CONFIG_SCHEMA_VERSION
    installed_at: datetime | None = None
    sources: dict[SourceId, CodexSourceConfig] = Field(default_factory=lambda: {DEFAULT_SOURCE_ID: CodexSourceConfig()})
    cache_root: Path = Field(default_factory=default_user_cache_root)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    idle_minutes: int = Field(default=DEFAULT_IDLE_MINUTES, ge=0)
    runtime_minutes: int = Field(default=DEFAULT_RUNTIME_MINUTES, gt=0)
    model_timeout_seconds: int = Field(default=DEFAULT_MODEL_TIMEOUT_SECONDS, gt=0)

    _selected_source: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def unique_source_ids(self) -> Self:
        if len({name.casefold() for name in self.sources}) != len(self.sources):
            raise ValueError("source_id keys must be unique ignoring case")
        return self

    def for_source(self, source_id: str) -> Self:
        if source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        selected = self.model_copy()
        selected._selected_source = source_id
        return selected

    def enabled_source_configs(self, source_id: str | None = None) -> list[Self]:
        if self._selected_source is not None:
            if source_id is not None and source_id != self._selected_source:
                raise PipelineError(f"unknown source_id: {source_id}")
            source_id = self._selected_source
        if source_id is not None and source_id not in self.sources:
            raise PipelineError(f"unknown source_id: {source_id}")
        enabled = [
            self.for_source(name)
            for name, source in self.sources.items()
            if source.enabled and (source_id is None or name == source_id)
        ]
        if not enabled:
            raise PipelineError("no enabled Codex source selected; set sources.<source_id>.enabled to true")
        return enabled

    @property
    def source_identity(self) -> tuple[str, str]:
        # Published storage/evidence identities retain the acquisition application.
        return ("codex", self.source_id)

    @property
    def source_settings(self) -> CodexSourceConfig:
        return self.sources[self.source_id]

    @property
    def source_root(self) -> Path:
        return self.source_settings.source_root

    @property
    def codex_home(self) -> Path:
        """Internal adapter convenience; the public configuration uses source_root."""
        return self.source_root

    @property
    def source_id(self) -> str:
        if self._selected_source is not None:
            return self._selected_source
        names = [name for name, source in self.sources.items() if source.enabled]
        if len(names) != 1:
            raise PipelineError("select exactly one enabled source with --source <source_id>")
        return names[0]

    @property
    def source_provider(self) -> str:
        return "codex"

    def source_storage_paths(self, source_id: str | None = None) -> dict[str, Path]:
        source_id = source_id if source_id is not None else self.source_id
        settings = self.sources[source_id]
        paths = {
            kind: (getattr(settings, kind + "_root") or default_app_root() / kind / "codex" / source_id)
            .expanduser()
            .absolute()
            for kind in ("raw", "data", "state")
        }
        paths["cache"] = self.cache_root.expanduser().absolute() / "codex" / source_id
        return paths

    @property
    def raw_root(self) -> Path:
        return self.source_storage_paths()["raw"]

    @property
    def data_root(self) -> Path:
        return self.source_storage_paths()["data"]

    @property
    def state_root(self) -> Path:
        return self.source_storage_paths()["state"]

    @property
    def source_raw_root(self) -> Path:
        return self.raw_root

    @property
    def source_data_root(self) -> Path:
        return self.data_root

    @property
    def source_state_root(self) -> Path:
        return self.state_root

    @property
    def source_cache_root(self) -> Path:
        return self.source_storage_paths()["cache"]

    @property
    def include_archived(self) -> bool:
        return self.source_settings.include_archived

    def require_supported_chat_sources(self) -> None:
        self.enabled_source_configs()

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
                raise PipelineError("installed_at is missing; run `tkn-codex-chat-note init` first")
            installed_at = datetime.now().astimezone()
        return PipelineConfig(
            installed_at=installed_at.astimezone().isoformat(timespec="seconds"),
            sessions_root=self.sessions_root,
            raw_root=self.raw_root,
            source_id=self.source_id,
            session_note_profile=self.generation.session_note_profile,
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


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML has no bundled type stubs.
    """Reject duplicate YAML keys instead of silently discarding a source."""


def _unique_mapping(loader: _UniqueKeyLoader, node: Any) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise PipelineError(f"duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _read_layer(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)
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
        if key == "sources" and value == {}:
            target[key] = {}
        elif isinstance(current, dict) and isinstance(value, dict):
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
    document = _without_null_provider_settings(AppConfig().model_dump(mode="python", by_alias=True))
    # Defer source defaults until validation: an explicit map must not acquire a
    # hidden built-in source. Later layers merge declared sources by stable ID.
    document.pop("sources")
    return document


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
    if raw_version == 2 or isinstance(raw_version, str) and re.fullmatch(r"[234]\.[0-9]+\.[0-9]+", raw_version):
        raise PipelineError(
            "legacy configuration requires a new schema-7 config with source-local roots; "
            "use `storage migrate --from-config <old-config> --dry-run` with the new --config; "
            "the old configuration and data are not modified"
        )

    if isinstance(raw_version, str) and re.fullmatch(r"[56]\.[0-9]+\.[0-9]+", raw_version):
        raise PipelineError(
            "configuration schema 5/6 requires an explicit config-only update: "
            "move chat.providers.codex.sources to top-level sources (for schema 5, "
            "key the Codex entry by its source_id and rename home to source_root); "
            'remove chat and set schema_version to "7.0.0"; retain IDs and final raw/data/state roots, '
            "including old default paths; storage 5 needs no data migration. "
            "Generation providers remain under generation.providers"
        )
    if not isinstance(raw_version, str) or not _CONFIG_SCHEMA_VERSION_PATTERN.fullmatch(raw_version):
        raise PipelineError(
            f"invalid configuration schema_version {raw_version!r}: {path}; "
            f'expected a quoted MAJOR.MINOR.PATCH value such as "{CONFIG_SCHEMA_VERSION}"'
        )
    parts = tuple(int(part) for part in raw_version.split("."))
    current_major, current_minor, _current_patch = _CONFIG_SCHEMA_VERSION_PARTS
    major, minor, _patch = parts
    if major != current_major:
        direction = "newer" if major > current_major else "older"
        action = (
            "upgrade tkn-codex-chat-note"
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
            "upgrade tkn-codex-chat-note"
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
    source_map = result.get("sources")
    if isinstance(source_map, dict):
        if len({str(key).casefold() for key in source_map}) != len(source_map):
            raise PipelineError("source_id keys must be unique ignoring case")
        for settings in source_map.values():
            if isinstance(settings, dict):
                for key in ("source_root", "raw_root", "data_root", "state_root"):
                    if settings.get(key) is not None:
                        if isinstance(settings[key], str) and not settings[key].strip():
                            raise PipelineError("source paths must not be empty")
                        settings[key] = resolve(settings[key])
    chat = result.get("chat")
    providers = chat.get("providers") if isinstance(chat, dict) else None
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict):
                # Legacy standalone migration readers still resolve home here.
                entries = provider.get("sources", {"legacy": provider})
                if not isinstance(entries, dict):
                    continue
                if "sources" in provider and len({str(key).casefold() for key in entries}) != len(entries):
                    raise PipelineError("source_id keys must be unique ignoring case within a provider")
                for settings in entries.values():
                    if not isinstance(settings, dict):
                        continue
                    for key in ("source_root", "home", "raw_root", "data_root", "state_root"):
                        if settings.get(key) is not None:
                            if isinstance(settings[key], str) and not settings[key].strip():
                                raise PipelineError("chat source paths must not be empty")
                            settings[key] = resolve(settings[key])
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
    legacy_global = Path.home() / ".tkn" / "genai_chat_note_pipeline" / "config.yaml"
    if (
        not global_config_path().exists()
        and legacy_global.is_file()
        and explicit_path is None
        and not project_config_path(working).exists()
    ):
        raise PipelineError(
            f"legacy user configuration found: {legacy_global}; copy it to {global_config_path()} "
            "and update to schema 7, preserving source IDs and final storage paths, "
            "or pass an explicit schema-7 --config; no legacy config or data is modified"
        )
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
            "cache_root": config.cache_root.expanduser().absolute(),
        }
    )
    effective_leaves = _leaf_paths(_config_properties(resolved.model_dump(mode="python", by_alias=True)))
    sources = {key: sources.get(key, "built-in defaults") for key in effective_leaves}
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
        return files("tkn_codex_chat_note").joinpath(CONFIG_EXAMPLE_RESOURCE).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        raise PipelineError(f"packaged config example is unavailable: {exc}") from exc


def initialize_user_config(
    path: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create the user config safely from the packaged example."""

    target = (path or global_config_path()).expanduser().absolute()
    legacy_global = Path.home() / ".tkn" / "genai_chat_note_pipeline" / "config.yaml"
    if path is None and not target.exists() and legacy_global.is_file():
        raise PipelineError(
            f"legacy user configuration found: {legacy_global}; copy it to {target} "
            "and update to schema 7 with top-level sources and existing final storage paths; "
            "use --config <new-path> config init only to create a separate configuration"
        )
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
    document = _without_null_provider_settings(config.model_dump(mode="json", by_alias=True))
    for source_id, settings in document["sources"].items():
        for kind, path in config.source_storage_paths(source_id).items():
            if kind != "cache":
                settings[kind + "_root"] = str(path)
    return document


def initialization_config(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    refresh_installed_at: bool = True,
) -> tuple[AppConfig, Path, tuple[str, ...]]:
    """Load the target config for init, tolerating only retired init-owned keys."""

    target = (path or global_config_path()).expanduser().absolute()
    if not target.is_file():
        raise PipelineError(f"config not found: {target}; run `tkn-codex-chat-note config init` first")
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
