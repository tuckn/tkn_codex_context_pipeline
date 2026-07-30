# Tkn Codex Context Pipeline

An independent, local-first pipeline that reads Codex app Project state and
`~/.codex/sessions` and generates durable Session Note v2 Markdown files.
It never writes markers, configuration, or context into a Project folder.

Japanese documentation: [README_ja.md](README_ja.md)

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- `codex` on `PATH` for summary generation

## Installation

The default is an editable installation so repository source changes are
reflected without reinstalling the CLI:

```console
uv tool install -e "C:\path\to\tkn_codex_context_pipeline"
tkn-codex-context --help
```

Replace the example path with the actual repository folder. Because the path
is explicit, the command can be run from any working directory. With an
editable installation, Python source changes from `git pull` are immediately
used by `tkn-codex-context`.

To replace an existing installation with an editable installation:

```console
uv tool install -e "C:\path\to\tkn_codex_context_pipeline" --force
```

Run the command again after changing dependencies, package metadata, or entry
points, or after moving the repository to another folder.

To use a non-editable installation:

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline" --force
```

A non-editable installation does not follow later repository changes. Run the
same installation command again after `git pull` to update the installed CLI.

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
`installed_at`. A normal pull
automatically processes only chats created or updated at or after that time.
Older chats require an explicit `pull --backfill` or `rebuild`.

To rebuild existing pipeline storage, inspect the destructive plan and then
force initialization. Model, path, and runtime settings are preserved;
`installed_at` is refreshed.

```powershell
tkn-codex-context init --force --dry-run
tkn-codex-context init --force
```

List the registered Projects when you need to map a Project name or current
root back to its internal ID:

```powershell
tkn-codex-context projects list
tkn-codex-context projects list --json
```

The default output is a human-readable table containing status, name, internal
Project ID, and current root. `--json` also includes the registered root
metadata for scripts and more detailed inspection.

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

### Fetch Project metadata

After initialization, fetch added or changed Project metadata from the Codex
app. This is a one-way update from the Codex app into the local registry.
Dry-run does not change the registry or Project directories:

```powershell
tkn-codex-context projects fetch --dry-run
```

Then apply:

```powershell
tkn-codex-context projects fetch
```

### Reading Project fetch results

`projectFetch.projects` in `projects fetch` and `session-notes pull` contains
the following fields for each Project currently present in the Codex app:

| Field | Meaning |
| --- | --- |
| `sourceProjectId` | Internal Project ID read from the Codex app |
| `projectId` | Project ID used by the registry and storage folder; it is always identical to `sourceProjectId` in the current design |
| `name` | Current Project name shown by the Codex app |
| `status` | Whether this fetch associated the Codex app Project with a registry record |
| `method` | How the registry record was selected |
| `roots` | Currently active Codex app roots; the first is Primary and the remainder are Secondary |

`projectFetch.projects[*].status` currently has one possible value:

- `bound`: the Codex app internal ID was associated with a registry
  `projectId`. This is used both when reusing an existing record and when
  creating a new record.

A Project missing from the Codex app does not appear in
`projectFetch.projects`. Use `projects list` to inspect all saved Projects.
Its `status` values mean:

- `active`: a Project with the same internal ID existed in the Codex app at
  the most recent fetch.
- `inactive`: the Project is no longer present in the Codex app. Its registry
  record, Session Notes, and state are retained. The same ID becomes `active`
  again if it returns.
- `unknown`: a nonstandard registry record has no status. Records created by
  normal `init` or `projects fetch` do not use this value.

`method` has these possible values:

- `project-id`: an existing registry record with the same internal Project ID
  was found, and its name and root metadata were refreshed.
- `new`: no record had the same internal Project ID, so a new registry record
  was created. With `--dry-run`, this means the record is planned but has not
  been written.

In `projects list --json`, `roots[*].status` is `active` for a current root and
`historical` for a previous root retained as an attribution alias.

### Generate Session Notes

`session-notes pull` pulls Codex chats eligible for normal processing and
creates or updates Session Notes. It automatically fetches Project metadata
before scanning, so its output contains both `projectFetch` and `report`.

Inspect the selection with dry-run first. Dry-run does not call the generative
AI and does not change the registry, Session Notes, refresh state, cache, or
run reports.

```powershell
tkn-codex-context session-notes pull --dry-run
```

The main `report` fields mean:

| Field | Meaning |
| --- | --- |
| `reportPath` | Saved run report; `null` in dry-run because no report is written |
| `mode` | `daily` for a normal pull or `backfill` for explicit historical processing |
| `force` | Whether unchanged fingerprints and generation conditions are forcibly regenerated |
| `scan.files` | Number of Codex JSONL files read |
| `scan.eligible` | Number of create or update candidates after fingerprint checks |
| `scan.unchanged` | Previously processed sources whose source and generation conditions are unchanged |
| `scan.staleGenerator` | Sources unchanged but selected for regeneration because generation conditions changed |
| `scan.ignoredFiles` | Files excluded by the date window, idle requirement, internal-chat filters, or attribution checks |
| `selectedCount` | Number of Session Notes planned for creation or update after applying `--limit` |
| `selected` | Projects, threads, and sources selected by dry-run |
| `processed` | Session Notes successfully created or updated by a non-dry-run pull; always empty in dry-run |
| `failed` | Threads that failed during a non-dry-run pull |
| `deferred` | Threads postponed because the runtime limit was reached |

If dry-run reports `selectedCount: 0` and `selected: []`, no Session Note is
planned for creation or update. `reportPath: null` and `processed: []` are
normal dry-run behavior and do not indicate an error.

`scan.ignoredFiles` is the total number of excluded files; the later,
specialized counters are not a complete breakdown. Files rejected early by
the date window or idle requirement increment only `ignoredFiles`. It is
therefore not an error when `ignoredFiles` equals `scan.files` while the other
exclusion counters remain zero.

After review, generate the notes:

```powershell
tkn-codex-context session-notes pull
```

Normal pulls process chats that have been idle for at least 30 minutes.

#### Existing Session Note update behavior

An existing Session Note is reported as `scan.unchanged` and skipped when its
source fingerprint, schema, model, reasoning effort, prompt version, and
renderer version all match the current conditions. The generative AI is not
called, and neither the Session Note nor state is modified.

A changed source, an older schema, or different generation conditions such as
the model automatically make the note eligible for creation or update. A
schema newer than the current implementation stops with an error rather than
being overwritten.

Use `--force` to regenerate notes even when all conditions match:

```powershell
tkn-codex-context session-notes pull --force --dry-run
tkn-codex-context session-notes pull --force
```

Normal `pull --force` covers chats at or after `installed_at`. To force the
entire history, run the historical and normal windows separately:

```powershell
tkn-codex-context session-notes pull --backfill --all --force --dry-run
tkn-codex-context session-notes pull --backfill --all --force
tkn-codex-context session-notes pull --force
```

#### Backfill historical chats

`pull --backfill` processes chats from before `installed_at`. It uses the same
fingerprint, schema, and model checks as a normal pull, so unchanged current
notes are skipped. Dry-run does not call the generative AI or write files.

```powershell
tkn-codex-context session-notes pull --backfill --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context session-notes pull --backfill --all --dry-run
tkn-codex-context session-notes pull --backfill --all
```

`--backfill` requires either `--project-id` or `--all` to prevent accidental
full-history processing. `--project-id` and `--all` are valid only with
`--backfill`. `--project-id` accepts an internal Project ID, exact current
Project Name, or CURRENT ROOT. Resolution order is exact ID, exact current
Name, then CURRENT ROOT. Root comparison normalizes Windows path case, `/`
versus `\`, and trailing separators. If a Name or CURRENT ROOT matches more
than one active Project, the command stops and lists the matching Project IDs.

#### Rebuild one Project

`rebuild` re-evaluates every chat attributed to one Project, regardless of
`installed_at` or the idle threshold, and reconstructs its Session Note
directory and refresh state as a consistent set. Existing notes and state
remain in place if generation or staged validation fails.

Every numeric schema version older than the current schema is regenerated.
Notes already using the current schema with matching source and generation
conditions are reused. A newer schema stops as unsupported. `--force`
regenerates every eligible note, including current unchanged notes.

```powershell
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot>
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot> --force
```

#### Validate one Session Note

`validate` checks one Session Note against the current schema, required
frontmatter, source thread/ref and fingerprint, required headings, and status
consistency between frontmatter and the body. It does not modify the file or
call the generative AI.

```powershell
tkn-codex-context validate <session-note.md>
```

Commands emit structured JSON results except for the human-readable default of
`projects list`; use `projects list --json` when machine-readable output is
needed.

Progress logs go to standard error by default, while the final JSON result stays
on standard output. Interactive runs therefore show messages such as
`[info] Starting thread 1/7: ...`, while scripts can safely pipe or capture
standard output. Logging uses only Python's standard-library `logging` module
and adds no logging dependency.

- `-q` / `--quiet`: suppress progress logs and show errors only.
- `-v` / `--verbose`: include `[debug]` diagnostics and raw progress events.

```powershell
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot>
tkn-codex-context -q session-notes rebuild --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context -v session-notes pull
```

## Scope

The first release generates session summaries only. Decisions, current working
context, and global context are intentionally out of scope.

Codex app Projects may contain one primary root and multiple secondary roots.
All configured roots are active and equal for chat attribution; secondary
roots are not treated as historical roots, and roots may belong to different
Git repositories.

## Project and thread attribution

Stored paths, reports, and `projectId` values use the internal Project ID from
the Codex app's `local-projects` state. When `--project-id` receives a Name or
CURRENT ROOT, that value is used only to resolve the CLI input; the resolved
internal ID is still used for storage. Project names and roots are mutable
metadata and are not used as identity. Two sidebar Projects remain distinct
even when they use the same root; the same internal ID remains one Project when
its name or roots change.

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
