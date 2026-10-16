# Remote Linux machines over SSH

OpenMachine uses your existing authenticated OpenSSH connection and a prepared
worker on a Linux account. The same workspace and execution tools then operate
on that target, with results retained on the target rather than guessed locally.

## 1. Prepare the account and connection

The target needs Linux, **Python 3.12+ as `python3`**, Git and an available
**systemd 255+ user manager** for command execution. Its account must be allowed
to read the project and, when enabled, write files or run commands there.

Create or choose a normal SSH alias such as `my-build-host`. Authentication must
work noninteractively with verified host keys. Have the operator verify a new
host fingerprint through a trusted channel; never disable host-key checking.
With that existing setup, these are diagnostic commands, not provisioning:

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes my-build-host \
  'python3 --version; git --version; systemctl --version; systemctl --user show-environment >/dev/null'
```

Check each displayed version and the user-manager result. This combined shell
command's final exit alone does not validate all prerequisites. A user manager
available during login does not prove it will survive the final logout. Arrange
an appropriate existing session/lingering policy with the administrator; Core
does not enable lingering, install systemd, change accounts or use sudo.

## 2. Build and explicitly transfer the worker

On the controller, using the selected Core version:

```sh
openmachine worker-bundle --directory ./workers
```

Retain the returned archive path and SHA256. The filename is the full SHA256
followed by `.pyz`. Select a new versioned target directory, then transfer the
archive using your normal approved file-transfer procedure. For example, after
setting `WORKER` to the **actual returned local archive path**:

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes my-build-host \
  'mkdir -p /home/alex/.local/share/openmachine/workers'
scp -o BatchMode=yes -o StrictHostKeyChecking=yes "$WORKER" \
  my-build-host:/home/alex/.local/share/openmachine/workers/
```

`alex` and the paths are examples; replace them with the intended account's
verified paths. Do not overwrite an archive used by original work. On the target,
run `sha256sum` on the actual copied archive and compare it with the controller's
returned digest. A successful copy alone is not integrity verification.

## 3. Add a read-only machine and workspace

Add this fragment to the selected controller configuration, replacing the alias,
workspace root and **all 64 synthetic `a` characters** with the real checksum:

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

The project must already exist. Run the explicit probe with your selected
absolute `CONFIG` and `STATE` paths:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" doctor --machine build
```

The first probe can return `identity_verified=false` and a nonzero exit because
the identity is not yet pinned. Review the observed target/account/canonical-home
identity and workspace availability. Verify the intended host and account before
setting `ssh_identity` to the **returned identity hash**, not the worker checksum.
An unavailable or wrong target is a blocker, not a reason to invent a hash.

## 4. Pin identity and grant only the intended operations

Add the verified 64-hex `ssh_identity` under `[machines.build]`. Keep grants false
for read-only use. Enable `execution_enabled` for authorized commands and
`workspace_write_enabled` for typed edits independently. Re-run the probe and
require verified identity plus available workspaces.

Apply the changed configuration through the manual restart or the existing
managed candidate-configuration workflow. Core rejects enabled SSH mutations
without a pinned identity. Probing never uploads, installs or enables a worker.

## 5. Read, execute and verify on the target

Start with a read-only connected prompt:

```text
Use OpenMachine on machine build, workspace remote-project. Read the relevant
Skills and README.md, and return its actual text and SHA256. Report target
identity and any unsupported operation. Do not run commands or modify files.
```

After deliberate command authorization:

```text
On that same verified remote workspace, run only python3 -c
'print("remote-ready")' with a 30-second timeout. Save the execution ID before
starting. Read that original remote execution and report the marker, status,
exit code and systemd-scope cleanup. Do not rerun after a transport failure.
```

Pass machine `build` to subsequent original result reads. A local controller
result is not remote acceptance. Arguments travel as bounded JSON on SSH stdin;
ordinary tool calls do not interpolate them into a remote shell.

## Recovery and common errors

| Observation | Next step |
| --- | --- |
| SSH authentication or host-key failure | Repair the operator's existing OpenSSH setup; no password prompts or agent forwarding are supplied by Core. |
| Worker missing or checksum mismatch | Verify the intended versioned archive and explicit copy; do not overwrite a live archive. |
| Identity mismatch | Verify the target independently; do not automatically accept a changed host/account. |
| User manager unavailable | Ask the administrator to establish the required session policy; do not silently enable lingering. |
| Workspace unavailable | Verify the path on the target, not on the controller. |
| Disconnect or lost start reply | Read the original remote execution ID; unavailable transport does not mean non-admission. |
| Old worker reports unsupported | Plan an explicit compatible worker update and retain old result/cancel routes. |

The user scope owns admitted remote command lifetime, not the SSH connection.
Controller restart or disconnect need not stop that command, but reboot, manager
teardown or lost target state cannot be repaired by reconstructing the work.
Task checkpoints remain on the controller; execution/file receipts remain with
their original target owner. Preserve both, including old archives/interpreters.

The [SSH operating guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/service/ssh.md)
owns protocol, time-budget and retained-worker compatibility details.
