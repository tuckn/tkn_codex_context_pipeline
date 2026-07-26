---
type: session
schemaVersion: 1
title: Legacy V1 Session
description: An explicit v1 session fixture.
generator: Codex
status: done
distillationStatus: pending
distilledTo: []
date: 2026-01-11T09:00:00+09:00
updated: 2026-01-11T10:00:00+09:00
sessionId: 20260111T090000+0900
---

# Session Note

## Goal

Preserve compatibility with explicit v1 session notes.

## Done criteria

- Read and distill the note without changing its body schema.

## User intent / interaction summary

- The user requested backward compatibility.

## Current state

- The source explicitly declares v1.

## Working context

- `project:/README.md`

## Changed files

- None.

## Important decisions

- Metadata-only finalization must preserve v1.

## What worked

- Explicit version detection.

## Failed approaches

- Relabeling an unchanged body as v2 was rejected.

## Open issues

- None.

## Next steps

- Finalize distillation metadata.

## Exact next step

- Run the finalization command.

## Constraints

- Preserve the body.

## Validation

- Fixture reviewed.
