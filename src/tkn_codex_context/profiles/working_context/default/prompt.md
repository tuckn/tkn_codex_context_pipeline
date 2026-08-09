---
type: prompt
id: f6d2dbd3-f06c-49dd-9d72-73cd51c7c0e8
version: "1.0"
---

# Default Working Context synthesis instructions

Create a concise orientation dashboard describing what is currently true for one
Codex Project. This is not a chronological summary, transcript, activity log, or
replacement for its evidence artifacts. Replace stale statements with the best
supported current statement.

## Source fidelity and precedence

- Use only facts in the supplied sources. Do not infer goals, health, priority,
  approval, completion, blockers, relationships, definitions, or next actions.
- Prefer current repository and Git evidence for present filesystem and Git
  state; reviewed Accepted Decision Records for durable judgments; other
  Accepted Decision Records for established judgments; and newer Thread Notes
  for work state and outcomes.
- A `Proposed` decision, an assistant proposal, or an unresolved idea is not
  current truth. It may appear only as active work, a risk, or a limitation when
  the source explicitly establishes that status.
- When sources conflict, prefer explicit corrections and the later, more
  authoritative source. Do not preserve superseded text merely as history.
- Decision Records are authoritative for durable judgments; do not duplicate
  the same judgment as an independent Thread Note fact.
- Every item must contain one or more exact source references from the managed
  input. Do not invent, normalize, or broaden references.
- Use `project:/` for application-owned Project data and `repo:/` for current
  repository evidence. They are logical references, not filesystem URLs.
- Keep each field non-redundant and concise. Use empty arrays or empty strings
  when optional content is not established. The renderer omits empty sections.
- Write natural Japanese except for headings, paths, commands, identifiers, and
  product names.

## Dashboard fields

- `title`: a short Project-specific display title without the words Working
  Context unless needed for clarity.
- `description`: one compact standalone sentence summarizing the Project's
  current purpose and state.
- `projectStatus`: use `active`, `paused`, `blocked`, `completed`, or `archived`
  only when established; otherwise `unknown`.
- `currentFocus`: the central current workstream only when sources establish it.
- `blocked` and `mainBlocker`: set blocked true only when a source establishes a
  blocker that prevents the next material action.
- `exactNextAction`: a concrete source-backed next action. Do not convert a broad
  aspiration or model recommendation into an action.
- `projectOverview`: one to four durable orientation statements. This field is
  required and must not be a timeline.
- `currentTruth`: the smallest useful set of currently valid statements. This
  field is required.
- `currentOutcome`: recent material outcomes that define the present state.
- `activeWork`: active or explicitly unfinished work only.
- `risksAndConstraints`: material constraints, known risks, blockers, and
  verification limitations that affect current work.
- `effectiveDecisions`: Accepted, non-superseded decisions that currently guide
  the Project. `decisionRef` must be the exact Decision Record source ref.
- `semanticGlossary`: Project-specific terms whose local meaning or distinction
  improves orientation. Do not add generic dictionary definitions. Keep the
  list small.
- `taxonomyItems`: source-backed concepts, artifacts, systems, components, or
  workstreams. `parent` is another emitted label or empty.
- `taxonomyRelations`: explicit source-backed relationships between emitted
  taxonomy labels. Do not infer architecture from names or paths.
- `keyEvidence`: only references worth opening during orientation or resumption.
- `resumption`: source-backed context needed to continue work.
- `sourceLimitations`: missing, conflicting, stale, bounded, or indirect source
  evidence that materially limits the dashboard.

## Mode: `synthesize-working-context`

Synthesize a current dashboard from the supplied source artifacts. Compare all
sources in the batch before returning output.

## Mode: `merge-working-context-drafts`

Merge bounded, source-backed drafts into one dashboard. Remove duplication,
resolve conflicts using the precedence rules, and preserve exact source refs.
Do not treat draft wording as a new source.

## Mode: `repair-invalid-draft`

Correct the supplied draft only enough to satisfy the validation error and this
contract. Do not add facts during repair.
