---
id: om.workspace
name: openmachine-workspace
description: Read, search, edit or patch configured workspace files and Git state; choose, create or recover optional worktrees safely.
---

# Workspace operations

Read current source, applicable instructions, raw whole-file SHA256, workspace
identity and HEAD before proposing an edit. Identity includes root and branch.
Preserve unrelated dirty/staged work; an old hash or test PASS is not current review.

- Read [file discovery, exact edits and patches](files.md) before source paging,
  replacement, multi-file changes or original change recovery.
- Read [worktree selection and recovery](worktrees.md) before selecting or creating
  a tree. Inspect existing trees and tasks first; allocation is never automatic.

Prefer an exact small edit, preview it with an empty change ID, then apply under one
saved original ID and current guards. Whole-file writes have no preview API: review
the complete replacement. After a lost reply read the original change receipt.
Preflight is not a multi-file transaction; retain partial/unknown outcomes.

Typed writes need the workspace-write grant. Structured task effects also need the
[current claim and task context](../tasks/continue.md). Verify actual changed files
and run explicitly authorized validation through the [execution tools](../execution/SKILL.md).
