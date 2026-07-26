---
type: decision
title: Preserve legacy decision readability
description: A realistic unversioned v1 decision fixture.
generator: Codex
status: Accepted
scope: project
promotionStatus: pending
promotedTo: []
date: 2026-01-12T09:00:00+09:00
updated: 2026-01-12T10:00:00+09:00
decisionId: DR-0001
---

# DR-0001: Preserve legacy decision readability

## Context

Existing projects contain decision records without an explicit schema version.

## Decision

Treat an unversioned decision as v1.

## Consequences

- Existing records remain readable.
- Semantic migration is required before writing v2.

## Alternatives considered

- Reject all unversioned records.

## Related files

- `state:/sessions/example.md`

## Notes

- Review the record before migration.
