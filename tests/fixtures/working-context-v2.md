---
type: workingContext
schemaVersion: 2
title: Current V2 Working Context
description: A structured v2 project control-plane fixture.
projectId: 20260203_example_project_ijkl9012
generator: Codex
status: active
projectStatus: active
health: healthy
priority: normal
currentFocus: Validate artifact compatibility.
blocked: false
mainBlocker: ""
exactNextAction: Run the fixture-based integration tests.
lastMeaningfulActivity: 2026-02-03T10:00:00+09:00
reviewAfter: 2026-03-01
dependencyProjectIds: []
promotionStatus: pending
promotedTo: []
date: 2026-02-03T09:00:00+09:00
updated: 2026-02-03T10:00:00+09:00
---

# Working Context

## Purpose

Maintain a trustworthy example project dashboard.

## Current Outcome

- Schema v2 is defined.

## Current Truth

- New artifacts use v2.
- Readers support v1 and v2.

## Active Workstreams

- Validate file-based fixtures.

## Blockers And Risks

- None.

## Important Constraints

- Do not expose private paths.

## Effective Decisions

- `state:/decisions/DR-0003-semantic-migration.md`

## Dependencies

- None.

## Key Files And Evidence

- `project:/README.md`
- `state:/working-context.md`

## Resumption

### Recommended Session

- `state:/sessions/20260203T090000+0900-schema-tests.md`

### Exact Next Action

- Run the fixture-based integration tests.

## Maintenance

### Stale Items

- None.

### Review Due

- 2026-03-01
