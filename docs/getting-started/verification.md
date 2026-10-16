# Verify each layer

Use this checklist during installation and after an update. Each row proves
only its own boundary; a pass in one row does not imply the next row passed.

| Layer | Evidence to collect | What it does not establish |
| --- | --- | --- |
| Installed CLI | `openmachine --version` and the selected executable | Running service identity or client access. |
| Configuration | `doctor` returns valid configuration and expected local directories | Effective file permissions, network readiness or task-store compatibility. |
| Core serving | Selected private socket and successful MCP initialization | Tunnel or ChatGPT acceptance. |
| Managed runtime, when used | `doctor --service` verifies the selected identity and a permitted readiness-file read | ChatGPT or GitHub health. |
| Tunnel | Upstream client readiness for the exact Core socket | Approved tool catalog or successful client call. |
| ChatGPT read | Actual `get_capabilities` and file text/hash matching the local file | Native command or write permission. |
| Native execution | Original receipt: succeeded, expected output, exit and verified cleanup | Correctness of an unrelated business task. |
| File edit | Original change receipt and current expected file bytes/hash | Passing tests or completion of all task stages. |
| Durable task | Current-plan evidence accepted and explicit task completion | Autonomous continuation after Chat disconnects. |

## Useful read-only commands

Use your selected paths, not another installation's defaults:

```sh
openmachine --version
openmachine --config "$DEMO/config.toml" --state-dir "$DEMO/state" doctor
```

For a managed macOS installation only:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" doctor --service
```

`CONFIG` and `STATE` above must be the existing installation's absolute paths.
Do not run the managed-service check as if it created or adopted a service.

## Keep safe evidence

Retain the release/commit, observation time, task and operation IDs, actual
status/error, relevant hashes and the verification boundary. Avoid publishing
raw logs, private paths, host inventories, tokens or full configuration.

For a reported failure, preserve the first error and the original ID. Query the
original result before deciding whether any retry is safe. See
[Troubleshooting](../troubleshooting.md) for catalog mismatch, permission, SSH
and interrupted-task cases.
