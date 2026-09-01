from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tkn_codex_context.inference import (
    InferenceExecutionError,
    invoke_structured,
    provider_name,
    resolve_provider_executable,
    validate_ollama_base_url,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def inference_config(provider: str, **overrides: str) -> SimpleNamespace:
    values = {
        "provider": provider,
        "codex_bin": "codex",
        "claude_bin": "claude",
        "copilot_bin": "copilot",
        "ollama_base_url": "http://127.0.0.1:11434",
        "model": "test-model",
        "reasoning_effort": "high",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_provider_names_and_local_ollama_boundary() -> None:
    assert provider_name("codex") == "Codex"
    assert provider_name("claude-code") == "Claude Code"
    assert provider_name("github-copilot") == "GitHub Copilot"
    assert provider_name("ollama") == "Ollama"
    assert validate_ollama_base_url("http://localhost:11434/") == "http://localhost:11434"
    with pytest.raises(ValueError, match="loopback"):
        validate_ollama_base_url("https://ollama.example.com")
    with pytest.raises(InferenceExecutionError, match="unsupported"):
        provider_name("unknown")


def test_explicit_provider_executable_path_is_preserved(tmp_path: Path) -> None:
    executable = tmp_path / "provider.exe"
    executable.write_bytes(b"test")

    assert resolve_provider_executable(str(executable), provider="provider") == str(
        executable.absolute()
    )


def test_codex_uses_native_output_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"answer":"codex"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("tkn_codex_context.inference.subprocess.run", fake_run)
    result = invoke_structured(
        inference_config("codex"),
        "prompt",
        SCHEMA,
        cwd=tmp_path,
        timeout=30,
    )

    assert result == {"answer": "codex"}
    assert "--output-schema" in captured["command"]
    assert captured["input"] == "prompt"


def test_claude_code_uses_structured_output_and_disables_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"structured_output": {"answer": "claude"}}),
            stderr="",
        )

    monkeypatch.setattr("tkn_codex_context.inference.subprocess.run", fake_run)
    result = invoke_structured(
        inference_config("claude-code"),
        "prompt",
        SCHEMA,
        cwd=tmp_path,
        timeout=30,
    )

    assert result == {"answer": "claude"}
    assert "--json-schema" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert captured["input"] == "prompt"


def test_github_copilot_uses_noninteractive_stdout_and_schema_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout='{"answer":"copilot"}', stderr="")

    monkeypatch.setattr("tkn_codex_context.inference.subprocess.run", fake_run)
    result = invoke_structured(
        inference_config("github-copilot"),
        "prompt",
        SCHEMA,
        cwd=tmp_path,
        timeout=30,
    )

    assert result == {"answer": "copilot"}
    assert "-s" in captured["command"]
    assert "--no-ask-user" in captured["command"]
    assert "--deny-tool=shell" in captured["command"]
    assert "Return only one JSON object" in captured["input"]


def test_ollama_uses_local_chat_api_with_json_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"message": {"role": "assistant", "content": '{"answer":"ollama"}'}}
            ).encode("utf-8")

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("tkn_codex_context.inference.urlopen", fake_urlopen)
    result = invoke_structured(
        inference_config("ollama", model="qwen3.5:9b"),
        "prompt",
        SCHEMA,
        cwd=tmp_path,
        timeout=30,
    )

    assert result == {"answer": "ollama"}
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["body"]["format"] == SCHEMA
    assert captured["body"]["stream"] is False
    assert captured["body"]["think"] is True
