# Service and runtime management

For ordinary consumer installations on supported Apple Silicon macOS, install
and update through the official npm package and `openmachine update`. The
wheel-based commands below are advanced developer/offline management paths for
platforms without a published npm runtime package; they are not a second
consumer installation method.

Manual `serve` runs Core in your terminal. On macOS, the managed user service
adds verified readiness, retained installations and bounded runtime recovery.
Neither path manages the tunnel, a remote service or the ChatGPT conversation.

## Choose the correct path

For a first experiment, use [manual configuration](../getting-started/configuration.md).
For a managed installation, use the owning logged-in macOS GUI account, a trusted
wheel and its exact dependency lock, plus a stable harmless readiness file in a
configured local workspace. Linux has no managed service implementation here.

Never run manual `serve` alongside a managed service using the same state.
Keep the selected Python and all environments needed by original workers.

## Install and verify a wheel-based service

After building the v0.2.24 wheel, set `CONFIG` and `STATE` to the intended absolute
paths. Ensure workspace `project` exists and contains a readable `README.md`:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" service install \
  --wheel "$PWD/dist/openmachine-0.2.24-py3-none-any.whl" \
  --requirements "$PWD/requirements.lock" \
  --probe-workspace project --probe-path README.md
openmachine --config "$CONFIG" --state-dir "$STATE" service start
openmachine --config "$CONFIG" --state-dir "$STATE" service status
openmachine --config "$CONFIG" --state-dir "$STATE" doctor --service
```

Install registers the service without starting it or granting machine writes.
The readiness check verifies actual version/configuration/process identity and
the permitted file read. It returns a hash, not file contents. Deleting the
readiness file can make a running service unready.

The state-derived LaunchAgent lives under `~/Library/LaunchAgents/`. Retained
versions, selection and snapshots live under `<state-dir>/service/`; the private
service log is separate from command output logs. The service uses its captured
installation PATH, not your interactive shell startup files.

## Stop, startup preference and recovery

| Action | Meaning |
| --- | --- |
| `service stop` | Unload the current job; legacy compatibility behavior can load it again at a later GUI login. |
| `manage stop` | Persist deliberate Stop and unload the verified job. |
| `manage start` | Explicitly clear deliberate Stop and request readiness. |
| `manage startup --enabled true` or `false` | Set runtime login preference independently of deliberate Stop. |
| `service restart --reset-recovery` | Explicitly renew an eligible exhausted readiness-recovery budget after diagnosis. |

Management-contract-1 commands require both absolute global paths:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" manage --contract 1 status
openmachine --config "$CONFIG" --state-dir "$STATE" manage --contract 1 stop
```

Check actual native status after a change. A recorded preference is not proof the
OS accepted it. Neither kind of Stop cancels admitted user commands. Recovery is
bounded, preserves its budget and can remain blocked; do not delete service state
or signal arbitrary PIDs to obtain readiness.

## Updates and offline payloads

[Updates](../getting-started/updates.md) covers exact wheel/commit updates and
compatible rollback. `--candidate-config` stages a deliberate config change
through the same compatibility/activation checks instead of overwriting active
configuration blindly. A rejected or deferred candidate is not active.

Core also supplies an advanced offline payload builder and `prepare-runtime`.
A payload binds a selected Core wheel, dependencies, relocatable Python, Git and
file hashes. The trusted manifest digest must come from a verified producer;
a checksum is not a signature, and preparation is not public distribution.

`manage install/update` accepts a trusted payload with one saved operation ID.
Read it with `manage operation --operation-id ...` after uncertainty. Explicit
`continue` advances only eligible known-unperformed/deferred work; `retire` is
limited to a deferred update proven not to have activated. Preparation alone
does not start or select a service. There is no automatic release-feed updater.

## Success and limitations

Require actual `ready` identity/file evidence (or deliberate `stopped` when that
was requested), not a PID or preparation success. `activation_pending`,
`recovery_required`, `incompatible` and unknown states need diagnosis. A compatible
software rollback does not undo workload effects or erase uncertain history.

Use the [service guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/service/management.md)
and [offline payload guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/service/runtime-payload.md)
for exact lifecycle and producer commands. They do not establish native startup
permission, signed installer distribution or actual ChatGPT-route acceptance.
