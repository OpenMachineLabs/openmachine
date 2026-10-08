# Observe results and prepare bounded Chat recovery

[Domain guide](SKILL.md)

On this page:

- [Original-result observation](#original-result-observation)
- [Chat continuation](#chat-continuation)

## Original-result observation

The service lifespan starts one observer, independent of clients and Desktop.
Constructing Application or reading a task does not start it. Discovery uses
changed metadata in 50-record/2-MiB pages, not repeated scans of unchanged history.
At most 32 active tasks share four actual local reader slots, normally due every
two seconds with a two-second read budget. Terminal tasks retire. Timed-out readers
keep slots and ownership until actual exit; no task/workspace lock spans backoff.

Only previously admitted command/control/cancel/change receipts are collected.
Result and handoff publish together through revision-checked task update, never
stage acceptance, a new effect, a GitHub mutation or an AI call. Quiet reads do not
advance material progress. Unknown/corrupt records are blockers, not repair consent.
Activation may defer observation; every retry reservation/result publication keeps
normal lifecycle admission. Pause still permits collection of original outcomes.

Local failures retain a durable episode: three fast attempts including the first,
1/2-second delays, then 30-second cooldown with 0.8-1.2 jitter, bounded by one hour/
120 attempts or earlier authority expiry. Permanent permission/identity/evidence
faults block retries. Pause/restart cannot renew budgets. `read_task` separates
last-known execution facts, runtime heartbeat, real material progress, blockers
and next attempt; a stopped or stale observer advertises no live retry timer.

SSH adds two separate slots, one per pinned target across aliases/tasks, normally
every five seconds. Existing one-attempt transport, ten-second connection and
30-second exchange bounds remain. Recognized transient failures share a connection
budget: three fast attempts, 2/4-second delays, then 60-second cooldown with jitter,
up to one hour/60 attempts or earlier task expiry. Identity/authentication/protocol/
corrupt-history failures stop retries. Private `ssh-recovery.json` owns these
budgets; tasks reference them. Receipt faults keep separate task budgets. Neither
adding tasks nor pausing/restarting renews a budget; verified original reads clear
connection failure without replay or stage acceptance.

## Chat continuation

New structured tasks default to `recovery_policy: disabled`. Explicitly selecting
`task_chat` prepares one task-owned ordinary Chat; the originating Phone, Web or
Desktop Chat need not know its own URL. `create_task` and `read_task` expose
`preparing`, `ready`, `blocked` or `disabled`, with business state and delivery
observations separate. Foreground work under current task authority does not wait
for recovery readiness. A committed task, selected connection or visible Chat URL
alone is not readiness for unattended continuation.

Configure the existing local browser route once, with explicit visible selections:

```toml
[chat_recovery]
workspace = "project"
connection = "<existing OM connection label>"
browser_workspace = "<current ChatGPT workspace label>"
model = "<explicitly selected model label>"
```

These labels are not credentials. The current authenticated account is used
as-is. A legacy `browser_account` label remains accepted for compatibility but is
not a current recovery identity or routing guard. Setup may select the named
existing connection and model, but cannot log in, switch accounts/workspaces, install a connection,
grant permissions or substitute a model. Missing or ambiguous setup is a blocker.
Authorized managed configuration changes use `update --candidate-config`; do not
edit the active configuration or bypass an unresolved activation guard.

Keep the task contract self-contained: retain necessary instructions and readable
inputs within its existing bounds. Source hashes identify inputs; they do not
restore unavailable attachments or originating Chat context. Missing input remains
a blocker, not permission to guess or copy unrelated private history.

For `task_chat`, creation commits its recovery policy, registers a stable preparation
in the private sidecar, then acknowledges that registration in the canonical task. Only matching
records permit browser dispatch. Startup discovers committed, unacknowledged local
registration gaps through bounded changed-metadata pages. An acknowledged entry
that is missing or corrupt blocks rather than creates a replacement Chat. Exact
creation retries retain their original receipt through registration-only revision
advancement. No business operation is inferred or accepted by registration.

Automatic initial preparation runs in the existing observer-owned Codex slot only
after the normal ten-minute attributable-activity and observer-restart grace, with
no live continuation claim and no verified running original command. The existing
30-second checks and original stop, expiry, scope and observation guards apply.
Registration alone does not open a browser while the caller is active. Preparation
sends one fixed read-only setup message in a task-owned Chrome tab. The receiver calls
`read_task` with the original `preparation_id`, reports the returned receipt exactly
and ends its turn without claiming, changing policy or executing business work.
Only a successful external MCP call persists that first read witness. Ordinary or
internal reads, a browser report and a receiver's uncorrelated claim do not prove it.

Readiness requires the matching Runtime read witness, exact receipt and full setup
message, verified signed-in route, workspace/connection/model selections, ended receiver turn,
matching native final URL/tab evidence and verified native execution cleanup.
Pending confirmations, drafts and incomplete evidence are not `ready`. Late results
may retain a discovered destination but cannot undo pause, disable, expiry or a
scope conflict. A successful setup read does not prove that future writes will
never prompt; the representative authorized write remains a separate live gate.

Foreground command, file, GitHub and input effects retain their normal authority,
scope, ownership and original-result gates without waiting for Chat setup. Recovery
preparation or delivery blockers remain visible for unattended continuation.
The single `configure_chat_recovery` operation accepts only an explicit enable/disable
override, never a caller-selected URL or replacement controller. Accepted update
IDs consume revisions; exact retries, including after pause, remain observations.
Overrides preserve destination, attempts, original executions and prior uncertainty.
After repairing a proven setup failure or a verified pre-send blocker, an explicit
enable can reschedule preparation if no destination or receiver read was recorded.
It retains the original attempts and deadline, and a pre-send blocker retains the
ten-minute delay from its observation. Sent or uncertain effects cannot be retried this way.

Both preparing and bound entries count toward the 32-active/128-retained limit in
the bounded 512-KiB recovery store. Setup and continuation episodes each have at most three browser attempts
within one hour or earlier authority expiry, each at most 600 seconds. Only verified
no-send and generating evidence permits a retry, at least ten minutes later. Proven
pre-launch contention consumes no attempt but retains the original deadline.
Other pre-launch rejections retain their exact blocked reason; stop, authority
expiry or actual denial is not transient contention. Reads, claim expiry,
restart, handoff churn and equivalent re-enables never renew either budget.
Sent or uncertain setup never authorizes another message or replacement Chat.
The 600-second bound accommodates observed slow-browser setup without relaxing
evidence, no-replay or retry requirements.

The same observer checks continuation every 30 seconds, including tasks with no
active command and accepted final stages awaiting `finish`. Dispatch waits ten
minutes after attributable external task activity and observer restart, with no
live claim or verified running original command. Internal reads and setup evidence
are not business progress. Original stop, expiry, scope and observation-budget
guards remain; unknown effects permit only reconciliation instructions.

One private launch boundary reserves fixed setup or continuation intent before the
existing Codex/Chrome path. The global `browser:codex` lease prevents overlapping
browser subtasks; the observer retains its slot until actual exit. No additional
daemon, planner or account backend is installed. Continuation verifies the full
fixed message, not just a marker, and retains sent, claimed, advanced and manually
superseded observations separately. After 15 minutes without advancement, report
`continuation_not_observed`; never resend a sent or uncertain episode. Only genuine
later business progress can establish another continuation obligation.

After re-entry, Chat reads the same task and original receipts, claims its current
eligible handoff, and continues only the remaining authorized work with current
task/plan revisions and epoch. Pause for a real human decision using task controls;
never use recovery to undo pause or rerun completed effects. Explicitly finish the
original task after all applicable stages are accepted.

Historical format-1/format-2 task bytes and original bindings remain readable;
reads do not enroll unbound history or reinterpret an old binding as prepared.
Explicit guarded `revise_plan` can enroll the original nonterminal task while
retaining its operation receipts. Format-4 task records require state-contract-16 readers
before managed activation; pending or uncertain setup also defers activation.
Existing old routes feed the same continuation path, not
a second resume mechanism. Retired Events archives remain inert.

The [Spec 76 Issue](https://github.com/OpenMachineLabs/openmachine/issues/76) records
actual route/write and Phone/Web/Desktop acceptance separately from installed SDK
and synthetic browser tests. Offline hosts, unavailable login, persistent generation,
human decisions and exhausted or uncertain episodes remain explicit limits. This
is bounded task continuation, not restoration of hidden Chat state or exactly-once
browser execution.
