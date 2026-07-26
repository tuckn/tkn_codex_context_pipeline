---
type: decision
schemaVersion: 1
title: Preserve explicit v1 decisions
description: An explicit v1 decision fixture.
generator: Codex
status: Accepted
scope: project
promotionStatus: no-action
promotedTo: []
date: 2026-01-13T09:00:00+09:00
updated: 2026-01-13T10:00:00+09:00
decisionId: DR-0002
---

# DR-0002: Preserve explicit v1 decisions

## Context

Metadata-only review does not change the decision body.

## Decision

Keep the record as v1 until a semantic migration is required.

## Consequences

- The declared schema continues to match the body.

## Alternatives considered

- Change only the version number.

## Related files

- `state:/working-context.md`

## Notes

- None.
