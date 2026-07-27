# Project Handoff: Session Note v2 CLI

Updated: 2026-07-26

## Project purpose

This repository contains an independent, local-first data pipeline that turns
Codex app chats into durable Session Note v2 artifacts.

The larger context-engineering flow is:

```text
raw Codex chat
  -> bronze Session Note v2
  -> long-term decisions and current working context
  -> cross-project knowledge
```

Only the first transformation is implemented here. Decision records, current
working context, and global context are intentionally out of scope for the
initial release.

The pipeline does not write markers, configuration, or context into a Codex
Project root. Generated notes remain in the existing external context store:

```text
~/.tkn/codex-context/state/<projectId>/sessions/
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
session-notes pull
session-notes backfill --project-id <id>
session-notes backfill --all
session-notes rebuild --project-id <id> [--force]
validate <session-note>
```

Every mutation command supports `--dry-run`. Dry-run does not modify the
registry, notes, refresh state, pipeline cache, or run reports.

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
src/tkn_codex_context/session_notes.py
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
- `session_notes.py`: selection, fingerprinting, model invocation, rendering,
  validation, atomic commit, rollback, cache resume, and rebuild
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
time as `installed_at`, and creates empty Project storage. Normal runs process
only chats created or updated at or after this time. Older chats require
explicit backfill or rebuild. `init --force` preserves configuration values,
refreshes `installed_at`, and transactionally replaces data, state, and cache.

The canonical Project registry is `data/project-registry.jsonl`. Session Notes
are stored under `data/projects/<projectId>/sessions/`; per-Project refresh
checkpoints are stored under
`state/projects/<projectId>/chat-refresh-state.json`. Run reports are stored
under `state/reports/`. Resumable work is stored under the cache root.
Execution-only model files use Python's standard temporary directory (`%TMP%`
on Windows and normally `/tmp` on Linux).

Do not commit a real `.tkn/config.yaml`. Only
`.tkn/config.example.yaml` is intended for version control.

## Project binding

The Codex app internal ID in `local-projects` is the authoritative
`projectId`, registry key, directory name, and CLI selector. Project names and
roots are mutable metadata and never establish identity. Two sidebar Projects
remain distinct even when they share a name or root.

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

## Session Note v2 contract

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

The parser includes user and assistant messages, tool actions/results,
validation evidence, and cwd changes. Secret-like text is redacted and large
event text is truncated before model input.

Unchanged source and generator fingerprints are a no-op and do not call the
model. A changed generator fingerprint causes regeneration even if the source
chat is unchanged.

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

Incomplete normal-run and rebuild artifacts remain resumable in the pipeline
cache. Successful work removes its pending cache. Rebuild performs a staged,
validated sessions-folder cutover and restores the prior live sessions and
state if cutover fails.

Source JSONL files are always read-only.

## Validation completed

The current implementation passed:

```text
pytest:       51 passed
Ruff:         passed
strict mypy:  passed
uv build:     wheel and source distribution built successfully
path scan:    no private absolute paths in versioned source
```

Tests cover:

- config precedence, relative paths, unknown-key rejection, and dry-run config
- multi-root primary/secondary behavior
- root/name/new/pending Project binding
- historical aliases and unknown registry-field preservation
- explicit assignment, cwd fallback, projectless, and ambiguous exclusion
- Session Note v2 schema and WI hierarchy
- source provenance, redaction, size limits, and status consistency
- unchanged no-op and stale-generator regeneration
- note/state rollback and resumable cache
- rebuild failure recovery, resume, and atomic cutover
- CLI JSON output, exit behavior, dry-run, and validation

## Read-only live verification

The implementation was exercised against the current computer's real Codex
app state, JSONL sessions, and context registry using dry-run only.

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

## Repository state at handoff

The implementation files are currently untracked relative to the initial
commit. Nothing has been staged, committed, or pushed by Codex.

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
uv run tkn-codex-context session-notes pull --dry-run
```

Use `projects list --json` when the full registered root metadata is needed.
Review Project IDs and ambiguous historical threads before applying broad
backfill.

After approval:

```powershell
uv run tkn-codex-context projects fetch
uv run tkn-codex-context session-notes pull
```

Historical processing should start with a small Project-scoped batch:

```powershell
uv run tkn-codex-context session-notes backfill `
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
- The initial release does not distill decisions or working/global context.
- Removing or changing the existing Plugin and configuring Task Scheduler are
  separate tasks.
