# Service operations

[Overview](../README.md) | [Operations](operations.md) | [Tasks](tasks.md)

## Install and control

Use the existing logged-in macOS user, a trusted built wheel and its exact lock.
Choose a stable harmless file in a configured local workspace for readiness.
Do not run manual `serve` on the same state as a managed service.

```sh
.venv/bin/openmachine service install --wheel "$PWD/dist/openmachine-0.2.5-py3-none-any.whl" --requirements "$PWD/requirements.lock" --probe-workspace project --probe-path AGENTS.md
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

The captured installation `PATH`, not shell startup files, supplies Git/gh/SSH.
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

An identical update is a no-op, not a restart. Update/rollback activate the selected
service even when it was stopped. Brief MCP disconnection is possible; read the
same task/execution/control identities afterward, never repeat input or work merely
because a response disappeared. Original workers retain their own interpreter,
session, receipts and immutable files; software rollback does not undo effects.

For explicitly changed configuration or SSH routes, leave the active file unchanged
and pass `update --candidate-config /absolute/new.toml`. The owner snapshots exact
bytes, validates local/target readers, and restores actual pre-transition content
on failure, including valid edits since the previous update. An independent racing
edit is preserved with uncertainty, not overwritten. Cooperative locks do not
fence external editors or another runtime/state root.

Protocol 1/state contract 9 are not proof of compatibility. Actual readers must
cover every retained execution, task, workspace format, SSH budget, managed
recovery and Events record, even terminal-only history or disabled integrations.
An old reader that ignores a directory cannot pass. Formats 1/2 and large retained
workspace ranges may block downgrade; `0.1.0a6` and earlier are not managed rollback
targets. No history is rewritten or deleted to permit a transition.

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
