# MCP tool reference

This is a navigation map to the released tool families, not a second copy of
their JSON schemas. The configured runtime advertises the authoritative catalog;
the client must separately expose and approve it. Read a tool's current guide
with `inspect_skills(action="read", tool="TOOL_NAME")` before using it.

## Discovery and workspace

| Tool | Purpose | Tutorial |
| --- | --- | --- |
| `get_capabilities` | Runtime version, configured targets, grants and support; not live health. | [Local machine](../features/local-machine.md) |
| `inspect_skills` | Discover/read packaged or explicitly configured instructions. | [Skills](../features/skills.md) |
| `inspect_context` | Bounded selected-workspace and opt-in target observations. | [Context](../features/context.md) |
| `inspect_workspace` | Files, literal search, Git status/diff and registered worktrees. | [Workspace](../features/workspace.md) |
| `read_workspace_file` | Bounded source pages with whole-file hash and context. | [Workspace](../features/workspace.md) |
| `write_workspace_file` | Guarded whole-file creation/replacement; durable when using an original change ID. | [Workspace](../features/workspace.md) |
| `edit_workspace_file` | Preview/apply unique exact non-overlapping replacements. | [Workspace](../features/workspace.md) |
| `apply_workspace_patch` | Preview/apply an ordered multi-file `om_patch_v1` change. | [Workspace](../features/workspace.md) |
| `read_workspace_change` | Read original file-change evidence; optional current hashes are separate. | [Workspace](../features/workspace.md) |
| `create_worktree` | Create explicitly authorized Git isolation with an original execution receipt. | [Workspace](../features/workspace.md) |

## Commands and browser

| Tool | Purpose | Tutorial |
| --- | --- | --- |
| `start_execution` | Admit a native argv command under the intended workspace and grants. | [Execution](../features/execution.md) |
| `read_execution` | Original status/output/cleanup and raw-byte cursor. | [Execution](../features/execution.md) |
| `list_executions` | Receipt summaries in stable ID order, not a task search. | [Execution](../features/execution.md) |
| `control_execution` | Sequence-guarded PTY input, resize or interrupt. | [Execution](../features/execution.md) |
| `cancel_execution` | Request cancellation of the original owned worker; no rollback. | [Execution](../features/execution.md) |
| `reconcile_execution` | Record eligible local cleanup proof without replay or signaling. | [Execution](../features/execution.md) |
| `start_codex_task` | Delegate one authorized local native-browser goal. | [Browser](../features/browser.md) |
| `read_codex_task` | Read semantic result and native final-page evidence. | [Browser](../features/browser.md) |

The [SSH guide](../features/ssh.md) explains which operations use a prepared remote
worker. Browser delegation remains local. A generic command exit does not certify
a browser business action.

## Tasks

| Tool | Purpose |
| --- | --- |
| `create_task` | Store a structured goal, scope, authority and stage plan. |
| `control_task` | Claim/renew, pause/resume/cancel, revise scope, record acceptance and finish under current guards. |
| `find_tasks` | Bounded original Task rows by ID/anchor/query or declared machine/Workspace and pinned historical target; generation-safe paging. |
| `read_task` | Read current state or paged original operations/history/evidence. |
| `wait_task` | One bounded observation wait, not a scheduler or automatic wake-up. |
| `save_task` | Retain legacy checkpoints; not conversion to structured supervision. |

See [Tasks](../features/tasks.md). Mutating operations within a structured task
need current `task_context`; task creation or a successful read grants no new
machine or GitHub authority. Never copy old revision/epoch values into new work.

## Native connections and GitHub

| Tool | Purpose |
| --- | --- |
| `list_integrations` | Passive saved accounts and supported native connection methods. |
| `connect_integration` | Start one explicitly authorized Device Flow with an original request ID. |
| `read_integration_login` | Read the original login attempt without refresh/restart. |
| `cancel_integration_login` | Cancel an unfinished attempt, not an established provider grant. |
| `disconnect_integration` | Disable a selected local binding with the observed revision. |
| `github_read` | Scoped repository/source/Issue/PR/check/review/Project/Release reads. |
| `github_issue` | Create/update a scoped Issue with concurrency guards. |
| `github_pull_request` | Create/update a PR or mark it ready for review. |
| `github_comment` | Create/update/delete a supported top-level comment. |
| `github_review` | Create/update a PR review; self-comment is not independent approval. |
| `github_review_thread` | Resolve/unresolve an exact current PR review thread. |
| `github_merge` | Merge an authorized current PR only through the applicable checks. |
| `github_project` | Add an existing Issue/PR to a supported Project or update a supported field. |
| `github_release` | Create an explicitly authorized Release for an existing verified tag. |

The `github_*` tools are configuration-dependent. Native connection selection,
Core scope and provider permissions remain separate. See [GitHub](../features/github.md)
and its linked operating contract for exact action enums, account binding,
pagination and original-destination recovery.

## Common conventions

Inputs that look like `machine`, `workspace_name`, `cwd`, `worktree`, IDs, hashes,
revisions or `expected_*` values describe actual observed targets and evidence.
They are not generic defaults. Follow returned cursors/snapshots and report
incomplete coverage. Tool errors, unknown outcomes and a missing client enum must
not be worked around by replaying or using an unapproved backend.

The [packaged Skills](https://github.com/OpenMachineLabs/openmachine/tree/main/openmachine/skills)
own detailed technical contracts. The
[MCP contract baseline](https://github.com/OpenMachineLabs/openmachine/blob/v0.2.22/test/contract/mcp/mcp_contract.json)
records the released schemas. Compare both with the actual live catalog when
diagnosing a version mismatch.
