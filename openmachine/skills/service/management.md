# Service operations

[Domain guide](SKILL.md)

On this page:

- [Advanced wheel-based installation and control](#advanced-wheel-based-installation-and-control)
- [Exact updates and compatible rollback](#exact-updates-and-compatible-rollback)
- [Bounded runtime recovery](#bounded-runtime-recovery)
- [Diagnose without replay](#diagnose-without-replay)
- [Product management](product-management.md)

Use [product management](product-management.md) for management-capable installations.
It owns persistent Stop, login preference and original install/update operations.
The wheel-based commands below remain a compatibility entry point: `service stop`
only unloads the job, whereas `manage stop` preserves Stop until explicit Start.
Neither command cancels admitted user work.

## Advanced wheel-based installation and control

For ordinary consumer installations on supported Apple Silicon macOS, prefer
the official npm package and `openmachine update`. Use the wheel/commit
commands below only for development, offline management, or platforms without
a published npm runtime package.

Use the existing logged-in macOS user, a trusted built wheel and its exact lock.
Choose a stable harmless file in a configured local workspace for readiness.
Do not run manual `serve` on the same state as a managed service.

```sh
.venv/bin/openmachine service install --wheel "$PWD/dist/openmachine-0.2.25-py3-none-any.whl" --requirements "$PWD/requirements.lock" --probe-workspace project --probe-path AGENTS.md
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

The captured installation `PATH`, not shell startup files, supplies Git and
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

For an explicit development commit, no tag or GitHub Release is needed:

```sh
openmachine update --commit FULL_40_CHARACTER_LOWERCASE_SHA
openmachine --version
openmachine service status
```

This online path fetches only that commit from the official Open Machine repository,
verifies the checked-out HEAD, builds an isolated wheel, and uses the same commit's
`requirements.lock` with the existing installer and activation checks. It requires
Git, pip, network access and existing noninteractive Git authentication for a private
repository. Only fetch uses your Git credential configuration; checkout and build
use an isolated environment. It never changes your working checkout. `--commit` and
`--wheel` are mutually exclusive; `--requirements` is only for `--wheel`.
Different commits remain distinct candidates even when their version and wheel
bytes match. This command uses the invoking CLI's existing compatibility checks;
a future state contract still requires the reviewed incoming implementation.

`--version` shows `VERSION (COMMIT)`. Doctor and MCP capabilities keep the existing
`version` field and add `commit` for the running implementation. Service status
reports the selected installation's version and commit. Historical installations
without recorded provenance return `null` (`unknown` in `--version`); no command
infers an installed commit from the caller's Git checkout. Older target commits
may not expose provenance through their own CLI or MCP; this updater's result and
status reader still report the recorded source.

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

For a managed Core update, old **local cleanup-only** uncertainty is not itself
evidence that a controller replacement is unsafe. If the original command leader
has a settled exit result and no pending/unknown terminal control, activation
rechecks and holds the original execution, worker, control, workspace and resource
leases through cutover and any restoration. A matching unknown Task observation
is recognized by its exact original execution receipt digest. The controller can
then update without a machine reboot, even if older receipts lack a native session
identity. No execution or Task result is changed, no command is replayed, and
the process or its descendants are **not** claimed to have exited.
The candidate configuration must retain each affected local machine name as a
local machine so the new controller can still read its original execution and
Task evidence. Removing or changing that binding defers activation before stop.

Active workers, busy or missing original leases, unknown command/control results,
changed/malformed receipts, unbound Task uncertainty, incompatible retained-state
readers, lost permissions and other lifecycle gates still defer safely. The
original cleanup reconciliation tool remains stricter because changing an
execution result requires actual native cleanup proof. The selected Core's
service process may restart; macOS itself need not reboot for this case.

For explicitly changed configuration, leave the active file unchanged
and pass `update --candidate-config /absolute/new.toml`. The owner snapshots exact
bytes and includes them in the prepared installation identity, so the same wheel
or payload can activate a different configuration with a distinct rollback target.
It validates local/target readers and restores actual pre-transition content
on failure, including valid edits since the previous update. An independent racing
edit is preserved with uncertainty, not overwritten. Cooperative locks do not
fence external editors or another runtime/state root.

Protocol or state-contract numbers alone do not prove compatibility. Actual readers must
cover every retained execution, task, workspace format, managed
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
