# Configuration reference

Core uses one explicitly selected TOML configuration and a private state directory.
The defaults are `~/.config/openmachine/config.toml` and
`~/.local/state/openmachine/`. To select another installation, place both global
options before the subcommand:

```sh
openmachine --config /absolute/config.toml --state-dir /absolute/state doctor
```

Do not copy an existing operator's full configuration into an Issue. The
[example configuration](https://github.com/OpenMachineLabs/openmachine/blob/main/examples/config.toml)
contains placeholders, not credentials or a ready-to-use machine inventory.

## Minimal read-only configuration

Replace the root with an existing absolute directory:

```toml
schema_version = 1

[machines.local]
transport = "local"
execution_enabled = false
workspace_write_enabled = false

[workspaces.project]
machine = "local"
root = "/absolute/path/to/project"
```

`init --workspace` creates this local profile without overwriting a file. Machine
and workspace names are stable selectors used by tools and retained tasks; changing
a name is not a migration of historical references. Unknown configuration fields
are rejected. Use actual absolute paths rather than assuming shell expansion
inside TOML.

## Machine grants

| Setting | Type and default | Meaning |
| --- | --- | --- |
| `transport` | `"local"` or `"ssh"` | Which native target owns the operation. |
| `execution_enabled` | Boolean, `false` | Permit native commands after the remaining authorization checks. |
| `workspace_write_enabled` | Optional Boolean | Typed file-write permission; when omitted it inherits the legacy execution grant. |
| `ssh_alias` | SSH machines only | Existing noninteractive OpenSSH alias. |
| `ssh_worker` | SSH machines only | Absolute path to the checksum-named worker archive. |
| `ssh_identity` | SSH machines only | Verified target/account identity hash; required before enabling mutations. |

Set both grant booleans explicitly in new configurations. The following combinations
are intentional; none overrides native permissions, client approval or a task contract:

| Execution | Typed writes | Result |
| --- | --- | --- |
| `false` | `false` | Read-only typed workspace tools. |
| `false` | `true` | Typed file edits without native command execution. |
| `true` | `false` | Native commands allowed, typed writes disabled. Commands can still write using native account rights. |
| `true` | `true` | Both capabilities enabled under their respective checks. |

An execution grant is not an OS sandbox. See [Security](security.md).

## Workspaces and SSH machines

Each `[workspaces.NAME]` binds `machine` to one configured machine and `root` to
an existing absolute directory on that machine. Typed paths are relative to the
selected workspace. Registered Git worktrees can be selected explicitly; creating
one does not change the configured default root or grant ownership of other trees.

[Remote SSH](../features/ssh.md) covers worker preparation, first read-only probe,
identity verification, independent grants and remote result retrieval. Do not set
an archive checksum as the target identity. Local machines reject SSH-only fields.

Management-capable macOS installations also provide `manage machines`,
`machine-probe`, `machine-test`, `machine-add` and `machine-remove`. Writes use an
original operation ID and the observed configuration SHA256, rather than an
unconditional overwrite. Their CLI arguments are in [CLI reference](../reference/cli.md).
They do not provision an SSH worker or authorize deletion of remote files.

## GitHub and Skills

`[github]` supports `account`, `read_repositories`, `write_repositories`,
`read_owners`, `write_owners` and `attribution`. Exact repository scopes are the
narrowest option; write scope includes read access. An empty scope grants none.
Legacy `repositories` cannot be mixed with explicit read/write scope fields.
The selected native account and GitHub App resource permissions must also match.
See [GitHub](../features/github.md) for connection and verification.

`[skills].roots` lists up to 16 explicitly reviewed absolute directories.
It adds discoverable instructions, not permissions or executable plugins. See
[Skills](../features/skills.md).

## Apply changes safely

For a disposable manual server, stop that server, edit its configuration, run
`doctor` against the exact paths and restart it. A valid file is not a successful
remote connection; perform the relevant explicit probe afterward.

For a managed installation, prepare a separate candidate configuration, preserving
unrelated settings, and use the compatible update workflow's `--candidate-config`.
When `manage` advertises `update:configuration_consent`, pass the confirmed active
file digest as `--config-sha256` and the reviewed candidate file digest as
`--candidate-sha256`, together. Core checks both before admission and verifies the
retained candidate. When `update:selection_consent` is advertised, also
confirm the installation from `manage status` and submit its `selection_sha256`
with `--expected-selection-sha256`. Core rejects changed selection before
admission, even if both files match. Read the original `--operation-id` after a
lost reply. With `update:workspace_reference_guard`, safe unreferenced local
workspace changes use the same candidate update; protected Task, Change and
registered Git worktree routes are retained or refused, never rewritten.
These guards do not change Connector sharing or fence uncooperative editors.
Preserve the active profile, original tasks and retained installations. Current
work, incompatible readers or a changed target can defer activation; never delete state to force it through.

The schema is defined by the selected version's
[configuration implementation](https://github.com/OpenMachineLabs/openmachine/blob/v0.2.22/openmachine/config.py).
Do not add fields from an unreleased checkout to an older runtime.
