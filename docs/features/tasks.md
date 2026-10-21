# Persistent tasks and explicit continuation

A task records the goal, boundaries, authority, stages and original operation
evidence of a piece of work. Use it for changes that need verification across
multiple steps or retrieval after a conversation interruption.

**Persistence does not mean autonomous ChatGPT execution.** Core can preserve
and observe already admitted work without the original caller; it does not wake
ChatGPT, decide the next stage or create new authority after disconnection.

## Prerequisites and storage

The controller needs a [supported SQLite engine](../introduction/requirements.md)
and its original private state directory. Task data lives in the controller's
SQLite ledger. Execution and file-change receipts retain their existing owners,
including remote target owners. Do not use task prose as a replacement for those
receipts or manually edit database rows.

Read-only task discovery needs no new machine mutation. A new structured task
needs explicit purpose/scope, acceptance conditions, source evidence, target
identity, allowed tools, expiry and stage definitions. GitHub work also binds the
intended native account. The current runtime's creation Skill owns the exact
schema; do not copy stale revisions, hashes or example IDs into a live contract.

## Create a bounded task

In an authorized disposable workspace with typed writes enabled:

```text
Create one durable OpenMachine task for this project: create hello.txt only if
absent, containing 'hello from OpenMachine' plus a newline, then read back and
verify the exact bytes and SHA256. No other files, commands or GitHub operations.
Inspect the workspace and applicable Skills first. Record the contract, original
task/change IDs and verification evidence, then finish only after acceptance.
```

The assistant discovers existing matching work first, creates a task only when
needed, claims the current handoff, and attaches its permitted operations through
`task_context`. A stage is accepted from actual original evidence, not a prose
claim that the file was written. `finish` follows acceptance of the current plan.

## Recover after an interruption

Save the task ID shown in the conversation. In the same or a new authorized
conversation, use:

```text
Continue OpenMachine task REPLACE_WITH_ORIGINAL_TASK_ID. First read its current
contract, foreground state, operations and original results. Do not create a new
task or repeat any completed, partial or unknown operation. Continue only the
remaining authorized work under the current claim/revision rules; report any
blocker or additional consent needed.
```

When the ID is unknown, use `find_tasks` with relevant query/anchors, read the
matching candidates and confirm the intended task. Do not pick a task merely
because its timestamp is newest. Read original execution/change/GitHub results
before new effects. `read_task` is lookup, not a fresh grant of execution authority.

For a device or Workspace list, `find_tasks` accepts `machine`, `workspace`
and an optional exact `workspace_identity` from the original target. Workspace
matches use current declared Task targets, including multi-target Tasks. A
machine-only query additionally finds original operations on that machine; use
`machine + target_identity` to match original SSH operations pinned to that
identity without assuming a Workspace. Rows contain Task state, declared targets,
up to eight distinct original operation targets, the owning Task-store identity,
and available timestamps. Absent metadata is null, never inferred. Pages are
ordered by Task ID; pass the returned `generation` on later pages, restarting
on `task_index_changed`. Managed Task migration upgrades old SQLite indexes
without rewriting original Task records. Until then, scoped lookup explicitly
returns `task_index_upgrade_required` instead of scanning the full store.

## Understand controls and status

| Control or state | Meaning |
| --- | --- |
| `claim` / `renew` | Acquire a currently eligible handoff or renew the retained claim under current guards. |
| `pause` | Stop new task admission; already running commands are not automatically cancelled. |
| `resume` | Resume eligible paused work, not a substitute for claim renewal. |
| `cancel` | Request task cancellation and handle original admitted work under its cancellation rules. |
| `revise_plan` | Explicitly change accepted scope/targets/stages and invalidate affected acceptance. |
| `waiting_for_ai` | Evidence or a decision needs the controller; not proof that ChatGPT will wake. |
| `finish` | Complete the accepted current plan, not merely stop waiting. |

Follow `foreground`, blockers and current revisions rather than reconstructing
state from old chat text. Waiting has finite budgets and does not renew authority.
An expired contract, unresolved effect or changed target must remain visible.

## Limits and repair

Tasks are not a scheduler, a general document database or a Git history backup.
`save_task` supports legacy checkpoints; it does not upgrade them into structured
supervision. Missing/corrupt original records are not evidence that no work ran.
Rebuilding the derived index does not repair or replace task history.

Read the runtime's [task guides](https://github.com/OpenMachineLabs/openmachine/tree/main/openmachine/skills/tasks)
for creation, continuation, verification and narrow maintenance operations.
Never delete uncertainty, clear locks or recreate a task to obtain a green status.
