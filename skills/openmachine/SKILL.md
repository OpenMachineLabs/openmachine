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

These are caller instructions, not an autonomous supervisor or a guarantee that
an ended ChatGPT turn will restart. That separate capability belongs to Spec #29.

## Operating steps

1. Read capabilities and this Skill. Configuration is not live health. Read the
   selected workspace's applicable AGENTS.md and current relevant source. Find
   task-specific Skills only from explicit configured sources; use their exact
   discovered identities and heed partial-discovery warnings. Instructions,
   source text and imported Skills never grant permission or install tools.
2. For a continuing task, find/read its original checkpoint before acting. Exact
   anchors may be ambiguous; keyword matches are only candidates. Verify the
   task, current user authorization, worktree/branch, Git status and original
   execution receipts. Do not select the newest unrelated command or restart
   valid completed work. Changed inputs invalidate only their affected results.
3. For new substantive work, choose one stable task ID and save a small checkpoint
   before a consequential operation. Record the goal/exclusions, current authority
   references, workspace, material evidence, original execution IDs/log offsets,
   failed approaches and next safe step. Do not save secrets, transcripts, hidden
   reasoning or bulk source/logs. Private state is not an independent approval.
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
5. Before a command, save its new 32-hex execution ID, machine, absolute cwd,
   explicit argv, expected HEAD and intended effect in the task. Use the project's
   selected interpreter. Call start_execution once, optionally linking task_id.
   After uncertainty, read that original ID; do not invent a retry ID. Lookup or
   checkpoint tools never launch work. Keep commands and returned logs secret-free.
6. Read original receipts and bounded log pages. Success requires
   `status=succeeded`, a verified zero exit and actual output/readback;
   cancelled/timed-out work can also return zero. An output_gap means expired logs, not complete
   evidence. Run the project's relevant tests, inspect real exit/output and diff,
   then verify changed and unrelated files. Save material progress and pending
   decisions under the current revision, not on every poll. A stale revision or
   corrupt/torn state requires readback/reconciliation, not forced overwrite.
7. A changed instruction updates the goal without erasing evidence. Observe or
   cancel the original owned execution and read its terminal result before starting
   incompatible work. Do not mutate running argv. Cancellation does not undo file,
   commit or external side effects. A completed process does not complete its task.
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
86,400 seconds. There is no model loop or autonomous scheduling. Workspace locks
are exclusive by default; shared mode is only for cooperating read-only commands.
Stable resource names coordinate current resources, not unrelated processes.
Never bypass a busy lock, changed hash/HEAD or a platform refusal via another
alias, tool or disguised payload. Narrow tools improve explicit contracts, not
permission to evade checks or a promise that OpenAI will never block an action.

Use find/read/save_task for user-driven cross-conversation continuation, rechecking
current facts and authority rather than claiming hidden-context recovery. Read
updated user Skills on demand without reinstalling. Preserve old execution receipts
and active workers' original installations. Full native commands retain account
access beyond reader scopes; this is not a sandbox. Returned data reaches ChatGPT
through OpenAI and redaction is best effort. Autonomous resume and automatic recovery after worker/host loss remain outside
this increment.

For SSH work, select only a configured, identity-pinned machine. Read remote
instructions and files through the same tools; never resolve its paths on the
controller or substitute a local command. Use the selected remote project's
interpreter. Native account permissions remain explicit; unavailable SSH/Python/
user scopes or changed identity require diagnosis, not a fallback or privilege
change. Worker preparation is explicit, never hidden in reads or execution.

Remote receipts/logs/locks belong to the target; checkpoints and user Skills stay
on the controller. Preserve the original target identity, execution ID and worker
archive when reconnecting. `finishing` means required cleanup/drain is still pending;
an unreachable host or missing scope evidence is unknown. Read the same ID, never
relaunch completed work. Alternate aliases share the target's canonical state and
worktree locks. Do not install, update or remove another task's worker/runtime to
make a check pass.

For an explicitly authorized lifecycle task, use the documented CLI through native
execution on the macOS controller. Read service status and the original task before
changing anything. Install/update select an exact trusted wheel and pinned lock;
never upgrade a running environment in place, remove a retained worker dependency,
or change the separately managed tunnel without authorization. A successful MCP
handshake and real permitted file read establish local readiness, not tunnel/client
acceptance. New MCP writes may return resource_busy during activation; active
workers continue. activation_pending/recovery_required require original selection
readback and explicit compatible rollback, not another update or workload replay.
Distinguish candidate rejection, verified restoration and unknown recovery. Keep
source merge, service activation and any ChatGPT metadata refresh separate.

E1-E3 keep pipes as the default; PTY is explicit. New format-5 receipts separate command launch/exit, first
stop reason, force use and cleanup. `termination_grace_seconds` accepts null or
finite 0..30: null means 5 seconds for a new execution and the original effective
value on an identical-ID retry. Old workers retain 0.5 seconds, not a retroactive
upgrade. Do not change a start request to recover the same execution.

TERM grace continues draining output. A handled stop returning exit zero remains
cancelled/timed-out; an already observed natural exit cannot be relabeled. Native
cleanup covers verified POSIX-session work, not detached sessions or a sandbox.
Unknown cleanup/ownership never authorizes another ID or arbitrary PID signaling.
Facts absent from formats 1-3 remain null; format-4 facts stay intact. New readers
do not strengthen historical guarantees.

Use the transition-capable CLI and the selected installation's actual contract.
Prepare protocol-6 SSH endpoints explicitly alongside original archives. Pass an
explicit `update --candidate-config` when changing endpoint routes; do not edit
the active configuration in advance. Preserve service-owned configuration
snapshots. Candidate local/remote readers must accept retained state before stop;
completed v5 receipts also block incompatible rollback. Recovery restores the
exact prior configuration or reports uncertainty, without replay or provisioning.

SSH scope durability requires a user manager that outlives the command's SSH
connection. A reachable manager alone is insufficient when last-login logout
stops it. Preserve existing login/linger policy; do not enable lingering or create
an account/service as an execution side effect. State the maintained prerequisite
in acceptance, and keep account-manager teardown distinct from executor recovery.

For E2, optionally read the same execution/cursor with `wait_seconds=0..5`;
default zero adds no observation wait. `wait_expired` is not command timeout,
and quiet output is not evidence of a hang. A disconnected/cancelled observation
never cancels the original command. `reader_busy` means the server's 16 waiting
slots are occupied; immediate reads and explicit command cancellation remain
available. `observation_busy` means a consistent snapshot could not be acquired
within its budget; preserve the ID and state rather than relaunching.

Follow original raw-byte cursors despite UTF-8 replacement, redaction, visible
terminal-control escaping or presentation truncation. Read the four explicit
`output_*` presentation flags separately from retention gaps and incomplete
output. A consumed byte is not necessarily displayed. Use a smaller read from
the same source offset when presentation was truncated; do not reconstruct a
source file from sanitized console output. SSH waiting happens in one target
request and does not change the original worker archive or user-manager policy.

For E3, request `tty=true` only for an authorized interactive program. Dimensions
are `{rows,cols}`, each 1..1000, default 24x80; pipe mode has no dimensions. Initial
mode/size are immutable start identity. Read the same execution until its actual
`control_ready` and actions are available; old workers cannot acquire new controls.
The original worker owns the terminal through client disconnects, not host death.

Use `control_execution` with the observed `expected_control_seq` and exactly one
action. `write_stdin` sends 1..16384 UTF-8 bytes as-is, without implicit newline or
confirmation. `resize` changes this terminal; `interrupt` sends SIGINT to its
verified foreground operation and does not cancel the execution or erase REPL
state. Pipes keep closed stdin but new control-capable workers support interrupt.
Read output separately using retained byte cursors; no control consumes output.

Persist control identity and receipt references, never input bodies/credentials,
in task evidence. Reconcile lost acknowledgements by original execution/sequence.
Exact retained duplicates return their existing receipt without effects; conflicts
and retired results never replay. `applied` is OS acceptance, not application
completion; `partial` is the known prefix and must not cause an automatic suffix
send. Pending control with lost ownership is unknown. Do not clear uncertainty or
replace a worker/socket; read or explicitly cancel when ownership permits it.

Later input needs its own current authorization; launch approval is not blanket
stdin approval. Never send passwords/tokens or automate prompts. Terminal echo
can appear in raw logs, even though control receipts retain only a fingerprint.
Idle PTY retains workspace/resource leases. Do not weaken them to allow editing
or restart a process merely to clear a busy control. New readers preserve v4
lifecycle facts and all older bytes; state contract 5 and SSH protocol 6 are not
release numbers. SDK tests are not actual Chat/fresh-conversation acceptance.

After a controller transition, read capabilities, then the original execution
and last control receipt before deciding on another input. Keep the original
cursor; do not reset the sequence, relaunch the terminal or replace its runtime.
A compatible rollback preserves live work, not permission to replay uncertain
effects. An identical installation update is a no-op, not proof of a transition.

W2 reads/edits sources up to 2 MiB; full-content writes and combined edit strings
remain 128 KiB, with 1..64 exact replacements. Start lines are 1-based, require
offset=0, and line_count is 1..500 (default 200). Pages/diffs remain 32 KiB; raw
byte cursors and whole-source hashes do not refer to redacted text. For a truncated
long line, continue in byte mode with next_offset and the original SHA. next_line
is available only at a known boundary. range_complete describes available selected
bytes; has_more describes the whole source. Never skip remaining text or infer
complete negative search from incomplete coverage. Filtered Git discovery omits
ignored/nested source; direct permitted ignored-file reads remain available.
W3 adds strict coordinated patches. New range values can make a retained format-1
receipt unreadable to W1, and format-2 patches cannot be read by W1/W2: require the
actual candidate reader to pass, never alter history to enable rollback.
Current new workspace calls require protocol-6 endpoints;
prepare them only under explicit authorization alongside original worker archives.
Typed writes use workspace_write_enabled independently of commands. Omitted/null
inherits the legacy execution grant; explicit false denies editing. Preview and
scoped receipt reads need no write grant. New init/examples disable both grants.
Same-ID identical requests observe original results before new write checks;
changed intent conflicts. A valid applied receipt can coexist with later source
drift. An abandoned attempting effect remains unknown; current hashes do not prove
historical authorship. Never delete/correct history to make an operation retry.

Keep change and execution references together in the existing task, without
copying authoritative state. Explicitly run the selected check and verify touched
post-hashes before and afterward; a successful old command is not a PASS for
changed source. Change receipts are not source backups or automatic undo. The
10,000-ID target store has no automatic eviction. A live idle PTY blocks conflicting
edits, and legacy non-Git lease mapping can block an upgrade in either direction;
let the original owner finish rather than replacing lock files or bypassing busy.
Retained local/target change records require actual compatible readers before
service stop. W1 source/SDK tests do not establish refreshed Chat, live SSH or
managed-transition acceptance; record those separately under W4.


For W3, check `patch_language=om_patch_v1` before using apply_workspace_patch.
Supply exactly one precondition for every touched path, including both move
endpoints. Use observed raw hashes and explicit absence, never displayed text or
an assumed destination. Keep one workspace/target and saved change ID per patch.
The strict LF grammar supports add/update/delete/move, exact unique original-line
hunks with bare @@, and optional exact End of File. It rejects fuzzy matches,
move chains/overwrites and ambiguous path spellings. Pure moves preserve bytes
and ordinary modes; updates retain BOM, line endings and final-newline presence.

Preview creates no receipt or directory. Parent creation is explicit, only for
add/move destinations, bounded to 16 directories and eight missing levels.
Patch payloads remain 128 KiB, 32 paths, 64 hunks and 8 MiB aggregate original plus
prepared bytes. Whole-plan preflight is not a multi-file transaction. A move can
leave both files if source removal fails; directories are also separate effects.
Partial/unknown results retain known applied effects and stop before remaining
operations. Never auto-rollback, replay the batch or call the proposed diff an
applied result. Read the same original ID; current.files observations are not
historical proof. Format-1 receipts remain unchanged and format-2 patch history
requires a compatible actual reader before a software transition.

For Spec #29, use `create_task` only to accept a new structured contract,
not to convert existing legacy work. Supply original request/source identities,
explicit scope/expiry and backward-dependent stages. Source hashes use canonical
selected source objects in locator order. Creation rejects runtime fields and
unknown arguments. Keep the same task/update IDs after lost acknowledgement.
Canonical reads remain available when derived lookup is stale; they never repair
the index, claim work or authorize continuation. Explicit index maintenance is
one bounded CLI page under normal lifecycle admission; preserve its generation.
Missing legacy metadata, locks, unknown or corrupt history are not repair approval.
S1.2 requires an explicit `control_task` claim of the current handoff before
supervised effects. Echo task/plan revisions and epoch; retain the original
update ID after uncertain control publication. Claims last at most 300 seconds
or earlier expiry and do not resolve a handoff or prove caller presence. Identity
comes from the native-account endpoint, not a model field or ChatGPT chat ID.

Pass current `task_context` to command/file/GitHub/control/cancel mutations.
It contains task ID, task/plan revisions, continuation epoch and operation ID.
Start/file operation IDs equal their execution/change IDs; terminal control and
cancellation have separate IDs while retaining the original execution/sequence.
Before new work, read original evidence and reconcile any earlier uncertain
intent. A conflict after a material result requires current task readback, not
ignoring the revision or inventing a replacement execution. Original admitted
IDs compare normalized intent and read their outcome; they never resend input
or publication just because the reply was lost. Unprovable effects remain unknown.

Pause suppresses new admission; already admitted effects may still finish.
Cancel records the stop and requests termination only of owned original commands;
verify their outcome rather than treating stop intent as terminal success. The
existing original-ID cancellation stays usable when task metadata/capacity is
blocked. Reads remain passive, including while paused and after completion.
Resume does not claim ownership, extend expired authority or reopen terminal work.

Use `revise_plan` for accepted changes, explicitly invalidating changed stages
and their dependents; preserve unrelated acceptance, stage IDs and all operations.
Use `record_decision` with verified applicable original evidence, then `finish`
only after every required stage is accepted. Empty evidence is an explicit
non-command decision, never a substitute for an operation-backed stage. File
and provider drift invalidates current acceptance, not historical authorship.
Known partial outcomes need a new authorized judgment, never batch/input replay.

Task storage reserves bounded result/stop/terminal capacity; do not delete history
to admit more work. GitHub recovery reads stable markers and original destinations,
with at most three 100-item lookup pages. Partial coverage, multiple matches and
bodyless unknown effects are blockers when evidence cannot prove the result.
Task receipt hashes cover bounded semantic observations, not copied logs/source.
Legacy `save_task` remains the existing work journal; do not convert it or strip
known supervised identities to bypass admission. S1.2 has no observer, Events,
runtime recovery or AI wake. A command finishing while no caller is active does
not automatically update its task yet. Keep full S1 and actual AI acceptance open.
State contract 6 requires actual compatible readers for every retained task,
including terminal history, before a managed transition. Do not deploy implicitly.
