# Service operations

[Domain guide](SKILL.md)

On this page:

- [Legacy wheel-based installation and control](#legacy-wheel-based-installation-and-control)
- [Exact updates and compatible rollback](#exact-updates-and-compatible-rollback)
- [Bounded runtime recovery](#bounded-runtime-recovery)
- [Diagnose without replay](#diagnose-without-replay)
- [Product management](#product-management)
- [Policy and old-Core limits](#policy-and-old-core-limits)
- [Original maintenance operations](#original-maintenance-operations)

Use [product management](#product-management) for management-capable installations.
It owns persistent Stop, login preference and original install/update operations.
The wheel-based commands below remain a compatibility entry point: `service stop`
only unloads the job, whereas `manage stop` preserves Stop until explicit Start.
Neither command cancels admitted user work.

## Legacy wheel-based installation and control

Use the existing logged-in macOS user, a trusted built wheel and its exact lock.
Choose a stable harmless file in a configured local workspace for readiness.
Do not run manual `serve` on the same state as a managed service.

```sh
.venv/bin/openmachine service install --wheel "$PWD/dist/openmachine-0.2.18-py3-none-any.whl" --requirements "$PWD/requirements.lock" --probe-workspace project --probe-path AGENTS.md
.venv/bin/openmachine service start
.venv/bin/openmachine service status
.venv/bin/openmachine doctor --service
.venv/bin/openmachine service restart
.venv/bin/openmachine service stop
```

Global absolute `--config` and `--state-dir` options precede the subcommand.
Install registers without starting the service or enabling execution. Start/restart
require actual version/configuration/process identity and permitted file-read
readiness. The probe returns a hash, not contents. Editing that file is allowed;
deleting it or losing access makes the service unready.

The state-derived LaunchAgent is
`~/Library/LaunchAgents/co.openmachine.runtime.<state-id>.plist`.
The stable launcher, immutable versions, selection and configuration snapshots
live below `<state-dir>/service/`. Preserve the selected base Python too: never
move a venv or delete an old worker's installation. Tasks, executions, changes,
Skills and their stable locks retain their existing owners and paths.

The captured installation `PATH`, not shell startup files, supplies Git/SSH and
explicitly requested command-line tools.
Packaged runtime installations derive their native `PATH` from the selected retained
payload, so activation/restoration selects that candidate's Git without changing
the legacy definition or the prior installation's captured environment.
The private `service/service.log` is separate from bounded execution logs.
No privileged daemon, tunnel or remote service is installed. `stop` unloads the
current job; its installed LaunchAgent can load at the next GUI login. It is not
persistent disable, cancellation, uninstall or rollback of user work.

## Exact updates and compatible rollback

```sh
.venv/bin/openmachine update --wheel /absolute/new/openmachine-VERSION-py3-none-any.whl --requirements /absolute/new/requirements.lock
.venv/bin/openmachine rollback
.venv/bin/openmachine doctor --service
```

Preparation snapshots the artifacts, builds at a distinct final path, validates
dependencies/identity with installed readers, and probes private MCP/file readiness.
A rejected candidate does not stop the healthy service. Activation takes lifecycle
admission, rechecks, persists pending intent, switches and verifies the managed
instance. Failure either verifies restoration or retains `recovery_required`.

When upgrading the state contract, invoke the reviewed incoming installed
environment's CLI, not an old CLI that cannot validate that future contract.
Failed preparations remain immutable diagnostic evidence. Use a corrected reviewed
artifact for a subsequent attempt; do not invent `installation.json`, delete the
failed runtime, or weaken reader checks to reuse an incomplete preparation.

An identical update is a no-op, not a restart. Legacy unload-only stop allows update/rollback to activate the selected
service. Explicit deliberate Stop is preserved through both operations. Brief MCP disconnection is possible; read the
same task/execution/control identities afterward, never repeat input or work merely
because a response disappeared. Original workers retain their own interpreter,
session, receipts and immutable files; software rollback does not undo effects.

For explicitly changed configuration or SSH routes, leave the active file unchanged
and pass `update --candidate-config /absolute/new.toml`. The owner snapshots exact
bytes and includes them in the prepared installation identity, so the same wheel
or payload can activate a different configuration with a distinct rollback target.
It validates local/target readers and restores actual pre-transition content
on failure, including valid edits since the previous update. An independent racing
edit is preserved with uncertainty, not overwritten. Cooperative locks do not
fence external editors or another runtime/state root.

Protocol or state-contract numbers alone do not prove compatibility. Actual readers must
cover every retained execution, task, workspace format, SSH budget, managed
recovery, retired Events archive and connection record, even terminal-only history or disabled
integrations. Native connection format 2 needs the contract-11 reader; old-only
format-1 connection state retains its exact hash for supported prior readers.
Metadata checks do not read or print credential values and do not prove live OAuth.
An old reader that ignores a directory cannot pass. Formats 1/2 and large retained
workspace ranges may block downgrade; `0.1.0a6` and earlier are not managed rollback
targets. No history is rewritten or deleted to permit a transition.

A task-index generation may legitimately advance between the candidate process
snapshot and the local compatibility read. Only that observed pre-effect generation
mismatch permits up to three full reader checks with short pauses. Other reader,
format, integrity or authorization failures remain fail-closed; continuing changes
exhaust the finite budget without stopping or replacing the selected service.

The removed Work adapter leaves any private `events.json` and its original lock
untouched. Lifecycle checks validate the bounded archive envelope and canonical
fingerprint without interpreting or scheduling its nested delivery records.
`events_retired: true` is required from a candidate when that archive exists;
older candidates that could reactivate Work notifications are rejected. No task,
connection, execution or workspace format changes as part of this retirement.

Remote updates are explicit: new requests need a matching checksum-named endpoint;
preserve old archives/interpreters and original-ID read/cancel routes. Offline,
unprepared or unpinned targets and incompatible live non-Git leases block the
affected transition. Idle PTYs keep their leases. Let original owners finish;
never clear locks, provision silently or delete environments to force an update.
Edit-only target probing does not require an execution-specific user manager.

## Bounded runtime recovery

One selected service parent owns one MCP child; independent execution workers
retain their own sessions and evidence. After readiness, the parent checks MCP
identity and a permitted file every 30 seconds, with one five-second probe slot.
Healthy checks do not write recovery state; a busy slot waits one second.
An unresponsive owned child is stopped only after durable recovery accounting,
using bounded TERM/KILL cleanup, never arbitrary PID signalling.

Startup/readiness failure allows three fast attempts including the first, with
1/2-second delays, then 60-second cooldown with 0.8-1.2 jitter. One episode is
bounded by 15 minutes/15 attempts. Only verified identity/file readiness clears it;
identity, permission and corrupt-history failures block retries. Process/service
restarts preserve the budget in `service/service.json`; reads do not renew it.
After diagnosis, explicitly renew a valid budget through
`openmachine service restart --reset-recovery`. Deleting corrupt history is not
recovery. Explicit stop prevents automatic restart for the current login session.

## Diagnose without replay

| Observation | Required action |
| --- | --- |
| User-service domain unavailable | Use the owning logged-in user; do not elevate or alter session policy. |
| MCP works, readiness file fails | Verify the selected file and required macOS permission manually, then recheck. |
| Stale socket | Managed startup removes only a verified private dead socket under the listener lock; manual serve refuses it. |
| `activation_failed_restored` | Candidate failed, previous instance verified; inspect failure without replay. |
| `activation_pending` / `recovery_required` | Preserve selection, installations and receipts; read status, then explicitly request compatible rollback. |
| Changed launcher or incomplete setup | Preserve unknown ownership; reconcile only verified task-owned artifacts. |
| Corrupt/incompatible retained state | Preserve original evidence; do not reinitialize history or bypass its reader. |
| Runtime ready, ChatGPT unavailable | Diagnose the separate tunnel/client route; local readiness is not client acceptance. |

No continuity is promised through reboot, sleep, logout, interpreter removal,
disk loss or account tampering. A new runtime cannot reconstruct a dead PTY.
Source merge, installed tests, native transitions and actual ChatGPT/Tunnel
acceptance remain separate evidence; use disposable fixtures, not production
workloads, for authorized transition tests.

## Product management

`manage` is the management-contract-1 CLI. Both absolute `--config` and
`--state-dir` are required, before `manage`. It does not discover, adopt or scan
other installations. Commands emit one bounded JSON result on stdout; syntax
errors use argparse's stderr/exit 2. Result envelopes separate
`management_contract`, `capabilities`, `implementation_version`, and
`implementation_state_contract`. Installed status additionally reports actual
`product_version`, `state_contract`, selection, policy and native startup state.
Every result has `observed_at_ms`; a retained result is a timestamped observation,
not a continuing health guarantee. Consumers must validate the contract and JSON,
not infer support from the product version string.

| Command after `manage --contract 1` | Effect |
| --- | --- |
| `status` | Actual native identity and MCP/configuration/file readiness; no repair. |
| `configure --workspace /absolute/project` | Create validated private first-run config, never overwrite. Optional `--enable-commands` and `--enable-workspace-writes` grant the two capabilities independently; both default off. The same inputs are available on `init`. |
| `bind --identity SHA256` | Bind an existing owned managed registration to an exact verified retained payload implementation. Inspect status first. No service start or policy change. |
| `start` | Clear deliberate Stop and request actual readiness, retaining recovery budgets. |
| `stop` | Persist deliberate Stop before unloading the verified job; workers retain their original IDs and sessions. |
| `startup --enabled true` / `false` | Persist runtime login preference and request native enable/disable, without changing stopped intent or terminating a running service. |
| `install --operation-id HEX32 --payload /absolute/payload --sha256 SHA256 --probe-workspace project --probe-path README.md` | Submit one finite offline preparation/registration operation. A clean installation finishes deliberately stopped, with runtime login startup disabled. Use explicit `startup` and `start` afterward. |
| `update --operation-id HEX32 --payload /absolute/payload --sha256 SHA256 [--candidate-config /absolute/candidate.toml]` | Submit the exact trusted payload and configuration for compatibility checks and activation. No download or feed discovery. |
| `operation --operation-id HEX32` | Read the original request, phase, outcome, ownership and actual selection fingerprint. Never retries. |
| `continue --operation-id HEX32` | Explicitly continue only a recorded known-unperformed or deferred step under the original configuration/selection identity. |
| `retire --operation-id HEX32` | Terminally reject only a deferred update proven not to have activated, preserving its request, prepared payload and history so a later update may be admitted. |
| `rollback` | Explicit existing compatible restoration; never reverses user workload effects. |

`retire` is intentionally narrower than cancellation. It succeeds only when the operation is a deferred update, the accepted selection and configuration are unchanged, no activation is pending, the prepared candidate is not selected, and the currently selected service is stably ready or deliberately stopped. The original operation remains format 1 and becomes the existing terminal `rejected` phase with reason `retired_before_activation`; no payload, request or retained installation is deleted. Repeating the same retirement is readback, not another effect.

Normal status distinguishes `not_installed`, `stopped`, `ready`, `unready`,
`recovering`, `blocked`, `activation_pending`, `busy`, `incompatible` and `unknown`.
Sanitized `reason` codes identify unavailable permissions, definitions or state.
A PID alone is not readiness. `startup.enabled` comes from the owning GUI user's
`launchctl print-disabled` override and the verified RunAtLoad definition, separate
from `policy.start_at_login`. A denied or unverified change is not success; read
status to compare requested policy with the effective native registration.
Tunnel and ChatGPT evidence remain `not_probed`.

A bound result's `entry` contains `python`, `path`, `config`, `state` and retained
anchor `identity`. The actual command-line consumer is:

```sh
/retained/python -I /absolute/state/service/manage.py --contract 1 status
/retained/python -I /absolute/state/service/manage.py --contract 1 stop
```

Use the exact returned paths, not these example paths. Core's entry resolves the
selected implementation on each call. It does not pin the companion to an obsolete
venv or a replaceable App. The retained anchor supplies management for a selected
Core without management support; its selected old runtime still owns serving. The entry's configuration,
state and anchor binding cannot be silently repointed.

For first installation, the verified payload's bootstrap Python can run
`-I -B -m openmachine.cli` with the explicit global paths and the commands above.
After successful product install/update, the registration has the bound entry.
For an already prepared management-capable payload, `bind` accepts its returned installation
identity. The product host owns validation of its stable Applications location;
Core registers only retained Python/entry/environment paths, never donor App
paths. First-run configuration is local-only; later grant changes use the existing
explicit configuration workflow and candidate snapshots.

After a lost first-run configuration reply, `configure --verify-existing` with
the original inputs only reads and verifies an exact private owned configuration.
It never creates, overwrites or repairs a file, and rejects changed grants or
profiles. The additive `configure:verify_existing` capability lets Desktop
reconcile this step before submitting its original install operation. Installed
state still rejects first-run configuration; this is not a configuration editor.

### Policy and old-Core limits

Persistent Stop uses service selection format 2 and state contract 14. The policy
is independent of login startup, so relaunch, login, ordinary recovery, update and
restoration cannot clear it. An update or restoration of a deliberately stopped
runtime validates a disposable candidate but never leaves the registered service
running and never clears a recovery episode by skipping readiness. Only explicit
Start clears Stop. Legacy `service stop` retains its unload-only contract;
`service start` is also an explicit Start, while restart does not clear deliberate
Stop. Budget renewal still requires the existing explicit reset command.

The original `launch.py` remains byte-exact. Format-2 native startup uses the bound
management entry; exact prior/generated plist definitions cover interrupted policy
publication without permitting arbitrary changed files. First-upgrade activation
and failed restoration keep the old selection/launcher contracts until commit.
After bootout and exact profile restoration, the remaining pending handoff names
only the restored installation, so its original older supervisor can validate it.
Admission stays closed until readiness; the failed candidate and original
operation remain retained.
For an existing format-1 installation, Start keeps the original launch format;
a login-choice change persists through native registration alone. The first
deliberate Stop adopts format 2 before bootout. This avoids changing launchd's
cached arguments underneath a running service. A stopped-intent login can leave
an idle loaded job; management reports it as stopped, and explicit Start reloads
its verified definition while preserving the login choice.
Policy fingerprints are checked with actual candidate readers. A pre-14 reader
cannot consume format 2, and a candidate that omits the policy proof is rejected
before native stop. There is no permissive old-reader downgrade.

Supported old-Core transition is deliberately limited: the retained manager can
observe and explicitly Start an owned legacy managed Core. Old Core does not gain
`manage`, persistent Stop or runtime-login controls. Those require selecting a management-capable runtime;
legacy `service stop` remains available with its documented semantics. Bind from
the reviewed incoming retained implementation, rather than calling new commands
on an old CLI. During update, the worker verifies/retains the incoming payload,
checks its management contract and identity, then execs that incoming bootstrap
before environment preparation or future-state validation. Unsupported incoming
management contracts require their reviewed implementation, never a forced old
reader. This is not a promise of arbitrary future management compatibility.

### Original maintenance operations

The finite worker runs in its own native session, independent of GUI and managed
MCP process groups. Submission publishes intent before spawn and passes the
existing `service/operation.lock` descriptor to the worker; preparation and
transition do not reacquire it. The lifecycle lock, immutable environments, actual
readers and exact configuration snapshots remain their existing owners.

Records retain exact payload digest/source, original configuration and selection
fingerprints, candidate snapshot identity, bounded phase and outcome. There are at
most 128 retained records, each at most 64 KiB; no cleanup or scheduler is included.
Subprocess replies are bounded to 64 KiB per stream and finite deadlines. Worker
stdio is detached to `/dev/null`, not a GUI-owned pipe or unbounded log.

Phases are `accepted`, `retaining`, `retained`, `preparing`, `prepared`, `activating`,
`deferred`, `completed`, `rejected`, `restored`, or `recovery_required`. Same-ID
submission reads the original even if new arguments differ. Another pending,
deferred or uncertain operation is busy; it is not a queue. Reads report live lock
ownership separately. `completed`, `rejected` and verified `restored` are final;
a lost reply does not change a recorded outcome.

An interrupted `accepted`, `retained`, `prepared` or `deferred` phase is
`continuation_required`; only an explicit `continue` may advance it. Interrupted
retention, preparation or activation remains `recovery_required`. Never replace
its ID or replay the phase, even when current selection happens to match the
requested target. A pending selection remains `activation_pending` in status.
Explicit compatible rollback can reconcile the native selection but does not
fabricate the missing original operation outcome or unblock uncertain history.

Product activation conservatively defers active/unresolved local execution, task,
workspace-write, native-login or busy lifecycle admission. Original
workers, target archives, receipts and local/SSH observation budgets are never
cancelled or reset to pass a gate. A deferred operation does not auto-activate when
work becomes idle. Configuration or policy drift rejects continuation rather than
rebasing the accepted request. Incomplete preparation is retained; no immutable
runtime or original history is replaced to retry it.

Retained unknown GitHub outcomes stay unknown across compatible local activation;
they do not represent a local worker to stop or replay. Historical workspace
operations are nonblocking only when their exact retained task result names a
pre-admission validation failure that cannot have allocated a change receipt.

### Historical execution cleanup and activation

A native command can exit with code zero while its process-session cleanup is
unverified. That code proves the observed command result, not that every child
exited, that the intended business effect succeeded, or that repeating the
command is safe. Some retained receipts predate persisted session and kernel boot
identities. `reconcile_execution` cannot reconstruct those missing identities;
preserve `cleanup_identity_unavailable` and the original unknown outcome.

An original-ID `reconcile_execution` can instead verify native cleanup on the
same boot when the receipt already contains its original session and boot IDs.
If a frozen Task still holds the earlier `unknown` observation, activation can
match that original receipt fingerprint to a valid, receipt-hash-bound native
reconciliation. This is a lifecycle-only allowance: the original Task operation
remains unknown, while the effective execution cleanup is already verified.
No boot boundary, Task rewrite or command replay is needed in this case. A
missing original session or boot ID cannot qualify for reconciliation.

Otherwise, for a known command exit with only native cleanup unresolved, the
incoming manager can admit a compatible update after a verified kernel boot boundary:

- A valid original `cleanup.boot_id` must differ from the current kernel boot ID.
- If that identity was never retained, the first managed activation attempt
  records `outcome.execution_boots` in its existing maintenance operation. Each
  entry binds the original execution ID and complete receipt SHA256 to the boot
  ID observed while the original ownership and resource locks are held. That
  attempt remains `deferred` with reason `activation_deferred`.
- A later explicit continuation must observe a different kernel boot ID and
  the same original receipt bytes. Restarting the service, waiting, or an
  operator's assertion does not satisfy this check. On macOS the existing native
  reader uses `kern.bootsessionuuid`; on Linux it uses the kernel `boot_id`.

The boundary proves only that processes from the observed earlier kernel boot
cannot still be running locally. Execution receipts and their Task observations
remain unknown; no reconciliation, success, Task completion, command replay or
permission to rerun is inferred. Task activation requires the exact same machine,
original execution ID and retained observation digest, without an unresolved
sequenced control request. Missing command results, pending/partial controls,
busy original owners or resources, unreadable records and mismatched evidence
continue to block. Original identity, control, worker, workspace and named-resource
locks remain held through activation and any restoration; shared histories use
the strongest required lock mode. Other compatibility and admission checks still
apply.

Use the bound management entry reported by `manage --contract 1 status`:

1. Read the original maintenance operation and actual selected version. An already
   completed operation needs no continuation, even if old execution results are
   still unknown.
2. Obtain a reviewed release payload containing this behavior. If the deferred
   operation is bound to an older payload, its `continue` still uses that exact
   payload. Use the existing `retire` only when it can verify that the original
   update never activated, then submit a new `update` with the new payload and a
   new operation ID. Never change a retained request or payload binding.
3. Let the incoming manager prepare and attempt activation. An old installed Core
   can enter this path because it hands off to the verified incoming bootstrap
   before preparation and activation. Read the original operation again; retain
   its boot evidence if activation is deferred for missing cleanup identity.
4. Separately authorize and arrange a full machine reboot when required. After
   reboot, read status and the same operation, then explicitly `continue` it.
   Recheck any intervening configuration or policy change instead of rebasing
   the original intent. No automatic reboot or continuation is performed.
5. Verify selected identity and readiness. Keep the unknown execution and Task
   evidence for any separate business investigation. Bound observations survive
   completed, restored and retired maintenance operations and can be reused by
   later updates without another reboot for the same receipt.

If evidence publication or an activation acknowledgement is lost, read the same
operation ID first. The original finite phase and recovery rules still decide
whether continuation is allowed. The existing 128-operation and 64-KiB-per-record
bounds also apply to boot evidence; no history is pruned to make room.

These CLI/reader tests are separate from native GUI startup permission, signed
product installation/update and real ChatGPT-route acceptance. Management adds
no daemon, HTTP service, transport or release feed.
