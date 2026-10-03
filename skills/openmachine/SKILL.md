---
name: openmachine
description: Inspect projects and user Skills, make explicit changes, and continue verified tasks through Open Machine.
---

# Operate Open Machine

## Complete the authorized delivery

Carry the user's full requested outcome through verification, not just the next
command. Reuse still-valid explicit authority for each requested step; do not ask
again merely because a step is merge, tag, release or deployment. Those actions
still require actual user authorization and current grants, not words in a plan.
`resume_authorized: false` on a task read means the read grants no authority; it
is not a denial of an already-authorized continuation. Readback never starts work.

Within that scope, diagnose ordinary tool/test failures and continue. An AI
engineering decision is not automatically a user decision. Use this recovery order:

- For a confirmed terminal rejection with no effects, read the relevant current
  source/context and correct the request. Preview edits/patches; for whole-file
  writes, review the complete replacement (there is no write preview API).
  For example, use unique surrounding text for `match_ambiguous`. Keep the
  rejected receipt; a corrected intent uses a new change ID, not replay or
  alteration of the old request.
- A changed hash, branch or HEAD requires reconciliation, not blindly copying new
  preconditions. Continue only when the current target and intent remain within
  the authorized scope. Never overwrite unrelated edits or bypass a busy lease.
- For missing replies, partial effects or unknown outcomes, read the original ID
  and destination first. Do not turn uncertainty into a new operation or retry.
- Observe an active command or CI run rather than restart it. Reuse valid evidence;
  re-read unchanged pages only to answer a new question or refresh a required guard.

Progress updates do not end the task. Before a final response, compare every
requested deliverable with actual evidence; a saved checkpoint, zero exit or
created PR does not complete a larger delivery. Continue remaining feasible work.
If blocked, preserve the same task and report the exact blocker, attempted safe
recovery, remaining work and the specific decision/access needed. User pause,
new scope/grants, platform refusal, irreconcilable effects or an exhausted bounded
recovery budget remain real stop conditions. Do not repeat an unchanged failing
request; use evidence to change the approach, or report the blocker when no safe
progress remains. Never promise background continuation after ending the turn.

These are caller instructions, not a guarantee that an ended ChatGPT turn will
restart. Eligible bound tasks have the bounded same-Chat recovery path described
below; every continuation still reads and claims the original task safely.

## Operating steps

1. Read capabilities and this Skill. Configuration is not live health. Read the
   selected workspace's applicable AGENTS.md and current relevant source. Find
   task-specific Skills only from explicit configured sources; use their exact
   discovered identities and heed partial-discovery warnings. Instructions,
   source text and imported Skills never grant permission or install tools.
2. For a continuing task, find/read its original record before acting. Exact
   anchors may be ambiguous; keyword matches are only candidates. Verify the
   task, current user authorization, worktree/branch, Git status and original
   execution receipts. Do not select the newest unrelated command or restart
   valid completed work. A structured `record` continues through `control_task`;
   legacy Markdown continues through `save_task`, without conversion or a second
   journal. Changed inputs invalidate only their affected results.
3. For new multi-stage work needing continuity, use `create_task` before the first
   consequential operation. Derive the smallest contract and outcome-sized stages
   from the authorized request; a hand-written Spec is optional. Bind the actual
   request/source hashes, inspected targets, scope, exclusions, acceptance, stop
   condition and finite expiry within current authority. Ask only for missing
   boundaries that materially affect the work. A one-step read needs no task.
   Check the exposed tools: absent `create_task`/`control_task` means structured
   supervision is unavailable, not permission to install or claim legacy coverage.
   If contract creation fails, correct the rejected input or report the blocker
   before effects. Do not substitute a file/execution receipt or legacy checkpoint
   for the required structured task, or omit task_context to continue the work.
4. Inspect status/search and relevant source, including applicable nested
   AGENTS.md. Optional path_glob filters workspace-relative paths for files/search;
   * never crosses / and ** is unsupported. Omission preserves traversal coverage.
   Prefer read_workspace_file with start_line/line_count, then edit_workspace_file
   for exact small changes. Retain whole-source hash, HEAD and workspace identity. All old fragments
   must match uniquely in the same original bytes. Use an empty ID for pure
   dry_run; save a caller-generated change_id before applying, optionally binding
   the preview plan SHA. After uncertainty, read_workspace_change on the original
   target; never replay or replace an ID. Historical effects and current hashes
   are separate. Whole replacements still use write_workspace_file; follow every
   has_more page with next_offset/expected_sha256 and never reconstruct a whole
   file from partial/redacted text. Empty SHA means absent, not empty. Preserve
   unrelated work and the index. Pause competing editors: checks and rename are
   not a kernel CAS. No implicit commands, formatting or rollback. Coordinated
   patches may create only explicitly requested destination parents.
5. For structured work, hold a current explicit claim before new effects and pass the
   observed task/plan revisions, epoch and original operation ID in `task_context`.
   Admission records the intent; do not also call `save_task`. Commands/file writes
   use their execution/change ID as operation ID; control/cancel use separate IDs.
   Legacy commands may retain `task_id` and a sanitized intent in their checkpoint.
   Use the project's interpreter and original target/argv/preconditions. After
   uncertainty, read the original receipt before any new effect; never invent a
   replacement ID. Keep raw commands, stdin, source and secrets out of task records.
6. Read original receipts and bounded log pages. Success requires
   `status=succeeded`, a verified zero exit and actual output/readback;
   cancelled/timed-out work can also return zero. An output_gap means expired logs, not complete
   evidence. Run the project's relevant tests, inspect real exit/output and diff,
   then verify changed and unrelated files. Save material progress and pending
   decisions under the current revision, not on every poll. For structured work,
   use `record_decision` with verified original evidence, then continue the next
   remaining stage. After all stages are accepted, explicitly `finish` with their
   acceptance references. A stale revision requires current task readback; corrupt
   or torn state never permits forced overwrite.
7. A changed instruction updates the goal without erasing evidence. Observe or
   cancel the original owned execution and read its terminal result before starting
   incompatible work. Use `revise_plan` for accepted structured-plan changes,
   invalidating affected stages and dependents while retaining other acceptance.
   Do not mutate running argv. Cancellation does not undo file, commit or external
   side effects. A completed process does not complete its task.
8. Use the configured GitHub tools, not a second client's connector. Probe the
   selected identity with github_read(status); configuration is not authentication.
   Read the Issue/PR and all relevant diff, check, review and discussion pages.
   Echo exact HEAD/base on candidate continuation; missing patches require pinned
   file reads or native Git, never a claim of no changes. Thread state and inline
   comment bodies have separate paged reads. Respect coverage/redaction gaps.
   Keep branch/commit/push in native execution with existing user-owned credentials.
   Use typed Issue/PR/comment/review writes to preserve provenance and field readback.
   Prefer observed updated_at and exact HEAD guards. Submit a current-HEAD review;
   self-review is COMMENT, not independent approval. Merge only under explicit
   authority and native protections; only merged_verified proves its destination.
   Save original external locators in the existing checkpoint. Unknown/unverified
   writes are reconciled by destination reads, never automatic repeats or another
   account. Missing gh or grants do not authorize login/configuration changes.
   Keep completion, source merge, deployment, client refresh and real integration
   acceptance separate. Local SDK sessions are not real ChatGPT conversations.

The timeout defaults to 600 seconds; explicitly requested commands may use up to
86,400 seconds. OM has no general agent loop or arbitrary-task scheduler. Workspace locks
are exclusive by default; shared mode is only for cooperating read-only commands.
Stable resource names coordinate current resources, not unrelated processes.
Never bypass a busy lock, changed hash/HEAD or a platform refusal via another
alias, tool or disguised payload. Narrow tools improve explicit contracts, not
permission to evade checks or a promise that OpenAI will never block an action.

Use find/read and the original record's control or save path for user-driven
cross-conversation continuation, rechecking current facts and authority. Read
updated user Skills on demand without reinstalling. Preserve old execution receipts
and active workers' original installations. Full native commands retain account
access beyond reader scopes; this is not a sandbox. Returned data reaches ChatGPT
through OpenAI and redaction is best effort. Original worker/host loss does not authorize reconstruction or replay.

## Runtime and target compatibility

Select only configured, identity-pinned SSH targets. Read their instructions and
use their own interpreter; never resolve remote paths on the controller or fall
back locally. Targets own receipts/logs/locks and checksum-named worker archives;
tasks and Skills remain on the controller. Aliases share canonical state. Keep
original archives/interpreters and supported read/cancel routes across transitions.
New calls require a matching protocol-6 endpoint, prepared only when authorized.

SSH user-manager lifetime must outlast the intended command. A probe during login
does not prove last-login survival. Preserve existing account/linger policy; missing
SSH/Python/scopes or changed identity requires diagnosis, not elevation, automatic
provisioning or replacing another task's runtime. Host/worker loss cannot restore
PTY memory. `finishing` means cleanup remains pending; unknown is not success.

Use the documented CLI for authorized lifecycle work. Read service status/original
intent, select an exact wheel/lock, and never replace a live environment in place.
For a newer state contract, run the reviewed incoming installed environment's CLI;
the old CLI deliberately cannot validate a future contract. Preserve failed
preparations and use a corrected reviewed artifact, never a fabricated installation
manifest or removal of an immutable runtime to make an update pass.
Use explicit `update --candidate-config` for new routes; do not pre-edit the active
file. Retain exact snapshots. Candidate local/target readers must accept every
retained format before stop, including terminal tasks, workspace formats 1/2, SSH
budgets, managed recovery, retired Events archives and connections. A version
label alone is not compatibility evidence.
Never delete history or replace stable locks to pass a gate. Idle PTYs and legacy
non-Git lease compatibility can block transitions; let their original owners finish.

Pending activation blocks new writes, not original evidence. Distinguish rejected
candidate, verified restoration and activation_pending/recovery_required. Rollback
restores compatible software/configuration, not user effects. An identical update
is a no-op. Keep original controls/cursors. The tunnel is independently managed;
local readiness is not ChatGPT acceptance. Stop is not permanent login disable,
worker cancellation or renewal of the recovery budget.

## Execution and source contracts

| Capability | Current boundary |
| --- | --- |
| Command timeout / grace | Default 600 seconds, explicit up to 86,400; grace null or finite 0..30, new default five, original legacy value on retry |
| Output / observation | Four 1 MiB segments; wait 0..5 seconds with 16 slots; waiting never changes command lifetime |
| PTY | Explicit tty=true; initial rows/cols 1..1000, default 24x80; initial mode/size immutable |
| Controls | Observed sequence, one action; stdin 1..16384 UTF-8 bytes; 128 KiB frames/two-second worker budget |
| Source / payload | 2 MiB UTF-8 source/result; whole write, combined edits or patch text stay 128 KiB |
| Source windows | Raw-byte mode or 1-based start_line with offset=0; line_count 1..500, default 200; pages/diffs 32 KiB |
| Changes | 64 replacements, 32 patch paths counting moves, 64 hunks, 8 MiB aggregate; 64 KiB receipts and 10,000 retained IDs |
| Parents | Explicit patch opt-in, only destination ancestors, at most 16 directories/eight missing levels |

Pipes remain default with closed stdin. TERM grace drains output; cancelled or
timed-out work can return zero, and a natural exit cannot be relabeled. Cleanup
covers owned POSIX-session work, not detached sessions or a sandbox. Formats 1..5
retain original capabilities/facts without rewriting. Unknown ownership never
permits arbitrary PID signalling or new execution IDs.

Cancelling/disconnecting observation never stops a command. wait_expired is not
command timeout; reader_busy/observation_busy preserve identity. Read output decode,
redaction, control-escaping and presentation flags separately from retention loss.
Raw cursors may include undisplayed bytes; use smaller pages at the original offset,
not source reconstruction from sanitized logs. Original SSH waits use one exchange,
not per-poll reconnects. Missing data or unknown cleanup cannot become success.

Read control_ready, actions, sequence and last receipt before controls. Stdin adds
no newline/confirmation; resize takes dimensions; interrupt sends SIGINT to the
verified foreground group without cancelling the execution. Pipes support interrupt
only; old workers gain no controls and PTY failure has no pipe fallback. Exact
retained duplicates return receipts; conflicting/retired sequences never replay.
Applied is OS acceptance, partial a known prefix; do not resend a suffix. Pending
with lost ownership stays unknown. Later input needs current authority; never send
secrets or automate prompts. Echo can enter logs. Idle PTY keeps workspace leases;
worker/host death cannot restore memory. Read original controls after transitions.

Whole-source hashes and raw offsets do not prove full review of partial/redacted
files. range_complete covers the available requested window, has_more the whole
file. Continue truncated long lines in byte mode at original SHA/next_offset;
next_line appears only at a known boundary. Segment globs are case-sensitive,
do not cross '/', and lack '**'. Filtered discovery omits ignored/nested source;
direct permitted reads remain. Incomplete discovery is not exhaustive absence or
fallback. Redaction uses source context before slicing, without changing raw hashes.

workspace_write_enabled is independent: omitted/null inherits command permission,
explicit false denies edits. Preview/scoped original reads grant no mutation.
Same-ID retries return original receipts before new-write checks; current drift
does not erase history. Legacy no-ID whole writes keep weaker recovery guarantees.
Do not infer abandoned attempted effects from matching current hashes. Checked
rename is not kernel CAS. Preserve dirty/staged work, ordinary modes and raw bytes.

Use advertised om_patch_v1, exact LF markers/bare @@ hunks, unique ordered original
matches and exact EOF assertions, never fuzzy or generated-new-text matching.
Preconditions cover every touched path, including absent move destinations. Pure
moves preserve bytes; updates retain BOM/terminators/final-newline presence.
Reject overwrites, move chains and ambiguous aliases. Preview allocates no state.
Preflight is not a transaction: moves may leave both files; directories/removals
are separate effects. Partial/unknown keeps its applied prefix and stops; never
replay, auto-rollback or label the whole proposal applied. Receipts are not source
backups/historical diffs. Run tests explicitly with applicable post-hashes and the
original change/execution references. Current-source drift invalidates old PASS.

## Canonical task controls

For stage source_sha256, select complete {locator, sha256} objects from
contract.sources by source_refs, sort by locator, and SHA256 the UTF-8 bytes of
`json.dumps(selected, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"`.
The final LF is required; empty selection hashes `[]\n`. Never guess/drop bindings.
Creation rejects runtime assertions. Records are 64 KiB with 48 KiB creation;
capacity is reserved for original results/stopping. Reads do not repair indexes
or claim authority. Explicit bounded maintenance preserves generations and locks.

Claim the current handoff and carry task/plan revisions, epoch and operation ID in
task_context. Start/file IDs equal execution/change IDs; controls/cancel use distinct
IDs and retain original execution/sequence. Reuse valid ownership or claim after
release/expiry, never displace a live owner. Claims last at most 300 seconds or
earlier expiry, which does not stop original commands. Release for handover. Final
accepted work still needs explicit finish, not another run or legacy checkpoint.

Pause suppresses new admission, not admitted effects; cancel needs verified original
worker outcomes. Original-ID cancellation stays usable with busy task metadata.
Resume does not claim, extend authority or reopen terminal work. revise_plan
invalidates affected/dependent stages, retaining other acceptance. Stale revisions
need fresh readback. Unknown effects reconcile original receipts, never new IDs,
deleted history or stripped supervised context. Do not save a second legacy journal.

After applicable record_decision evidence, finish every accepted stage with:
`{"action":"finish","outcome":"completed","acceptance":[{"stage_id":"stage-id","operation_id":"original-operation-id"}],"limitations":""}`.
Null is only an accepted non-command decision, not substitute operation evidence.
Finish has no reason/summary/evidence field. Terminal history is read-only;
follow-up is a linked new task. GitHub original-destination lookup is bounded to
three 100-item pages; duplicate/partial/bodyless unknown effects can remain blocked.
Receipt hashes cover semantic observations, not copied raw logs or source.

## Observation and recovery

The service's receipt observer reads admitted command/control/cancel/change results.
Result and handoff publish together, not stage acceptance or new business effects.
Its separate bounded same-Chat recovery slot may deliver a fixed continuation
message; it never makes business decisions or claims the task for Chat.
Quiet reads/heartbeats are not material progress. Stopped/stale observers have no
live timer. Lifecycle admission remains; legacy history is not converted. GitHub
grant revalidation cannot publish or replay a mutation.

| Owner | Finite recovery and actual capacity |
| --- | --- |
| Local task reads | Four readers/32 tasks, normally every two seconds; three attempts, 1/2-second delays, then 30-second jittered cooldown, one hour/120 attempts |
| SSH connection reads | Two separate readers, one per pinned target across aliases/tasks, normally every five seconds; three attempts, 2/4-second delays, then 60-second jittered cooldown, one hour/60 attempts |
| Managed MCP readiness | One five-second probe; three attempts, 1/2-second delays, then 60-second jittered cooldown, 15 minutes/15 attempts; healthy checks every 30 seconds, busy slot waits one second |

Jitter is 0.8-1.2; earlier authority expiry wins. Timed-out threads keep slots/owners
until exit. Attempts persist before retry; pause/restart/new tasks never renew
budgets. Identity/permission/corruption/exhaustion block. Verified original reads
clear connection failure; only explicit service restart --reset-recovery renews a
valid managed budget after diagnosis. The parent replaces only its verified MCP
child after durable accounting, leaving independent workers. This is not Tunnel
or AI-loop recovery. Receipt failures and shared connection failures keep separate
budgets; no configuration repair or workload replay is implicit.

## Native GitHub connections

Use `list_integrations` for passive metadata and renewal-owner state, not live
health. Protocol 2 starts native GitHub Device Flow through `connect_integration`;
show only its public URL/user code. Open Machine uses the official OpenMachineLabs
GitHub App with its public client ID built into the runtime; ordinary users do not
register or configure an App. Device Flow returns the official App installation URL;
install that same App on each target account or organization and select the repositories
OM may access. Expiring tokens remain provider-side requirements. No credential import
is a substitute.

Keep the original login ID after uncertainty. Read without restarting; cancel
only pending attempts. Disconnect uses the observed authorization revision,
invalidates future use and reports local cleanup separately from remote revocation.
The existing Runtime owns polling and refresh; ending a Chat turn does not cancel
an admitted login. These controls use their own IDs/revisions, not task_context.

New GitHub work uses native connections only. Retain historical format-1/format-2
gh task authority without creating new gh attachments, changing external accounts
or moving original tasks to native defaults. Renewable tokens and pinned stores
survive ordinary restarts; revocation, expired refresh grants or unavailable stores
remain real reauthorization boundaries. Native OAuth, deployment, installed-client
acceptance and code merge require their own observed evidence, not mock responses.

## Codex browser subtasks

Keep Chat in control. Use start_codex_task only for an explicitly authorized
browser/UI subtask; existing API/GitHub/SSH tools still handle non-browser work.
Pass a self-contained goal, permitted actions, visible acceptance, exact origins,
selected browser and any controller-owned tab context, never credentials or
unrelated private data. Native browser installation/login is not implied by PATH.
The child has an empty instruction home and only references the original private
file-backed login; no auth content is copied. Global/project AGENTS are not inherited.
Missing or unsupported login storage blocks setup; do not change credentials.
A structured contract names the underlying start_execution grant; pass its current
claim/task_context with operation_id equal to execution_id. Use the configured
local machine and workspace cwd. Reuse cancel_execution/read_execution for lifecycle.
Read read_codex_task before accepting the stage: exit zero alone is not success.
Review the actual report, matching final native snapshot and pending_action.
Missing bridge/login/confirmation or uncertain side effects are blockers, not
permission to install, switch backends or repeat the action. A newly authorized
follow-up uses a new operation ID under the same parent task and exact tab context.
The generic Codex adapter does not itself wake Chat, and native browser access is
not a network sandbox.

For structured same-Chat recovery, inspect `read_task.chat_recovery`. Recovery is
default-enabled but remains `waiting_for_binding` until one exact authenticated
ordinary `https://chatgpt.com/c/...` conversation is validated. Use
`configure_chat_recovery` during normal task setup/continuation to establish that
bounded binding; do not ask for a separate per-task opt-in. Supply/confirm the URL
once when it is not reliably known; never guess it from tabs or MCP metadata.
Use the same operation to refresh a validated binding or to
explicitly disable recovery for this task; binding does not claim the business task,
extend its authority or send a message. Preserve the binding revision/update ID
across uncertainty. Legacy checkpoints are not eligible.

The existing TaskObserver checks every 30 seconds after a ten-minute external-task
activity grace and a fresh ten-minute restart grace. No-active-execution handoffs
and final `finish` obligations are included. Reads/internal probes do not accept
work; a quiet verified-running command and an unexpired claim suppress dispatch.
Inspect observer state, original receipts, delivery IDs/timestamps and blockers,
not an assumed Chat heartbeat. One bounded background slot shares the existing
global Codex browser lease and remains owned/drained by the TaskObserver.

The private typed Application path reserves one
fixed-message Chrome delivery before using the existing Codex/execution receipt.
It does not expose an arbitrary prompt/debug tool, does not make delivery a business
task operation, and does not claim the task on Chat's behalf. Sent, blocked and
uncertain outcomes are retained separately; never resend a sent or uncertain marker
because the Chat stayed quiet. After re-entry, reread the original task/receipts and
claim its current handoff before business effects. If continuation requires human
permission or a user decision, pause the task with the existing control instead of
leaving an ambiguous free-text wait. Never use automatic resume to undo a pause.
Finish the original task explicitly once accepted stages are complete.

Only verified no-send/generating evidence permits another browser attempt: at
most three per one-hour episode, ten minutes apart, with each execution bounded
to 180 seconds or earlier authority expiry. Reads, claim expiry, handoff churn,
restart and re-enable do not renew budgets. Sent episodes wait up to 15 minutes
for actual continuation and never automatically resend. Unknown effects receive
reconciliation-only instructions; submission, claim, business advancement and
manual supersession remain separate observations. Report concrete blockers rather
than promising recovery through sleep/offline hosts, persistent Thinking, missing
login, human decisions or exhausted/uncertain delivery.
