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
`connection_id`, numeric `account_id` and `authorization_revision`. Format-2 and
format-3 operation locators retain that binding for dispatch and destination recovery.
Changing the default, disconnecting or reconnecting cannot silently change a task's
account. Explicit connection selectors must match the accepted binding.

Format-1 records and format-2 gh bindings retain their original serialization,
request hashes, connection identifiers and receipts. Reading this history is passive.
The gh request backend is retired: claims, continuation and remote reconciliation
that need its GitHub authority fail with `integration_legacy_connection`, including
unbound format-1 GitHub tasks. Neither an explicit native selector nor a new default
can transfer that authority. Start separately authorized native work only after
resolving any original uncertain effect; retirement does not prove its outcome or
permit replay. Local pause/cancel controls remain available under their existing guards.

Empty optional fields are omitted from legacy canonical bytes. New creation retries
compare the original caller request, not subsequently resolved connection state.
Routine native credential rotation preserves authorization revisions. Account
recovery does not replay a GitHub mutation or renew task authority.

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

New structured tasks default to `recovery_policy: task_chat`. Runtime prepares one
task-owned ordinary Chat before new business effects; the originating Phone, Web
or Desktop Chat need not know its own URL. `create_task` and `read_task` expose
`preparing`, `ready`, `blocked` or `disabled`, with business state and delivery
observations separate. Use `disabled` only on the user's explicit instruction.
A committed task, selected connection or visible Chat URL alone is not readiness.

Configure the existing local browser route once, with explicit visible selections:

```toml
[chat_recovery]
workspace = "project"
connection = "<existing OM connection label>"
browser_account = "<authenticated account label>"
browser_workspace = "<current ChatGPT workspace label>"
model = "<explicitly selected model label>"
```

These labels are not credentials. Setup may select the named existing connection
and model, but cannot log in, switch accounts/workspaces, install a connection,
grant permissions or substitute a model. Missing or ambiguous setup is a blocker.
Authorized managed configuration changes use `update --candidate-config`; do not
edit the active configuration or bypass an unresolved activation guard.

Keep the task contract self-contained: retain necessary instructions and readable
inputs within its existing bounds. Source hashes identify inputs; they do not
restore unavailable attachments or originating Chat context. Missing input remains
a blocker, not permission to guess or copy unrelated private history.

Creation commits its recovery policy, registers a stable preparation in the private
sidecar, then acknowledges that registration in the canonical task. Only matching
records permit browser dispatch. Startup discovers committed, unacknowledged local
registration gaps through bounded changed-metadata pages. An acknowledged entry
that is missing or corrupt blocks rather than creates a replacement Chat. Exact
creation retries retain their original receipt through registration-only revision
advancement. No business operation is inferred or accepted by registration.

Initial preparation runs in the existing observer-owned Codex slot without waiting
for an abandonment grace or relinquishing the controlling Chat's claim. It sends
one fixed read-only setup message in a task-owned Chrome tab. The receiver calls
`read_task` with the original `preparation_id`, reports the returned receipt exactly
and ends its turn without claiming, changing policy or executing business work.
Only a successful external MCP call persists that first read witness. Ordinary or
internal reads, a browser report and a receiver's uncorrelated claim do not prove it.

Readiness requires the matching Runtime read witness, exact receipt and full setup
message, verified account/workspace/connection/model route, ended receiver turn,
matching native final URL/tab evidence and verified native execution cleanup.
Pending confirmations, drafts and incomplete evidence are not `ready`. Late results
may retain a discovered destination but cannot undo pause, disable, expiry or a
scope conflict. A successful setup read does not prove that future writes will
never prompt; the representative authorized write remains a separate live gate.

New command, file, GitHub and input effects check readiness after original-ID
reconciliation, before intent admission and again before dispatch. Existing receipt
reads and authorized stopping remain possible. A claim is not a readiness bypass.
Known later route blockers invalidate readiness for new business effects. The
single `configure_chat_recovery` operation accepts only an explicit enable/disable
override, never a caller-selected URL or replacement controller. Accepted update
IDs consume revisions; exact retries, including after pause, remain observations.
Overrides preserve destination, attempts, original executions and prior uncertainty.
After repairing a proven setup failure or a verified pre-send blocker, an explicit
enable can reschedule preparation if no destination or receiver read was recorded.
It retains the original attempts and deadline, and a pre-send blocker retains the
ten-minute delay from its observation. Sent or uncertain effects cannot be retried this way.

Both preparing and bound entries count toward the 32-active/128-retained limit in
the bounded 512-KiB recovery store. Preparation has at most three browser attempts
within one hour or earlier authority expiry, each at most 600 seconds. Only verified
no-send/generating evidence permits a retry, at least ten minutes later. Proven
pre-launch contention consumes no attempt but retains the original deadline.
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
retaining its operation receipts. New task and sidecar format 3 require actual
state-contract-15 readers before managed activation; pending or uncertain setup
also defers activation. Existing old routes feed the same continuation path, not
a second resume mechanism. Retired Events archives remain inert.

The [Spec 76 Issue](https://github.com/OpenMachineLabs/openmachine/issues/76) records
actual route/write and Phone/Web/Desktop acceptance separately from installed SDK
and synthetic browser tests. Offline hosts, unavailable login, persistent generation,
human decisions and exhausted or uncertain episodes remain explicit limits. This
is bounded task continuation, not restoration of hidden Chat state or exactly-once
browser execution.

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
