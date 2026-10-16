# Local machine

Local operations act on the computer running Core, under its native account.
Use them to inspect a project, edit its files or run an explicitly authorized
command without copying the project into a hosted execution environment.

## Before you begin

Complete [installation](../getting-started/installation.md) and register an
existing absolute workspace. The machine name is a configuration key, not an
automatically discovered device. A typical read-only entry is:

```toml
[machines.local]
transport = "local"
execution_enabled = false
workspace_write_enabled = false

[workspaces.project]
machine = "local"
root = "/absolute/path/to/project"
```

This is a fragment of a configuration with `schema_version = 1`. Replace the
example root. Review [permissions](../configuration/reference.md) before enabling
commands or typed edits. A managed service's PATH can differ from your terminal.

## Inspect the intended project

Paste into an authorized connected conversation:

```text
Use OpenMachine to inspect machine local, workspace project, read-only. Read
the relevant Skills and applicable AGENTS.md. Show the observed workspace,
branch and HEAD, then summarize README.md. Do not execute project scripts,
install dependencies, create tasks, edit files or inspect other workspaces.
```

The assistant should use `get_capabilities`, `inspect_context` and bounded
workspace reads. It must distinguish a confirmed non-Git/unborn workspace from
unavailable Git, and read applicable instructions rather than treating a returned
instruction path as already-read content.

## Verify and extend

Check that the reported workspace and machine match your selected configuration.
For a file read, compare the returned whole-file SHA256 with the actual file.
Incomplete pages require continued reads with the original hash/snapshot.

Use [Workspace](workspace.md) for file changes, [Execution](execution.md) for
commands and [Context](context.md) for opt-in environment information. A native
permission denial needs diagnosis in the owning account; do not run Core with
elevated privileges as a generic fix.

## Limits

Workspace scopes bound typed file operations, not everything a permitted native
command can access. macOS privacy controls and ordinary filesystem permissions
still apply. Core does not install programs, grant Full Disk Access, allocate a
machine or discover unrelated project directories automatically. See
[security](../configuration/security.md).
