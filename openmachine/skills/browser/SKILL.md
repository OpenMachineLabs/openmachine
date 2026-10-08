---
id: om.browser
name: openmachine-browser
description: Delegate a bounded explicitly authorized browser or UI goal to installed native Codex, then verify semantic results and original execution evidence.
---

# Browser subtasks

Keep Chat in control. Delegate only an explicitly authorized browser/UI subtask;
use existing API, workspace, GitHub and SSH tools for their own operations.

Read [browser delegation](reference.md) before starting. Supply a self-contained
goal, permitted actions, visible acceptance, selected browser/tab context and exact
origins. Preserve the original execution ID and current task context. Installation,
login, account switching and permission confirmation are separate authority.

Use `read_codex_task` to inspect the actual report, final native page evidence and
pending action before accepting a stage. Exit zero alone is insufficient. Missing
bridge/login, confirmation or uncertain side effects remain blockers. Cancellation
is not rollback. Task-owned automatic continuation has its separate explicit
[recovery guide](../tasks/recovery.md); a generic browser subtask does not wake Chat.
