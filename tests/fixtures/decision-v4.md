---
type: decision
schemaVersion: 4
title: Omit empty Decision Record sections
description: Render only Decision Record sections that contain source-backed information.
generator: Codex
status: Accepted
scope: project
implementationStatus: verified
promotionStatus: no-action
promotedTo: []
projectWorkingContextTargets: []
repositoryDocumentationTargets: []
globalContextTargets: []
skillAutomationTargets: []
sourceThreadNoteRefs:
  - project:/thread-notes/example.md
sourceThreadNoteSetSha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
date: 2026-08-08T09:00:00+09:00
updated: 2026-08-08T10:00:00+09:00
decisionId: DR-0004
---

# DR-0004: Omit empty Decision Record sections

## Decision

Keep structured decision fields, but omit empty optional sections from the human-readable Markdown.

## Why

**Context**

- Fixed v2 records contained many empty sections.

**Rationale**

- Readers should reach the central judgment without navigating placeholder content.

## Verification

**Evidence**

- The v3 fixture omits empty-value placeholders.

**Validation Date:** 2026-08-08
