# Tkn Codex Context Pipeline

An independent, local-first pipeline that reads Codex app Project state and
`~/.codex/sessions` and generates durable Session Note v2 Markdown files.
It never writes markers, configuration, or context into a Project folder.

Japanese documentation: [README_ja.md](README_ja.md)

## Scope

The first release generates session summaries only. Decisions, current working
context, and global context are intentionally out of scope. The existing
context store remains the destination:

```text
~/.tkn/codex-context/state/<projectId>/sessions/
```

Codex app Projects may contain one primary root and multiple secondary roots.
All configured roots are active and equal for chat attribution; secondary
roots are not treated as historical roots, and roots may belong to different
Git repositories.

## Install and develop

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

Install the CLI as a uv tool:

```powershell
uv tool install .
tkn-codex-context --help
```

## Configuration

Create the global config and watermark:

```powershell
tkn-codex-context config init
tkn-codex-context config show
```

Configuration is merged in this order:

1. built-in defaults
2. `~/.tkn/codex-context-pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. `--config`
5. CLI options

Only `.tkn/config.example.yaml` is versioned. Do not commit real configuration.
Relative paths in a YAML layer are resolved relative to that YAML file.

## Safe first run

Inspect Project bindings and eligible chats without changing the registry,
notes, refresh state, cache, or reports:

```powershell
tkn-codex-context projects sync --dry-run
tkn-codex-context session-notes run --dry-run
```

Then apply:

```powershell
tkn-codex-context projects sync
tkn-codex-context session-notes run
```

Normal runs process only chats created or updated after `installed_at` and idle
for at least 30 minutes. Historical processing is explicit:

```powershell
tkn-codex-context session-notes backfill --project-id <projectId> --dry-run
tkn-codex-context session-notes backfill --all
tkn-codex-context session-notes rebuild --project-id <projectId> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectId> --force
tkn-codex-context validate <session-note.md>
```

Every command emits a structured JSON result. `--verbose` adds progress logs;
`--quiet` limits logs while preserving JSON output.

## Project and thread attribution

The registry's existing `projectId` is authoritative. Initial Codex app
binding uses, in order, a saved source binding, a unique exact root match, a
unique exact Project-name match, or a deterministic new context Project.
Conflicts remain pending and produce no notes.

Thread attribution uses an explicit Codex app assignment first, then a unique
cwd match against all active roots, then saved historical aliases.
Projectless or ambiguous threads are excluded and reported.

## Internal-format boundary

The adapter depends on Codex app's private `.codex-global-state.json` and local
JSONL session formats. These are not public compatibility contracts. The
reader validates required structure and fails closed when a format is missing,
damaged, or incompatible; it does not guess Project identity. Source JSONL
files are always read-only.

Generation uses an ephemeral Codex CLI process with a read-only sandbox and
structured output. Source and generator fingerprints make unchanged threads a
no-op. Notes and refresh state are written atomically only after validation;
normal and rebuild work is cached and resumable after an interrupted run.
