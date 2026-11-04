---
id: om.workspace
name: openmachine-workspace
description: Read, search, edit or patch local files and Git state; choose, create or recover optional worktrees safely.
---

# Workspace operations

A local machine can explicitly set `local_access = "read_only"` or
`local_access = "full_access"` in native configuration. With either approval,
`workspace_name` accepts a canonical absolute directory as well as a configured
name. Configured Workspaces remain names and context; they do not restrict this
whole-device reach. Full retains the existing command and typed-write grants.
Read Only denies both even if their historical flags were enabled. Omission fails closed and cannot reuse historical Workspace-scoped Consent.
Credential paths, links, hashes, native receipts and Task checks still apply.
Non-Git whole-device mutations use one conservative shared lease domain; existing
busy historical leases must be clear before changing the lock domain.

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

New write/edit/patch calls respect durable Task ownership of the canonical Git
worktree or selected non-Git root, including aliases and overlapping roots. Typed
reads and previews do not acquire ownership. Non-Git reservations use the actual
selected root; the existing conservative execution lease is unchanged. Worktree
creation retains destination/common-metadata locks without reserving the source
checkout for the Task lifetime. Explicitly revise the Task to the verified new
worktree before its first mutation.
