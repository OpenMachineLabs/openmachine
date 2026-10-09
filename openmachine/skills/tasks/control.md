# Control task authority and plans

[Domain guide](SKILL.md)

Read the original record before each control change. Supply its current task and
plan revisions, continuation epoch and one saved update ID. Control transitions
retain exact revision CAS; a stale response needs readback and an eligibility
check. Repeating any retained identical update ID observes history rather than renewing it.

## Select the transition guide

| Action | Required procedure |
| --- | --- |
| `claim`, `renew`, `release` | [Continuation ownership](continue.md#claim-renew-and-release) |
| `pause`, `cancel`, `abandon`, `resume`, `revise_plan` | Admission and plan rules below |
| `record_decision`, `finish` | [Verify stages and finish](verify.md) |
| `bind_effect_scope` | Historical effect scope below |

## Admission and outstanding effects

Application persists normalized intent before dispatch, then releases the task
lock before domain IO. Each domain retains source, sequence, permission and lease
checks. Per-effect locks span bounded dispatch, not a command's lifetime. Capacity
reserves active-work slots: 62 for normal work, 63 for execution controls, and
64 for cancellation. Completed history does not consume these slots. Low disk
space rejects new work before dispatch; original observations and cancellation
retain their commit paths. SQLite cannot guarantee writes on a physically full
disk. One unresolved
mutation blocks conflicting work; original commands still allow authorized controls.
A new PR has no number yet: its creation scope identifies the operation type and
source/target branches, so numbered historical PR writes and different creation
branches remain independent. The same source/target pair still excludes duplicate
creation; older unnumbered writes without verified identity remain conservative.
These task-side guards do not replace GitHub's live authorization or merge policy.
Original-ID cancellation remains available when task metadata/capacity is blocked.

Pause prevents new admission, not already-admitted effects. Cancellation records
stop intent and requests termination only of owned commands; read their outcomes.
Explicit `abandon` retires a Task without asserting cleanup or success of unknown
effects. It requires observed uncertainty, never a known-running command; local
executions require an observed original leader exit and changes require terminal
publication evidence. Original receipts, unresolved rows and cross-Task GitHub
resource exclusions survive. The terminal `abandoned` state records the reason.
Restart separately in an isolated worktree with independent effect scope; do not
replay unknown operations, override active leases or delete historical records.
Late results do not clear pause or reopen terminal history. Resume does not claim
ownership, extend expired authority or prove a worker is alive. `revise_plan`
retains original stage IDs/order and operation evidence. Changed stage definitions
require explicit invalidation of those stages and their dependents. Any material
contract change outside `sources`, `expires_at_ms` and `authority_refs`, including
a target HEAD/identity change, requires invalidating every retained stage. Only
unaffected stages under these rules keep acceptance; invalidated stages need
current-plan evidence. `record_decision` cannot reuse an operation from an older
plan revision, even when that operation's historical outcome remains verified.

An expired, nonterminal contract can be explicitly reauthorized with `revise_plan`.
Supply the complete revised contract and stage definitions, a future expiry and
at least one new `authority_refs` entry. A reference records the supplied authority;
it does not verify a ChatGPT user's identity or create a native grant. Current
targets, exact workspace identity/HEAD and repository account grants are checked.
A live foreign owner must release first. Cancellation and terminal history cannot
be reopened; an expired paused task stays paused, with no new owner or handoff,
until a separate explicit resume.

Outstanding local and SSH effects, including partial results, block plan replacement. Unresolved
GitHub effects may survive a revision only with unchanged repositories and original
native account bindings. Their operation IDs, request/source hashes, original plan
revisions, receipts and uncertainty remain intact. Revision does not reconcile an
unknown outcome, accept evidence or dispatch replacement work.

## Historical effect scope

`bind_effect_scope` is a `control_task.transition.action`, not a separate tool.
Supply the original operation ID, method and complete normalized original GitHub
arguments. It verifies their original plan/source/request fingerprint and appends
a bounded immutable scope witness; it neither dispatches nor proves non-execution.
Missing proof leaves conservative uncertainty. Read the full
[scope and historical-evidence rules](../github/reference.md#scoped-uncertainty-and-historical-evidence)
before this transition or continuation around an unresolved GitHub effect.
