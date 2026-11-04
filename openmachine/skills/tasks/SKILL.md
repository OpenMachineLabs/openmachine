---
id: om.tasks
name: openmachine-tasks
description: Create durable task contracts, continue original work, manage claims and plans, verify completion, or diagnose bounded recovery.
---

# Task operations

Use one canonical record for a logical outcome. Domain execution/change/provider
receipts remain authoritative. A record or read never creates execution authority.

| Intent | Read next |
| --- | --- |
| Start new multi-stage work with continuity | [Create](create.md) |
| Find/resume work, claim/renew/release, or wait | [Continue](continue.md) |
| Observe original results and bounded observer errors | [Continue](continue.md#original-result-observation) |
| Pause/cancel/resume, revise scope or bind historical effect scope | [Control](control.md) |
| Accept stage evidence and explicitly finish | [Verify](verify.md) |
| Repair derived lookup or continue legacy checkpoints | [Maintenance](maintenance.md) |

Inspect current schemas before claiming support. Missing tools do not authorize
installation, dropping `task_context`, or replacing a required structured task with
a legacy checkpoint. Keep original task, update and operation IDs. Unknown effects
require original readback; source hashes do not restore missing Chat inputs.

Honor `foreground.stop_foreground=true` and `task_attention_required` as mandatory
end-of-turn boundaries, with the narrow [historical handoff recovery](continue.md#recover-a-historical-handoff-timeout)
exception for a subsequent explicit user continuation after the interrupted turn
has ended. Lease expiry alone may permit the bounded same-claim flow in
[Continue](continue.md); it never grants new effects or resets a budget.

A Task reserves a verified local target when its first command or typed file
mutation is admitted. The owner is the original Task ID. Claims, release/handoff,
pause, expiry, disconnection and restart do not release that reservation. Continue
the same Task with its lawful current claim and context; a different caller does
not create another owner. Original live, partial or unknown effects and abandonment
retain ownership. Safe terminal retirement or a verified plan revision that drops
a settled target releases it atomically.

`task_resource_conflict` identifies the canonical root and blocking Tasks. Choose
another worktree or reconcile and safely complete the original Task. Do not omit
context, invent another operation ID or use shared shell access to bypass it.
The boundary is cooperating mutations through one authoritative Core state;
arbitrary shell paths, external editors and independent state directories are
not isolated. Task reservations are separate from short native operation leases.
