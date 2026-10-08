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
`connection_id`, numeric `account_id` and `authorization_revision`. Format-2 through
format-4 operation locators retain that binding for dispatch and destination recovery.
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

Check foreground state and renewal eligibility first. Honor
`foreground.stop_foreground=true` and `task_attention_required` as the existing
end-of-turn boundaries; do not renew or claim to suppress them. Stop intent,
expired task authority, changed ownership and platform/provider refusals remain
binding. The ability to renew an expired retained claim does not override these
boundaries or permit replaying an original effect.

Claim and renewal check the configured machine/workspace, current readable context,
mutation grants and repository account authority, while allowing the accepted
branch identity or HEAD to be historical. They acquire continuation ownership
without changing the accepted workspace or admitting an effect. Resume still
checks workspace identity while allowing a historical HEAD. Reconcile original
work and explicitly revise the plan against the current exact workspace identity
and HEAD before new effects or acceptance after either has changed.

Application persists normalized intent before dispatch, then releases the task
lock before domain IO. Each domain retains source, sequence, permission and lease
checks. Per-effect locks span bounded dispatch, not a command's lifetime. Capacity
reserves space/slots for original results, stopping and completion. One unresolved
mutation blocks conflicting work; original commands still allow authorized controls.
A new PR has no number yet: its creation scope identifies the operation type and
source/target branches, so numbered historical PR writes and different creation
branches remain independent. The same source/target pair still excludes duplicate
creation; older unnumbered writes without verified identity remain conservative.
These task-side guards do not replace GitHub's live authorization or merge policy.
Original-ID cancellation remains available when task metadata/capacity is blocked.

Pause prevents new admission, not already-admitted effects. Cancellation records
stop intent and requests termination only of owned commands; read their outcomes.
Late results do not clear pause or reopen terminal history. Resume does not claim
ownership, extend expired authority or prove a worker is alive. `revise_plan`
invalidates changed stages and dependents while preserving unrelated acceptance,
stage identities and original operation evidence.

An expired, nonterminal contract can be explicitly reauthorized with `revise_plan`.
Supply the complete revised contract and stage definitions, a future expiry and
at least one new `authority_refs` entry. A reference records the supplied authority;
it does not verify a ChatGPT user's identity or create a native grant. Current
targets, exact workspace identity/HEAD and repository account grants are checked.
A live foreign owner must release first. Cancellation and terminal history cannot
be reopened; an expired paused task stays paused, with no new owner or handoff,
until a separate explicit resume. Reauthorization preserves the recovery policy.

Outstanding local and SSH effects, including partial results, block plan replacement. Unresolved
GitHub effects may survive a revision only with unchanged repositories and original
native account bindings. Their operation IDs, request/source hashes, original plan
revisions, receipts and uncertainty remain intact. Revision does not reconcile an
unknown outcome, accept evidence or dispatch replacement work.

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

## Scoped uncertainty and historical evidence

Format 4 separates original operation identity from the resources an effect can
change. The native GitHub writer routes share a repository lease during admission,
dispatch and publication. Retained uncertainty then excludes only conflicting
resources across tasks, anchors and account selectors. Issue/PR metadata shares
the same repository-and-number namespace. The original operation ID still never
dispatches again.

| Unresolved effect | Exclusion retained |
| --- | --- |
| Labels or assignees replacement | The same field; disjoint body/title, reviews and merge can continue |
| PR state or draft transition | Overlapping state/draft changes, submitted reviews and merge |
| Submitted review | The same review collection and dependent merge/state transitions |
| Review body replacement | The same review body; it does not resubmit the review decision |
| Merge | Another merge or dependent state change; omitted commit messages also depend on PR title/body |
| Comment mutation | The same comment or unresolved parent collection; an unchecked parent argument cannot narrow a comment ID |
| Missing GitHub creation destination | A conservative repository exclusion until original evidence identifies the destination |
| Uncertain execution or partial workspace effect | Conflicting work on its original target; existing worker and workspace leases remain authoritative |

An old operation without a scope stays conservative. The explicit
`bind_effect_scope` control takes its original operation ID, GitHub method, complete
normalized arguments and a reason. It recomputes the original request hash with
the original plan and source hash, derives scope from the actual owner arguments,
and appends an immutable witness. The original operation, locator, outcome and
receipt stay unchanged. A matching request proves scope, not non-execution. No
current destination snapshot can prove a historical request was never dispatched.
The witness input is bounded at 128 KiB; only its fingerprint, compact scope,
principal and timestamp are retained, not raw bodies or parameters.

Scope binding rechecks the current owner, authority, workspace identity and native
grants. A changed local HEAD does not block this evidence-only action; new local
effects and stage acceptance still require the accepted HEAD. Plan revision pins
each unresolved GitHub operation's own repository and native account binding.
Explicit changes to unrelated repositories remain possible under ordinary scope
validation and stage invalidation.

For a retained 0.2.11 task, first inspect the running server's capabilities and
the actual `control_task` schema. The scoped reader supports record formats 1..4;
`bind_effect_scope` is a `control_task.transition.action`, not a separate tool.
The original 0.2.11 runtime lacks this action even when newer source or Skill text
is present. Update through the managed compatibility checks only under explicit
installation authority, then refresh the client's tool catalog. Do not drop
`task_context` to work around an older catalog.

Read the original task and destination before continuing. Resume only under
existing user authority, claim the same task, and retain its original operation ID.
For format 3, a valid control upgrades the record without replacing its operations.
Older native-account records require an explicit plan revision retaining their
bindings and declaring a recovery policy before scope enrollment; retired gh history cannot
be transferred to a native account. Supply the exact complete normalized original
arguments to `bind_effect_scope`; if they are unavailable, retain the conservative
scope. To reconcile the outcome, call the original GitHub method with those same
arguments and operation ID: this path reads the destination and never dispatches
again. Scope binding alone leaves the outcome unknown. A missing, changed or
unverifiable destination leaves the original uncertainty intact.

New owner failures retain a failure code and effect classification. An explicit
business preflight rejection is failed/known-no-effect without a reconciling read
that could replace the result. A dispatched or acknowledged write with insufficient
evidence remains unknown. Passive unproven observations preserve the original
failure evidence. Same-ID verified reconciliation never resubmits the request.

Verified historical GitHub results are frozen. They establish the original effect;
subsequent bot comments, timestamps and metadata do not replace their digest.
Current acceptance remains separate: each merge reads the current HEAD/base,
protections, checks, review decisions and discussions. A stage can accept nonempty,
successful evidence independent of all unresolved scopes, while the unresolved
IDs remain visible. Finish still blocks every original unresolved effect.

## Bounded foreground waiting

`wait_task` performs one read-only foreground wait for a structured task. It accepts
an observed task revision and waits at most 30 seconds for a task revision, ready
handoff, terminal result or derived attention condition. Expiry returns
`foreground.status=wait_expired`, `stop_foreground=true`, whether original work is
still running, and a bounded resume prompt. It never extends an execution timeout,
claims continuation, dispatches another effect or cancels original work.

Attention distinguishes an unconsumed handoff after its two-minute grace, an
expired continuation without verified original work, and a pending execution whose
observer is stopped, stale or blocked. Silent output alone is not failure: a
verifiably observed command remains running until its own timeout or result. New
business effects for an attention task fail with `task_attention_required`; passive
reads and original-ID cancellation remain available. A later caller reads the same
record and receipts, then claims an eligible handoff before new work.

The operating Skill treats `foreground.stop_foreground=true` as an end-of-turn
boundary and reports the reason plus resume prompt. This foreground mechanism is
not Desktop notification or task-owned Chat recovery, and it cannot inject a message
into a Chat that has already disconnected.

Every persisted observation advances `revision` only when its content changes.
Format 4 also records the last material `control_revision`. Business admission can
use an observed revision within that interval if plan, epoch, live ownership and
all domain guards still match. Control transitions keep exact revision CAS;
ownership, plan/scope, stop intent and new business admission invalidate earlier
contexts. This does not make a stale plan or released claim usable.

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

For `task_chat`, creation commits its recovery policy, registers a stable preparation
in the private sidecar, then acknowledges that registration in the canonical task. Only matching
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
