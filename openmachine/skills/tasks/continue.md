# Continue and wait for original work

[Domain guide](SKILL.md)

On this page:

- [Locate the original work](#locate-the-original-work)
- [Claim, renew and release](#claim-renew-and-release)
- [Bounded foreground waiting](#bounded-foreground-waiting)

If a fixed setup message supplies `preparation_id`, this is a read-only receiver
verification, not ordinary continuation: call `read_task` with that original ID,
report `preparation_read.receipt` exactly and end the turn without claiming or
executing. Follow [recovery setup](recovery.md#chat-continuation).

## Locate the original work

Use `find_tasks` with an exact ID or stable project/PR anchor, then read the actual
record, current target/instructions, Git state and original receipts. Keywords are
candidates and exact anchors can still be ambiguous. Never select the newest
unrelated command as the task. Continue canonical records through `control_task`;
legacy Markdown remains on the [save path](maintenance.md#legacy-checkpoints).
Changed inputs invalidate affected results without erasing valid evidence.

## Claim, renew and release

Read the current record, claim its eligible handoff, and carry this context on
supervised command/file/GitHub/control/cancel effects:

```text
task_id, task_revision, plan_revision, continuation_epoch, operation_id
```

Start and file operation IDs equal the original execution/change IDs. Input and
cancellation have distinct operation IDs while retaining original execution/sequence.
A bare `task_id` is only a legacy association, not a structured execution grant.
Claims last at most 300 seconds or earlier handoff/authority expiry. Native-account
ownership is not a ChatGPT conversation identity, caller-presence proof or
untrusted-chat isolation.

On reattachment, inspect original outstanding receipts and current task/plan/epoch.
Reuse a valid owned claim or claim an eligible handoff after release/expiry; do not
displace a live owner. Release a claim when deliberately handing work over. Claim
expiry does not stop admitted work or erase its results. The final accepted stage
still has a handoff until explicit completion; reclaim and finish without repeating it.

When continuing the same retained claim, use `control_task` with the exact current
task/plan revisions, continuation epoch, a new update ID and this transition:

```json
{"action":"renew","claim_id":"0123456789abcdef0123456789abcdef"}
```

Replace the example ID with the retained `control.owner.claim_id`. Renewal
requires the same native principal and claim ID, no stop intent, a
nonterminal task and unexpired task authority. An expired original claim can be
renewed while it is still retained and unreplaced, including when an admitted
operation cleared the handoff. Renewal keeps the claim ID, epoch, plan and original
operations. Its new expiry is no later than 300 seconds from the control update or
the task authority expiry. Reading, observing and retrying the identical last
update never extend a claim. Explicit renewals retain material history and remain
subject to the record's finite capacity; they are not an unlimited keepalive.

Before and after long external operations or CI waits, and immediately before
submitting a review, merging or another critical effect, read the current task.
Compare `control.owner.expires_at_ms` with the returned `observed_at_ms`, allowing
time for the next bounded step and its readback. GitHub activity and progress
updates do not extend the lease. During long external waits, recheck between
bounded observations.

If time may be insufficient, renew the same retained claim before expiry using
the current task/plan revisions and epoch. Carry the returned record's context
into subsequent effects. A stale control snapshot requires another task read and
an eligibility check, not a retry with stale values. Renewal does not extend the
task's authority or authorize a background keepalive.

Lease expiry alone returns `foreground.status=renewal_required`, reason
`continuation_expired` and `stop_foreground=false`. A new effect is still rejected
with `task_renewal_required` and no admission. This permits one bounded recovery
in the current Chat turn, not an automatic renewal or a new execution:

1. Read the current task and original operation results. Match the caller's
   retained claim ID, native principal, plan and epoch; do not adopt a different
   claim merely because it uses the same native account. Reconcile unknown or
   partial results under their existing effect exclusions.
2. If authority and grants remain valid and no hard foreground stop applies,
   explicitly renew that same claim with the observed revisions and a new update
   ID. Use the returned context before new effects. A read grants no permission.
3. Only a confirmed no-effect `task_conflict` caused by observation-only revision
   drift permits one fresh read and one corrected renewal: the original claim,
   plan, epoch and format-4 `control_revision` must still match. Never relax CAS.
   A second conflict or another blocker ends this recovery attempt. A missing
   reply instead requires original update readback, not a new renewal ID.

Honor `foreground.stop_foreground=true` and `task_attention_required` as mandatory
end-of-turn boundaries. Pause/cancel, expired task authority, changed ownership,
unverified execution and platform/provider refusals are not lease recovery.
Renewal never resets command timeouts, foreground wait limits or observation and
Chat recovery budgets. Do not repeat renewal to hide an unchanged blocker or
extend work without progress. Neither renewal nor reattachment replays an effect.

Claim and renewal check the configured machine/workspace, current readable context,
mutation grants and repository account authority, while allowing the accepted
branch identity or HEAD to be historical. They acquire continuation ownership
without changing the accepted workspace or admitting an effect. Resume still
checks workspace identity while allowing a historical HEAD. Reconcile original
work and explicitly revise the plan against the current exact workspace identity
and HEAD before new effects or acceptance after either has changed. This target
change invalidates all retained stages; original operations stay historical but
cannot serve as current-plan `record_decision` evidence. Follow the exact
[plan revision rules](control.md#admission-and-outstanding-effects).

## Bounded foreground waiting

`wait_task` performs one read-only foreground wait for a structured task. It accepts
an observed task revision and waits at most 30 seconds for a task revision, ready
handoff, required renewal, terminal result or derived attention condition.
`renewal_required` returns immediately for the guarded same-turn flow above;
it grants neither new effects nor a second wait for that operation in this turn. Wait expiry returns
`foreground.status=wait_expired`, `stop_foreground=true`, whether original work is
still running, and a bounded resume prompt. It never extends an execution timeout,
claims continuation, dispatches another effect or cancels original work.

Hard attention distinguishes a stopped task, expired task authority, an unconsumed
handoff without a retained claim after its two-minute grace, and a pending execution
whose observer is stopped, stale or blocked. Claim expiry alone requires renewal
even when an original command remains active. Silent output alone is not failure: a
verifiably observed command remains running until its own timeout or result. New
business effects for an attention task fail with `task_attention_required` or the
existing `task_stopped`/`task_authority_expired` code; passive
reads and original-ID cancellation remain available. A later caller reads the same
record and receipts, then claims an eligible handoff before new work.

Call `wait_task` at most once for the same active operation in one Chat turn.
Treat `foreground.stop_foreground=true`, including `attention_required` or
`wait_expired`, as a mandatory end-of-turn boundary: do not wait again, renew,
claim continuation, start another effect or hide the result in a progress update.
For `task_attention_required`, read the task once if needed to present current
facts, then end the turn. Report the concise reason, whether original work may
still be running, and the returned resume prompt. This foreground mechanism is
not Desktop notification or task-owned Chat recovery, and it cannot inject a message
into a Chat that has already disconnected.

Every persisted observation advances `revision` only when its content changes.
Format 4 also records the last material `control_revision`. Business admission can
use an observed revision within that interval if plan, epoch, live ownership and
all domain guards still match. Control transitions keep exact revision CAS;
ownership, plan/scope, stop intent and new business admission invalidate earlier
contexts. This does not make a stale plan or released claim usable.

Read [Control](control.md) for changed plans/authority and [Verify](verify.md) to
accept and finish. Reattachment and renewal never replay an original effect.
