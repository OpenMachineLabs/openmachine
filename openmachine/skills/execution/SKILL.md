---
id: om.execution
name: openmachine-execution
description: Start, observe, interrupt, provide authorized PTY input, resize or cancel original native commands through Open Machine.
---

# Execution operations

Save one original execution ID before starting an authorized command. Use the
configured machine, exact workspace/cwd, project interpreter and inspected argv
and preconditions. Native commands are not sandboxed by reader scopes.

For single-repository work, set `cwd` directly to that repository or worktree.
With explicit native whole-device Full authority, `cwd` need not belong to a
configured Workspace. Retired Workspace-scoped authorization cannot execute.
The original execution ID, native permissions, leases and Task checks still apply.

Read [terminal execution and controls](terminal.md) for timeouts, leases, output
cursors, PTY input, stop/cleanup states and lost-response recovery. Read the
[task continuation guide](../tasks/continue.md) when carrying structured context or
waiting for an admitted task operation.

Read the original execution after uncertainty; do not allocate a replacement ID.
Cancellation requests termination and does not undo prior effects. Require
`status=succeeded`, verified exit/output and domain readback for command success;
cancelled or timed-out commands can also exit zero. A completed command is not a
completed task.

New shell launches, including shared-access commands and bare calls, respect Task
writer reservations at their verified launch root. Distinct Git worktrees remain
independent even when they share Git metadata. Internal `cd` or `git -C` does not
change the launch binding. A finished command releases its execution lease, not
its unfinished Task's reservation. Original-ID reads and lawful cancellation
remain available; missing receipts never authorize resubmission.
