---
id: om.execution
name: openmachine-execution
description: Start, observe, interrupt, provide authorized PTY input, resize or cancel original native commands through Open Machine.
---

# Execution operations

Save one original execution ID before starting an authorized command. Use the
configured machine, exact workspace/cwd, project interpreter and inspected argv
and preconditions. Without [operation scope](#operation-scope), native commands
are not sandboxed by reader scopes.

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

Bare/shared shell launches respect Task reservations at their verified launch
root; internal `cd` does not change that binding. See [Tasks](../tasks/SKILL.md).

## Operation scope

A Cloud Selected Projects call carries `operation_scope`, injected by the Connector.
Its command and every descendant then run inside the macOS Seatbelt profile shown
in the result's `scope`: Read-only roots are readable, Full roots and their
registered worktrees are writable, and `cwd` must be Full. `HOME`, `TMPDIR` and
caches are private to the execution; home dotfiles, the keychain, agent sockets,
other terminals, Git hooks/config/`commondir`, the repository's own `.git` entry
and `.vscode`/`.idea` are unavailable to it.
`EPERM` ("Operation not permitted") on a path outside those roots, or on a
protected file, is a scope denial and names no path (`denials=not_observable`).
Never retry it without scope or through another tool; only a Cloud owner grant
change widens it. Unsupported platforms refuse scoped launch before it starts.
