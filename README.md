# Tkn Codex Context Pipeline

An independent, local-first pipeline that reads Codex app Project state and
`~/.codex/sessions`, generates durable Session Note v2 Markdown files, and
distills durable Decision Record v2 files from those notes.
It never writes markers, configuration, or context into a Project folder.

Japanese documentation: [README_ja.md](README_ja.md)

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- `codex` on `PATH` for Session Note or Decision Record generation
  - For Windows, install using the following command: `powershell -ExecutionPolicy Bypass -c "irm https://chatgpt.com/codex/install.ps1 | iex"`

## Installation

Install the repository with the following command. Replace `C:\path\to\tkn_codex_context_pipeline` with the actual path to this repository.

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline"
tkn-codex-context --help
```

The second command confirms that `tkn-codex-context` can be run after installation. This installation uses the code as it existed when the command was run and does not automatically track later repository changes.

Reinstall after every repository update, such as after `git pull`, to make the updated code and dependencies available to the installed command:

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline"
tkn-codex-context --help
```

For development, an editable installation can be used instead:

```console
uv tool install -e "C:\path\to\tkn_codex_context_pipeline"
```

The `-e` (`--editable`) option makes the installed command reference the repository source code directly, so source-code edits take effect without reinstallation. If an update changes dependencies in `pyproject.toml`, package metadata, or entry points, run the same editable installation command again to update the tool environment.

If a normal reinstall fails, the installed command still uses stale dependencies, or the tool environment or entry point is damaged, recreate the tool environment with `--force`:

```console
uv tool install "C:\path\to\tkn_codex_context_pipeline" --force
tkn-codex-context --help
```

To repair an editable installation while preserving editable mode, add `-e` to the forced installation command.

## Configuration

Inspect the Codex app Projects and storage plan, then initialize the pipeline:

```powershell
tkn-codex-context init --dry-run
tkn-codex-context init
tkn-codex-context config show
```

`init` creates the global configuration and Project registry, then creates empty
`sessions/` and `decisions/` directories for each Project in the Codex app
sidebar. It does not generate Session Notes or Decision Records. The command records the current time as
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
│           ├── sessions/
│           └── decisions/
└── state/
    ├── projects/
    │   └── <projectId>/
    │       ├── chat-refresh-state.json
    │       └── decision-build-state.json
    └── reports/

~/.cache/codex_context_pipeline/
└── resumable work cache
```

`data/` holds durable Project registry, Session Note, and Decision Record data. `state/` holds
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
Session Note and Decision Record generation profiles are application-owned and
have no user configuration key.
Legacy `summary_prompt: null` is ignored; remove any non-null
`summary_prompt` entry before running the current CLI.

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
before scanning. Its default JSON output is compact: `projectFetchSummary` and
`reportSummary` contain booleans and counts, and `reportPath` points to the
saved full run report. Use `--full-output` only when the complete per-Project
and per-thread detail is needed on standard output.
Generated notes use `type: summary`; the directory and command names remain
`sessions` and `session-notes` for compatibility with the Project context
layout.

Inspect the selection with dry-run first. Dry-run does not call the generative
AI and does not change the registry, Session Notes, refresh state, cache, or
run reports.

```powershell
tkn-codex-context session-notes pull --dry-run
```

The main compact output fields mean:

| Field | Meaning |
| --- | --- |
| `ok` | Whether the run completed without failed threads or pending Project bindings |
| `reportPath` | Saved run report; `null` in dry-run because no report is written |
| `projectFetchSummary.projectCount` | Number of Projects returned by the Codex app |
| `projectFetchSummary.boundCount` | Number of Projects with a usable local root binding |
| `projectFetchSummary.newCount` | Number of newly discovered Projects |
| `projectFetchSummary.pendingCount` | Number of Projects still requiring a root binding |
| `reportSummary.mode` | `daily` for a normal pull, `backfill` for explicit historical processing, or `rebuild` |
| `reportSummary.selectedCount` | Number of Session Notes planned after applying `--limit` |
| `reportSummary.processedCount` | Number of Session Notes successfully created or updated |
| `reportSummary.failedCount` | Number of failed threads |
| `reportSummary.deferredCount` | Number of threads postponed by the runtime limit |
| `reportSummary.warningCount` | Number of run warnings |
| `reportSummary.scan.*` | Numeric scan counters such as files, eligible, unchanged, and ignored files |

The default output intentionally omits large `projects`, `selected`,
`processed`, and error-detail arrays. For a non-dry-run command, inspect the
file at `reportPath` for those details. Dry-run does not write a report, so use
`--full-output` when the exact selected Projects, threads, and sources must be
reviewed:

```powershell
tkn-codex-context session-notes pull --dry-run --full-output
```

If dry-run reports `reportSummary.selectedCount: 0`, no Session Note is planned
for creation or update. `reportPath: null` and
`reportSummary.processedCount: 0` are normal dry-run behavior.

`reportSummary.scan.ignoredFiles` is the total number of excluded files; the later,
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
source fingerprint, artifact schema, model, reasoning effort, summary prompt,
output schema, Markdown template, generator prompt envelope, and renderer
version all match the current conditions. The generative AI is not called, and
neither the Session Note nor state is modified.

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

### Generate Decision Records

`decisions build` uses the stored Session Note v2 files for one Project as its
primary input. The normal path does not reread the original Codex chat. Only
Session Notes with an `Explicit Decision` section are candidates. Multiple
notes are sent in bounded synthesis batches, and the output unit is a central
decision rather than a Session Note. When several notes establish, refine, or
verify the same decision, one record lists all supporting `sourceSessionRefs`.
The model also receives an index of existing Decision Records so the same
decision can reference an existing ID instead of creating a duplicate.

Start with the read-only plan. `decisions build` is read-only by default: it
does not call the generative AI or change the registry, Session Notes, Decision
Records, state, or run reports.

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot>
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --full-output
```

`reportSummary.selectedCount` is the number of selected Session Notes,
`synthesisBatchCount` is the number of model batches, `createdCount` is the
number of new records, `updatedCount` is the number of resynthesized unreviewed
records, and `referencedExistingCount` is
the number of links to existing records. Use `--full-output` to inspect the
individual selected Session Notes during planning.

After review, explicitly enable generation and writes:

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --write
```

New records are stored under
`data/projects/<projectId>/decisions/DR-NNNN-<slug>.md`. Each record contains
Context, Decision, Rationale, Consequences, Applicability, Verification,
Materialization, and Supersession sections. A decision is `Accepted` only when
the source establishes explicit user acceptance or an operational practice
that is already in effect; otherwise it is `Proposed`. Decision status and
implementation/verification status remain separate fields. Incomplete or
blocked verification is retained under `Verification` as `Limitations`.

Decision generation does not modify its input Session Notes. The Decision
Record keeps the forward dependency in `sourceSessionRefs`, while
`decision-build-state.json` owns per-source processing state and the reverse
`decisionIds` index. Run reports expose the current mapping as `decisionRefs`.
A no-action result is also state-only, leaving the same Session Note available
for later working-context distillation.

An unchanged Session Note with the same decision profile is skipped. Use
`--force` together with the explicit write flag to re-evaluate it. A matching
decision links to the existing ID. An unreviewed Codex-generated record may be
resynthesized in place when combined sources materially correct or improve it,
while preserving its ID and original date. Reviewed records do not have their
central judgment rewritten automatically; only new `sourceSessionRefs` and
`Related Evidence` are appended as provenance.

```powershell
tkn-codex-context decisions build --project-id <projectIdOrNameOrRoot> --write --force
```

Validate one generated Decision Record v2 without changing it:

```powershell
tkn-codex-context decisions validate <decision-record.md>
```

Commands emit structured JSON results except for the human-readable default of
`projects list`; use `projects list --json` when machine-readable output is
needed. Session Note and Decision Record commands emit a compact summary by default and save the
complete non-dry-run report at `reportPath`; add `--full-output` to emit the
complete report JSON.

Progress logs go to standard error by default, while the final JSON result stays
on standard output. Interactive runs therefore show messages such as
`[INFO] Starting thread 1/7: ...` and
`[SUCCESS] Completed thread 1/7: ...`, while scripts can safely pipe or capture
standard output. Logging uses only Python's standard-library `logging` module
and adds no logging dependency.

In an ANSI-capable interactive terminal, `[SUCCESS]` lines are green and
`[ERROR]` / `[CRITICAL]` lines are red. Redirected output, `NO_COLOR`, and
`TERM=dumb` remain uncolored. On Windows, the CLI enables virtual-terminal
processing when the console supports it.

- `-q` / `--quiet`: suppress progress logs and show errors only.
- `-v` / `--verbose`: include `[DEBUG]` diagnostics and raw progress events.

```powershell
tkn-codex-context session-notes rebuild --project-id <projectIdOrNameOrRoot>
tkn-codex-context -q session-notes rebuild --project-id <projectIdOrNameOrRoot> --dry-run
tkn-codex-context -v session-notes pull
```

## Scope

The current implementation generates Session Notes and Decision Records.
Current working context and global context remain out of scope.

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

### Application-owned generation profiles

Session Note and Decision Record generation resources are application-owned developer assets. Users
cannot select or override a prompt, schema, template, or profile. The current
profile is loaded as one bundle; additional developer-maintained patterns can
be added later as sibling profile directories:

```text
src/tkn_codex_context/profiles/
├── summary/
│   └── default/
│       ├── prompt.md
│       ├── output.schema.json
│       └── template.md
└── decision/
    └── default/
        ├── prompt.md
        ├── output.schema.json
        └── template.md
```

| Resource | Role |
| --- | --- |
| `prompt.md` | Versioned editorial policy, field meanings, development-label definitions, and source/merge/repair mode instructions |
| `output.schema.json` | Strict generated-JSON fields, types, enums, and limits used by Codex structured output and Python validation |
| `template.md` | Versioned deterministic Markdown heading order and section placement |

The resources form one pipeline:

```text
source events + prompt + output schema
    -> validated intermediate JSON
    -> Python renderer + template
    -> Session Note

Session Note + existing decision index + prompt + output schema
    -> validated intermediate decision JSON
    -> Python renderer + template
    -> Decision Record
```

The output schema governs the model's intermediate JSON, not the completed
Markdown file. Final frontmatter, required headings, event-ID integrity, and
`schemaVersion` remain application contracts enforced by Python. The bundle
loader verifies the strict schema and exact template placeholders, but semantic
field changes must still be coordinated by the developer:

| Change | Usually update |
| --- | --- |
| Editorial policy without changing fields | Prompt and its `version` |
| Limits or enum values on existing fields | Schema, prompt when it documents them, and tests |
| Add, remove, or rename a generated field | Schema, prompt, Python validation/rendering, and tests; template if layout changes |
| Reorder or rename Markdown sections | Template and its `version`; Python validation/tests when required headings or placeholders change |
| Change final frontmatter or an incompatible Session Note format | Python renderer/validation, tests, and normally `SESSION_SCHEMA_VERSION` |

The schema is identified by SHA-256; prompt and template also have explicit
versions. All three hashes participate in the generation fingerprint and their
provenance remains visible in `config show` and generated note metadata.

Sync the development dependencies, then run the tests, static checks, and
build:

```powershell
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv build
```
