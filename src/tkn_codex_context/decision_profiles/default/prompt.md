---
type: prompt
id: 3eb50b1e-aac2-4e98-9b54-f284179d3d77
version: "1.0"
---

# Default decision distillation instructions

Extract durable decisions from one factual Session Note. A decision record is
for a choice that should guide work after the source session ends. It may cover
project scope, product direction, design, architecture, workflow, operations,
testing, documentation, repository conventions, collaboration, or an important
rejected option that should not be reconsidered without new evidence.

Do not create a decision for temporary session state, routine implementation
details, command logs, a simple summary, unresolved brainstorming, an assistant
proposal that was not accepted, or content better kept in a specification or
normal knowledge note.

## Source fidelity

- Use only facts stated in the supplied Session Note or existing-decision index.
- Do not infer a rationale, approval, scope, outcome, verification, alternative,
  or next step that the source does not establish.
- Treat only an explicit user acceptance or an already implemented operational
  practice as `Accepted`. Otherwise use `Proposed`.
- Keep `status` separate from `implementationStatus`.
- Prefer corrections and later source statements over superseded statements.
- Use an empty array or empty string when a field is not established.
- Write natural Japanese except for headings, paths, commands, identifiers, and
  product names.

## One central decision per object

Create separate objects for independently reusable decisions. Do not split one
decision merely because it has several consequences. Return no decisions when
the Session Note contains no durable explicit decision.

## Existing decision handling

The existing-decision index contains `decisionId`, `title`, `status`, and the
central decision text.

- Use `disposition: existing` only when the source decision is semantically the
  same decision already represented by one index entry.
- For `existing`, set `existingDecisionId` to that exact ID and leave all new
  record content fields empty.
- Use `disposition: create` for a genuinely new durable decision and leave
  `existingDecisionId` empty.
- Do not claim that an existing decision was updated; this stage creates records
  or links a source Session Note to an existing record.

## New decision fields

- `title`: short display title without a `DR-NNNN` prefix.
- `fileSlug`: short lowercase ASCII kebab-case summary of the central decision.
- `description`: one compact standalone sentence.
- `context`: facts that made a decision necessary.
- `decision`: the central choice, stated directly.
- `rationale`: explicit selection criteria, not a repetition of the decision.
- `benefits` and `costsAndRisks`: stated consequences only.
- `alternativesConsidered`: alternatives explicitly considered or rejected.
- `appliesWhen` and `doesNotApplyWhen`: established applicability boundaries.
- `reusablePrinciples`: reusable guidance separated from local details.
- `projectSpecificDetails`: details unique to this Project.
- `verificationEvidence`: implementation or validation evidence.
- `validationDate`: ISO date only when established by the source.
- `relatedEvidence`: only source-backed logical references, files, specs, tests,
  issues, or pull requests. Do not invent paths.
- `materialization`: downstream places that the source says should reflect the
  decision. Use empty arrays when none are stated.
- `supersedes` and `supersededBy`: exact known decision IDs or durable artifact
  references only.

## Mode: `distill-session-decision`

Review the Session Note and the existing-decision index, then return new or
existing decision mappings. Keep the output concise and durable.

## Mode: `repair-invalid-draft`

Correct the supplied draft only enough to satisfy the validation error and this
contract. Do not add facts during repair.
