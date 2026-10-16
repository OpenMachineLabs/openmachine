# Update OpenMachine

An update has three separate identities: the source commit, the installed
package and the running service. A fourth is the client's approved tool catalog.
Check each one; a merge to `main` does not update any of the others.

## npm installer on Apple Silicon

`npm install -g openmachine@latest` installs or updates the npm launcher, **not** an
existing managed Core runtime. `openmachine --version` reports the launcher
version; `openmachine core --version` reports the bundled Core CLI version, which
may differ from the running service. Check `openmachine status` and use the
explicit managed update process below when upgrading an existing Core installation.

## Choose the version and preserve original work

Read the [release notes](https://github.com/OpenMachineLabs/openmachine/releases)
and select an exact release tag or explicitly reviewed development commit.
Record current version/commit and existing task/execution IDs. Keep the current
configuration, state, old worker archives and interpreters. Do not clear history
or locks to force an upgrade or rollback.

## Manual source installation

Build the selected release in a **separate directory and virtual environment**
using [Installation](installation.md) and that release's own lock. Validate its
Python/SQLite requirements first. Do not overwrite a running worker's environment.

Quiesce the old server deliberately, start the new compatible server with the
intended existing config/state and re-read the original records before new work.
A manually managed installation does not gain the managed service's activation
or restoration guarantees. Use a disposable state first to test an unfamiliar
candidate; it is not a migration of production history.

## Existing managed macOS service

Build the new trusted wheel and matching lock first. With `CONFIG` and `STATE`
set to the existing absolute paths, and artifact paths replaced:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" update \
  --wheel /absolute/new/openmachine-VERSION-py3-none-any.whl \
  --requirements /absolute/new/requirements.lock
openmachine --config "$CONFIG" --state-dir "$STATE" service status
openmachine --config "$CONFIG" --state-dir "$STATE" doctor --service
```

`VERSION` is a placeholder, not a published artifact name. An explicitly reviewed
development commit is another supported input:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" update \
  --commit FULL_40_CHARACTER_LOWERCASE_SHA
```

Use a real full commit SHA. This path needs Git, network access and any required
noninteractive repository authentication; it uses that commit's lock and does
not change your working checkout. It is not automatic tracking of `main`.

When a candidate advances the state contract, use the reviewed incoming
implementation's CLI as required by the
[service operating guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/service/management.md).
An old CLI cannot validate arbitrary future state. Managed offline installations
use original `manage install/update` operation IDs; see [Service](../features/service.md).

## Verify, restore or diagnose

After verified activation, check actual service version/commit, original results,
the refreshed client catalog and a harmless real tool call. A deliberate managed
Stop remains stopped. Deferred or recovery-required updates are not successful
activations and do not silently retry when the machine becomes idle.

Use explicit compatible `rollback` only after diagnosis. It restores a retained
software installation, not files or other user-work effects. Never delete an
incompatible record to make rollback pass. See
[Troubleshooting](../troubleshooting.md) and the [verification checklist](verification.md).
