---
type: prompt
id: 3eb50b1e-aac2-4e98-9b54-f284179d3d77
version: "3.0"
---

# Default decision distillation instructions

Synthesize durable decisions from one or more factual Session Notes. A decision record is
for a choice that should guide work after the source session ends. It may cover
project scope, product direction, design, architecture, workflow, operations,
testing, documentation, repository conventions, collaboration, or an important
rejected option that should not be reconsidered without new evidence.

Do not create a decision for temporary session state, routine implementation
details, command logs, a simple summary, unresolved brainstorming, an assistant
proposal that was not accepted, or content better kept in a specification or
normal knowledge note.

## Source fidelity

- Use only facts stated in the supplied Session Notes or existing-decision index.
- Do not infer a rationale, approval, scope, outcome, verification, alternative,
  or next step that the source does not establish.
- Treat only an explicit user acceptance or an already implemented operational
  practice as `Accepted`. Otherwise use `Proposed`.
- Keep `status` separate from `implementationStatus`.
- Use `verified` only when the sources contain evidence that validates the
  implemented or operational result. Preserve incomplete, failed, or blocked
  checks in `verificationLimitations`; do not hide them behind other evidence.
- Prefer corrections and later source statements over superseded statements.
- Use an empty array or empty string when a field is not established.
- Keep fields non-redundant. Do not repeat the decision as rationale, a context
  fact as a consequence, or verification evidence as a project-specific detail.
- The Decision Record v3 renderer omits empty optional sections. Do not add
  filler merely to make an optional field visible.
- Write natural Japanese except for headings, paths, commands, identifiers, and
  product names.

## One central decision per object

The unit of output is a central decision, not a Session Note. Compare all
supplied Session Notes before producing output. When several notes establish,
repeat, refine, or verify the same central decision, return one decision object
with the union of their `sourceSessionRefs`. Create separate objects only for
independently reusable decisions. Do not split one decision merely because it
has several consequences. Return no decisions when the sources contain no
durable explicit decision.

Every decision object must list one or more exact `sourceSessionRefs` from the
application-managed input. Do not invent or normalize those references. A
Session Note may support several independent decisions, and a decision may be
supported by several Session Notes.

## Existing decision handling

The existing-decision index contains `decisionId`, `title`, `status`,
`reviewStatus`, `updateAllowed`, `sourceSessionRefs`, and the central decision
text. Updateable entries also include a bounded `recordExcerpt`; preserve its
source-backed facts unless a supplied Session Note corrects or supersedes them.
When `qualityUpgradeRequired` is true and the supplied sources support that
decision, use `update`, not `existing`, so the record adopts the current
quality contract.

- Use `disposition: existing` only when the source decision is semantically the
  same decision already represented by one index entry and the existing record
  needs no factual correction or material evidence improvement.
- For `existing`, set `existingDecisionId` to that exact ID and leave all new
  record content fields empty.
- Use `disposition: update` only when the same decision is represented by an
  index entry with `updateAllowed: true`, and the combined sources materially
  correct or improve its context, rationale, applicability, verification,
  limitations, materialization, or provenance. Set `existingDecisionId` and
  provide a complete replacement draft using all relevant source refs.
- Never use `update` when `updateAllowed` is false. A reviewed record's central
  judgment is not rewritten automatically.
- Use `disposition: create` for a genuinely new durable decision and leave
  `existingDecisionId` empty.
- Do not claim that an existing decision was updated; this stage creates records
  or adds source provenance to an existing record without rewriting its central
  judgment.
- A decision ID mentioned inside a Session Note is not an existing local record
  unless that ID is present in the supplied existing-decision index. Treat an
  unavailable or legacy ID as information contained by the Session Note, not as
  `existingDecisionId` or a directly resolvable `relatedEvidence` entry. The
  Session Note's own source ref already preserves that evidence. Mention the
  unavailable legacy reference in `sourceLimitations` when it matters.

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
- `verificationLimitations`: incomplete, failed, blocked, indirect, or
  otherwise limited validation stated by the sources.
- `validationDate`: ISO date only when established by the source.
- `relatedEvidence`: only source-backed logical references, files, specs, tests,
  issues, or pull requests. Do not invent paths.
- `materialization`: downstream places that the source says should reflect the
  decision. Session Notes and Decision Records are evidence artifacts, not
  repository documentation destinations. Do not place input session refs,
  `DR-NNNN` files, or this generated record in `repositoryDocumentation`. Use
  empty arrays when no downstream destination is stated. The v3 artifact stores
  destination arrays in Frontmatter and renders only `followUp` in the body.
- `supersedes` and `supersededBy`: exact known decision IDs or durable artifact
  references only.

## Mode: `distill-session-decision`

Review all Session Notes together with the existing-decision index, synthesize
same-decision evidence across sources, then return new or existing decision
mappings. Keep the output concise and durable.

## Mode: `repair-invalid-draft`

Correct the supplied draft only enough to satisfy the validation error and this
contract. Do not add facts during repair.
