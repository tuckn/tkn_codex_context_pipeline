# Project handoff: Codex conversation pipeline

Updated: 2026-09-06 · Version: 0.5.0

## Purpose and current state

The CLI preserves locally available Codex JSONL evidence and builds one Thread
Note per conversation, then Decisions and Working Context for automatic Project
scopes, explicit cross-Project work scopes, and the unassigned collection.

The normal workflow is `config init` → edit configuration → `clone` → periodic
`pull`. All stages are orchestrated; per-Project backfill is no longer the entry
point. The CLI has no compatibility aliases for retired commands. It does not
install a scheduler, fetch cloud ChatGPT history, or write into source Projects.
See the aligned [English README](../README.md) and [Japanese README](../README_ja.md)
for installation, configuration, command contracts, and operation.

Source membership and semantic work are distinct. Project metadata is optional
for ingestion. Custom scopes explicitly select source Project IDs and/or thread
IDs; semantic grouping by a model is not implemented. Raw and canonical evidence
are independent of those groupings.

## Implementation boundaries

| Module | Responsibility |
| --- | --- |
| `cli.py` | Current command surface, JSON stdout, readable stderr and exit codes |
| `config.py` | Validated layered configuration 2.2.0, provider settings, scope selectors, safe config initialization |
| `storage.py` | Non-destructive storage version 2 setup, ownership and overlap checks, OS locks |
| `raw_capture.py` | Original-byte content-addressed captures, retained manifest and replay |
| `catalog.py` | Sessions/archive discovery, optional app metadata, conservative duplicate merging, canonical events, membership history and eligibility |
| `scopes.py` | Shared note references, automatic Project/collection scopes, configured work scopes |
| `pipeline.py` | Clone/pull orchestration, per-stage fingerprints and checkpoints, edited/reviewed protection, current/partial reporting |
| `provenance.py` | Immutable entity versions and blobs, processing activities, current index and validation |
| `thread_notes.py` | Evidence-backed Thread Note generation, rendering, validation and pending-generation recovery |
| `decisions.py` | Incremental synthesis batches, existing record reuse, reviewed-record protection and validation |
| `working_context.py` | Selected note/decision/repository evidence, bounded inputs, context synthesis and source-change validation |
| `inference.py`, profile/resource modules | Provider transports and application-owned prompts, schemas and templates |

The low-level `Project` object remains a builder input adapter. In the new
pipeline it can represent a thread or a synthesis scope using explicit note,
repository and decision paths. Old registry/initialization helpers remain for
internal tests but are not the public lifecycle. Do not route new commands
through their reset or installation-watermark behavior.

## Data and identity contracts

Storage version: 2. Configuration: 2.2.0. Thread Note: 4. Decision Record: 5.
Working Context: 5 (`scopeId`, `scopeStatus`). Canonical events/catalog/provenance:
1.0.0. The source provider is always Codex; the inference provider is separately
configurable.

[Data contract](data-contract.md) is the downstream integration reference. Raw
bytes, normalized events, note IDs, source locators, generation profile hashes,
and versioned dependencies are retained. RDF/OWL vocabularies, global IRIs,
PROV-O mapping, semantic entity resolution, and graph publication belong to
another repository.

Project moves and overlapping scopes do not copy or re-identify Thread Notes.
Conflicting same-ID logs are retained and reported rather than arbitrarily
selected. Decision records no longer supported by current scope inputs are
preserved as stale and omitted from current Working Context inputs. An empty
Decision result is a valid stage outcome.

## Operational invariants

- `clone` initializes safely and is repeatable; it never resets owned storage.
  `pull` requires initialized storage. `raw ingest` can initialize a capture-only
  store, which `pull` can subsequently process.
- Old or late-arriving conversation history is selected by current source and
  stage fingerprints, not by an installation timestamp.
- Mutation commands write normally. `--dry-run` makes no model calls and writes
  no folders, locks, cache, state or reports. It reports uncomputed downstream
  stages as waiting for upstream results.
- A failure does not discard Raw or completed notes/batches. A later `pull`
  retries unfinished work. Active conversations, runtime limits and note limits
  leave explicit deferred states.
- Edited unreviewed files require `--allow-edited` for replacement. `--force`
  does not bypass protection. Reviewed note/context regeneration is blocked;
  reviewed Decisions may be referenced without modification.
- Only full clone/pull runs with current inputs and complete stages claim
  `complete`. Individual stage builds cannot advertise a complete pipeline.
- Locks coordinate writers. Immutable provenance snapshots remain readable
  during subsequent runs. Catalog, index and reports are separately atomic,
  so consumers use the indexed snapshots and as-of/run metadata.

## How to resume development

Read the relevant contract and module above, then inspect current changes and
applicable AGENTS instructions. Keep examples generic: committed documentation,
tests and sample configuration must not reveal private local directory layouts.
Do not run against personal chat history merely to test orchestration.

```console
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy
uv build
```

Tests use synthetic JSONL and fake inference providers. The pipeline integration
suite covers projectless/archive/cross-Project input, no-op runs, late history,
append updates, preserved note IDs, failed-stage and batch resumption, concurrent
lock rejection, dry-run immutability, reviewed/edited protection, leaf-stage
freshness, and evidence/hash/relation validation. Existing builder tests cover
structured generation and atomic output/state rollback.

Use framework/OS temporary directories. Follow AGENTS.md if a dedicated fallback
is necessary, and remove it after the run. Inspect wheel contents after changing
packaged resources. Use `uv tool install . --reinstall` only when refreshing the
user's installed CLI is requested; repository edits alone do not update it.

## Deliberate limits

Local Codex formats can change. Unsupported records are retained in Raw and
reported; the parser does not promise a full archive of all cloud or app-visible
conversations. Automatic retention cleanup, cloud connectors, scheduler setup,
and semantic scope discovery are not implemented. The provenance contract is
artifact/stage-level, not a complete transcript of every inference request.

Version 0.5 does not migrate the previous Project-based output layout. Retain
older stores and configure fresh roots for the new workflow. Private build
state has no downstream compatibility promise; evolve published data contracts
and profile versions deliberately when changing output meaning.
