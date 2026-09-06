# Data contract: conversation evidence and derived context

Version: 1.0.0 · CLI: 0.5.0 · Storage: 2

This is the downstream contract for the conversation-based pipeline. Consumers
should read the catalog and provenance index, not infer identity from filenames
or depend on private checkpoint files. This document specifies the JSON fields;
`provenance validate` provides structural, hash, and relationship checks for the
published evidence. Markdown validators check the individual artifact schemas.

## Responsibility boundary

This repository captures local Codex evidence, normalizes supported events,
builds artifacts, and records versioned inputs, outputs, and processing
activities. It does not fetch cloud ChatGPT history or resolve semantic identity
across unrelated conversations.

The downstream repository owns global IRIs, RDF/JSON-LD serialization, OWL
vocabulary, PROV-O mapping, entity resolution, reasoning, and graph publication.
It can map the records below without parsing human prose to discover all
artifact-level generation dependencies. Sentence-level factual claims still
require the note's citations and a downstream interpretation policy.

## Roots and references

| Reference | Resolution |
| --- | --- |
| `raw:/<relative-path>` | Relative to the configured `raw_root` |
| `data:/<relative-path>` | Relative to the configured `data_root` |
| `repo:/<root-index>/<filename>` | Repository snapshot location in the scope's ordered `repositoryRoots`; use the retained evidence blob for historical bytes |
| `codex/<threadId>` | Logical conversation source reference in Thread Note metadata; not a filesystem path |
| `#L000123` | One-based line locator within the referenced immutable Raw capture |
| `#inference-input` | The prepared input for a source; resolve its entity's `snapshotRef` for bytes |

References are locators, not global identity. Resolve only supported prefixes
under their declared roots. Reject traversal outside those roots. Catalog
fragments such as `data:/catalog/scopes.json#work:example` identify catalog
objects; they are not filenames. Absolute repository paths are observations of
one machine and are not portable IDs.

## Identity and version

- `sourceProvider` is `codex`. `sourceId` names the acquisition store, such as
  `windows`; it is separate from the inference provider.
- `threadId` is the original Codex conversation ID.
- `threadKey` is UUIDv5 using the URL namespace and `codex-thread:<threadId>`.
  It is independent of Project membership and source-file location. Logs with
  the same ID merge only when identical or provable append extensions; divergent
  versions fail visibly. The original captures remain available.
- Artifact `id` is UUIDv4 and identifies a Thread Note, Decision Record, or
  Working Context across regeneration. Existing IDs and creation dates are
  preserved. `decisionId` such as `DR-0001` is local to its scope, not a global ID.
- Scope IDs are `project:<original-id>`, `work:<configuration-key>`, or
  `unassigned`. Their storage key is UUIDv5 using `codex-scope:<scope-id>`.
  Configuration keys are durable identity: renaming one creates a different
  scope. Changing only its title does not.
- Entity `version` is `sha256:<hex>` over exact snapshot bytes. Identify an
  entity version with the pair `(id, version)`. Text normalization can change
  the version even when displayed text looks identical.
- A canonical event ID is `<threadKey>:<captureSha256>:<localId>`, where
  `localId` is `L` plus a minimum of six digits. It identifies a line in one
  capture. It is not a promise of stable event identity across rewritten logs.

## Published documents

| Document | Schema version | Principal fields |
| --- | --- | --- |
| Raw `manifest.jsonl` | integer `1` | `sourceId`, `sourceRef`, `captureRef`, `sha256`, `byteCount`, capture timestamps and source metadata |
| `catalog/threads.json` | `1.0.0` | `asOf`, `threads` |
| `catalog/scopes.json` | `1.0.0` | `asOf`, `scopes` |
| Canonical event JSON | `1.0.0`, parser `1` | `threadKey`, `threadId`, capture reference/hash, source times, `diagnostics`, `events` |
| Provenance entity/activity/index JSON | `1.0.0` | See below |
| Thread Note Markdown | integer `4` | UUID `id`, `sourceThreadIds`, capture references/hashes, generation metadata and event-backed work items |
| Decision Record Markdown | integer `5` | UUID `id`, scope-local `decisionId`, `scopeId`, applicability `scope`, source-note refs/hash, review and implementation status |
| Working Context Markdown | integer `5` | UUID `id`, `scopeId`, `scopeStatus`, source hashes, generation and review metadata |

Readers should check versions and reject incompatible schema changes. Private
state/ledger schema numbers do not define the downstream format. Some internal
builder report fields still use `projectId` for the selected scope; consumers
should use the published scope catalog and artifact `scopeId`.

### Conversation catalog and canonical events

Each thread entry contains original IDs, observed source/capture refs, its
current selected capture, canonical reference/hash, eligibility/processing
status, and note reference/ID when available. `membership` includes:

- `status`: `explicit`, `projectless`, `inferred`, `ambiguous`, `unmatched`, or
  `state-unavailable`.
- Nullable `sourceProjectId` and `projectKind`.
- `candidateProjectIds` for ambiguity without selecting a winner.

`membershipHistory` records changed observations with `observedAt`, the
membership object, and the captured `metadataRef` if available. Observation
history is not the effective time of a change inside Codex.

Canonical events contain `id`, `localId`, `rawRef`, `kind`, `actor`, `name`,
`text`, `timestamp`, `turnId`, and `cwd`. Raw locators allow verification against
the original line. Canonical text uses the application's parser and may normalize
or redact content; it is not an exact copy of every field in the source record.
`diagnostics` lists invalid lines, unknown top-level record types, and metadata
thread IDs. Raw preserves fields the parser does not understand.

### Scope catalog

A scope entry contains `id`, `title`, `kind` (`project`, `work`, `collection`),
`threadKeys`, `dataRef`, `repositoryRoots`, processing `status`, and stage reports
when run. Threads can belong to several scopes without duplicated notes.
An unassigned collection does not assert a common purpose. Old scopes no longer
selected are omitted from the current scope catalog; retained artifacts appear
as `inactive` in the provenance index. Historical scope definitions remain in
evidence snapshots used by activities.

### Entity records

Entities have the following required fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | `1.0.0` |
| `id` | Stable logical identity string; artifact entities use their UUID |
| `version` | `sha256:` followed by the exact content hash |
| `sha256` | Lowercase 64-character SHA-256 digest |
| `ref` | Observed locator for this version |
| `kind` | For example `raw`, `sourceMetadata`, `canonicalEvents`, `scope`, `threadNote`, `decision`, `repositoryFile`, `gitSnapshot`, `inferenceInput`, `workingContext` |
| `mediaType` | JSON, Markdown, plain text or newline-delimited JSON |
| `byteCount` | Snapshot length in bytes |
| `snapshotRef` | `data:/provenance/blobs/<prefix>/<hash>` |

Entity records are stored in `provenance/entities/` under a SHA-256 key over
`id + NUL + sha256`. The same identity/version can have several observed
locations; the first entity record keeps its locator, while activities and the
current index can show newer locators. Historical bytes are always resolved
using `snapshotRef`.

### Generation activities

An activity in `provenance/activities/<uuid>.json` contains:

- UUID `id`, UUID `runId`, `stage`, and `subject` (thread key or scope ID).
- `startedAt`, `endedAt`, and `status` (`completed` or `partial`).
- `agent`: application/software version and, for inference, provider, model,
  reasoning effort, profile/prompt/schema/template hashes and prompt version.
- `used` and `generated`: inline entity-version records.
- `relations`: `derivedFrom` edges with `from` (generated) and `to` (used), each
  containing an `id` and `version` pair.

Stages include `normalize`, `thread-note`, `decisions`, `prepare-input`, and
`working-context`. Working Context preparation preserves exact source bytes
and a distinct JSON input containing the normalized/bounded text passed to the
model. Git evidence is a captured textual observation, not a complete Git
repository snapshot.

Edges describe stage-level dependencies. In a synthesis batch every generated
artifact is linked conservatively to the batch's retained input set; this does
not mean every sentence depends on every input. A Decision activity can generate
zero records and still complete successfully. Partial decision batches can
publish successful outputs, with the scope remaining incomplete. Failures with
no generated output can be represented only in the run report. This is not a
complete model-request or token-level audit log.

## Completion and consumer procedure

`provenance/index.json` contains `schemaVersion`, `runId`, `asOf`,
`pipelineComplete`, `artifacts`, and `activityRefs`. Artifacts include their
entity fields plus `status` and a `threadKey` or `scopeId`. `current` means
current for the inputs observed by that published run, not a live guarantee.
Other statuses include `pending`, `deferred`, `blocked`, `failed`, `stale`,
`not-run`, and `inactive`.

1. Read the index and retain its run ID. To require a completed full pipeline,
   require `pipelineComplete: true`; inspect the matching run report for
   source coverage, warnings, failures, and excluded conversations.
2. Select appropriate artifact statuses and review/implementation metadata.
   Successful automated validation is not human review or independent proof
   that a decision was implemented.
3. Read historical content from `snapshotRef` and verify SHA-256/byte count.
   Use mutable Markdown paths only when their hash still matches the index.
4. Traverse activities using `(id, version)` pairs. Do not join on titles,
   filenames, Project paths, or scope-local decision numbers.
5. Re-read the index run ID before committing an import, and retry if it
   changed. Immutable snapshots allow a consumer to keep reading a previously
   published run while a new run updates mutable notes.

`raw ingest` publishes a capture report but does not republish derived data or
its index. The previous index remains an as-of snapshot. Use `status`/the latest
run report when comparing it with more recent acquisition. Individual builds
publish their selected stages but always set full `pipelineComplete` to false.
A new or interrupted run can leave the previous index intact: inspect the latest
run record when live operational freshness matters. The index and catalog are
separate atomic files, not a multi-file database transaction; the provenance
index's retained snapshots are the stable export boundary.

`provenance validate` checks schema/structure, indexed identity uniqueness,
snapshot hashes and lengths, activity IDs, relationship endpoints, and mutable
files advertised as current. It does not assess semantic correctness, assert
that every cloud chat was captured, or perform RDF/OWL reasoning.

## Retention

Raw captures, canonical snapshots, provenance blobs, and activities are retained
without automatic garbage collection. Failed and deferred work never causes
source deletion. Original logs can disappear after capture and still be
processed from retained Raw. Checkpoints and note hashes allow repeated
`clone`/`pull` to resume without repeating completed unchanged inference.

Back up Raw, data, and state together to retain evidence, logical identities,
and resumability. Treat these as private stores: original conversation content,
source metadata, and local repository paths can be present in snapshots.
