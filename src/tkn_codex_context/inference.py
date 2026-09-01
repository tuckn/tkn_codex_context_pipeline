"""Structured inference backends for application-owned generation profiles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

InferenceProvider = Literal["codex", "claude-code", "github-copilot", "ollama"]

PROVIDER_NAMES: dict[str, str] = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "github-copilot": "GitHub Copilot",
    "ollama": "Ollama",
}


class InferenceConfig(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def codex_bin(self) -> str: ...

    @property
    def claude_bin(self) -> str: ...

    @property
    def copilot_bin(self) -> str: ...

    @property
    def ollama_base_url(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def reasoning_effort(self) -> str: ...


class InferenceExecutionError(RuntimeError):
    """A bounded, user-readable inference transport or output failure."""


def provider_name(provider: str) -> str:
    """Return the stable artifact-facing name for an inference provider."""

    try:
        return PROVIDER_NAMES[provider]
    except KeyError as exc:
        raise InferenceExecutionError(f"unsupported inference provider: {provider}") from exc


def is_supported_generator(value: str) -> bool:
    """Return whether an artifact generator name belongs to this application."""

    return value in PROVIDER_NAMES.values()


def validate_ollama_base_url(value: str) -> str:
    """Validate a credential-free loopback Ollama endpoint."""

    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("ollama_base_url must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ollama_base_url must not contain credentials, a query, or a fragment")
    if parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("ollama_base_url must use a loopback host")
    return normalized


def schema_grounded_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Add a strict JSON-only contract for providers without schema-native prompting."""

    return (
        f"{prompt.rstrip()}\n\n"
        "Return only one JSON object. Do not use Markdown fences or add commentary. "
        "The JSON object must match this JSON Schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n"
    )


def resolve_provider_executable(value: str, *, provider: str) -> str:
    """Resolve a configured CLI, including common Windows per-user install locations."""

    configured = value.strip()
    candidate = Path(configured).expanduser()
    if candidate.is_file():
        return str(candidate.absolute())
    discovered = shutil.which(configured)
    if discovered:
        return discovered
    if os.name != "nt" or configured.casefold() not in {provider, f"{provider}.exe"}:
        return configured
    local_app_data = os.getenv("LOCALAPPDATA")
    roaming_app_data = os.getenv("APPDATA")
    fallbacks: list[Path] = [Path.home() / ".local" / "bin" / f"{provider}.exe"]
    if provider == "copilot":
        if local_app_data:
            fallbacks.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "copilot.exe")
        if roaming_app_data:
            fallbacks.append(Path(roaming_app_data) / "npm" / "copilot.cmd")
    for fallback in fallbacks:
        if fallback.is_file():
            return str(fallback.absolute())
    return configured


def _parse_json_object(text: str, *, provider: str) -> dict[str, Any]:
    try:
        value = json.loads(text.lstrip("\ufeff").strip())
    except json.JSONDecodeError as exc:
        raise InferenceExecutionError(f"{provider} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InferenceExecutionError(f"{provider} output was not a JSON object")
    return value


def _run_process(
    command: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    provider: str,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise InferenceExecutionError(f"{provider} timed out after {exc.timeout} seconds") from exc
    except OSError as exc:
        raise InferenceExecutionError(f"cannot execute {provider}: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = (completed.stderr.strip() or completed.stdout.strip())[-2000:]
        raise InferenceExecutionError(
            f"{provider} exited with {completed.returncode}: {diagnostic or 'no diagnostic output'}"
        )
    return completed


def _invoke_codex(
    config: InferenceConfig,
    prompt: str,
    schema: dict[str, Any],
    *,
    cwd: Path,
    timeout: int,
) -> dict[str, Any]:
    schema_path = cwd / "schema.json"
    output_path = cwd / "output.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        config.codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        config.model,
        "-c",
        f'model_reasoning_effort="{config.reasoning_effort}"',
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    _run_process(command, prompt=prompt, cwd=cwd, timeout=timeout, provider="Codex")
    if not output_path.is_file():
        raise InferenceExecutionError("Codex completed without an output file")
    return _parse_json_object(output_path.read_text(encoding="utf-8-sig"), provider="Codex")


def _invoke_claude_code(
    config: InferenceConfig,
    prompt: str,
    schema: dict[str, Any],
    *,
    cwd: Path,
    timeout: int,
) -> dict[str, Any]:
    command = [
        resolve_provider_executable(config.claude_bin, provider="claude"),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "--model",
        config.model,
        "--effort",
        config.reasoning_effort,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
    ]
    completed = _run_process(
        command,
        prompt=prompt,
        cwd=cwd,
        timeout=timeout,
        provider="Claude Code",
    )
    envelope = _parse_json_object(completed.stdout, provider="Claude Code")
    output = envelope.get("structured_output")
    if not isinstance(output, dict):
        raise InferenceExecutionError("Claude Code completed without structured_output")
    return output


def _invoke_github_copilot(
    config: InferenceConfig,
    prompt: str,
    schema: dict[str, Any],
    *,
    cwd: Path,
    timeout: int,
) -> dict[str, Any]:
    command = [
        resolve_provider_executable(config.copilot_bin, provider="copilot"),
        "-s",
        "--model",
        config.model,
        "--effort",
        config.reasoning_effort,
        "--no-ask-user",
        "--no-color",
        "--no-auto-update",
        "--no-custom-instructions",
        "--disable-builtin-mcps",
        "--no-remote",
        "--no-remote-export",
        "--deny-tool=shell",
        "--deny-tool=write",
        "--deny-tool=read",
        "--deny-tool=url",
        "--deny-tool=memory",
    ]
    completed = _run_process(
        command,
        prompt=schema_grounded_prompt(prompt, schema),
        cwd=cwd,
        timeout=timeout,
        provider="GitHub Copilot",
    )
    return _parse_json_object(completed.stdout, provider="GitHub Copilot")


def _ollama_think(config: InferenceConfig) -> bool | str:
    if config.model.casefold().split(":", 1)[0] == "gpt-oss":
        return config.reasoning_effort
    return config.reasoning_effort != "low"


def _invoke_ollama(
    config: InferenceConfig,
    prompt: str,
    schema: dict[str, Any],
    *,
    timeout: int,
) -> dict[str, Any]:
    endpoint = f"{validate_ollama_base_url(config.ollama_base_url)}/api/chat"
    body = json.dumps(
        {
            "model": config.model,
            "messages": [{"role": "user", "content": schema_grounded_prompt(prompt, schema)}],
            "stream": False,
            "format": schema,
            "think": _ollama_think(config),
            "options": {"temperature": 0},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is loopback-validated
            payload = response.read()
    except HTTPError as exc:
        diagnostic = exc.read(2000).decode("utf-8", errors="replace").strip()
        raise InferenceExecutionError(
            f"Ollama returned HTTP {exc.code}: {diagnostic or exc.reason}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise InferenceExecutionError(f"cannot call Ollama at {endpoint}: {exc}") from exc
    envelope = _parse_json_object(payload.decode("utf-8-sig"), provider="Ollama")
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise InferenceExecutionError("Ollama response is missing message.content")
    return _parse_json_object(message["content"], provider="Ollama")


def invoke_structured(
    config: InferenceConfig,
    prompt: str,
    schema: dict[str, Any],
    *,
    cwd: Path,
    timeout: int,
) -> dict[str, Any]:
    """Run one provider call and return a JSON object matching the requested schema."""

    if config.provider == "codex":
        return _invoke_codex(config, prompt, schema, cwd=cwd, timeout=timeout)
    if config.provider == "claude-code":
        return _invoke_claude_code(config, prompt, schema, cwd=cwd, timeout=timeout)
    if config.provider == "github-copilot":
        return _invoke_github_copilot(config, prompt, schema, cwd=cwd, timeout=timeout)
    if config.provider == "ollama":
        return _invoke_ollama(config, prompt, schema, timeout=timeout)
    raise InferenceExecutionError(f"unsupported inference provider: {config.provider}")
