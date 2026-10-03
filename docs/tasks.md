# Task operations

[Overview](../README.md) | [Operations](operations.md) | [Service](service.md)

## One original record

A task is a logical outcome that can reference several commands or file changes.
Task progress is controller-owned; operations keep their authoritative receipts:

```text
<state-dir>/tasks/index.json          # bounded lookup and integrity metadata
<state-dir>/tasks/<task-id>/task.json # structured record for new task identities
<state-dir>/tasks/<task-id>/task.md   # legacy checkpoint, never both for one ID
```

For new multi-stage work needing continuity, derive the smallest contract and
outcome-sized stages from the authorized request and call `create_task` before
consequential work. A hand-written Spec is optional; one-step reads need no task.
Inspect exposed tools before claiming structured support. Continue existing
structured records through `control_task` and legacy records through `save_task`,
without conversion or another journal. A task read adds no execution authority.

Creation binds original request/source identities, objective, scope, exclusions,
acceptance, stop condition, authority references, configured targets, tools and
finite expiry. Unknown fields and caller-authored runtime facts are rejected.
Actual target identity, HEAD and current grants are checked before new admission,
without holding task locks across target/network reads. References do not grant
permissions or prove the referenced source was inspected.

Canonical bytes are sorted compact UTF-8 JSON, no BOM, plus one LF. A stage's
`source_refs` select complete `{locator, sha256}` objects from `contract.sources`,
sorted by locator. Compute their `source_sha256` exactly:

```python
hashlib.sha256((json.dumps(selected, sort_keys=True, ensure_ascii=False,
    separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")).hexdigest()
```

The newline matters; an empty selection hashes `[]\n`. Hash source objects, not
only digest strings or referenced file bytes. Never invent hashes or omit bindings
to silence a validation error.

Records are at most 64 KiB, with creation limited to 48 KiB to reserve evidence
capacity. A complete fsynced directory publishes before derived index refresh.
A committed canonical record remains readable when the index is stale. A lost
durability acknowledgement is uncertain; retain the original task/update IDs and
observe them, rather than allocating replacement work. No arbitrary overwrite API.

## GitHub account binding

New GitHub task contracts resolve native `github_connections` per repository and retain
`connection_id`, numeric `account_id` and `authorization_revision`. Their format-2
operation locators keep that exact binding for dispatch and destination recovery.
Changing the default, disconnecting or reconnecting cannot silently change a task's
account. Explicit connection selectors must match the accepted binding.

Format-1 records retain their original serialization, request hashes and external
`gh` policy; reads and continuation never migrate them to a new account. Empty
optional fields are omitted from legacy canonical bytes. New creation retries
compare the original caller request, not subsequently resolved connection state.
Existing format-2 gh bindings are historical-only as well; neither user-supplied
bindings nor default selection can use them to create new work. Routine credential
rotation preserves authorization revisions. Account recovery does not replay a
GitHub mutation or renew task authority.

## Claims, effects and completion

Read the current record, claim its eligible handoff, and carry this context on
supervised command/file/GitHub/control/cancel effects:

```text
task_id, task_revision, plan_revision, continuation_epoch, operation_id
```

Start and file operation IDs equal the original execution/change IDs. Input and
cancellation have distinct operation IDs while retaining original execution/sequence.
A bare `task_id` is only a legacy association, not a structured execution grant.
Claims last at most 300 seconds or earlier handoff/authority expiry. Identical
last-update retries do not extend ownership. Native-account ownership is not a
ChatGPT conversation identity, caller-presence proof or untrusted-chat isolation.

On reattachment, inspect original outstanding receipts and current task/plan/epoch.
Reuse a valid owned claim or claim after release/expiry; do not displace a live
owner. Release a claim when deliberately handing work over. Claim expiry does
not stop admitted work or erase its results. The final accepted stage still has
a handoff until explicit completion; reclaim and finish without repeating it.

Claim and resume revalidate workspace identity and current grants, but tolerate a
historical HEAD so a task can reattach after its own commit. They neither change
the accepted HEAD nor authorize new work against it. Reconcile the original commit
and explicitly revise the plan before new effects or acceptance on a changed HEAD.

Application persists normalized intent before dispatch, then releases the task
lock before domain IO. Each domain retains source, sequence, permission and lease
checks. Per-effect locks span bounded dispatch, not a command's lifetime. Capacity
reserves space/slots for original results, stopping and completion. One unresolved
mutation blocks conflicting work; original commands still allow authorized controls.
Original-ID cancellation remains available when task metadata/capacity is blocked.

Pause prevents new admission, not already-admitted effects. Cancellation records
stop intent and requests termination only of owned commands; read their outcomes.
Late results do not clear pause or reopen terminal history. Resume does not claim
ownership, extend expired authority or prove a worker is alive. `revise_plan`
invalidates changed stages and dependents while preserving unrelated acceptance,
stage identities and original operation evidence.

Use `record_decision` only with applicable verified evidence, then explicitly finish:

```json
{"action":"finish","outcome":"completed","acceptance":[{"stage_id":"stage-id","operation_id":"original-operation-id"}],"limitations":""}
```

Include every accepted stage and its original operation references. Use null only
for an accepted non-command decision with no operation evidence. Finish does not
accept `reason`, `summary` or the decision's `evidence` field. A zero exit, claim,
notification or saved checkpoint cannot substitute for task acceptance. Terminal
history stays read-only; follow-up work is a new linked task.

Original operation retries compare intent and read original results, not resubmit.
Missing/corrupt receipts and uncertain publication never authorize replay. Known
supervised identities cannot shed their context to acquire new effects. GitHub
reconciliation reads markers and original destinations with at most three 100-item
lookup pages; duplicate/partial evidence remains unknown. Bodyless uncertain writes
may remain unresolved. Receipt hashes represent bounded semantic observations,
not full raw logs, source files or provider responses. There is no exactly-once
promise for arbitrary shell/API effects.

## Lookup and explicit maintenance

`find_tasks` gives candidates by exact ID/anchor or keywords; even a unique keyword
match requires checking the record. Exact anchors may be ambiguous. Read original
records before acting and retain index generations between pages.

```sh
openmachine --config /absolute/config.toml --state-dir /absolute/private/state rebuild-task-index
```

Maintenance validates at most 50 records/2 MiB per page; continue with its observed
`--expected-generation` until complete. New work requires complete lookup; original
result publication and stopping do not. At most 10,000 entries are considered,
without eviction or migration. Missing legacy metadata, established locks, unsafe
paths, orphans and corrupt records block repair rather than permit guessed state.
The index is derived lookup, not a second progress ledger. Reads never repair it.

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

Open Machine does not expose MCP Events subscriptions. `events_delivery` remains
false and native task ownership is not proof that a ChatGPT caller is present.
Original results and pending handoffs stay durable independently of Chat.

Same-Chat recovery is default-enabled for eligible structured tasks. The existing
service-owned TaskObserver checks bound tasks every 30 seconds, including tasks
with no active execution and accepted final stages still awaiting `finish`.
`read_task` projects `chat_recovery` without changing canonical task revisions.
A task with no verified ordinary-Chat destination reports
`waiting_for_binding`, while an explicit per-task disable is retained across restart.
Legacy checkpoints remain ineligible.

`configure_chat_recovery` binds one exact authenticated `https://chatgpt.com/c/...`
conversation to one configured local controller workspace and an existing task
authority reference, with expiry no later than the task contract. The binding does
not claim the business task, extend its authority or send a message. One canonical
conversation may own one active task; at most 32 active and 128 retained bindings
fit in the bounded private `chat_recovery.json` store. Service compatibility checks
retain this authority/deduplication state before managed activation.

Each newly accepted configuration update ID consumes one binding revision, even
when the policy is unchanged. Exact retained ID/request retries leave the bytes
unchanged; neither case resets delivery history or extends authority implicitly.
A reserved delivery must be observed before rebinding its destination/controller.
Per-task disable remains available and retains the original receipt route.

Successful external MCP calls attributable to a bound task record activity in the
private recovery store, not the task. Internal reads, readiness probes and Codex
delivery activity do not count; neither attribution nor a native claim authenticates
presence in that Chat. Dispatch waits ten minutes after attributable activity and
after observer restart, and requires no live claim or verified running original
operation. Stop/expiry/identity/grant and original observation-budget guards remain.
Unknown original effects allow only a reconciliation-only wake. A missing handoff
is constructed through the existing revision-checked task transition, never by
resetting operations or accepting stages.

One observer-owned background slot keeps native dispatch/receipt reads off the MCP
event loop and ordinary receipt coordinator. The observer retains its coordination
lease until that slot drains. The existing global `browser:codex` lease prevents
overlap with ordinary browser subtasks; no second daemon or planner is installed.

The private delivery path rechecks task stop/expiry/ownership, current handoff,
recovery authority and controller identity, then reserves one delivery identity
before using the existing Codex/Chrome execution path. Codex opens a new task-owned
tab for the exact bound conversation, refuses unsafe page/composer states, sends
only the fixed single-line sub-2-KiB recovery message, verifies both the complete
message and marker with native browser evidence and exits. Marker-only submission
is uncertain, not successful delivery. A sent report, task claim and business-task advancement
remain distinct observations. Timeout or missing evidence is `outcome_unknown` and
is never a reason to resend blindly. Recovery delivery is not recorded as the next
business operation and never claims the business task on Chat's behalf.

An episode permits at most three browser attempts, at least ten minutes apart,
within one hour or earlier authority expiry. Each browser execution is bounded to
180 seconds or remaining authority. Only verified native no-send evidence for a
generating page permits a retry. Proven pre-launch contention consumes no attempt
but retains the original episode deadline. Other unsafe preflight states block;
missing receipts or ambiguous clicks remain uncertain. Reads, claims, handoff-only
changes, restarts and equivalent re-enables never renew a consumed budget.

Submission, predetermined claim and actual new intent/accepted decision/finish
have separate timestamps and projections. Manual progress is reported separately.
After 15 minutes without continuation, a sent episode reports
`continuation_not_observed`, without resending. Only genuine later business progress
can establish a new continuation obligation; uncertain delivery remains blocked.
Format-2 recovery state retains original execution IDs and finite budgets across
restart. The original execution store owns detailed receipts, not this small index.

After re-entry, Chat reads the same task and original receipts, claims its current
handoff or reuses a valid claim, and admits only the next safe operation with current
task/plan revisions and epoch. A read is not a claim, and a claim is not stage
acceptance. Pause, cancellation, expiry, terminal state and uncertain original
effects keep their existing guards; unknown effects receive reconciliation-only
instructions. Do not rerun completed work because the previous Chat turn ended.

The former Work adapter remains retired. Old private Events archives stay inert and
unchanged; the [lifecycle reader](service.md) checks their retention without sending
callbacks. Sleeping/offline controllers, persistent generation, unavailable login,
human decisions and exhausted or uncertain episodes remain visible limits. This is
bounded same-Chat re-entry, not guaranteed restoration or exactly-once UI execution.

## Legacy checkpoints

`save_task` creates at expected revision zero and updates the observed revision.
Identical latest-write retries remain unchanged; anchors are additive and an empty
list preserves existing locators. Titles/anchors/revision/hash metadata live in
the index, checkpoint prose only in Markdown. Checkpoints are bounded to 32 KiB,
index to 2 MiB. Save concise goals, exclusions, authority, worktree/branch, original
handles/cursors, relevant outcomes and next action; never secrets, transcripts,
hidden reasoning or bulk source/logs.

Each file replacement is durable/atomic; checkpoint plus index is not a two-file
transaction. Torn pairs, missing established locks, lost directories, stale hashes
or generations fail rather than reinitialize. Reconnect by reading the same task,
current user instructions, Git and original operation receipts; reuse valid results
and perform only remaining authorized work. A changed instruction retains earlier
evidence and observes or explicitly stops incompatible original work. A checkpoint
restores neither hidden model state nor new authority, and schedules no model loop.

A retained local start rejection is not a lost worker. A recorded pre-dispatch
`resource_busy`, its free original launch lock and an absent execution directory
prove a failed-before-launch result. The original task operation remains with its
ID and request hash; no execution receipt is invented. Remote, ambiguous, live or
partial destinations remain unresolved. This correction never replays an effect.
