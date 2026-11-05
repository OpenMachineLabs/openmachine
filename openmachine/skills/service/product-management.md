# Managed product lifecycle

[Domain guide](SKILL.md) | [Wheel-based lifecycle and recovery](management.md)

## Product management

`manage` is the management-contract-1 CLI. Both absolute `--config` and
`--state-dir` are required, before `manage`. It does not discover, adopt or scan
other installations. Commands emit one bounded JSON result on stdout; syntax
errors use argparse's stderr/exit 2. Result envelopes separate
`management_contract`, `capabilities`, `implementation_version`, `implementation_commit`,
and `implementation_state_contract`. Installed status additionally reports actual
`product_version`, `product_commit`, `state_contract`, selection, policy and native startup state.
Every result has `observed_at_ms`; a retained result is a timestamped observation,
not a continuing health guarantee. Consumers must validate the contract and JSON,
not infer support from the product version string.

| Command after `manage --contract 1` | Effect |
| --- | --- |
| `status` | Actual native identity and MCP/configuration/file readiness; no repair. |
| `configure --workspace /absolute/project` | Create validated private first-run config, never overwrite. Optional `--local-access` (`read_only` or `full_access`) selects native access, defaulting to `read_only`. Independent `--enable-commands` and `--enable-workspace-writes` flags both default off. The same inputs are available on `init`. |
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
Tunnel and client evidence remain `not_probed` in `tunnel` and `client`. The
legacy `chatgpt` field mirrors `client` for released Desktop builds; new readers
use `client`.

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

When `capabilities` includes `configure:local_access`, a management caller may
explicitly select the native mode. Full read/write and execution capability needs
all three options on `configure` (or `init`):

```text
--local-access full_access --enable-commands --enable-workspace-writes
```

Neither boolean promotes Read Only to Full, and Full alone enables neither
boolean. A caller requiring this input must check the additive capability, not
infer support from `management_contract=1` or a version string. This configures
native OS-account capability; it does not authorize another transport's remote
requests or enlarge OS privileges. No existing installation is migrated.

After a lost first-run configuration reply, `configure --verify-existing` with
the original inputs only reads and verifies an exact private owned configuration.
It never creates, overwrites or repairs a file, and rejects a changed native
access mode, command/write flags or workspace profile. The additive
`configure:verify_existing` capability allows an
authorized management caller to verify this step before continuing its original
install operation. Installed state still rejects first-run configuration; this
is not a configuration editor.

### Confirmed configuration changes

When `capabilities` includes `update:configuration_consent`, `manage update`
accepts paired `--config-sha256` and `--candidate-sha256`. Bind the exact active
configuration shown during confirmation and the exact reviewed candidate, not
freshly substituted digests at dispatch. `manage machines` provides the active
file digest; hash the candidate file bytes separately. Without a candidate path,
both refer to the active file. These raw-file hashes are distinct from MCP's
normalized `configuration_sha256`.

The owner checks the active digest under maintenance admission and retains only
the confirmed candidate bytes before publishing the original operation. Changed
inputs return `configuration_changed` or `candidate_configuration_changed`;
invalid digests return `invalid_configuration_consent`. When
`update:selection_consent` is advertised, `manage status` supplies the
`selection_sha256` of the selected Core installation and stable policy. Submit
that confirmed value as `--expected-selection-sha256` together with both file
digests. A changed selection returns `selection_changed` without admitting
another update; a missing or malformed guard returns `invalid_selection_consent`.
A verified rejection before a record exists reports `admitted: false`; missing
or uncertain replies still require original-ID readback. Existing records are read before new inputs
are considered, never resubmitted. Activation reuses the original selection and
configuration guards, preserving deliberate Stop and retained results.

Omitting both flags retains the earlier update contract; a consent-dependent
client must not silently fall back when this capability is absent. Preserve all
unrelated settings in the candidate. With `update:workspace_reference_guard`,
rename, root replacement and removal of an existing unreferenced local workspace
use the same candidate update. The owner checks retained Task target names,
original workspace Change read routes and registered Git worktrees before
selection changes. Protected or unprovable changes refuse safely; the service
probe workspace remains pinned. No project files or historical receipts are
rewritten, remote workspace rebinding is not supported, and Connector sharing
requires a separate confirmation. This is not an OS sandbox.

### Policy and old-Core limits

Persistent Stop uses service selection format 2 and state contract 14. The policy
is independent of login startup, so relaunch, login, ordinary recovery, update and
restoration cannot clear it. An update or restoration of a deliberately stopped
runtime validates a disposable candidate but never leaves the registered service
running and never clears a recovery episode by skipping readiness. Only explicit
Start clears Stop. Legacy `service stop` retains its unload-only contract;
`service start` is also an explicit Start, while restart does not clear deliberate
Stop. Budget renewal still requires the existing explicit reset command.

Capability `resume` restores only an existing format-2 `desired=running` policy.
It shares Start's identity, readiness and recovery checks and reuses a live job,
but never changes Stop, login preference, configuration or recovery budget.
Stopped and legacy installations are only observed. Controllers may request it
once on launch after reading the advertised capability; a lost response requires
status readback, not another Start or resume attempt.

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

Product activation conservatively defers active/unresolved execution, task,
workspace-write, native-login or busy lifecycle admission. Original
workers, receipts and native observation budgets are never
cancelled or reset to pass a gate. A deferred operation does not auto-activate when
work becomes idle. Configuration or policy drift rejects continuation rather than
rebasing the accepted request. Incomplete preparation is retained; no immutable
runtime or original history is replaced to retry it.

Retained unknown GitHub outcomes stay unknown across compatible local activation;
they do not represent a local worker to stop or replay. Historical workspace
operations are nonblocking only when their exact retained task result names a
pre-admission validation failure that cannot have allocated a change receipt.

These CLI/reader tests are separate from native GUI startup permission, signed
product installation/update and real client-route acceptance. Management adds
no daemon, HTTP service, transport or release feed.

## Ubuntu native runtime

Ubuntu 24.04 x86_64 supports management contract 1 for a nonroot local user with
an available systemd user manager/bus. Other Linux distributions, releases and
architectures are rejected. The same `manage configure/install/start/stop/resume`,
`startup`, original-operation readback and update contracts apply. Use the existing
`service restart` command; a deliberately stopped runtime remains stopped.

The owned unit lives in `~/.config/systemd/user/` and is bound to the private
state path. Core verifies its bytes, path ownership, loaded definition, overrides
and actual supervisor identity before control. A missing manager, a changed unit,
an unknown listener and a failed exit are distinct failures; Core never adopts an
unrelated job. The existing private MCP/file readiness and bounded recovery owner
remain authoritative. `KillMode=process` preserves native Execution workers;
the supervisor terminates its own MCP child, and Linux parent-death signaling
terminates that child after abnormal supervisor loss. Original operations are
read back, never replayed by login or recovery.

`manage startup --enabled true` enables the user unit. It does not enable linger.
Status separately reports the observed `linger`, `logout_continuity` and
`boot_start`; unavailable loginctl evidence remains unknown. Without linger,
logout/boot continuity is unsupported. Host logout policy can terminate user
processes, including workers. Core does not change that policy or grant privileges.
Persistent Stop is committed before native effects and survives login, resume,
restart and updates.

First configuration defaults to Read Only. `manage configure --local-access
full_access` (also available on `init`) requires an explicit choice; command and
workspace-write grants remain independent. `--verify-existing` accepts only the
exact requested permissions and workspace, without overwriting existing settings.
