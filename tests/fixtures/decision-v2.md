---
type: decision
schemaVersion: 2
title: Use semantic schema migration
description: A structured v2 decision fixture.
generator: Codex
status: Accepted
scope: mixed
implementationStatus: verified
promotionStatus: pending
promotedTo: []
date: 2026-02-02T09:00:00+09:00
updated: 2026-02-02T10:00:00+09:00
decisionId: DR-0003
---

# DR-0003: Use semantic schema migration

## Context

Schema v2 changes required metadata and body sections.

## Decision

Migrate the meaning of v1 content before declaring v2.

## Rationale

The version must describe the actual artifact contract.

## Consequences

### Benefits

- Readers can trust the declared schema.

### Costs And Risks

- Migration requires classification of legacy content.

## Alternatives Considered

- Relabel unchanged v1 content as v2.

## Applicability

### Applies When

- A writer changes legacy body content.

### Does Not Apply When

- A reader performs orientation only.

### Reusable Principle

- Version identifiers describe semantics, not timestamps.

### Project-Specific Details

- None.

## Verification

- Evidence: Compatibility tests.
- Validation Date: 2026-02-02

## Related Evidence

- `project:/README.md`

## Materialization

- Project Working Context: Pending.
- Repository Documentation: Updated.
- Global Context: None.
- Skill / Automation: Fixture validation.
- Follow-up: Run all tests.

## Supersession

- Supersedes: None.
- Superseded By: None.
