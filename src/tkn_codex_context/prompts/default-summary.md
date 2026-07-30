---
type: prompt
id: f5dfc679-13d3-4fcc-9736-b7d4e6bb5c11
version: "1.0"
---

# Default Codex chat summary instructions

Create a concise, source-near factual summary from only the supplied chat
events or partial summaries. The result must preserve the user's requests,
material changes, explicit decisions, validation evidence, and last known
state without inventing goals, results, or next steps.

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

## State and labels

- Use `unresolved` only for an unfinished explicit user request.
- Put checks outside the completed request in `unverified`.
- A `done` result must have no unresolved items or continuation point.
- Use only the labels permitted by the supplied output schema.
- Respect every item and length limit in the supplied output schema.
