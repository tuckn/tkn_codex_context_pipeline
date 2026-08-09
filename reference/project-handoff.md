# Project Handoff: Thread Note, Decision Record, and Working Context CLI

Updated: 2026-08-09

## Project purpose

This repository contains an independent, local-first data pipeline that turns
Codex app chats into durable Thread Note v3 artifacts, distills durable
Decision Record v4 artifacts from those notes, and synthesizes Working Context
v3 dashboards from Project evidence.

The larger context-engineering flow is:

```text
raw Codex chat
  -> factual Thread Note v3
  -> concise Decision Record v4
  -> current working context
  -> cross-project knowledge
```

The Thread Note, Decision Record, and Project Working Context transformations
are implemented. Cross-Project and global context remain out of scope.

The pipeline does not write markers, configuration, or context into a Codex
Project root. Generated artifacts remain in application-owned external storage:

```text
~/.tkn/codex_context_pipeline/data/projects/<projectId>/thread-notes/
~/.tkn/codex_context_pipeline/data/projects/<projectId>/decisions/
~/.tkn/codex_context_pipeline/data/projects/<projectId>/working-context.md
```

The existing context-engineering Plugin was not changed or removed as part of
this implementation.

## Current implementation

The repository uses:

- Python 3.11+
- uv
- Hatchling with a `src` layout
- Pydantic v2
- PyYAML
- pytest
- Ruff
- strict mypy

Package and command names:

```text
package: tkn-codex-context-pipeline
CLI:     tkn-codex-context
```

Implemented commands:

```text
init [--force]
config show
projects fetch
thread-notes pull [--force]
thread-notes pull --backfill --project-id <id-name-or-root> [--force]
thread-notes pull --backfill --all [--force]
thread-notes rebuild --project-id <id-name-or-root> [--force]
validate <thread-note>
decisions build --project-id <id-name-or-root> [--write] [--force]
decisions validate <decision-record>
working-context build --project-id <id-name-or-root> [--write] [--force] [--allow-edited]
working-context validate <working-context>
```

Thread Note mutation commands support `--dry-run`. `decisions build` and
`working-context build` are read-only by default and require `--write` before
they call Codex or change durable state.

## Important files

```text
pyproject.toml
.tkn/config.example.yaml
README.md
README_ja.md
src/tkn_codex_context/config.py
src/tkn_codex_context/app_state.py
src/tkn_codex_context/projects.py
src/tkn_codex_context/chat_logs.py
src/tkn_codex_context/thread_notes.py
src/tkn_codex_context/decisions.py
src/tkn_codex_context/decision_resources.py
src/tkn_codex_context/working_context.py
src/tkn_codex_context/working_context_resources.py
src/tkn_codex_context/cli.py
tests/
```

Responsibilities:

- `config.py`: strict layered YAML configuration and the `installed_at`
  normal-run boundary
- `initialization.py`: safe first initialization and transactional force reset
- `app_state.py`: fail-closed adapter for Codex app local Project state
- `projects.py`: binding Codex app Projects to durable context `projectId`s
- `chat_logs.py`: read-only JSONL parsing and canonical event generation
- `thread_notes.py`: selection, fingerprinting, model invocation, rendering,
  validation, atomic commit, rollback, cache resume, and rebuild
- `decisions.py`: Thread Note selection, existing-decision indexing, decision
  generation, deterministic rendering, source finalization, atomic commit, and
  rollback
- `working_context.py`: Project evidence collection, bounded current-truth
  synthesis, semantic validation, deterministic rendering, edited-artifact
  protection, atomic commit, and rollback
- `cli.py`: UTF-8 console behavior, JSON results, logging, command routing, and
  exit codes

## Configuration contract

Configuration precedence is:

1. built-in defaults
2. `~/.tkn/codex_context_pipeline/config.yaml`
3. `./.tkn/config.yaml`
4. an explicitly supplied `--config`
5. CLI options

Main defaults:

```yaml
codex_home: ~/.codex
data_root: ~/.tkn/codex_context_pipeline/data
state_root: ~/.tkn/codex_context_pipeline/state
cache_root: ~/.cache/codex_context_pipeline
provider: codex
model: gpt-5.6-sol
reasoning_effort: high
idle_minutes: 30
runtime_minutes: 230
model_timeout_seconds: 1800
```

`init` creates the configuration and Project registry, records the current
time as `installed_at`, and creates empty Project storage. Normal pulls process
only chats created or updated at or after this time. Older chats require
explicit `pull --backfill` or rebuild. `init --force` preserves configuration values,
refreshes `installed_at`, and transactionally replaces data, state, and cache.

Normal and backfill pulls skip notes whose source fingerprint, current schema,
model, reasoning effort, prompt version, and renderer version still match.
`pull --force` bypasses this no-op check. Rebuild treats every numeric schema
version below the current version as legacy, while refusing future versions.

The canonical Project registry is `data/project-registry.jsonl`. Thread Notes
are stored under `data/projects/<projectId>/thread-notes/`; Decision Records are
stored under `data/projects/<projectId>/decisions/`; Working Context is stored
at `data/projects/<projectId>/working-context.md`. Per-Project checkpoints
are stored under `state/projects/<projectId>/chat-refresh-state.json` and
`decision-build-state.json` and `working-context-build-state.json`. Run reports are stored under `state/reports/`.
Resumable Thread Note work is stored under the cache root.
Execution-only model files use Python's standard temporary directory (`%TMP%`
on Windows and normally `/tmp` on Linux).

Do not commit a real `.tkn/config.yaml`. Only
`.tkn/config.example.yaml` is intended for version control.

## Project binding

The Codex app internal ID in `local-projects` is the authoritative
`projectId`, registry key, and directory name. `--project-id` accepts that ID,
an exact current Name, or CURRENT ROOT as a CLI convenience, but always
resolves to the internal ID before pipeline work. Resolution order is ID,
Name, then normalized CURRENT ROOT. Duplicate Name or root matches fail with
the candidate IDs. Project names and roots are mutable metadata and never
establish stored identity. Two sidebar Projects remain distinct even when they
share a name or root.

Registry updates preserve unknown fields and use atomic replacement.

Codex Projects can have one primary root and multiple secondary roots. Every
configured root is active for attribution. Secondary roots are not historical
roots, and roots may belong to different Git repositories. When an active root
is later removed from the app Project, it becomes a historical alias in the
registry.

## Thread attribution

Attribution precedence:

1. explicit Codex app thread assignment
2. unique cwd match against every active root
3. saved historical aliases

Projectless and ambiguous threads are excluded and reported. Explicit
assignment wins even if the chat cwd is outside the currently active roots.

## Thread Note v3 contract

Required body sections:

```text
Summary
Key Developments
Last Known State
```

Optional sections:

```text
Evidence
Source Notes
```

Multiple independent work items render as `WI` H3 sections with label H4
sections. A single work item renders label H3 sections directly.

Automatically generated notes include:

- `reviewStatus: unreviewed`
- source thread ID and source ref
- source Project ID when available
- source fingerprint
- generator model and reasoning effort
- prompt and renderer versions
- automated validation status

Thread Notes do not store downstream processing status or references. Each
consumer owns its processing state and provenance independently.

The parser includes user and assistant messages, tool actions/results,
validation evidence, and cwd changes. Secret-like text is redacted and large
event text is truncated before model input.

Unchanged source and generator fingerprints are a no-op and do not call the
model. A changed generator fingerprint causes regeneration even if the source
chat is unchanged.

## Decision Record v4 contract

`decisions build` scans current Thread Note v3 files with an `Explicit
Decision` development. Planning is read-only and does not call the model.
`--write` generates strict structured output from bounded batches of Thread
Notes plus an existing-decision index. The output unit is a central decision,
not a Thread Note. One decision may cite multiple `sourceThreadNoteRefs`, and one
Thread Note may support multiple decisions. Each new central decision becomes
one `DR-NNNN-<slug>.md` file, or links to an existing decision ID when the model
identifies the same decision.

`Decision` is the only body section rendered for every record. `Why`,
`Consequences`, `Alternatives`, `Scope`, `Verification`, `Related Evidence`,
`Follow-up`, and `Supersession` are rendered only when they contain
source-backed content. Empty values remain available to structured processing
but do not become `None.` placeholders in human-readable Markdown. New records
use `schemaVersion: 4` and keep decision status, implementation status, and
promotion status separate. Materialization targets for working context,
repository documentation, global context, and Skills are stored in Frontmatter
instead of the body. Existing v1-v3 records remain readable and are not
automatically rewritten. Codex-generated unreviewed v2-v3 records are
quality-upgrade candidates and can be resynthesized as v4 only during an
explicit write run while preserving their ID and original date.

Decision generation leaves source Thread Notes unchanged. Decision Records
own forward provenance through `sourceThreadNoteRefs`; reverse `decisionIds`,
source fingerprints, and no-action outcomes live in
`decision-build-state.json`.

## Working Context v3 contract

`working-context build` uses validated Thread Notes, Decision Records, selected
root documentation, and a read-only Git snapshot. Planning is read-only and
does not call the model. `--write` uses bounded synthesis batches and a final
merge when needed. Repository evidence has precedence for current file/Git
state; reviewed Accepted decisions have precedence for durable judgments;
newer Thread Notes provide current work state. Proposed decisions and
unaccepted assistant suggestions are not promoted into current truth.

`Project Overview` and `Current Truth` are required. `Current Outcome`, `Active
Work`, `Risks And Constraints`, `Effective Decisions`, `Semantic Context`,
`Key Evidence`, `Resumption`, and `Source Limitations` render only when they
contain source-backed content. Semantic Glossary entries, Taxonomy items, and
Taxonomy relationships must cite exact known source refs. Empty sections and
`None.` placeholders are omitted.

The source-set fingerprint and generated artifact hash live in
`working-context-build-state.json`. An unchanged source/profile build is a
no-op. A changed build refuses to overwrite an artifact whose current hash no
longer matches the tracked generated hash; `--write --allow-edited` is the
explicit replacement gate. Artifact and state writes are transactional.

## Generation and commit safety

Codex CLI generation uses:

```text
codex exec
--ephemeral
--ignore-user-config
--skip-git-repo-check
--sandbox read-only
--output-schema
--output-last-message
```

Generated notes are completed and validated in the pipeline cache before being
committed. A note and its refresh state are treated as one transaction:
failures restore the previous note and state.

Decision Records are rendered and validated before their transaction is
finalized. New records, resynthesized unreviewed generated records, appended
provenance, and `decision-build-state.json` are committed together; failures
remove new records and restore existing records and prior state. Source Thread
Notes are not part of normal Decision generation writes. Reviewed central
judgment content is not automatically rewritten.

Working Context is rendered and validated before replacing the live artifact.
Its sources are fingerprinted again after model generation. Source changes,
validation failure, or state-write failure restore the previous artifact and
state.

Incomplete normal-run and rebuild artifacts remain resumable in the pipeline
cache. Successful work removes its pending cache. Rebuild performs a staged,
validated Thread Notes folder cutover and restores the prior live Thread Notes and
state if cutover fails.

Source JSONL files are always read-only.

## Validation completed

The current implementation passed:

```text
pytest:      131 passed
Ruff:         passed
strict mypy:  passed
uv build:     wheel and source distribution built successfully
wheel check:  decision modules and prompt/schema/template resources included
```

Tests cover:

- config precedence, relative paths, unknown-key rejection, and dry-run config
- multi-root primary/secondary behavior
- root/name/new/pending Project binding
- historical aliases and unknown registry-field preservation
- explicit assignment, cwd fallback, projectless, and ambiguous exclusion
- Thread Note v3 schema and WI hierarchy
- Decision Record v4 conditional structure, v2-v3 readability, deduplication
  references, no-action state, and
  Thread Note distillation metadata
- Working Context v3 source precedence, conditional sections, Semantic
  Glossary and Taxonomy evidence, unchanged no-op, edited-file protection, and
  atomic artifact/state writes
- source provenance, redaction, size limits, and status consistency
- unchanged no-op and stale-generator regeneration
- note/state rollback and resumable cache
- rebuild failure recovery, resume, and atomic cutover
- CLI JSON output, exit behavior, dry-run, and validation

## Read-only live verification

The implementation was exercised against the current computer's real Codex
app state, JSONL thread logs, and context registry using dry-run only.

Observed summary at the time of implementation:

```text
Codex app Projects:               19
Projects bindable:                19
Existing-context matches:         10
New context Projects proposed:     9
Pending Project bindings:          0
Explicit thread assignments:      19
Registered projectless threads:    2

JSONL files scanned:              339
Backfill candidates:              164
Ambiguous threads excluded:        14
Projectless threads excluded:       1
Unmatched threads excluded:         4
Internal chats excluded:          145
```

The pipeline configuration directory did not exist before or after dry-run,
and no report file was created. No live registry, note, or refresh-state write
was performed.

The projectless count in the backfill scan is lower than the app-state count
because filtering of internal or otherwise ineligible chats occurs before the
attribution counters.

On 2026-08-03, a second read-only verification of `decisions build` resolved
the current Project by internal ID with 22/22 Projects bound and no pending
binding. The Project had no stored Thread Notes at that moment, so the correct
result was `selectedCount: 0`, no model call, and no write.

## Repository state at handoff

The Decision Record implementation changes are present only in the working
tree. Nothing has been staged, committed, or pushed by Codex.

Before committing, inspect:

```powershell
git status --short
git diff --check
```

The build output and local virtual environment are ignored.

## Recommended next steps

First inspect and initialize the real configuration and Project storage:

```powershell
uv run tkn-codex-context init --dry-run
uv run tkn-codex-context init
uv run tkn-codex-context projects list
uv run tkn-codex-context thread-notes pull --dry-run
uv run tkn-codex-context decisions build --project-id <projectId>
uv run tkn-codex-context working-context build --project-id <projectId>
```

Use `projects list --json` when the full registered root metadata is needed.
Review Project IDs and ambiguous historical threads before applying broad
backfill.

After approval:

```powershell
uv run tkn-codex-context projects fetch
uv run tkn-codex-context thread-notes pull
uv run tkn-codex-context decisions build --project-id <projectId> --write
uv run tkn-codex-context working-context build --project-id <projectId> --write
```

Historical processing should start with a small Project-scoped batch:

```powershell
uv run tkn-codex-context thread-notes pull --backfill `
  --project-id <projectId> `
  --limit 5 `
  --dry-run
```

Then remove `--dry-run` only after reviewing the selected thread IDs and
Project binding.

## Known boundaries

- `.codex-global-state.json` and Codex JSONL are private internal formats, not
  public compatibility contracts. The adapter intentionally fails closed.
- Project identity uses only the Codex app internal ID. Cwd fallback compares
  both original and resolved roots; nested active roots can still make old,
  unassigned threads ambiguous.
- A missing real config is allowed only for dry-run, where the current time is
  used as an in-memory normal-pull boundary. A write pull requires `init`.
- Working Context generation is implemented per Project; cross-Project and
  global context remain separate future stages.
- Removing or changing the existing Plugin and configuring Task Scheduler are
  separate tasks.
