# Prepared SSH targets

[Domain guide](SKILL.md)

On this page:

- [SSH](#ssh)

## SSH

### SSH machine preparation contract

Management advertises `ssh-setup` for a retained machine preparation operation. Use
`manage ssh-setup --action ACTION --operation-id ORIGINAL_ID` with one bounded
JSON object on stdin. The ID is 32 lowercase hex characters. Actions are
`inspect`, `trust`, `prepare`, `status`, `save`, `continue` and `cancel`.
The owning management entry remains bound to its exact configuration and state.

`inspect` accepts a unique `name` and either an existing `alias`, or a single
`host`, `user`, optional `port` (22) and optional native `identity_file` reference.
It retains a plan before network discovery, resolves an alias without rewriting
SSH configuration, and checks the supported prerequisites. It never installs a
worker or enables permissions. Direct connections compare discovered public keys
with existing SSH trust. Changed keys block; an unknown key requires a separate
`trust` request containing the reviewed `fingerprint` and `confirmed: true`.
Trust belongs to this connection, not to the operator's global known_hosts file.
Special trust markers and unknown keys on complex aliases require the operator's
existing SSH trust workflow. Private key bytes and passphrases are never inputs.

`prepare` requires the plan's `sha256` and `confirmed: true`. It copies only the
current distribution's immutable worker to the displayed checksum-named path
under the remote account's canonical home. A matching worker there is reused.
Existing archives, system packages, SSH configuration and lingering remain
unchanged. The native preparation has no command or file-write grant. Following
an interrupted reply, use `status` with the same ID: it verifies the exact host,
account, archive and worker response without copying again. Unverified preparation
cannot be cancelled or replaced by another automatic attempt.

`save` with `confirmed: true` registers the verified machine with both grants
disabled and no workspace. It admits the existing maintenance update under its
original identity and configuration preconditions; `continue` advances only that
deferred update. A completed setup cannot be cancelled or undone by changing its
recorded status. Historical `project_ready` records retain their original atomic
save and recovery behavior, but new requests cannot create that phase.

Workspace mappings and machine-wide `execution_enabled` /
`workspace_write_enabled` grants are independent configuration changes. Use
the existing confirmed candidate-configuration `manage update` contract with
original operation identity, matching configuration digest, validation and rollback.
Clients choose how to present inputs and confirmations; Core returns phases and
reason codes, not screens or prompts. Machine registration grants neither command,
workspace-write, account nor AI-client authority.

Direct routes use the typed `ssh_connection` configuration instead of `ssh_alias`.
The reviewed host key and native key-file reference remain local; public
capabilities omit that object. Every exchange retains strict host verification,
batch authentication, disabled agent forwarding and bounded transport. State
contract 22 verifies retained setup records before an update or rollback.

### Prepared targets

The supported target baseline is Linux, Python 3.12+ as `python3`, Git and an
available systemd 255+ user manager. The controller uses existing system OpenSSH,
authentication and verified host keys, with no password prompt or agent forwarding.
No privilege, host-key, account-session, route or daemon changes are implicit.

```sh
openmachine worker-bundle --directory ./workers
```

Explicitly copy the returned checksum-named `.pyz` to an operator-selected versioned
target path and verify its SHA256. The curated archive contains only target-capable
standard-library code. Never overwrite an archive used by original work.
Configuration begins read-only; replace the synthetic checksum and actual paths:

```toml
[machines.build]
transport = "ssh"
ssh_alias = "my-build-host"
ssh_worker = "/home/alex/.local/share/openmachine/workers/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pyz"
execution_enabled = false
workspace_write_enabled = false

[workspaces.remote-project]
machine = "build"
root = "/home/alex/project"
```

`doctor --machine build` probes Python, manager, directories and hashed host/account/
canonical-home identity. Initial `identity_verified=false` is expected; verify the
intended target before setting `ssh_identity` and enabling the intended grant.
Restart after configuration changes. Default doctor never contacts hosts or resolves
their paths on the controller. Unavailable identity is not local fallback.

Use the same tools with the configured machine. Arguments travel as bounded JSON
on SSH stdin, not shell interpolation. Hashes, Git checks and file publication run
on the target. Every alias to the same target account shares canonical home/state,
worktree locks and original IDs. Checkpoints stay on the controller; new task-linked
starts validate them there, while original-ID recovery does not require that
checkpoint to survive. No worker upload/install is hidden inside reads or probes.

Workers retain their archive in transient user scopes. SSH disconnect/controller
restart does not own their lifetime. Cancellation and completion clean the owned
scope, including child process groups. `finishing` persists until cleanup is
observed; unavailable scope/manager evidence is unknown. Reads do not stop units;
repeated authorized cancellation may request pending scope cleanup. Scope lifetime
is command timeout plus effective grace and 30 seconds; systemd stop budget is
grace plus seven seconds. Transport uses one attempt, ten-second connection and
30-second exchange budgets, not hard real-time guarantees under kernel/IO stalls.

The user manager must outlive the intended command (existing login or established
lingering policy). A probe during login does not prove last-login survival.
OM never enables lingering or reconstructs work after manager teardown, reboot,
disk loss, account tampering or deliberate cgroup escape. Preserve original target
state, archives and interpreters. Scope observations enrich returned copies, not
old worker receipts. New calls use matching protocol-7 endpoints; updates retain
original compatible read/cancel routes rather than silently provisioning them.
