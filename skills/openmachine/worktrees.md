# Choose and reuse a worktree

Read this reference before selecting or creating a Git worktree. Use the main
[operating Skill](SKILL.md) for task authority, foreground waits and GitHub work.

## Choose the location

Treat worktree isolation as a task decision. Do not create a tree for every Chat,
PR, subtask, dirty status or tool retry. Inspect the intended work, current Git
status and `inspect_workspace(action="worktrees")`. Use stable project/PR anchors
to find related tasks, then read their actual targets and original operations.
Listing reports Git registration and path grants, not ownership or an idle tree.
Read a suitable candidate's status, branch, instructions and current work before
reuse; do not adopt another task merely because its files are clean.

- Continue in the existing tree when the task needs its uncommitted changes.
- Reuse the tree already holding the intended branch/PR when its work and current
  operations are compatible with this continuation.
- Create one tree when independent edits, a branch switch, or an active writer
  would otherwise interfere and no suitable authorized tree can be reused.
- If necessary changes exist only in an occupied tree, coordinate the original
  work instead of silently copying, stashing or abandoning those changes.

## Create and verify once

For creation, save one execution ID and call `create_worktree` with the inspected
source identity, absent destination and explicit locally available base reference.
For independent work inspect the repository's default branch; for continuation
use the intended PR/branch. Never guess that the currently checked-out branch is
the correct base. The runtime pins the commit without fetching, does not copy
uncommitted files and does not run configured hooks/filters. Conditional includes
are rejected because the new checkout may activate different filters; no automatic
Git configuration repair is authorized. Bounded worktree operations reject
partial/promisor repositories before inspecting objects, preventing implicit
fetches. Empty `new_branch` means detached; a named
branch must be absent. The destination's parent must exist within current grants
and outside every existing Git working tree; a hidden nested checkout would still
write into an occupied tree. A suitable sibling needs an existing parent grant.
The narrow creation path coordinates destination/common-Git locks and can run
while the source has an ordinary exclusive file operation. Active writers of the
destination's configured parent scope still prevent creation.

Read the original execution until its returned state permits a next step under
the foreground waiting contract. Verified creation returns
`context.created_worktree`; a directory or zero process exit alone is insufficient.
Uncertainty, cancellation or a lost reply never justifies a second creation ID.
Use existing execution reads/cancellation, without a second operation journal.

## Continue in the selected tree

Creation and selection change no default directory or permissions. Inspect the
selected tree, revise the original task target with its optional `worktree`,
identity and HEAD, then echo `worktree` on file tools and use its root as command
`cwd`. Existing unknown-local-effect and task guards still apply. Do not retarget
around unresolved work. Reuse this target throughout related work; no automatic
retargeting per tool or task stage is needed. Non-Git tasks keep their existing
workspace behavior. These instructions improve AI choices; they do not promise
automatic conflict prediction or eliminate later merge conflicts.

## Preserve work before removal

There is no automatic cleanup. Remove a tree through ordinary authorized native
Git execution only after verifying no current users, task references, dirty files
or unpreserved commits need it. Do not infer deletion permission from task completion,
use force removal, or prune another task's work as part of creating this one.

