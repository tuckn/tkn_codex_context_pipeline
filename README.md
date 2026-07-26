# Tkn Codex Context Pipeline

An independent, local-first pipeline that reads Codex app Project state and
`~/.codex/sessions` and generates durable Session Note v2 Markdown files.
It never writes markers, configuration, or context into a Project folder.

Japanese documentation: [README_ja.md](README_ja.md)

## Installation

Install Python 3.11 or later and uv, then install the CLI from the repository
root:

```powershell
uv tool install .
tkn-codex-context --help
```

## Configuration

Inspect the Codex app Projects and storage plan, then initialize the pipeline:

```powershell
tkn-codex-context init --dry-run
tkn-codex-context init
tkn-codex-context config show
```

`init` creates the global configuration and Project registry, then creates an
empty `sessions/` directory for each Project in the Codex app sidebar. It does
not generate Session Notes. The command records the current time as
`installed_at`. A normal run
automatically processes only chats created or updated at or after that time.
Older chats require an explicit `backfill` or `rebuild`.

To rebuild existing pipeline storage, inspect the destructive plan and then
force initialization. Model, path, and runtime settings are preserved;
`installed_at` is refreshed.

```powershell
tkn-codex-context init --force --dry-run
tkn-codex-context init --force
```

The application separates its own files by purpose:

```text
~/.tkn/codex_context_pipeline/
├── config.yaml
├── data/
│   ├── project-registry.jsonl
│   └── projects/
│       └── <projectId>/
│           └── sessions/
└── state/
    ├── projects/
    │   └── <projectId>/
    │       └── chat-refresh-state.json
    └── reports/

~/.cache/codex_context_pipeline/
└── resumable work cache
```

`data/` holds durable Project registry and Session Note data. `state/` holds
refresh checkpoints and reproducible history such as run
reports. Cache is kept separately under `~/.cache` by default;
`XDG_CACHE_HOME` is honored when it is set. Model input and output needed only
during execution use Python's platform temporary directory, normally `%TMP%`
on Windows and `/tmp` on Linux.

Configuration is merged in this order:

1. built-in defaults
2. `~/.tkn/codex_context_pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. `--config`
5. CLI options

Only `.tkn/config.example.yaml` is versioned. Do not commit real configuration.
Relative paths in a YAML layer are resolved relative to that YAML file.

## Normal operation

After initialization, synchronize added or changed Codex app Projects. Dry-run
does not change the registry, notes, refresh state, cache, or reports:

```powershell
tkn-codex-context projects sync --dry-run
tkn-codex-context session-notes run --dry-run
```

Then apply:

```powershell
tkn-codex-context projects sync
tkn-codex-context session-notes run
```

Normal runs process chats that have been idle for at least 30 minutes.
Historical processing is explicit:

```powershell
tkn-codex-context session-notes backfill --project-id <projectId> --dry-run
tkn-codex-context session-notes backfill --all
tkn-codex-context session-notes rebuild --project-id <projectId> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectId> --force
tkn-codex-context validate <session-note.md>
```

Every command emits a structured JSON result. `--verbose` adds progress logs;
`--quiet` limits logs while preserving JSON output.

## Scope

The first release generates session summaries only. Decisions, current working
context, and global context are intentionally out of scope.

Codex app Projects may contain one primary root and multiple secondary roots.
All configured roots are active and equal for chat attribution; secondary
roots are not treated as historical roots, and roots may belong to different
Git repositories.

## Project and thread attribution

`projectId` is the internal Project ID stored in the Codex app's
`local-projects` state. Project names and roots are mutable metadata and are
not used as identity. Two sidebar Projects remain distinct even when they use
the same root; the same internal ID remains one Project when its name or roots
change.

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

## Development

Sync the development dependencies, then run the tests, static checks, and
build:

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```
