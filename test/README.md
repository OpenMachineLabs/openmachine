# Tests

| Directory | Responsibility | Entry point |
| --- | --- | --- |
| `system/` | Dependency-free repository and metadata checks | `python3 -m unittest discover -s test/system -p 'test_*.py'` |
| `unit/` | Configuration, readers, execution receipts and failure behavior | Installed-environment command below |
| `contract/` | Real MCP SDK discovery, private socket, coding workflow, cancellation and restart | Installed-environment command below |

Build and install the wheel as described in the root README. Run product checks
outside the checkout so source imports cannot hide missing wheel files:

```sh
REPO="$PWD"
PYTHON="$REPO/.venv/bin/python"
cd /tmp
"$PYTHON" -m unittest discover -s "$REPO/test/unit" -p 'test_*.py'
"$PYTHON" -m unittest discover -s "$REPO/test/contract" -p 'test_*.py'
```

The system suite remains standard-library-only. Product tests use the locked
runtime dependencies, disposable directories, short local processes and private
Unix sockets. CI does not use production projects, live SSH, GPUs, GitHub credentials,
Codex or inference APIs. No live account is needed for CI.

Local MCP tests are **not** real ChatGPT/Tunnel acceptance. Connect a separately
authorized upstream tunnel to the private socket and run the README's disposable
coding workflow from ChatGPT. Record that result against the candidate
commit and actual account surface. Missing access, client refresh or a real
write receipt remains `NOT RUN`, not `PASS`.

Keep all tests under root `test/`. Add a group only with behavior that needs it;
do not add placeholder suites or a second test framework.

The local workflow checks use small disposable Git projects, short owned process
groups and a few MiB of output. The 24-hour upper bound is validated with a short
command, not a 24-hour wait. No GPU or production canary is required.

Skills, task persistence and explicit edits use small private fixtures in the
existing unit/contract groups. Tests distinguish never-initialized state from
missing history, cover stale/torn saves and original execution links, and use
separate SDK sessions to verify no duplicate file/test execution. Worst-case
JSON escaping is exercised with one bounded text file. This is local protocol
evidence, not actual cross-conversation ChatGPT acceptance; record that separately
on the refreshed candidate connection before marking M2 accepted.

SSH unit checks cover configured routing, bounded structured transport, identity
and file preconditions, controller-only task references, shared target locks,
legacy format-3 and new format-4 cleanup observations and dependency-free deterministic worker bundles.
Mocked transport is explicitly not live SSH evidence. The SDK checks verify
remote metadata and local independence without contacting an unconfigured host.

M3 live acceptance uses an explicitly prepared Linux CPU-only disposable project
and the exact installed worker artifact. Record real SSH file/test/diff, same-ID
reconnection without replay, owned-child cancellation, alias contention and
important prerequisite failures. Separately exercise the refreshed tools from
actual ChatGPT; neither native adapter calls nor SDK sessions substitute for it.
Preserve original executions and unrelated fixture work. No production canary,
private CI runner, long benchmark or automatic service migration is required.

M4 retains the provenance cases and adds scoped identity, candidate pagination,
check/review/thread gates, field/commit readback and uncertain-write reconciliation
at the existing GitHub request boundary. Mocked responses are not live GitHub
acceptance. Real SDK checks cover the expanded tool schemas, non-idempotent write
annotations, scope rejection and local execution with missing gh/authentication.

Actual M4 acceptance uses a separately authorized disposable GitHub repository
and refreshed ChatGPT connection: one tested code change, push, PR/update/comment,
HEAD-bound review, authorized merge and actual destination readback. Retain original
IDs and verify no repeated test/publication after interruption. Keep credentials
out of CI and logs; neither native API probes nor local SDK calls substitute for
the actual client gate. No production repository settings are changed for a test.

M5 adds focused service selection, state compatibility, activation failure and
recovery checks in the existing unit group. SDK checks exercise actual identity
and file-read readiness, write exclusion during activation, and stale-socket
recovery without replacing a live listener. Mocked launchctl is not a native
macOS service test. A separately authorized disposable native run must prove
install/start/status/stop/restart, candidate rejection, real activation and
compatible rollback while preserving an original running worker, counter, tasks,
configuration and user Skills. Restart the test tunnel separately and read the
same execution from ChatGPT; do not repeat earlier coding/GitHub acceptance.
No production services or private CI runners are needed.

## Capability and application boundaries

The existing runtime suite classifies every installed production module and checks
its explicit source dependencies, including relative/function-local imports and
package re-exports. Unknown owners, forbidden edges, wildcard imports and cycles
fail. External callers use exact capability facades; task index validation cannot
call storage, and provider dependencies cannot reverse their declared order.
Focused fixtures prove the checker rejects these violations, not just current code.

The graph excludes type-only `TYPE_CHECKING` imports and does not model implicit
package initialization or dynamically constructed imports. Full-candidate review
and isolated startup/worker tests cover those separate boundaries; this is not a
general Python dependency analyzer. Isolated assembly also rejects disabled
integration imports and verifies that it creates no controller state. GitHub's
existing cases exercise the packaged facade and owning modules through the single
`connection.request` seam; subprocess-failure cases still run that actual backend.
Provider imports cannot reverse the connection/client/evidence/operations order.

Direct application checks cover all nine mutations against pending selection,
wrong-runtime identity and an exclusive lifecycle lease. Successful dispatch holds
one shared lease, preserves the typed arguments (including GitHub Issue/PR state),
and releases it before an actual background worker finishes. Original-ID tests
retain local conflict-before-new-task ordering, remote final request comparison,
and fail-closed uncertain pre-reads. Mocked routing never resolves remote paths on
the controller and retains cancellation after execution is disabled.

The SSH suite checks the exact nested archive members and starts the actual
archive with `-I -S`, without third-party site packages. A short command also
runs through the actual archive worker entry, using inherited leases and a
format-4 fixture with a controller-only task reference. Its terminal record,
output and single counter increment are verified; this is not a live Linux
scope or SSH transport test. Scope observations use the public execution reader
with a mocked user manager. Torn task saves inject failure into the actual store
owner rather than a facade re-export.

Build wheel and sdist artifacts and test outside the checkout so source files
cannot hide omitted capability, application, SSH or GitHub package resources.

Final modular-runtime acceptance is recorded in Spec #18 against exact commits,
artifact hashes, environments and original operation handles. In an explicitly
scoped disposable service, use the released pre-refactor wheel, activate the
candidate and roll back while one original worker remains active. Verify its
creation time, interpreter, counter and receipts without relaunching it or replacing
its environment. Retain original SSH archives and supported read/cancel routes.
Record actual local/SSH/GitHub delivery and fresh-conversation continuation
separately from SDK reconnection. Missing client access stays `NOT RUN`; installed
tests alone do not close the Spec. Do not replay still-valid accepted work.

## Frozen MCP contract

[contract/mcp_contract.json](contract/mcp_contract.json) records the accepted
`v0.1.0` tool names, complete input schemas, optional output schemas and annotations.
Its metadata identifies the source commit, released wheel checksum and SDK versions.
The fixture was captured from that installed release through actual `tools/list`
calls over a disposable private socket, before candidate test changes.

One catalog represents GitHub plus SSH. Existing discovery scenarios and the
GitHub-disabled test check all four enablement combinations. Expected results
omit GitHub tools when disabled and change only the five SSH-sensitive
`openWorldHint` values. Actual results are never normalized to hide differences;
duplicate names, unexpected pagination, missing tools and field changes fail with
an explicit diagnostic. Discovery does not contact GitHub or an SSH host.

Tests read this fixture; they never regenerate it from the candidate. An intentional
interface change needs explicit approval: install the newly accepted baseline with
its locked dependencies outside the checkout, capture the same four configurations,
retain only the named schema/annotation fields, and review the fixture and provenance
diff. Do not update it merely to silence a failed comparison. Descriptions and
runtime values are not snapshots; existing behavior tests retain responsibility for
errors, results, limits, state integrity, CLI and lifecycle behavior. S0 evidence and
the caller/move map remain in [Spec #18](https://github.com/OpenMachineLabs/openmachine/issues/18).

## Spec #28 E1

The focused execution suite uses real short pipe processes for launch/exit facts,
TERM handlers writing more than pipe capacity, distinct stop reasons, owned
session groups, late/repeated cancellation, missing cwd and bounded fault windows.
It preserves old-receipt bytes and original-ID effects. Service tests cover
explicit directional contracts, exact configuration restoration and pre-stop
refusal; mocked launchctl/SSH tests are not native acceptance.

The MCP fixture retains its original released baseline provenance and records
only the approved E1 grace-input delta separately. Installed discovery in all
four integration configurations checks the entire catalog without normalizing
actual output. E1 advertises format 4 and pipes only, not E2 waits or E3 controls.

Retain actual macOS managed transitions and Linux/Python/systemd SSH evidence
against the exact candidate artifact. Check original old-worker identity across
new readers, completed-v4 downgrade refusal before stopping a healthy service,
and an active v4 worker through compatible controller update/rollback. Separate
that product evidence from hosted Linux unit checks and real Chat acceptance.
No production job, GPU, NAS workload or additional test framework is required.

Actual SSH tests must keep the target's existing user manager alive without
changing host policy. Record independent fixture-login conditions explicitly;
the original execution SSH request still disconnects. Last-login manager teardown
is a separate environment failure, not successful cancellation or durable recovery.

## Spec #28 E2

Execution tests cover immediate/unread output, delayed independent readers,
zero-output exits, quiet expiry, strict finite inputs, preparing versus lost
owners, cancellation isolation, bounded lock contention, source-byte cursors,
UTF-8 replacement and visible control/redaction flags. Existing rotation and
E1 lifecycle cases remain intact. Only the approved `wait_seconds` input is added
to the frozen MCP fixture; no tool name or annotation is changed.

Real SDK/private-socket tests cover fresh-connection observation, unrelated
request responsiveness and original command survival after dropping a waiter.
A deterministic SDK-tool admission fixture fills all 16 slots and proves that
cancelling the asyncio await cannot release a still-running observer's slot.
A saturated default executor cannot queue command cancellation behind immediate
reads; cancellation owns independent per-request execution capacity.
Serialized-result tests include both MCP representations and the target envelope.
These checks are not actual ChatGPT/Tunnel acceptance.

Record affected live Linux SSH tests separately: one request waits on the target,
observer cancellation preserves the original worker/counter, and a new immutable
endpoint reads the original E1 worker without replacing its archive. Keep the
existing user-manager lifetime prerequisite explicit. No production workload,
account-policy change, service migration or repeated Spec #18 acceptance is needed.


## Spec #28 E3

The interactive unit group reuses the existing disposable process fixture. It
exercises real controlling terminals, dimensions and exact input; Python REPL
and shell foreground interruption; resize, independent cursors and idle leases;
concurrent/stale/retired controls; lost acknowledgements, partial/backpressured
input and bounded malformed frames; intent/final-publication failures and actual
worker-loss windows. EOF is not counted as application input in crash fixtures.
Native session tests retain an unrelated sentinel and never touch production.

New writers use format 5/state contract 3/SSH protocol 3. Retained v4 lifecycle
facts and formats 1-3 remain read-only; unsupported old controls never adopt a
process. The service tests cover the added mutation's lifecycle gate and actual
candidate-reader requirements. SSH checks retain old scope contracts, protocol
rejection before effects, uncertainty after incomplete control responses, and
pending controls with lost worker ownership even when the scope is still active.

The frozen MCP fixture preserves its released baseline and approved E1/E2 deltas;
E3 adds only `tty`, `terminal_size`, and one non-idempotent control tool. Full
installed SDK discovery verifies all four configured catalogs. Real private-socket
SDK sessions verify original-process continuation and input receipts across a
fresh connection, strict schemas and independent stopping/control capacity.
These are SDK tests, not actual ChatGPT acceptance.

Build/install direct and sdist-derived wheels outside the checkout and verify the
curated standard-library archive, including `control.py`. Record actual macOS and
disposable Linux SSH terminal/session behavior, original IDs/sequence/cursors and
artifact hashes separately. Preserve the target's existing user-manager policy
and original worker archives. E4 owns whole-candidate managed transitions and
actual Chat/fresh-conversation acceptance; missing evidence is never called PASS.

## Spec #28 E4

The existing interactive fixture checks that the candidate's actual state reader
leaves live and terminal v5 control receipts byte-identical. The installed SDK
continuation test now restarts the actual controller process between connections:
original worker/request identity, retained control receipt, raw-byte cursor and
single input effect must survive. It is not a native launchd update or Chat test.

Record native managed transitions against distinct immutable installation hashes;
an identical-wheel update is a no-op. Keep one original old pipe worker and one
v5 PTY through the applicable update/restart/compatible rollback paths. Verify
process identity, in-process state, counters, sequence/receipt, leases and final
cleanup. Separately prove that actual old readers reject active and completed v5
state before stopping the healthy service, and that failed activation restores
the exact prior configuration or leaves explicit recovery state.

Reuse unchanged E1-E3 evidence and original SSH archives. Run only disposable
projects with the existing user-manager policy; do not reboot a production host
or add a new test framework. Refreshed real Chat calls and a fresh conversation's
original-evidence readback are separate gates. Record exact candidate/artifact,
platform and original handles, with PASS/FAIL/NOT RUN; missing client evidence or
later MCP/task integration never becomes a pass through SDK tests.

## Spec #13 W1

`unit/test_workspace_changes.py` covers the same original-byte editor and durable
publisher through local and target entrypoints: preview purity, independent grants,
context/plan drift, exact/overlapping matches, preservation, aliases, capacity,
non-Git/PTY leases, concurrent same-ID calls and deterministic persistence/IO races.
The reader race fixture releases a writer between record and owner observations;
it must never classify stale prepared state as proof of no effect. Service cases
verify actual change readers and local/remote compatibility evidence before stop,
including old candidates that silently ignore a new directory.

The existing MCP suite uses installed private-socket SDK sessions for exact edits,
server restart/original-ID readback, four configured catalogs, strict replacement
arguments and worst-case JSON bounds. A direct SDK cancellation fixture verifies
that a disconnected observer does not terminate or duplicate its admitted edit.
Neither that fixture nor mocked SSH/launchctl is live Chat or native-transition
acceptance. W4 retains those real-client, live SSH and managed-upgrade checks.

The manually reviewed frozen-contract delta adds only two workspace tools and two
optional whole-writer inputs; unrelated schemas and prior provenance stay intact.
Run wheel/sdist-derived installation and the curated target archive outside the
checkout; target code must still import under `-I -S` without controller dependencies.
W1 deliberately advertises 128 KiB sources, change format 1, state 4 and SSH 4;
execution format 5 and the pinned MCP SDK remain unchanged.

## Spec #13 W2

The existing workspace group covers 2 MiB sources with small edits beyond the
old 128 KiB boundary, full hashes, preview and original-ID readback, exact bytes
and index preservation, EOF/Unicode/CRLF line windows, long-line byte continuation,
full-context redaction and independent payload/Skill limits. Filtered discovery
cases cover Git/non-Git selection, ignored/untracked source, strict segment globs,
nested repositories, clipped enumeration and names, changing snapshots and safe
Git capture. Target dispatch uses the same reader/search owners without commands.

Installed SDK checks cover the approved read/filter schema delta in all four
catalogs, a large-file edit/receipt path and worst-case escaped source/search
responses. Bounded result pages must advance by returned entries, not discarded
ones. The legacy raw-read fixture now verifies complete 32 KiB continuation.
No new tool, production module, dependency or test framework is introduced.

Use actual separately installed W1 and W2 readers to verify old receipt acceptance
and refusal of incompatible large-range receipts before a service stop. This is
reader/gate evidence, not native launchd rollback. Workspace record format 1/state
contract 4 remain; SSH protocol 5 distinguishes W2 selectors and source limits.
Build direct/sdist-derived wheels and the curated target archive outside the
checkout. Live SSH transport, refreshed Chat and native managed transitions remain
W4; mocked transport or installed SDK sessions cannot satisfy those gates.


## Spec #13 W3

The existing workspace group exercises strict om_patch_v1 parsing, exact original
hunks, BOM/mixed terminators/EOF serialization, add/update/delete/move and bounded
explicit parents. Whole-plan rejection leaves all target files/directories intact.
Fault injection covers per-effect intent/observation saves, late destinations,
source drift after move publication, replace/unlink acknowledgement loss, directory
sync and terminal receipt failure. Verify ordered applied prefixes, unattempted
suffixes and uncertainty without replay or automatic rollback. Preview checks the
same worst-case record budget without initializing any state.

Installed SDK tests use the one new tool through real private-socket sessions,
strict schemas, edit-only grants, worst-case escaped responses, controller restart
and original-ID readback. A disconnected request cannot cancel an admitted patch;
a partial result is an error when applying but a successful historical read.
The frozen fixture adds only the manually authored W3 tool and provenance; all
prior tools are unchanged and all four integration catalogs are compared exactly.

New patch records use format 2; existing write/edit records retain format 1 and
new readers accept both without rewriting. State contract 5/SSH protocol 6 do not
change execution format 5 or package/MCP versions. Use actual separately installed
W2 and W3 readers to demonstrate format-1 compatibility and format-2 downgrade
refusal, preserving receipt bytes. Candidate source tests are not native service
activation/rollback evidence.

Build direct and sdist-derived wheels, compare production/Skill contents with the
source and installation, and verify the curated target archive includes patch.py
under isolated -I -S startup. Target dispatch and transport-failure fixtures never
contact a live host. The current maintainer excluded node1-node3 SSH testing;
do not probe, enable or alter those hosts. Refreshed actual Chat/fresh-conversation,
live SSH transport and native managed transitions remain W4, not implied by the
installed SDK/unit results or by using an older OM to develop this candidate.


## Spec #13 W4

The existing workspace suite exercises a real bounded Unicode-path request that
would overflow its initial receipt. Reject it before allocating a change ID in
both empty and established stores; preserve original records, source and index.
Retained-ID reconciliation precedes new-admission sizing. Keep actual initial IO
failure tests: an uncertain allocated record is not cleaned up or replayed.

The existing patch SDK workflow explicitly starts one validation execution,
checks touched hashes before/after it, and stores both operation references in
the existing task. After controller restart it reads the original task, change
and execution; later source drift cannot inherit the old test PASS. The counter
remains one. No new supervisor, test framework or frozen schema is introduced.

Record actual managed transitions using distinct immutable W2/W3/W4 artifacts:
format-1 compatibility, retained format-2 history, compatible rollback and
incompatible-reader rejection before a healthy service stops. Preserve original
workers, counters, records and configuration bytes. Test wheel/sdist and isolated
standard-library target archives outside the checkout. SDK/native evidence is
separate from real refreshed Chat tools and a fresh conversation's readback.
Live SSH is NOT RUN, maintainer-waived for this W4 scope; offline tests and runtime
compatibility checks remain intact. Missing actual-client evidence stays pending.

## Spec #32 MCP migration

Use the exact SDK 2.2.0 pin and independently installed artifacts. The existing
contract suite exercises modern 2026-07-28, retained 2025-11-25 and automatic
negotiation, all four configured catalogs, success/error envelopes, invalid
arguments, private-socket ingress limits and concurrent/disconnected observers.
The frozen tool fixture remains unchanged; do not regenerate it to hide drift.

Upgrade acceptance is quiescent: finish or safely stop work, retain a recoverable
checkpoint and execution receipts, stop the service, activate the candidate, read
original history, then continue the same task with a new authorized execution.
Do not require zero downtime or a running worker to survive an upgrade. Retain
existing safety checks and reuse valid tests unaffected by the change.

Native/SDK results are not actual ChatGPT/tunnel acceptance. Record A6 separately
on the candidate connection; no Events delivery or automatic continuation is
implied by the SDK extension seam. Keep disposable evidence outside the repo.

## Spec #29 S0 continuity boundary

S0 freezes the next task contract in [Spec #29](https://github.com/OpenMachineLabs/openmachine/issues/29#s0-contract-freeze-and-probe-2026-10-01),
not in a second runtime or copied design document. No production task writer,
observer, callback sender, subscription or inference backend is added here.

The existing MCP suite tests the exact SDK 2.2.0 public extension/middleware seam:
additive Events methods, explicit top-level capability advertisement, unchanged
core tool behavior and legacy method rejection. The in-process SDK/ASGI fixture
rejects subscribe/unsubscribe deliberately; it has no subscription backend and
cannot demonstrate delivery, authorization, signatures, persistence or AI wake-up.
The private-socket fixture separately observes the installed OM baseline.

For an explicitly authorized socket, the opt-in probe makes only `server/discover`
and `events/list` requests. It never auto-discovers installations or sends a callback:

```sh
"$PYTHON" "$REPO/test/contract/probe_task_handoff.py" --socket /absolute/private/state/mcp.sock
```

Run outside the checkout with the locked installed environment. Exit 0 means only
Events discovery is ready; 2 means blocked/inconclusive discovery; 1 means the
bounded probe failed. Output omits raw responses, paths and callback credentials.
Each response is at most 64 KiB, each request at most five seconds, the whole probe
at most 15 seconds, with no retries/redirects. An incomplete catalog is not absence.
`ai_continuation=not_run` is unconditional: discovery is never end-to-end acceptance.

Actual S0 caller acceptance requires an enrolled disposable plugin/endpoint, a
verified principal and callback supplied by ChatGPT after initial opt-in. Preserve
one task and original operation IDs. Independently test running-command loss,
committed-result/caller absence, and a task with no active execution. The returning
AI must read original evidence and perform/verify the next harmless step without a
post-fault user message, Retry click or a probe script doing the AI's step. Record
event receipt, task read, claim and next effect separately. Pause, late delivery
and duplicate events must not reactivate work. Record missing eligibility, binding
or callback evidence as BLOCKED/NOT RUN, not as an SDK limitation or a waived goal.
No account/usage-cost promise follows from these tests. Stop owned disposable
services and retire test subscriptions explicitly; leave production untouched.

## Spec #29 S1.1 canonical task records

Run the existing installed unit and MCP suites. Continuity cases use real private
files, concurrent creators, explicit fsync failures and subprocess exit on both
sides of directory publication. They distinguish an uncommitted temporary, an
uncertain acknowledgement and a committed canonical record with stale metadata.
Bounded rebuild cases cover mixed legacy history, generation-checked restart,
50-record/2-MiB limits, missing locks and unsafe/corrupt/unknown state. No test
converts production checkpoints or uses their contents as executable instructions.

Service cases verify actual reader coverage, including synthetic terminal-only
history, and refusal before mocked service stop when a candidate omits that proof.
For separate installation acceptance, run the retained pre-S1.1 interpreter and
the candidate's `-I -m openmachine.service check-state CONFIG STATE` against only
disposable legacy/mixed/terminal fixtures. Preserve every original record byte;
an old reader must reject new directory/index ownership, not silently ignore it.
Record actual-version checks separately from synthetic/mocked service fixtures.

The private-socket MCP cases exercise both supported protocols: create/retry,
strict top-level/nested argument rejection, non-runnable legacy association,
stale-index readback and real server restart. The frozen contract adds one
manually authored `create_task` expectation and preserves all prior tool entries;
tests must not regenerate expected schemas from candidate SDK output.

These are S1.1 storage/admission-boundary checks, not full S1 acceptance. Retained
terminal fixtures alone do not prove a terminal writer; server restart does not prove
runtime recovery or automatic AI continuation. Production activation, actual
ChatGPT refresh, live SSH and Events remain separately scoped and NOT RUN unless
their own authorized evidence is recorded. Do not close Spec #29 for this slice.

## Spec #29 S1.2 task control and unified admission

Run the same installed suites; no separate runner or workflow simulator is added.
Continuity cases exercise actual canonical control publication on both sides of
process death, concurrent claims/intents, pause while domain dispatch is waiting,
stale task/plan/epoch, lost result publication, source drift, targeted plan
invalidation and reserved capacity. Verify original file/command counters remain
one. A real PTY case loses task publication after input acceptance, reads the
original control receipt without resending and verifies that task history does
not contain the input. Original cancellation stays usable with a busy task identity.

Provider tests distinguish simulated transport/destination facts from real hosted
publication. Original marker/number reads cover issue, PR, comment, review and
merge, with exact parent/HEAD, identity and destination checks. No retry emits
another POST/PATCH/PUT. Duplicate or incomplete bounded search remains unknown;
a bodyless pending effect cannot escape by dropping its task context. Stale task
contexts must fail before provider access, not after a mocked successful write.

The private-socket suite runs a complete create/claim/start/pause/server-restart/
original-result/resume/decision/finish sequence on both supported MCP protocols.
It proves manual SDK reattachment, not an unattended ChatGPT turn. Read-only task
and execution inspection leaves canonical bytes unchanged. The manually authored
fixture adds only `control_task` and optional strict `task_context` to all eleven
existing mutation tools; prior arguments and annotations remain intact. Check
unknown top-level and nested runtime/owner fields before SDK filtering and effects.

Validate actual S1.1 and candidate readers against disposable records produced by
real control/terminal transitions, including retained original executions, and
preserve the pre-S1.1 incompatibility gate. Source, installed package and wheel
resources must match; the sdist-derived wheel must reproduce the direct wheel.
Keep exact candidate, original receipts and diagnostics in the PR evidence.
This increment does not deliver the absent-caller observer (A4), runtime recovery,
Events, live SSH activation, production deployment or A16. Full S1 remains open.

## Spec #29 S1.3 local absent-caller observation

Use the existing continuity/runtime/MCP groups and installed-wheel commands.
Actual local commands and file receipts prove automatic canonical result/handoff
publication without a caller saving progress or repeating the operation. Real
subprocess death immediately before/after result publication proves original
counter/receipt and handoff preservation, including a stale derived index.
The private-socket test closes both clients on both supported protocols, then
checks canonical bytes with no further tool calls while the service stays up,
restarts during the command, or restarts after the command finishes while down.
All cases retain the same execution identity and one counter increment.

Focused fault cases cover late results racing pause/terminal decisions, actual
lifecycle-lock contention at startup/publication, finite durable read episodes,
restart without budget reset, four real blocked threads retaining their slots
and owner through timeout/shutdown/fatal coordinator failure, 32-task overflow,
corrupt-record aggregate bounds and unchanged-history discovery. Synthetic
receipts only test scheduling/budgets; they are not execution or provider proof.
Ordinary task reads remain passive while background writes may independently
advance the record. A stopped observer must not advertise an active retry timer.

Verify exact source/wheel/installed resources and sdist roundtrip, actual old
readers with retained recovery/terminal records, and the unchanged MCP input
catalog/format contracts. Qualify managed-service tests separately from fixtures.
These tests do not prove SSH recovery, Events delivery, actual ChatGPT wake,
production activation or all of S1/A16. No production fault, new test framework,
per-task daemon or second task database is part of this acceptance.
