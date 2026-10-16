# Command execution

Commands run in separate workers with original execution IDs, bounded logs,
timeouts and verifiable cleanup. Use this to run a test or script and retrieve
its actual outcome instead of treating a lost chat response as permission to rerun.

## Prerequisites

Enable `execution_enabled` only on the intended machine. Select its configured
workspace and actual `cwd`, inspect applicable instructions, and resolve the
needed executable. SSH additionally needs the [prepared target](ssh.md).
For structured task work, the current claim and task contract must admit the
operation. Client approval remains separate from Core's grant.

## Run and read one command

The [first task](../getting-started/first-task.md) provides a complete safe example.
The important tool inputs are an original 32-hex `execution_id`, the actual
`cwd`, an `argv` list and an explicit timeout. For example:

```json
["python3", "-c", "print(\"openmachine-ready\")"]
```

There is no implicit shell. Pipes, redirects, variable expansion and `cd` need an
explicitly intended shell command; do not interpolate untrusted text into one.
Each argument is bounded to 4,096 characters. Use a reviewed input file for large
scripts rather than hiding them in an oversized argument.

Save the execution ID before launch. Read it with `read_execution`, follow
`next_offset`, and inspect status, output, exit reason and cleanup. Reads can wait
up to five seconds for observation; this does not extend the command timeout.
The default command timeout is 600 seconds, with an explicit maximum of 86,400.

## Confirm success or diagnose

For the marker example, require the expected printed marker, succeeded status,
exit zero and verified cleanup. For a test or deployment, also check its semantic
result and destination. `accepted`, `running`, `stopping` and `finishing` are not
completed outcomes. A signal handler returning zero can still belong to a
cancelled or timed-out command.

After a lost response, read the **same ID**. `list_executions` helps inspect receipt
summaries, but its order is not a chronological task lookup. Prefer the task's
original IDs; do not select the newest unrelated execution.

## Interactive terminals and cancellation

Use `tty=true` only when an interactive terminal is actually needed. Inspect
`control_ready`, supported controls and sequence before `control_execution`.
`write_stdin` adds no newline; `resize` changes terminal dimensions; `interrupt`
signals the verified foreground group. Later input requires its own authorization.
Never automate confirmations or send secrets that could enter terminal logs.

`cancel_execution` requests cancellation of the original owned worker. Read back
the stop reason and cleanup. Cancellation does not undo completed effects. Idle
PTYs retain workspace leases; controller restart cannot reconstruct a dead PTY.

For eligible local cleanup-only uncertainty, `reconcile_execution` can record
later native cleanup proof without rerunning or signaling the command. It is not
a general repair button and cannot manufacture missing command-exit evidence.

## Limits

Output rotates through four 1 MiB segments. `output_gap` means earlier bytes are
no longer retained; keep essential full logs in explicitly chosen producer files.
Follow raw cursors even when displayed output is redacted or escaped. Native
commands are not OS-sandboxed. `workspace_access=shared` is a cooperative read-only
declaration, never a way to bypass a writer conflict.

Exact lifecycle, controls and recovery semantics are in the
[execution Skill](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/execution/terminal.md).
