"""Generate an isolated Thread Note comparison without updating the live store."""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

from tkn_genai_chat_note.chat_logs import fingerprint_events, read_thread_source
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.frontmatter import frontmatter_list_value, parse_simple_frontmatter, split_frontmatter_lines
from tkn_genai_chat_note.inference import provider_name
from tkn_genai_chat_note.thread_notes import (
    Candidate,
    Project,
    ProviderSummarizer,
    chunk_events,
    generator_fingerprint,
    prepare_events,
    render_note,
    validate_thread_note,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-log", type=Path, required=True)
    parser.add_argument("--baseline-note", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reuse-dir", type=Path, help="Reuse identical inference calls from a previous review")
    args = parser.parse_args()
    config = load_app_config().thread_note_pipeline_config()
    source_bytes = args.source_log.read_bytes()
    baseline_bytes = args.baseline_note.read_bytes()
    metadata = parse_simple_frontmatter(baseline_bytes.decode("utf-8-sig"))
    output = args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.jsonl").write_bytes(source_bytes)
    (output / "before.md").write_bytes(baseline_bytes)
    source = read_thread_source(output / "source.jsonl")
    if source.thread_log is None:
        raise ValueError("source log has no thread metadata")
    thread = source.thread_log
    header, _body = split_frontmatter_lines(baseline_bytes.decode("utf-8-sig"))
    if frontmatter_list_value(header, "sourceThreadIds") != [thread.id]:
        raise ValueError("baseline note and source log identify different threads")
    source_ref = "review:/source.jsonl"
    candidate = Candidate(
        project=Project("review", "Thread Note review", output, output),
        thread_id=thread.id, started_at=thread.timestamp,
        source_path=output / "source.jsonl", source_ref=source_ref,
        source_relative_ref="source.jsonl",
        fingerprint=fingerprint_events(thread.id, source.events, source_ref),
        events=source.events, source_last_event_at=source.last_event_at,
    )

    class RecordingSummarizer(ProviderSummarizer):
        def _invoke(self, prompt: str, *, overview_only: bool = False) -> dict[str, Any]:
            key = sha256((generator_fingerprint(config) + "\n" + prompt + str(overview_only)).encode()).hexdigest()
            cached = args.reuse_dir / f"call-{key}.json" if args.reuse_dir else None
            if cached is not None and cached.is_file():
                result = json.loads(cached.read_text(encoding="utf-8"))
                self.last_metrics["cachedCalls"] = self.last_metrics.get("cachedCalls", 0) + 1
            else:
                result = super()._invoke(prompt, overview_only=overview_only)
            (output / f"call-{key}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            index = len(list(output.glob("draft-*.json"))) + 1
            (output / f"draft-{index:02}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            return result

    def progress(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

    runner = RecordingSummarizer(config, observer=progress)
    prepared = prepare_events(candidate.events)
    chunks = chunk_events(prepared, runner.chunk_characters)
    reconstructed = {event.id: "" for event in prepared}
    manifest = []
    for index, chunk in enumerate(chunks, 1):
        for piece in chunk:
            reconstructed[piece.id] += piece.text
            manifest.append({
                "chunk": index, "eventId": piece.id,
                "textPart": piece.text_part, "textPartCount": piece.text_part_count,
                "start": piece.text_start, "end": piece.text_end or len(piece.text),
                "characters": len(piece.text), "sha256": sha256(piece.text.encode()).hexdigest(),
            })
    coverage = all(reconstructed[event.id] == event.text for event in prepared)
    if not coverage:
        raise ValueError("input partition changed or omitted source text")
    (output / "input-manifest.json").write_text(
        json.dumps({"completeRedactedTextCoverage": coverage, "pieces": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result = runner.generate(candidate)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result.update({
        "_generator": provider_name(config.provider),
        "_generatorModel": config.model,
        "_generatorReasoningEffort": config.reasoning_effort,
        "_generatorProvider": config.provider,
    })
    note = render_note(candidate, result, metadata)
    (output / "after.md").write_text(note, encoding="utf-8")
    validation = validate_thread_note(output / "after.md")
    report = {
        "sourceThreadId": thread.id, "sourceLog": str(args.source_log.absolute()),
        "sourceSha256": sha256(source_bytes).hexdigest(),
        "baselineNote": str(args.baseline_note.absolute()),
        "baselineSha256": sha256(baseline_bytes).hexdigest(),
        "baselineUnchanged": args.baseline_note.read_bytes() == baseline_bytes,
        "sourceUnchanged": args.source_log.read_bytes() == source_bytes,
        "provider": config.provider, "model": config.model, "reasoningEffort": config.reasoning_effort,
        "events": len(source.events),
        "completeRedactedTextCoverage": coverage,
        "userMessages": sum(e.kind == "user_message" for e in source.events),
        "timelineEntries": len(result["timeline"]),
        "beforeCharacters": len(baseline_bytes.decode("utf-8-sig")), "afterCharacters": len(note),
        "metrics": runner.last_metrics, "validation": validation,
    }
    (output / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Thread Note review saved: {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
