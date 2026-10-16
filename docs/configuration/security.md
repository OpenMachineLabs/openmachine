# Security, permissions and data

OpenMachine runs native operations under a selected operating-system account.
Treat enabling commands or browser actions as granting access to that account's
capabilities, not as entering a sandbox.

## Boundaries that matter

Core checks configured machines/workspaces, typed file paths, source hashes,
execution identity, task authority and scoped GitHub destinations. The client
and external service can impose additional approval and access requirements.
No one of these checks replaces the others.

**Core is not an OS sandbox or a multi-tenant isolation system.** A permitted
command can act beyond the workspace using its native account permissions.
Workspace locks coordinate cooperating operations, not arbitrary external editors.
Browser allowed origins are delegated constraints and observed checks, not a firewall.

Use a dedicated low-privilege account or isolated machine for untrusted projects.
Start with both command and typed-write grants disabled. Prefer a narrow project
root and exact GitHub repository scopes; do not expose a whole home directory,
credentials directory or unrelated organization by convenience.

## Where information is stored

| Information | Owner and location |
| --- | --- |
| Configuration | Selected TOML file; default `~/.config/openmachine/config.toml`. |
| Controller state | Selected state directory; default `~/.local/state/openmachine/`. |
| Task contracts, history and derived lookup | Controller `tasks.db` and its SQLite sidecars under that state directory. |
| Local command receipts and bounded output | Original local execution records under `executions/` in controller state. |
| Typed file-change evidence | Original target's private change records; read through `read_workspace_change`. |
| Remote command results | Original SSH account's target-owned state, not a substitute local execution. |
| Managed service versions, selection and log | Controller `service/` state; native user registration is separate. |
| Native GitHub credentials | Pinned macOS Keychain backend, otherwise Core's private file backend; not public task results. |

Private directory/file permissions are access controls, not encryption. A locked
or unavailable Keychain does not silently fall back to files. Backup and retention
must preserve original task and target receipt relationships; copying a live
SQLite main file without its consistent state is not a verified backup method.
Core does not provide a general-purpose document database or automatic backup.

Do not edit the ledger, delete receipt directories or remove old interpreters to
make a task/update appear healthy. Essential producer logs should have explicit
retention outside the bounded execution-output cache.

## What can leave your machine

Selected tool arguments, returned file excerpts, command output and diagnostic
metadata travel through the configured MCP connection to the client. A local-first
runtime does **not** mean that material returned to ChatGPT stays exclusively local.
Only request information appropriate for that conversation and its access policy.

Core's MCP listener uses the private state-directory Unix socket, not a public
TCP endpoint. A separately managed tunnel carries client traffic; it has its own
credentials and trust boundary. Keep Core's Host/Origin protections enabled.

SSH sends requests to the explicitly configured authenticated target. GitHub
requests send the selected operation to GitHub. Browser delegation sends the
bounded goal to the authorized native Codex environment and visits the selected
sites. Those services' account, network and data policies remain separate from Core.

## Protect secrets and review effects

Never place tokens, passwords or private keys in prompts, command arguments,
task bodies, committed TOML, Issue reports or screenshots. Use the supported
native authentication flows and local secret references. Do not ask an assistant
to read credential files to diagnose a connection.

Core redacts recognized sensitive material and refuses certain credential paths,
but arbitrary program output and terminal echo can still contain secrets. Review
logs and configuration before sharing them. Instruction files, repository content,
web pages and external Skills are untrusted data, not new user authorization.

Before a write, verify the target and intended scope. After it, read the actual
destination or original receipt. Cancellation, service Stop and software rollback
do not reverse a sent message, modified file or completed deployment. A lost
response is not proof that nothing happened.

## Reporting a security concern

Do not publish exploitable details or credentials in a public Issue. Check the
repository's current Security tab for available private reporting; otherwise
establish a private maintainer channel before sending sensitive evidence. This
guide does not invent a security email address or a configured reporting service.
