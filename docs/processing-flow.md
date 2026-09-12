### session-notes build: generate Session Notes from Raw

Capture and normalize conversation logs, then generate Markdown Session Notes for
eligible conversations. Use `--thread-id` to select one conversation. This command
does not generate Decisions or Working Context. The model receives event content
and IDs, generation instructions, and the output schema.

The following shows `tkn-genai-chat-note session-notes build`. The legend applies
to the diagram immediately below it.

| Diagram notation | Configuration key | Default location |
| --- | --- | --- |
| `C` | `chat.providers.codex.home` | `~/.codex` |
| `R` | `raw_root` | `~/.tkn/genai_chat_note_pipeline/raw` |
| `D` | `data_root` | `~/.tkn/genai_chat_note_pipeline/data` |
| `S` | `state_root` | `~/.tkn/genai_chat_note_pipeline/state` |

`T` is a conversation's `threadKey` and `H` is a content hash.
They are placeholders in the diagram.

```mermaid
sequenceDiagram
    autonumber
    actor U as User or scheduler
    participant P as Pipeline CLI
    participant C as Codex storage
    participant F as Storage R, D, S
    participant AI as Inference backend

    U->>P: session-notes build
    P->>P: Read config.yaml<br/>Paths, source identity, model
    P->>F: Read S/codex/{sourceId}/ledger.json and stage state

    P->>C: C/sessions/**/*.jsonl<br/>C/archived_sessions/**/*.jsonl
    C-->>P: Original conversation bytes
    P->>F: R/codex/{sourceId}/sessions/YYYY/MM/DD/rollout-*.jsonl<br/>Preserve original bytes
    P->>F: R/codex/{sourceId}/manifest.jsonl<br/>Update source, time, hash

    opt Project metadata is available
        P->>C: C/.codex-global-state.json
        C-->>P: Projects and conversation membership
        P->>F: R/codex/{sourceId}/metadata/H.json
    end

    P->>P: Parse Raw and normalize events<br/>IDs, messages, times, source line references
    P->>F: D/codex/{sourceId}/source-aligned/T/H.json<br/>Canonical Events

    loop New, changed, or unfinished eligible conversation
        P->>P: Prepare events and split long input
        P->>AI: Thread ID, event content and IDs<br/>Generation instructions and output schema
        AI-->>P: Partial timeline, overview, and evidence IDs
        opt Input was split
            P->>AI: Synthesize overview and final state
            AI-->>P: Overview and final-state JSON
        end
        P->>P: Preserve and concatenate timelines<br/>Derive timestamps and actors, validate, render Markdown
        P->>F: D/codex/{sourceId}/session-notes/YYYY/MM/*.md<br/>Session Note
        P->>F: Record provenance and checkpoint
    end
```

The saved Canonical Events and the summarizer's input originate from the same
parse. The current implementation passes in-memory events to the summarizer;
it does not re-read the saved canonical JSON for that step. Summarization is
per conversation, independent of work-scope grouping.
