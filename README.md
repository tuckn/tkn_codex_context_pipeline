# Tkn GenAI Chat Note Pipeline

Japanese: [README_ja.md](README_ja.md)

Preserve local AI conversations as source evidence and turn each conversation into
a reusable Session Note. Notes retain requests, corrections, failed attempts,
unresolved questions, a source-backed timeline, and the last known state.
They support later reconsideration from different viewpoints.

A **session** means one continuous sequence of conversation, listed chronologically
in one Markdown note. The name uses the ordinary meaning of session, independently
of any product terminology.

Version 0.11.0 ends at Session Notes. Classification and Working Context belong to
[tkn_genai_context_curation_pipeline](https://github.com/tuckn/tkn_genai_context_curation_pipeline);
Decision distillation belongs to
[tkn_genai_insight_pipeline](https://github.com/tuckn/tkn_genai_insight_pipeline).
Each CLI installs independently and exchanges versioned files.

## Usage: get the first result

### Requirements and installation

Python 3.11+, uv, readable local Codex JSONL logs, and a configured inference
provider for generation. Codex CLI is the default; Claude Code, GitHub Copilot
CLI, and local Ollama are inference alternatives. **Only Codex chat acquisition
is implemented.** Other inference providers do not add chat-source support.

~~~console
cd "C:\path\to\tkn_genai_chat_note_pipeline"
uv tool install .
tkn-genai-chat-note --help
tkn-genai-chat-note config init
~~~

Edit the displayed `~/.tkn/genai_chat_note_pipeline/config.yaml`. Select storage
roots and an available model. For Codex inference, check `codex --version` and
`codex login status` in the terminal; the desktop app does not replace the CLI.
See the packaged [configuration example](src/tkn_genai_chat_note/resources/config.example.yaml).

### First capture and generation

~~~console
tkn-genai-chat-note config show
tkn-genai-chat-note clone --dry-run
tkn-genai-chat-note clone
~~~

`clone` initializes missing owned storage, captures all locally available
history, normalizes supported events, and generates eligible Session Notes.
It writes by default and may use substantial inference time/tokens.
`--dry-run` reads local inputs and validates the plan; it makes no inference or
network calls and creates no directories, locks, caches, or reports.

### Daily updates and results

~~~console
tkn-genai-chat-note pull
tkn-genai-chat-note status
tkn-genai-chat-note provenance validate
~~~

`pull` captures changed or newly discovered logs, updates eligible notes, and
resumes unfinished work. Repeating a successful unchanged run makes no model
calls. The default idle interval is 30 minutes; Raw capture still precedes
deferral of active conversations. `--limit 20` bounds note generation attempts.

Open the note and report paths shown in the result. `status` reads the last-run
record, not live source state. Completion now depends only on eligible Session
Notes; no Scope, Decision, or Working Context build is required.

## Commands

Global options, including `--config` and inference options, precede the command.

| Command | Behavior |
| --- | --- |
| `config init` | Create packaged user configuration; preserve edits; `--force` backs up before replacement |
| `config show` | Read resolved values, five configuration layers, and summary profile hashes |
| `clone` | Initialize and capture/build available history; resumable |
| `pull` | Update an initialized store and resume notes |
| `raw ingest` | Capture source bytes without inference |
| `session-notes build` | Refresh notes; `--thread-id` selects one conversation |
| `session-notes validate <artifact>` | Read-only note validation |
| `status` | Read the previous run's coverage and report path |
| `provenance validate` | Read-only hash, identity, and relationship checks |
| `storage migrate` | Copy storage 2/3 into the new layout; inspect files first with `--dry-run` |

Build commands support `--dry-run`, `--force`, `--allow-edited`, and
`--full-output`. `--force` re-evaluates unchanged input but does not unlock
reviewed files. `--allow-edited` explicitly permits replacing manually edited,
unreviewed notes. Raw ingest supports `--dry-run` and `--full-output`.

Progress uses stderr; stdout is JSON. `-q` suppresses progress, `-v` adds
diagnostics. Exit codes: `0` successful command/plan, `1` failure, `2` incomplete
clone/pull (for example deferred or protected work). A leaf build's report
describes overall note coverage even when one thread was selected.

## Configuration

Precedence: built-in → user-global → current directory `.tkn/config.yaml` →
explicit `--config` → CLI options. Each supplied layer is validated before
merging. Relative paths resolve against the file declaring them. Schema 4.0.0
uses snake_case keys and quoted SemVer; unknown keys and newer unsupported
versions fail visibly.

~~~console
tkn-genai-chat-note --config "C:\path\to\config.yaml" clone
tkn-genai-chat-note --idle-minutes 0 --runtime-minutes 60 pull --limit 20
~~~

Keep `raw_root`, `data_root`, `state_root`, and `cache_root` separate from each
other, source logs, and configuration. These are shared roots; the CLI creates
`<provider>/<source_id>/` beneath each root.
Missing app metadata does not prevent conversation capture.

Inference transport configuration and authentication details are retained in
[inference providers](reference/inference-providers.md). Generation through an
external CLI may send selected inputs to its service; Ollama is restricted to
a loopback endpoint. Model availability and authentication are provider-owned.

### Session Note language

Set `generation.session_note_profile` in your existing `config.yaml` to `default-jp` (Japanese, the default) or `default-en` (English). The following is a configuration fragment; retain your other settings.

```yaml
generation:
  session_note_profile: default-en
```

For a single run, use `tkn-genai-chat-note --session-note-profile default-en pull`. The option precedes the command. `config show` reports the selected profile, its resources and hashes, and the configuration source.

Both built-in profiles preserve the same schema, headings, timeline, citations, and state rules. Only narrative language and explanatory notices change; times remain in Asia/Tokyo. Custom profile names, directories, and prompts are not supported. Bundles are packaged under `profiles/default-jp/` and `profiles/default-en/`.

Changing language makes an existing note eligible for regeneration on the next build/pull. Each conversation retains one note and its identity; this does not create parallel language editions. Reviewed or edited notes retain their existing protection, and dry-run never generates or writes. Interrupted work from a different profile is not reused.

### Chat sources and generation AI

`chat.providers` configures conversation acquisition; `generation.providers`
configures the AI used to generate notes. `--provider` changes only
`generation.active_provider`, independently of chat acquisition.

| Chat provider | Default home | enabled | Support |
| --- | --- | --- | --- |
| `codex` | `~/.codex` | `true` | Local log acquisition implemented |
| `claude-code` | `~/.claude` | `false` | Configuration only; acquisition, normalization, and note integration are pending |
| `github-copilot` | `~/.copilot` | `false` | Configuration only; acquisition, normalization, and note integration are pending |

Each section has `enabled`, `home`, and `source_id`. `include_archived` is
Codex-specific. Disabled source homes need not exist to load the configuration.
Enabling an unimplemented source stops processing before any writes, including
in dry-run mode. Disabling every source also stops processing. `config show`
remains available to inspect these settings.

`source_id` is a stable identity for a source installation, including its PC,
environment, account, and provider. Before first ingestion, choose identifiers
that distinguish environments within each provider, for example
`pc-a-windows-main-codex` and `pc-a-wsl-work-codex`. Use letters, digits, dots,
underscores, or hyphens, starting with a letter or digit. Source identity is
`(provider, source_id)`: different providers can share the same source_id. Use
different IDs for different environments of the same provider; do not rely on
case-only differences on Windows. Replace the example `my-windows-pc` before
first ingestion.
Changing an ID after ingestion requires storage/provenance migration; it is
not a display-label change.

A configuration defines one installation per provider; only Codex acquisition
is implemented. Separate environment configurations can run sequentially against
the same roots, retaining multiple Codex sources. Collecting several installations
in one run and acquiring Claude Code/Copilot conversations remain future work.

### Migrating older configuration

The configuration schema is `"4.1.0"`. Existing 4.0.x configuration remains readable and defaults to Japanese when no profile is specified. Schema 3.0.x fields `codex_home`,
`source_id`, and `include_archived` migrate in memory to
`chat.providers.codex`; `codex_home` becomes `home`. Schema 2.0.x–2.2.x and
integer `2` remain readable when `scopes` is empty or absent. Reading never
rewrites the original file; `config show` reports migrations and value sources.

For manual migration, update the schema and field locations together, preserving
`source_id` and existing storage paths. Do not mix the old and new forms.
Configuration-writing operations emit only the new form.

## Data and responsibility boundaries

~~~mermaid
flowchart LR
    L["Local Codex logs"] --> R["Raw copies and manifest"]
    R --> E["Canonical Events"]
    E --> T["Session Notes"]
    M["Observed Project membership"] --> C["Thread catalog"]
    T --> C
    C --> U["Context curation CLI"]
    T --> I["Insight CLI"]
    R --> P["Versioned evidence"]
    E --> P
    T --> P
~~~

Storage is ordered by role, acquisition application, source environment, then
kind of data. `P` below is the acquisition provider, `I` the source_id, `T` the
threadKey, and `H` a content hash. Changing `generation.active_provider` does
not change these paths.

| Storage path | Contents |
| --- | --- |
| `<raw_root>/P/I/sessions/...` | Latest Codex source copies preserving relative paths and bytes |
| `<raw_root>/P/I/archived_sessions/...` | Latest copies preserving Codex's archived layout |
| `<raw_root>/P/I/manifest.jsonl` | Raw source references, hashes, and acquisition metadata |
| `<raw_root>/P/I/metadata/H.json` | Observed application Project metadata |
| `<data_root>/P/I/source-aligned/T/H.json` | Canonical Events retaining source references |
| `<data_root>/P/I/session-notes/YYYY/MM/...md` | Current Session Notes by conversation start year/month |
| `<state_root>/P/I/pipeline.json` | Per-source initialization and storage version |
| `<state_root>/P/I/threads/T/...` | Per-conversation checkpoints |
| `<state_root>/P/I/ledger.json`, `reports/`, `last-run.json`, `normalization/` | Per-source run and normalization state |
| `<cache_root>/P/I/...` | Reusable generation work for one source |
| `<data_root>/catalog/threads.json` | Shared catalog of all sources, observations, states, and note references |
| `<data_root>/provenance/...` | Shared immutable snapshots, entities, activities, and published index |

For provider `codex` and source_id `my-windows-pc`, Raw goes to
`~/.tkn/genai_chat_note_pipeline/raw/codex/my-windows-pc/sessions/...`; notes go to
`~/.tkn/genai_chat_note_pipeline/data/codex/my-windows-pc/session-notes/YYYY/MM/...md`.
Other providers use separate folders even when they share a source_id.
Inspect resolved paths in `config show` under `storage.sourceRoots`.

Ownership markers and locks remain at the shared roots. Root locks serialize
shared catalog/provenance updates while preserving other sources' records.
`status` describes the currently configured source; `provenance validate` checks
the shared provenance. The same conversation in multiple environments retains
its threadKey, but each environment has independent note IDs and checkpoints.
Catalog rows are distinguished by `(sourceProvider, sourceId, threadKey)`.


Version 0.11.0 renames the artifact, command, and output directory to Session Note,
`session-notes`, and `session-notes/`. New notes use `type: sessionNote`,
`sessionNoteId`, and schema 6. Readers retain support for historical Thread Note
schemas 3–5. Migration copies old notes without changing their bytes; subsequent
`pull` regenerates eligible unreviewed notes in the new format and may invoke
inference. Reviewed or manually edited notes keep their existing protections.
Source identifiers such as `threadId` and `sourceThreadIds` still identify Codex
source conversations. Downstream consumers need explicit schema-6 support;
compatibility with the separate curation/insight applications has not been verified
for this rename. To update an existing CLI installation, run `uv tool install . --reinstall`.

### Migrating older storage

Storage is version `4`; configuration remains schema `"4.1.0"`. Initialize fresh
roots with `clone`. Ordinary processing stops with migration guidance when it
finds storage 2/3 or an older Raw-only store. Keep the old source_id and all four
roots configured, then run:

~~~console
tkn-genai-chat-note config show
tkn-genai-chat-note storage migrate --dry-run
tkn-genai-chat-note storage migrate
tkn-genai-chat-note provenance validate
tkn-genai-chat-note pull --dry-run
~~~

`--dry-run` lists exact source/destination files, sizes, and hashes without
creating files, folders, or locks. Normal execution copies and verifies files,
then updates mutable catalog/index/checkpoint references. It does not rewrite
note contents, IDs, review status, or immutable provenance snapshots, and it
never invokes inference. Repeating a completed migration changes nothing.
Conflicting destination files stop migration. Older Project-only stores without
storage-2 metadata cannot be migrated automatically. A Raw-only store has no
provenance index yet; validate provenance after its first note generation.

**Original Raw, notes, and canonical files remain in place.** Existing notes and
historical provenance can still contain their old references. New processing uses
the new layout; deleting old folders manually can break historical references.
Original shared management files are backed up under
`<state_root>/P/I/migrations/storage-v2-backup/` for storage 2 or
`<state_root>/P/I/migrations/session-note-v4-backup/` for storage 3. Known write errors restore
changed files. For storage-2 migration, to prevent older CLIs from writing the old layout again, the old
`<state_root>/pipeline.json` becomes a shared layout marker and its original is
backed up. When no legacy store exists, migration exits without creating anything.

Thread identity survives Project reassignment. Membership observations are
retained upstream; semantic scopes and approved relationships belong downstream.
Session Notes are derived records, not a replacement for original evidence.
Source and inference providers remain separate concepts.

Local `sessions` and, by default, `archived_sessions` are scanned. Projectless,
unmatched, and ambiguous conversations remain eligible. Internal/approval
conversations and sources without a clean user message are retained and
normalized but excluded from notes. Cloud-only ChatGPT/Work history is not
fetched. Unsupported records and divergent versions remain visible in reports.

See [data contract](reference/data-contract.md),
[Session Note format](reference/session-note-format.md), and
[processing sequence](reference/processing-flow.md) for IDs, hashes, schemas,
citations, storage details, and input preparation.

## Reinstall after updates

After source or resource changes:

~~~console
cd "C:\path\to\tkn_genai_chat_note_pipeline"
uv tool install . --reinstall
~~~

## Development and verification

~~~console
uv sync --locked
uv run pytest
uv run ruff check .
uv run mypy src
uv build
~~~
