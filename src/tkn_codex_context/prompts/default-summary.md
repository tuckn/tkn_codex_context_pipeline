---
type: prompt
id: f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11
version: "2.0"
---

# Default Codex chat summary instructions

Create a concise, source-near factual summary from only the
application-managed chat events or partial summaries. The result must preserve
the user's requests, material changes, explicit decisions, validation evidence,
and last known state without inventing goals, results, or next steps.

The application supplies a `MODE`. Apply the common instructions in this
document plus the one mode section whose name matches `MODE`. Do not apply the
other mode-specific procedures.

## Source fidelity

- Do not infer facts, decisions, outcomes, or recommendations that are absent.
- Cite `eventIds` for every material source-backed fact.
- Preserve corrections over superseded statements.
- Omit repetitive command-by-command chronology and incidental tool detail.
- Use an empty array or empty string when the source does not establish a field.

## Language and organization

- Write natural Japanese except for literal headings, paths, commands,
  identifiers, and product names.
- Avoid unnecessary English prose.
- Use short, independent summary items.
- Use one work item for a coherent task and multiple work items only for
  independent tasks in the same chat.
- Provide a short, descriptive ASCII `fileSlug`.

## Output elements

- `title`: a specific Japanese title for the work represented by this chat.
- `fileSlug`: a stable short ASCII kebab-case filename component.
- `description`: one compact standalone sentence stating the scope and outcome.
- `summaryItems`: one to five independent bullets covering the most important
  requests, decisions, actions, validations, results, and current state. Do not
  repeat the same fact in slightly different words.
- `workItems`: coherent tasks in the chat. Use one work item when the chat is
  one continuous task; split only genuinely independent tasks.
- `workItems[].title`: a short task name, not a sentence or generic label.
- `workItems[].developments`: material developments classified with exactly one
  permitted label and supported by event IDs.
- `evidence`: especially useful quantitative results, exact verification
  outcomes, durable artifacts, or operational facts. Do not duplicate ordinary
  narrative merely to fill this field.
- `lastKnownState`: the final observable state of the user's requested work.
- `sourceLimitations`: material uncertainty or a claimed result that was not
  independently verified. Use an empty array when none matters.

## Development labels

- `Request`: an explicit user request or acceptance criterion.
- `Clarification / Correction`: a corrected fact, changed requirement, or
  superseded understanding. Preserve the corrected state.
- `Proposal`: an option or recommendation that was not implemented or accepted.
- `Action`: a material implementation or state-changing step actually taken.
- `Reported Result`: an outcome reported by the user, assistant, or tool.
- `Validation`: a concrete check and its observed outcome.
- `Explicit Decision`: a decision explicitly made or accepted, not an inferred
  preference.

Do not classify the same development under multiple labels. Prefer the label
that best represents its role in the completed work.

## Last known state

- Use `unresolved` only for an unfinished explicit user request.
- Put checks outside the completed request in `unverified`.
- A `done` result must have no unresolved items or continuation point.
- `detail` states what is complete, incomplete, blocked, or awaiting input.
- `latestUserDirection` records the user's latest material direction, even when
  the requested work is otherwise complete.
- `continuationPoint` names the exact safe resumption point only when work is
  unfinished.
- Use only the labels permitted by the supplied output schema.
- Respect every item and length limit in the supplied output schema.

## Mode: `source-events`

Create a factual partial or complete result from the supplied events. Cite
`eventIds` for every material fact. When processing one part of a multi-part
chat, cover only facts supported by that part; do not guess what other parts
contain.

## Mode: `merge-partial-summaries`

Merge the ordered partial summaries into one compact result. Remove
duplication, preserve corrections over superseded statements, retain event IDs,
combine matching work items, respect every output limit, and do not add facts
or recommendations. Reassess the overall last known state from the ordered
partials instead of mechanically copying an earlier partial state.

## Mode: `repair-invalid-draft`

Correct the supplied draft only enough to satisfy the reported validation
error. Keep only source-backed facts, shorten rather than expand, write natural
Japanese except for literal identifiers, and do not add new event IDs. Preserve
valid content and return a complete replacement object.
