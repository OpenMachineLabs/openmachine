# Prepared SSH targets

[Domain guide](SKILL.md)

On this page:

- [SSH](#ssh)

## SSH

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
