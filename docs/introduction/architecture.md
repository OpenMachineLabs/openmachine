# Architecture

OpenMachine separates conversation, transport, native operations and evidence.
It is not another chat application and does not host a language model.

```text
You + ChatGPT
     |
     | approved MCP calls and returned results
     v
Supported connection / separately managed tunnel
     |
     | private Unix socket: <state-dir>/mcp.sock
     v
OpenMachine Core on your controller
     |-- configured local workspaces and native commands
     |-- prepared Linux workers over authenticated SSH
     |-- scoped native GitHub connection
     |-- authorized native Codex browser adapter
     `-- local task ledger and original operation receipts
```

The controller is the computer running Core. An SSH target is a different
machine with its own account, workspace, worker and operation results. Core does
not substitute controller facts when the target is unavailable.

## From request to verified result

The client discovers the tools that the runtime advertises. The operator's
configuration, client approval, native account permissions and any task contract
all remain relevant; none substitutes for another.

A file operation checks the observed source and workspace identity. A native
command gets an original execution ID and runs in a worker. Multi-stage work can
also have a durable task contract with explicit stages and authority. Results are
read back from their original owners before ChatGPT accepts the outcome.

A command finishing, a file changing and a task completing are three different
facts. The [verification guide](../getting-started/verification.md) makes those
distinctions concrete.

## What stays independent

The tunnel has its own installation, credentials and lifecycle. Core's `serve`
command opens no TCP listener and does not start that tunnel. The macOS managed
service controls Core, not the tunnel or a remote daemon.

Already admitted command workers and result observation can outlive a caller
disconnect. Task storage does not wake a disconnected chat, manufacture new
authorization or dispatch the next stage. Continue through the original IDs as
described in [Tasks](../features/tasks.md).

GitHub uses its own scoped account connection. Browser delegation uses an
existing compatible native Codex bridge. Neither integration is installed or
authorized merely by installing the Core wheel.

See [security](../configuration/security.md) before granting native execution.
