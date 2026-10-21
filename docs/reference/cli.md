# CLI reference

The installed CLI is authoritative for available syntax:

```sh
openmachine --help
openmachine --version
openmachine doctor --help
openmachine manage --help
```

Global `--config` and `--state-dir` options precede the subcommand. `manage`
requires both explicitly; use absolute paths. Examples below are argument maps,
not permission to mutate an existing installation.

## Install, inspect and serve

| Command | Inputs and purpose |
| --- | --- |
| `init` | `--workspace` selects an existing local directory; optional `--enable-commands` and `--enable-workspace-writes` independently grant first-run capabilities. Never overwrites. |
| `doctor` | Read configuration and local directory availability; live integrations remain unprobed. |
| `doctor --machine NAME` | Explicit SSH probe and identity/workspace result. |
| `doctor --github --repository OWNER/REPO` | Probe selected native account and an optional configured repository; does not start login. |
| `doctor --service` | Verify the selected managed service and its readiness-file read. |
| `worker-bundle --directory PATH` | Build the selected version's content-addressed worker archive; does not transfer it. |
| `serve` | Serve MCP on the private state-directory Unix socket only. |
| `rebuild-task-index` | Explicitly rebuild a bounded page of derived metadata, not task history; continuation needs the observed `--expected-generation`. |

The three `doctor` target options are mutually exclusive. `--repository` requires
`--github`. Read [installation](../getting-started/installation.md),
[configuration](../configuration/reference.md) and [SSH](../features/ssh.md)
before using the corresponding commands.

## Managed macOS service

| Command | Required inputs or important option |
| --- | --- |
| `service install` | `--wheel`, matching `--requirements`, `--probe-workspace`, `--probe-path`. Registers without starting. |
| `service start` / `status` / `stop` | Explicit Start, current status or legacy unload-only Stop. |
| `service restart` | Optional `--reset-recovery` renews an eligible recovery budget explicitly. |
| `update` | Either `--wheel` with `--requirements`, or `--commit` with a full lowercase 40-hex commit; optional `--candidate-config`. |
| `rollback` | Restore a compatible retained installation; does not undo workload effects. |
| `prepare-runtime` | `--payload` and trusted manifest `--sha256`; preparation without service selection/start. |

Wheel and commit update modes are mutually exclusive. `--requirements` is
required with a wheel and invalid with a commit. An incoming state contract can
require the reviewed incoming CLI; an old CLI is not a universal updater.

## Management contract 1

After the explicit global paths, use `manage --contract 1 COMMAND`:

| Command | Input and behavior |
| --- | --- |
| `status` | Actual native identity/readiness and retained policy; no repair. |
| `configure` | `--workspace`, optional independent grant flags; `--verify-existing` only verifies the exact first-run inputs after a lost reply. |
| `bind` | `--identity` of the exact verified retained implementation. |
| `start` / `stop` | Clear or persist deliberate Stop under native ownership checks. |
| `startup` | `--enabled true` or `false`; login preference is independent of Stop. |
| `install` | `--operation-id`, `--payload`, `--sha256`, `--probe-workspace`, `--probe-path`. |
| `update` | `--operation-id`, `--payload`, `--sha256`, optional `--candidate-config`; paired `--config-sha256` / `--candidate-sha256` and optional `--expected-selection-sha256` bind confirmed configuration and installation when advertised. |
| `operation` | `--operation-id`; read the original maintenance result without retry. |
| `continue` | `--operation-id`; explicitly advance eligible known-unperformed/deferred work. |
| `retire` | `--operation-id`; reject only an eligible deferred update proven not activated. |
| `rollback` | Explicit compatible restoration, not workload rollback. |
| `machines` | Read configured machine information. |
| `machine-probe` | `--name`; probe an existing configured SSH machine. |
| `machine-test` | `--name`, `--ssh-alias`, `--ssh-worker`; test a prepared route without adding it. |
| `machine-add` | `--name`, `--ssh-alias`, `--ssh-worker`, verified `--ssh-identity`, original `--operation-id`, observed `--config-sha256`. |
| `machine-remove` | `--name`, original `--operation-id`, observed `--config-sha256`; guarded configuration removal, not remote data deletion. |

For configuration consent, require the `update:configuration_consent` capability
and supply both file digests. `--config-sha256` is the active file digest returned
by `manage machines`; `--candidate-sha256` is the reviewed candidate file digest
(or the same active digest when no candidate path is supplied). These are SHA256
hashes of raw file bytes, not MCP's normalized `configuration_sha256`. A changed
active or candidate file rejects admission; the accepted candidate is retained
before dispatch. When `update:selection_consent` is advertised, pass the
`selection_sha256` from `manage status` as `--expected-selection-sha256`
alongside both file digests. Selection drift rejects before admission with
`selection_changed` and `admitted: false`. Omitting these guards retains the
legacy update behavior; a consent-dependent caller must not fall back.
Read the original operation after any lost reply.

Use fresh IDs for genuinely new authorized operations; use the original ID to
recover an existing one. Hashes are observed evidence, not arbitrary strings.
Read the installed subcommand's `--help` and the
[management guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/service/management.md)
before changing an installation. See [Service](../features/service.md) for examples.

## Interpret output

Most operations return JSON. Inspect status, reason, observation time and actual
selection/identity; a command returning a JSON object is not automatically success.
CLI syntax errors use stderr and exit 2. Operational failures may use structured
errors and nonzero exits. Do not parse only a product version or PID as readiness.

There is no CLI `login`, `skill install` or public-server installation shortcut
in this release. Native GitHub login uses the connection tools, and the tunnel
has its own separately documented client. Exact release syntax lives in
[cli.py](https://github.com/OpenMachineLabs/openmachine/blob/v0.2.22/openmachine/cli.py).
