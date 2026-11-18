# Device identity

Device identity is a headless Python component (`device/`, package
`openmachine_device`), shipped in the bundle with `om`: inside the Mac app, and
in the Linux release. It owns this computer's sign-in, device key and its own
bundle. It has no service and no long-running process: the app on a Mac, or
`om` on Linux, runs it for each action. It does not own Core, GitHub
credentials or any UI. Callers reach it only through the bundle's `entry.py`.

Its state is in `~/.local/state/openmachine-connector`, owner-only. Unexpected
owners, symbolic links, unsafe modes and changed bundles are refused, not
repaired. `config.json` is `{version, server, enabled, device, devices,
revoked, remote_access}`; readers ignore other keys. `om` is online only while
`enabled` is true and, when an intent file is bound, that intent is on. Every
stop writes `enabled` false.

## Sign-in and device keys

This computer keeps one device per account it signed in with. Each device key
is a file named by its fingerprint, `key-<SHA-256 hex>`, written once; the
private key never leaves the computer. `devices` holds the stored devices, each
`{device_id, grant_id, principal_id, key_fingerprint, hardware_id}`, bound to
its key and to this computer's hardware ID, which is never sent:
`IOPlatformUUID` on macOS; on Linux the state file `hardware-id` if it exists,
else `/etc/machine-id`. A sign-in creates `hardware-id`, once, only where
`/etc/machine-id` is unreadable or malformed, and `om` reads it by the same
rule. A state directory copied with its `hardware-id` is therefore the same
device on another computer: that file defeats the check below. `device` is the
one in use. If a stored device names another hardware ID or its key is missing,
the next `companion` or `remove` command forgets every stored device and
revokes nothing; `companion connection`, and a `login` that has just created
`hardware-id`, fail with `hardware_changed` instead. An unreadable hardware ID
is an error, never a mismatch: it refuses sign-in, going online and revocation.

Each sign-in creates a candidate key and a signed session request naming it,
the stored keys and the platform, returns the browser link for the caller to
open and verifies the approved account, key and device receipt. `login` takes
an optional `key` from the Console, sent only with the request that creates the
session; it is in no operation record, retry comparison, state file or error. A
request whose key Cloud did not approve had no effect: the operation is refused
with Cloud's code, and the session file and candidate key are deleted. Sign-in
uses the device in use or switches to another, stored or new, crash-safely and
without Cloud; nothing is revoked, and stored devices Cloud reports in
`stale_keys` are forgotten. A lost reply is recovered by reading the original
session. A sign-in sets `enabled` only when the configuration is unchanged
since it began and the intent is on.

Device requests sign `om-device-v1` under one-use challenges from Cloud: the
exact Cloud origin, HTTP method, path, challenge ID, challenge and SHA-256 of
the raw body. Sign-in session requests sign `om-device-v2`, which adds the
request time, under one challenge this computer makes for that sign-in and
uses for each of its requests.
[`test/vectors/proof.json`](../test/vectors/proof.json) is the contract both
sides test against.

## Remote access and connection

`remote-access` binds the path of the connection intent, `remote-access.json`,
and sets `enabled` from it: false for Off; true for On only with a registered,
unrevoked device and an active sign-in. The intent is written by the app on a
Mac, or by `om up` and `om down` on Linux; device identity never writes it.
`sign-out` sets `enabled` false before it signs out with Cloud, so the machine
stays offline even if Cloud cannot be reached; the operation is completed by
reading it back.

`connection()` is `{protocol, capabilities, binding, server, key_fingerprint,
observed_at_ms, paired, remote_access}`. `binding` digests `server` and the ID
and key fingerprint of the device in use. `remote_access.state` is the first
that applies: `error` (unreadable intent), `off`, `starting` with
`connector_identity_changing` (a switch is pending), `pairing_required` (no
device, no active sign-in, or revoked with `device_not_authorized`),
`starting` (`enabled` not set), `ready`. `connector_stopped` means `enabled`
is not true; `admission_blocked` means the state is not `ready`. An unbound
installation reports `managed` false.

`companion` actions are `snapshot`, `connection`, `operation`, `login`,
`cancel-login`, `sign-out`, `policy`, `remote-access`, `unpair`,
`continue-revocation` and `move`. `snapshot` is `{account, device, errors}`
plus `connection()`. Each effect records its operation ID before it starts; a
lost result is read by that ID, never replayed under a new one. `policy` sets
this device's access level and keeps no record: after a lost reply, the caller
reads `snapshot` and compares `device.policy`.

## Removal

`unpair` revokes the device in use. `remove` sets `enabled` false, ends a
sign-in in flight, revokes every stored device, each with its own key, and the
device that sign-in registered, then deletes the bundle and the sign-in. A
revocation proof is kept before it is sent, so a lost reply is read back. A
partial result stays `incomplete` with its original proof; `--local-only`
leaves the revocation with Cloud `unverified`.

## Setup and the bundle

`setup preview|start|status|continue` take only the bundle and an operation
ID. `start` creates the state directory and `config.json` if absent and keeps
the bundle; it creates no key. `inspect` verifies the kept bundle; `upgrade`
keeps, verifies and switches to a new bundle under one operation ID, then
removes every kept bundle but the selected one and the one before it. An
interrupted copy is removed and redone. `installation.json` holds the bundle's
identity and phase. `configure --server` selects the Cloud origin before
setup.

Bundles are kept under
`~/Library/Application Support/OpenMachine/Connector/versions/` on macOS and
`~/.local/share/openmachine/connector/versions/` on Linux. Setup and `upgrade`
take a bundle only where its manifest's `platform` (`darwin` or `linux`) and
`architecture` are the host's. Setup, `inspect` and `upgrade` refuse a bundle
in which any file differs from the SHA-256 its `manifest.json` lists.

`scripts/build_bundle.py` builds the bundle: a Python 3.12 runtime you supply
(an exact python-build-standalone `install_only` archive and its SHA-256), the
locked packages in `device/requirements.lock`, `om` built from the working
tree with the Go version `go.mod` pins, the license texts of every Go module
`om` links, and `manifest.json`. Its version is the repository's, from
`access/openmachine_access/__init__.py`. `--target linux-x86_64` or
`linux-aarch64` builds the Linux bundle on a host of that target: `om` is one
static binary, and the wheels are `manylinux2014`, for glibc 2.17. On a Mac,
`--sign-identity` signs the native files with an existing Developer ID
identity; notarization and publication are separate steps, done by the release
owner.

```bash
python scripts/build_bundle.py --python-archive cpython-3.12.<n>+<build>-<platform>-install_only.tar.gz \
  --python-sha256 <its SHA-256> [--target linux-x86_64] [--output dist/connector]
```

## Test

```bash
python -m pip install -r device/requirements.lock
python -m unittest discover -s test/device -p 'test_*.py'
```
