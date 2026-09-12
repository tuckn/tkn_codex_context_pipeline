## What a Session Note records

The artifact is called a **Session Note** (Japanese: **セッション記録**).
A session means one continuous sequence of conversation, recorded chronologically
in one Markdown note. This uses the ordinary meaning of session, independently
of terminology in GitHub Copilot, Claude Code, or other products.
“Chat summary” is an informal process name, not a requirement to compress the
whole record. Use `session-notes`, `type: sessionNote`, and `session-notes/` storage.
The built-in bundles are `profiles/default-jp` and `profiles/default-en`. Select one with `generation.session_note_profile`; Japanese is the default. Their schema and template are identical. Custom profiles are not supported.

- **Summary**: a short overview of the conversation's purpose and outcome.
- **Timeline**: dated, attributed entries with evidence IDs. Preserve questions,
  ideas, unaccepted options, failures, retries, corrections, actions, checks, and
  decisions. Group routine reads by purpose.
- **Last Known State**: the final observed state, latest user direction,
  unresolved requests, unverified checks, and continuation point.
- **Evidence / Source Notes**: useful exact checks and input/verification limitations,
  shown only when populated.

The application derives timestamps and actors from cited start/end events. The
renderer groups by date and displays seconds in `Asia/Tokyo`. Raw and canonical
events retain original timestamp precision and line references. Source order wins
for identical timestamps or clocks moving backwards; missing, invalid, or timezone-naive
timestamps display as unknown. Logged time is not a measurement of working duration.

Timeline entries use a time heading followed by fixed fields: `Actor`, `Type`, `Text`,
`EventRange`, and `Sources`. Actors are `User`, `AI`, and `Tool`; both assistant messages
and tool invocations use `AI`, while tool results use `Tool`. `Type` is the development
classification. `EventRange` gives the start/end IDs that determine time and actor (the
same ID twice for one event); `Sources` lists all supporting events. Never infer endpoints
from citation order. Separators are ASCII: ` - ` for time ranges, ` -> ` for event ranges,
and `: ` for fields. Missing time/day use `Unknown` / `Unknown date`.

```markdown
### 2026-05-17

- **11:27:35 - 11:27:47**
  - Actor: AI
  - Type: Action
  - Text: Investigated public access and listing behavior.
  - EventRange: L000010 -> L000020
  - Sources: L000010, L000012, L000020
```

Prose continuation lines stay indented below `Text`. Summary and Evidence use `Text`
with a child `Sources` field. Last Known State has fixed fields in this order: `Work State`,
`Detail`, `Latest User Direction`, `Unresolved`, `Unverified`, `Continuation Point`, `Sources`.
Its Sources support the state record as a whole; per-field citations are not inferred.
Unresolved/unverified items are nested `Text` records. Empty arrays display as `[]` and
empty prose as `null` (the JSON still uses empty strings). These mean no recorded value,
not that all checks passed or no user direction exists. Source Notes use `Text` records
without invented types or citations. Empty Evidence/Source Notes sections remain omitted;
omission means no recorded items, not an independent finding of no problems.
Downstream readers continue to accept the older layouts.
This Markdown is not RDF/PROV-O serialization: future conversion should use structured
source events and explicit IDs rather than infer roles from display text. The timeline
JSON schema is unchanged.

Chapter headings/order, optional-section conditions, and Last Known State field labels/order
live in `src/tkn_genai_chat_note/profiles/default-jp/template.md` / `src/tkn_genai_chat_note/profiles/default-en/template.md`.
`{{?evidence}}` / `{{/evidence}}` and `{{?source_notes}}` / `{{/source_notes}}` include
their heading and body only when the corresponding body has content. Conditional markers
occupy their own lines and cannot nest; unclosed, unknown, or duplicate blocks fail validation.
Python formats repeated body items and indentation, derives times/actors/references, and
validates the data. Inserted prose containing syntax such as `{{evidence}}` stays literal.
The output JSON and existing Markdown reader contracts remain unchanged; no dependency is added.

Long conversations are processed in parts. **Partial timeline entries are concatenated
without another model reduction**; only the overview and final state are synthesized.
There is no six-development-per-task limit or 9,000-character whole-record limit.
Each entry allows up to 900 characters. Validation checks user-message coverage,
references, and ranges crossing actors, days, or turns. These are structural and
reference checks, not proof of semantic correctness. Event text is no longer cut at
8,000 characters. After credential-shaped text is redacted, all remaining text is supplied
in order. Normal events stay whole; events larger than the default 120,000-character
serialized event-array budget are split into consecutive pieces carrying the original ID,
timestamp, part index/count, and text offsets. The budget excludes prompt/schema overhead
and is not a token limit. Repair calls receive the same source pieces. All partial timeline
entries survive merging. This preserves input coverage, not every detail in the generated
prose. Source Notes still disclose gaps already present in the source, such as tool-output
truncation; lossless input partitioning is not a source gap.

Session Notes contain source-backed facts. New ideas, recurring-work analysis,
automation suggestions, and diaries are downstream uses. Observable tool operations
and results are included; unlogged internal reasoning is not reconstructed.
Downstream readers must explicitly support Session Note schema 6. Integration
with the separate Decision and Working Context applications has not been verified
for this rename.

For a real generation comparison, use the development helper below. It saves the
baseline, a source-log snapshot, generated output, and validation report in a new
output directory. It uses the configured generation provider without updating live
notes or pipeline checkpoints.

```console
uv run python scripts/review_session_note.py --source-log <rollout.jsonl> --baseline-note <session-note.md> --output-dir <new-review-directory>
```

`--reuse-dir <previous-review-directory>` reuses cached inference responses only for
identical prompts, profiles, and provider settings. Current validation still runs;
repairs and uncached calls use the configured provider.
