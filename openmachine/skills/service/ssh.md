# Prepared SSH targets

[Domain guide](SKILL.md)

On this page:

- [SSH](#ssh)
- [Connect a prepared server from Desktop](#connect-a-prepared-server-from-desktop)

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

## Connect a prepared server from Desktop

Desktop connects to an existing Linux server through system SSH. Ask its operator
for an SSH alias and the absolute path of its compatible OM worker. Desktop does
not install software, change networking, manage credentials or accept host keys.

Before connecting, the operator must have prepared:

- A working SSH alias on this Mac, non-interactive authentication and a verified
  host key. The existing LAN or VPN route must already work.
- Linux with Python 3.12+ available as `python3`.
- The worker built from the attached Core's version with `openmachine
  worker-bundle --directory ./workers`, explicitly copied and SHA-256 verified
  by the operator. Its full path ends in the returned 64-character digest and
  `.pyz`. Preserve every archive used by original work.

See [SSH preparation](#ssh) for operator preparation and execution
prerequisites. Monitoring alone needs no workspace, command/file-write grant,
ChatGPT login or running local Runtime. It does not require a systemd user manager
unless execution is enabled on that existing target.

In Desktop, open **Machines**, then **Add machine**. Enter a unique machine name,
the existing SSH alias and the prepared worker path. **Test connection** checks
that exact candidate without saving it. Verify the reported identity with the
operator, select the confirmation checkbox, then **Save connection**. New
connections have both command execution and workspace writes disabled.

Core owns the saved configuration and retains its original maintenance operation.
A successful test is not a completed save. If saving is deferred or its reply is
lost, read the original connection status; explicitly continue only when offered.
Other configured machines, grants, workspaces, Skills and deliberate Stop remain
preserved. Existing targets appear automatically after attachment or reopening.

**Connected** means a recent SSH/worker exchange verified the expected version,
archive and pinned identity. It does not mean a remote daemon or task is ready.
Select a machine for its last check, last successful check, available OS and
architecture, and any setup error. **Refresh** respects the same bounded cadence
as automatic checks. Authentication, host trust, identity and worker failures
require operator attention; an unreachable connection does not prove power-off.

**Remove connection** affects only the saved connection. Core refuses removal
while configured workspaces, retained task/recovery references or remote receipts
still require it, or their absence cannot be verified. It never deletes remote
files or work. This Mac cannot be removed.

Legacy Markdown checkpoints remain readable controller notes and do not alone
keep a connection in use. Core still verifies their integrity and protects actual
work through task/operation references and original remote receipts, including
completed work.
