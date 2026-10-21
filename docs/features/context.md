# Context and environment

`inspect_context` collects bounded observations for a selected workspace so you
can choose the right target and understand its environment before acting. It is
not an agent memory database, a monitoring daemon or an authorization grant.

## Configure and select

Use an existing configured local workspace or a prepared, identity-pinned SSH
workspace. No additional configuration flag is needed. Choose only the sections
relevant to your question:

| Section | What it reads |
| --- | --- |
| `workspace` | One Git status page and target identity. |
| `worktrees` | Registered worktrees, not proof that they are idle. |
| `activity` | An explicitly selected task, stage dependencies/state and original operation index. |
| `runtime` | Controller provenance, configured grants and supported routes. |
| `environment` | Target observer and paths of explicitly requested executables. |
| `project` | Bounded root declarations and lock fingerprints, without evaluating them. |
| `resources` | Supported CPU, memory, filesystem and display-adapter observations. |
| `services` | Listener counts for explicitly selected TCP ports, without connecting. |

Default sections are `workspace`, `activity` and `runtime`. Supply a known
`task_id` for task activity; no selected task does not mean there is no activity.
Activity includes the bounded stage plan and available original evidence references,
not a decision that another task is blocked or safe to run. Check current owners
and shared resources; separate development from integration and release gates.

## Example

This is a tool call shape, not a terminal command:

```text
inspect_context(machine="local", workspace_name="project",
  sections=["workspace", "environment", "project", "services"],
  executables=["python3", "git"], ports=[8000])
```

Or ask ChatGPT:

```text
Inspect the project workspace and resolve python3 and git without running
either program. Check only whether TCP port 8000 has a listener. Do not connect
to it, run project code, install anything or change the machine.
```

## Verify the observation

Read the target identity, per-section source, timestamps, status and `complete`
flag. Follow returned next references when a page is incomplete. An executable
path is not its tested version. Listener absence is not port reservation, UDP
coverage or service health. Runtime metadata describes the controller, while
opt-in environment data describes the selected target observer.

## Limits and errors

Sections are not an atomic snapshot. Resource totals are not reserved capacity;
GPU metadata is not a utilization or complete compute-device inventory. Older
SSH workers may not support environment observation. Preserve that error instead
of substituting local facts or silently installing a new worker.

The [environment Skill](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/environment/SKILL.md)
owns detailed collection limits and source semantics.
