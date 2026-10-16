# OpenMachine

**Your machines. Your tools. One conversation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-222222)](LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-555555)](docs/introduction/requirements.md)

[Documentation](docs/index.md) · [Getting started](docs/getting-started/installation.md) ·
[Releases](https://github.com/OpenMachineLabs/openmachine/releases) · [Contributing](CONTRIBUTING.md)

OpenMachine Core is a **local-first MCP runtime that connects an authorized
ChatGPT conversation to your computer, configured SSH machines and developer tools**.

Choose the machines, project workspaces and permissions. ChatGPT requests an
operation; Core performs it under your native account and retains the original
result. You can inspect code, run tests, make guarded edits, work with GitHub and
continue a saved task without treating a lost response as permission to start over.

## What you can do

| Capability | Use it for |
| --- | --- |
| [Local machines and context](docs/features/local-machine.md) | Inspect the selected project, environment and opt-in resources before acting. |
| [Workspace files and worktrees](docs/features/workspace.md) | Read, search, preview edits and apply source-checked changes in the intended tree. |
| [Native command execution](docs/features/execution.md) | Run commands, read original results, use interactive terminals and verify cleanup. |
| [Remote Linux over SSH](docs/features/ssh.md) | Use the same tools on explicitly prepared, identity-pinned remote accounts. |
| [Persistent tasks](docs/features/tasks.md) | Keep goals, stages and evidence; find the original work and continue explicitly. |
| [GitHub integration](docs/features/github.md) | Work with scoped repositories, Issues, PRs, comments, reviews, Projects and Releases. |
| [Browser subtasks](docs/features/browser.md) | Delegate a bounded web task to a compatible, authorized native Codex installation. |
| [Skills](docs/features/skills.md) | Discover the operating guides packaged with the runtime and your reviewed Skill roots. |
| [Service and runtime](docs/features/service.md) | Manage a macOS user service, retained updates and compatible rollback. |

Tasks preserve progress and observe admitted work; they **do not automatically
wake ChatGPT or execute the next stage after a chat disconnects**. Optional
integrations have their own prerequisites and permissions.

## How it works

```text
You + ChatGPT
      |
      | approved MCP calls / original results
      v
Supported connection or separately managed private tunnel
      |
      | private Unix socket
      v
OpenMachine Core
      |-- local workspaces and native commands
      |-- prepared Linux workers over SSH
      |-- scoped GitHub and native browser integrations
      `-- persistent tasks and operation evidence
```

Core is the execution and evidence layer, not a chat UI or a hosted control plane.
See [architecture](docs/introduction/architecture.md) and
[current limitations](docs/introduction/limitations.md).

## Installation and quick start

The documented release is **v0.2.21**. Use **Python 3.12+**, Git and a
[supported SQLite engine](docs/introduction/requirements.md); Python's version
alone is not enough. Before public repository access is enabled, cloning requires
repository permission. From a directory without an existing `openmachine` clone:

```sh
git clone --branch v0.2.21 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.21-py3-none-any.whl
.venv/bin/openmachine --version
```

Then follow the [initial configuration tutorial](docs/getting-started/configuration.md)
to create a disposable, read-only workspace and start the private socket server.
[Connect to ChatGPT](docs/getting-started/connect-chatgpt.md), verify an actual
file read, then authorize the [first real command](docs/getting-started/first-task.md).

**Installing Core is not ChatGPT enrollment.** A supported connection, account or
workspace eligibility and client tool approvals are separate requirements. Core
does not install or start a tunnel. There is no assumed package-registry installer,
public plugin listing or signed desktop download in this source-install path.

## Documentation

The [user documentation](docs/index.md) is the entry point for all supported
capabilities. Start with [requirements](docs/introduction/requirements.md), then
[installation](docs/getting-started/installation.md) and
[layer-by-layer verification](docs/getting-started/verification.md).

For existing installations, use [updates](docs/getting-started/updates.md),
[configuration](docs/configuration/reference.md) and
[troubleshooting](docs/troubleshooting.md). The [CLI](docs/reference/cli.md) and
[MCP tools](docs/reference/tools.md) references link to detailed operating Skills
without duplicating their contracts. Source `main`, a release, a running service
and an approved client catalog are different identities.

## Security

Start read-only and grant only the intended machine and repository access.
Core checks scopes and original operation evidence, but **it is not an OS sandbox
or a multi-tenant isolation system**. Native commands use their account's rights.
Cancellation and software rollback do not undo completed user-work effects.

Configuration and task records are local to their owners; data returned through
MCP can leave the machine for the connected client. Never put secrets in prompts,
command arguments or committed configuration. Read the
[security and data guide](docs/configuration/security.md) before enabling changes.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, tests, review and public
metadata conventions. User guides live in `docs/`; detailed tool contracts live
in the [packaged Skills](openmachine/skills/README.md).

OpenMachine Core is licensed under the [MIT License](LICENSE).
