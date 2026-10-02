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

New GitHub task contracts resolve `github_connections` per repository and retain
`connection_id`, numeric `account_id` and `authorization_revision`. Their format-2
operation locators keep that exact binding for dispatch and destination recovery.
Changing the default, disconnecting or reconnecting cannot silently change a task's
account. Explicit connection selectors must match the accepted binding.

Format-1 records retain their original serialization, request hashes and external
`gh` policy; reads and continuation never migrate them to a new account. Empty
optional fields are omitted from legacy canonical bytes. New creation retries
compare the original caller request, not subsequently resolved connection state.
Account recovery does not replay a GitHub mutation or renew task authority.

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

## Task handoff Events

The single-user private endpoint exposes `task.handoff_ready` on MCP 2026-07-28
through the existing SDK extension. Filter exactly by `{task_id}`; payload contains
task/handoff IDs, task/plan revisions and reason, within a 4 KiB envelope. There is
no replay cursor. Legacy clients retain their tools. Every connected chat shares
native-account grants; anonymous subject/session data is not an authorization
boundary. Supported Work-route acceptance does not extend to generic chats, local
Codex or unsupported accounts, and does not promise cross-chat isolation.

Subscribe only to an active, unexpired structured task. Callbacks require signed
verification, HTTPS, public DNS resolution at every connection, address pinning,
hostname-verified TLS and no redirects. Synthetic/private DNS is not bypassed.
Callback URLs/secrets stay in private `events.json`, never task views. There are
at most 64 subscriptions, TTL at most one hour or task expiry, and five-minute
verification-cache/key-rotation windows. Refresh retains identity and budgets;
unsubscribe is idempotent and invalidates in-flight receipts.

The observer dispatches at most two webhook exchanges, one per subscription,
without holding task locks across IO. An unexpired claim defers delivery. Current
lifecycle/task admission and durable attempt reservation precede every send.
Retries retain event ID/body/occurrence time: four fast attempts with 1/2/4-second
delays, then 60-second jittered cooldown, up to one hour/60 attempts or earlier expiry.
Connection failures coalesce. Verification has two slots/five seconds; delivery
ten seconds; timed-out threads retain slots until exit. Responses are bounded to
4 KiB. Permanent refusal, 410/413 and revoked/expired grants do not retry.

A 2xx is receipt only. Readback exposes delivered, read-observed, claimed and actual
next-intent-admitted facts separately, with original IDs. `pending_handoff_id`
identifies a newer obligation. Reads may queue private in-memory event observation;
service publication does not change canonical task progress or claim work. An
acknowledged event is not resent to force a wake; missing takeover blocks after
15 minutes or earlier expiry. The caller must read original receipts, claim the
current handoff and submit a currently authorized effect. Pause/terminal/expiry
remain authoritative, including late replies. Platform refusal never authorizes
another event, polling loop, model backend or replacement task.

All three actual Work continuation cases are retained in
[Spec #29](https://github.com/OpenMachineLabs/openmachine/issues/29): an original
command still running, a committed result, and an accepted plan before execution.
Those are historical account/route-specific results, not acceptance of every new
candidate. Installation tests, event delivery and actual AI continuation remain
separate evidence. State contract 10 requires genuine readers of all retained task,
SSH, managed recovery and Events history before transition, even terminal-only or
disabled integrations. Never delete evidence to enable an old runtime.

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
