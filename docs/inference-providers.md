### Inference providers

The currently supported chat source is locally stored Codex conversation logs.
You can change the generative AI model used for inference through
`generation.active_provider` and the selected provider's `model` setting.
Set the selected provider's model and transport; model
availability and authentication belong to the chosen service.

| Provider ID | Required transport setting | Execution |
| --- | --- | --- |
| `codex` | `executable: codex` | Standalone `codex exec` |
| `claude-code` | `executable: claude` | Non-interactive Claude Code |
| `github-copilot` | `executable: copilot` | Non-interactive Copilot CLI |
| `ollama` | `base_url: http://127.0.0.1:11434` | Local chat endpoint, loopback addresses only |

For example, replace the generation block to use an already available local model:

```yaml
generation:
  active_provider: ollama
  providers:
    ollama:
      model: <installed-local-model>
      reasoning_effort: high
      base_url: http://127.0.0.1:11434
```

CLI providers send the selected generation input through their configured
service. Raw captures and provenance snapshots retain source content locally;
choose storage appropriate for private conversation data. Generation profiles,
output validation, and retry limits are application-owned. Changing a model,
provider, reasoning setting, or generation profile invalidates affected stages.
