# Data contract: chat evidence and Session Notes

CLI 0.15.0 · config 7.0.0 · storage 5 · catalog/provenance 1.0.0 · Session Note 6

This is the file-based interface specification for tools that consume this CLI's
output, including context curation and insight. It defines stable identities,
published schemas, content verification, reference resolution, and safe reads
during concurrent updates. Consumers use these published files rather than
depending on the producer's Python package or private checkpoints.

For installation and normal operation, see the [README](../README.md).
For the contents of a generated note, see [Session Note format](../docs/session-note-format.md).
Supported historical formats below describe reader compatibility; new stores
use the current versions listed above.

## Ownership

This application owns Codex chat capture, Canonical Events, source-near Session Notes,
observed Project membership, and their evidence. Semantic membership and Working
Context belong to context curation. Insight/Decision generation belongs to insight.
RDF/JSON-LD or other graph representations can be layered on these contracts;
they are not required to run any of the three CLIs.

## Identity, versions, and references

- `threadId` is the original conversation ID. For Codex, `threadKey` is UUIDv5
  in the URL namespace using `codex-thread:<threadId>`. It is independent of
  Project membership and file location.
- Session Note `id` is UUIDv4 and survives regeneration. `sessionNoteId` and
  filenames are display/legacy locators, not global IDs.
- `sourceProvider` is fixed to `codex`. `sourceId` identifies the acquisition
  input directory (PC/environment/storage root), configured under
  the map key `sources.<source_id>`; neither selects the inference provider.
  Existing source IDs, Raw references, and artifact IDs survive config migration.
  Storage namespaces use `(sourceProvider, sourceId)`, with an independent catalog and provenance for each source.
  Acquisition of other applications is outside this repository. Catalog rows use
  `(sourceProvider, sourceId, threadKey)`; identical threadKeys across environments are allowed.
  Each environment has its own note UUID and checkpoint.
- `data:/` resolves under this source's final data_root. `raw:/<provider>/<source_id>/`
  is a virtual source prefix: remove that prefix and resolve the remainder under
  this source's final raw_root. Never append the provider/source_id twice.
  References are locators, not identity; historical bytes use snapshotRef.
- Canonical IDs include threadKey, capture SHA-256, and a local event/line ID.
  They identify one version, not a stable event across arbitrary log rewrites.
- Entity versions are `sha256:<hex>` over exact bytes. Join evidence by
  `(id, version)`, not names or paths.

Raw schema-3 paths are latest source copies and can change. Historical evidence
must use the retained provenance `snapshotRef` and hash, not a mutable Raw path.
Legacy schema-1 hash captures remain readable after explicit layout migration. Raw-only acquisition does not
republish derived evidence; take backups of Raw/data/state together.

## Published artifacts

| Artifact | Version / content |
| --- | --- |
| Raw manifest | Namespaced reader supports 1/2/3, current writes use 3; source refs, hashes, byte counts and capture metadata |
| `store.json` | `1.0.0`; storageVersion 5, source identity, Raw prefix, legacy aliases |
| `catalog/threads.json` | `1.0.0`; `asOf` and `threads` |
| Canonical event export | `1.0.0`; parser 1, event locators, source metadata and diagnostics |
| Session Note Markdown | Generated Session Note 6; reader also supports historical Thread Note 3, 4, 5 |
| Provenance entities, activities, index | `1.0.0` |
| Pipeline report | Source-local integer 2; multi-source response integer 3; no downstream scope completion fields |

A thread entry includes `threadId`, `threadKey`, source provider/store/ref,
capture and canonical refs/hashes, status/reason, dates, membership history,
and, when a note exists, `noteId`, `noteRef` and `noteSha256`.
The last field is an additive 0.8 export field. Consumers of older catalogs can
obtain the published note hash from the matching provenance-index artifact.

`membership` preserves the source observation: explicit, projectless, inferred
from roots, ambiguous, unmatched, or unavailable. Nullable sourceProjectId,
projectKind, and candidate IDs preserve uncertainty. `membershipHistory` records
when the pipeline observed a change, not when the user made it. Prefer this
catalog over a note's older sourceProjectId when selecting current membership.

Unknown records, conflicting source versions, active conversations and
protected/failed notes remain visible. Excluded internal conversations are not
eligible summary inputs. A note is advertised as current only for the inputs
observed by that run. No complete cloud-history coverage is claimed.

## Provenance

Entity records contain schemaVersion, id, version, sha256, ref, kind, mediaType,
byteCount, and snapshotRef. Exact snapshots live under
`data:/provenance/blobs/<hash-prefix>/<hash>`. The entity filename hashes
`id + NUL + sha256`. A relocated entity can retain its first stored locator;
use snapshotRef for historical bytes.

Activities contain UUID id/runId, stage/subject, start/end times, status, agent,
used/generated entity versions, and derivedFrom edges. Agent metadata records
software version, inference provider/model/effort, and profile/prompt/schema/
template fingerprints. Current upstream stages are normalization and Session Note
generation. Relations express stage dependencies; they do not prove every
sentence follows from every source.

The index contains schemaVersion, runId, asOf, pipelineComplete, artifacts, and
activityRefs. Artifacts include status and threadKey. Old downstream artifacts
from a reused shared store are retained as inactive; this application does not
refresh or delete them.

## Consumer procedure

1. Read the catalog and index, preserving their bytes/run information.
2. Select current notes, and verify UUID, supported schema, content hash and
   source identity. Reject traversal outside declared roots.
3. Read historical bytes from snapshotRef; validate hash and byte count.
4. Preserve note identities and evidence versions in the consumer's output.
5. Recheck the input catalog and selected note hashes before committing.

The files are atomically replaced individually, not committed as one database
transaction. During a concurrent run a reader can observe different generations
and should reject inconsistent input and retry. The downstream CLIs implement
this conservative validation; they do not mutate upstream checkpoints.

`clone`/`pull` and a full Session Note build can be complete when all eligible
notes are current, independently of downstream execution. Read report coverage
and exclusions as well as pipelineComplete. `raw ingest` does not change the
previous derived-data index, so its timestamp can differ.

`provenance validate` checks structural compatibility, identities, snapshot
hashes/lengths, activity IDs/relations and current mutable artifacts. It does not
prove semantic accuracy, completeness of reasoning, or that a reported event
was independently verified. No automatic garbage collection or source deletion
is performed.

## Storage layout 5

`sources.<source_id>.raw_root/data_root/state_root` are optional final paths.
An omitted root is `~/.tkn/codex_chat_note_pipeline/<kind>/codex/<source_id>`.
The shared cache_root is a base; actual cache is `<cache_root>/codex/<source_id>`.
Source roots may share a parent but cannot overlap one another or input source_root directories.
Ownership markers bind each root to provider/source_id; locks are per actual root.

Raw contains sessions/, archived_sessions/, manifest.jsonl and metadata/.
Data contains source-aligned/, session-notes/, catalog/, provenance/ and store.json.
State contains pipeline.json, ledger.json, threads/, normalization/ and reports/.
Keep raw/data/state together for backup and relocation. State is required for
faithful restart and edited-note protection; public provenance is not a substitute
for private operational checkpoints. Cache can be regenerated.

`store.json` contains rawRefPrefix, rawRefAliases (old full prefixes), and
 dataRefAliases (old relative prefixes mapped to current relative prefixes).
Resolve aliases longest-prefix first, once, rejecting traversal before and after
translation. Python readers can use references.resolve_store_ref. Canonical data:/
locators resolve locally; Raw locators are source-qualified. Historical snapshots
and note bodies retain their exact bytes during migration.

`storage migrate --from-config <old-config> --dry-run` plans a copy into the fresh
roots in the destination config. The source configuration is standalone, never
combined with destination layers. Supported source configs are integer 2,
2.0.x–2.2.x, 3.0.x, 4.0.x–4.1.x, 5.0.x, 6.0.x and 7.0.x. Source stores 2/3/4 and completed 5
are supported; future formats and pending source migrations are rejected.
Keep the same source identity. Concurrent source writers must be stopped.

Apply copies source payloads, selected catalog/provenance, all required immutable
snapshots, and checkpoint state. Mutable locators are translated, but immutable
activities/entities/blobs and note bytes/UUIDs remain unchanged. Source config and
data are never modified. State/migration.json records a deterministic file plan;
conflicts/source changes fail closed, write errors roll back destination changes,
and interrupted copies resume using the same plan. Completion verifies the copied
provenance. A completed migration is a no-write operation on repetition.

After a storage-5 relocation, generation fingerprints remain valid. Old Thread
Notes can be regenerated by a later pull when their generation format differs,
subject to ordinary reviewed/edited protections. The migrator never calls a model.
Downstream curation/insight v0.2.0 accept named data roots and Session Note 6.
Keep input names stable while changing their paths.

## Multiple Codex sources (config 7)

The top-level `sources` map use stable source_id keys. Source values use source_root
instead of home and must not repeat source_id. ASCII letters/digits/._- are allowed,
starting with a letter or digit; lowercase kebab-case is recommended. Reject spaces,
Unicode, trailing dots, Windows device names, duplicate YAML keys, and case-only
IDs across the source map. Preserve exact IDs in storage identity and provenance.
Map layers merge by ID; an explicit map suppresses implicit source defaults and
an empty map clears all sources. Per-source output paths remain final directories.

Processing iterates enabled sources in map order with a shared generation-attempt
limit and deadline. All selected stores are preflighted before mutation; per-source
locks, catalogs, evidence, notes, and checkpoints stay independent. Reject overlapping
outputs and overlapping enabled input directories. Disabled inputs are not scanned.
`--source <source_id>` selects one enabled source; targeted thread builds and storage
migration require a single selection. Migration selects the same source ID in a
standalone schema-6/7 source config. Account-based filtering is outside this contract.

Single-source reports retain schema 2 and add sourceProvider/sourceId plus
attemptedSessionNoteCount. Multiple-source responses use schema 3 with sourceResults,
aggregate threadCounts, generatedSessionNoteCount, attemptedSessionNoteCount, failed,
warnings, and reportPaths. Source-local run reports remain schema 2 in each state_root;
there is no shared persisted batch report. Compact CLI output removes threads/rawIngest
inside sourceResults; --full-output retains them. Status/provenance inspect selected
stores without scanning live chat inputs. Source failures make the overall result fail.

Schema 5/6 to 7 is an explicit configuration-only update when IDs and final roots
are retained. Move chat.providers.codex.sources to top-level sources (schema 5
also needs the ID-keyed map and home renamed to source_root); remove chat.
The new default app root is ~/.tkn/codex_chat_note_pipeline. Old omitted roots
must be made explicit using their old resolved paths before using schema 7;
relative paths must keep their original meaning if the config file moves.
Legacy migration readers resolve omitted source roots against the old application
root, never the renamed default. Generation provider settings are independent.

Storage 5, Raw manifests, catalogs, provenance and Session Note schemas are unchanged;
existing consumers continue to read each data_root without package changes.
The storage ownership filename .tkn-genai-chat-note-root.json and applicationId
tkn-genai-chat-note-pipeline are stable storage-5 identifiers retained across the
rename. Existing immutable provenance and profile hashes remain intact. Newly
published software metadata uses tkn-codex-chat-note-pipeline and the current version.
Config show uses config.sources and storage.sourceRoots.<source_id>; the top-level
sources object in its JSON response still describes configuration value provenance.
