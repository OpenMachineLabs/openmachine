# Troubleshooting

Start from the first observed error and the exact selected installation. Retain
task/operation IDs and actual status; do not reinstall, clear state or repeat a
write merely because a chat response disappeared.

## Installation or initialization fails

Check [Python and SQLite requirements](introduction/requirements.md) with the
interpreter used by the selected CLI, not another shell Python. For
`task_sqlite_unsupported`, use a supported interpreter build rather than disabling
the guard. Match the wheel to its source release and dependency lock.

Use the absolute CLI path when `openmachine --version` reports an unexpected
version. `init` deliberately refuses an existing configuration; inspect it rather
than removing it. Workspaces must be actual absolute directories. Unknown TOML
fields can indicate an example from a different version.

## Core starts, but ChatGPT cannot connect

Check these boundaries separately: selected config/state, private socket, tunnel
binding/readiness, target organization/workspace association, client eligibility
and actual approved tools. Plain `doctor` does not probe the live MCP, tunnel or
ChatGPT route. For a managed service, `doctor --service` proves only its local
identity and readiness-file read.

The tunnel must point at the exact `mcp.sock` used by this runtime. Core does not
listen on an arbitrary localhost TCP port. Keep Host/Origin checks; do not expose
the private socket through an improvised unauthenticated public proxy. Follow
[Connect to ChatGPT](getting-started/connect-chatgpt.md) for the supported route.

Do not run a second server on the same state or repoint a production tunnel at a
test state. A missing client permission requires its account/workspace owner.

## A tool is missing, rejected or denied

Compare what `get_capabilities` advertises with the actual client tool definition.
GitHub tools depend on the selected configuration; external access can be denied
even when a local capability exists. Command grants and typed-write grants are
independent. Native filesystem/privacy permissions and task authority still apply.

A client-side approval or schema error is not proof the server executed the call.
A server operation ID, conversely, must be read before assuming non-admission.
Record which layer produced the error and the original error code. Do not use a
different tool or raw API to bypass the denial.

## Runtime and client schemas disagree

Record the installed CLI version, running Core version/commit and exact loaded
client definition. Read the running tool's Skill. Updating source or restarting
Core does not necessarily refresh the client's cached/approved catalog. Refresh
or reconnect through the client's supported procedure, then verify a harmless
real call before continuing the original work.

For example, if the server requires `control_task` claim renewal but the client's
`transition.action` enum lacks `renew`, do not substitute `resume` or create a
new task. Preserve the original task and wait for a matching approved catalog.
Catalog fingerprints and enriched diagnostic fields are available in v0.2.22;
older runtimes such as v0.2.21 may not expose them.

## A file changed, a patch was partial, or a workspace is busy

A stale file hash, HEAD or workspace identity protects against editing the wrong
bytes or target. Re-read the actual source and reassess the intended change.
Do not remove preconditions. Ambiguous exact replacements must be narrowed.

Read the original change ID after a lost write response. `partial` records a known
applied prefix; `outcome_unknown` cannot establish every effect. Check current
files separately and authorize only a justified remaining change. Do not replay
the whole patch or describe current files as reconstructed historical evidence.

For `resource_busy`, inspect the reported original workspace lock and admitted
operation. An idle PTY can still own that lease. Wait for the owner or explicitly
handle that original operation; `workspace_access=shared` is not a writer bypass.

## A task or command was interrupted

Use `find_tasks` to locate the intended original task, then `read_task` and its
original execution/change IDs. An execution can still be running after caller
disconnection. Read it; do not start a second copy. `waiting_for_ai` does not
promise that ChatGPT will resume automatically.

Renew only an eligible retained claim, or claim a currently eligible handoff under
current guards. Expired scope or an uncertain effect may block new operations.
Keep that blocker visible. Never finish a stage from a successful wrapper exit
without its actual acceptance evidence. See [Tasks](features/tasks.md).

For output loss, heed `output_gap` and raw cursors. For eligible local cleanup-only
uncertainty, inspect `reconcile_execution` requirements; it cannot recreate a
missing exit result or authorize another command.

## SSH connection or remote command fails

Test the existing SSH alias with batch mode and strict host-key verification.
Check the exact worker archive digest, Python/Git versions and systemd user-manager
prerequisites. `doctor --machine NAME` is an explicit probe, never provisioning.
Verify the returned target identity independently before pinning it.

An unexpected identity, unavailable remote workspace or old worker is not fixed
by substituting local results. Preserve target state and old archives used by
original jobs. Follow [Remote SSH](features/ssh.md) for the complete sequence.

## GitHub login works, but repository access fails

Check the native selected account, Core repository scope and official App resource
installation separately. Probe the intended repository with `doctor --github
--repository owner/project`. Git clone/push credentials are a separate setup.
Retired `existing_gh` connections cannot be used as a runtime fallback.

After an uncertain GitHub write, inspect the original task evidence and actual
Issue/PR/comment/Release destination. Reauthorization repairs account access,
not uncertainty about whether the write occurred. See [GitHub](features/github.md).

## Browser reports blocked or only the wrapper succeeded

Check the installed native bridge, compatible browser Skill and supported existing
login. A Codex executable alone is insufficient. Read `read_codex_task` for final
page evidence, semantic status and pending actions. Generic execution exit zero
does not prove the requested web action succeeded. Do not switch backend or copy
credentials to bypass a native block. See [Browser](features/browser.md).

## Service update or recovery is blocked

Read `service status` or explicit `manage --contract 1 status` with the correct
global paths. Preserve pending selections and original maintenance IDs.
`activation_failed_restored` means the old instance was verified after a candidate
failure, not that the new version activated. Deliberate Stop remains stopped.

Only an eligible explicit continuation may advance deferred known-unperformed
work. Unknown history, active owners or incompatible readers must not be deleted
to pass an update gate. Use compatible rollback after diagnosis, not a forced
downgrade. [Service](features/service.md) and [Updates](getting-started/updates.md)
describe the owner-specific commands.

## Safe information for a bug report

Provide Core version/commit, platform, reproduction steps in a disposable project,
the observed layer, error code, relevant IDs/hashes and which checks passed.
Remove tokens, private paths, host inventories and unrelated logs. State what is
unverified; do not label every local success as full product acceptance.
