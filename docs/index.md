# OpenMachine

**One MCP. Every machine. Real execution.**

OpenMachine Core is a local-first MCP runtime that lets an authorized ChatGPT
conversation inspect files, run commands and coordinate work on your computer
and explicitly configured SSH machines. You choose the machines, workspaces and
permissions. Core carries out tool calls and retains their results; ChatGPT
remains the controller of the conversation.

These user guides target **v0.2.21**, released on October 15, 2026, at commit
`6014a3129b989427c7053ff426424963b47e6d4f`. This is an early development release,
not a stable 1.x compatibility promise. See the
[release](https://github.com/OpenMachineLabs/openmachine/releases/tag/v0.2.21)
and [current limitations](introduction/limitations.md). A checkout of `main`, an
installed runtime and a client's approved tool catalog can be different.

## Start here

On **Apple Silicon macOS**, [install with npm](getting-started/npm-macos.md) (macOS 13+, Node.js 20+). For **other supported macOS and Linux setups**, [build Core from source](getting-started/installation.md).

For source installations, follow [initial configuration](getting-started/configuration.md). Then [connect to ChatGPT](getting-started/connect-chatgpt.md) and [verify access](getting-started/verification.md). npm installs set up the local service, **not** the MCP tunnel or ChatGPT permissions.

## Explore the capabilities

| Guide | What you can learn |
| --- | --- |
| [Local machine](features/local-machine.md) | Select the intended computer and operate under explicit native permissions. |
| [Context and environment](features/context.md) | Inspect project, runtime and opt-in resource information without running project code. |
| [Workspace](features/workspace.md) | Read and search files, preview guarded edits, apply patches and choose worktrees. |
| [Command execution](features/execution.md) | Run a command, read its original result, use PTY controls and verify completion. |
| [Remote SSH](features/ssh.md) | Prepare a Linux worker, pin its identity and use the same tools remotely. |
| [Persistent tasks](features/tasks.md) | Retain progress, find original operations and continue explicitly after interruption. |
| [GitHub](features/github.md) | Connect an account and work with repositories, Issues, PRs, reviews, Projects and Releases. |
| [Browser](features/browser.md) | Delegate a bounded browser subtask to a compatible, authorized native Codex installation. |
| [Skills](features/skills.md) | Discover built-in instructions and explicitly configured external Skills. |
| [Service and runtime](features/service.md) | Operate the macOS user service, update safely and understand offline payload preparation. |

## Configure, diagnose and go deeper

[Configuration reference](configuration/reference.md) documents machines,
workspaces and grants. [Security and data](configuration/security.md) explains
where records live, what leaves the machine and what Core does not isolate.
[Troubleshooting](troubleshooting.md) starts with the observed error rather than
reinstalling or replaying work. The [CLI](reference/cli.md) and
[tool reference](reference/tools.md) are compact maps to the actual interfaces.

The [architecture](introduction/architecture.md) explains the components.
[Updates](getting-started/updates.md) distinguishes a source change from a live
installation. Contributor procedures remain in
[CONTRIBUTING.md](https://github.com/OpenMachineLabs/openmachine/blob/main/CONTRIBUTING.md).

User tutorials live here. Detailed agent operating contracts remain in the
[packaged Skills](https://github.com/OpenMachineLabs/openmachine/tree/main/openmachine/skills),
not a second copy under `docs/`. In a connected session, use `inspect_skills` to
read the guide shipped with the **running** version.
