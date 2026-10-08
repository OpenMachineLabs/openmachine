# Delegate and verify a browser subtask

[Domain guide](SKILL.md)

## Codex browser delegation

`start_codex_task` delegates an explicit browser/UI subtask, not the whole Chat
workflow. Supply a local configured `machine`, workspace `cwd`, original
`execution_id`, bounded `goal`, `start_url` and exact `allowed_origins`. `mode`
is browser-only; `browser` is auto/iab/chrome/edge. Timeout is 1-1800 seconds.
The existing local execution grant, lifecycle gate, task claim and native workspace
scope apply. Structured contracts include `start_execution` (the underlying
operation) and use the same operation/execution ID; no second task record exists.

The adapter discovers the enabled native `node_repl` bridge in CODEX_HOME/config.toml
and reads the browser Skill from that bridge's installed desktop-app resources.
It uses `codex` on PATH when present; a packaged service without the developer's
shell PATH uses `codex-cli` beside that same configured native bridge, either as
an executable or the directory containing `CodexCLI.app/Contents/MacOS/codex`.
It does not scan other apps, install anything, copy browser libraries,
read authentication files, or change configuration. A compatible native desktop
bridge and a signed-in Codex installation are prerequisites, not supplied by CLI
installation alone. The child uses an empty temporary instruction home and
`project_doc_max_bytes=0`, so global and project AGENTS are not inherited. Only an
original private file-backed `auth.json` is referenced by a symlink; the adapter
never reads or copies its contents. Native Codex owns authentication and renewal.
The temporary home is removed on ordinary completion without deleting the original
auth file. Missing or unsafe file-backed login (including keyring-only setups)
blocks this adapter; it never changes credential stores or creates a new login.
Discovery metadata is passive; actual setup may be blocked.
Only the configured native browser bridge is delegated; shell, connectors,
subagents, memory and other plugins are disabled for this child. Native bridge
access is not constrained by the CLI's read-only shell sandbox. Navigation origins
are instructions plus observed checks, not a firewall or cross-chat isolation.

Use `read_codex_task` for semantic status and final native page evidence. Generic
`read_execution` success only says the wrapper finished; Chat reviews the report
before accepting a stage. `blocked`/`needs_confirmation` require the indicated
resolution, never implicit permission. Missing or mismatched evidence is unknown.
`cancel_execution`, original log/readback, timeouts and the `browser:codex` resource
lease are reused. The lease coordinates this OM runtime only, not manual browsing
or independent Codex jobs. Cancellation is not rollback of browser side effects.
No automatic Chat resume, desktop computer mode or alternative backend is added.

Give the child a self-contained goal, permitted actions, visible acceptance and
controller-owned tab context, without credentials or unrelated private data. Review
its actual report, matching final native snapshot and `pending_action`. An explicitly
authorized follow-up uses a new operation ID under the same parent task and exact
tab context.
