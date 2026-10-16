# Your first real task

Use the disposable workspace from [Initial configuration](configuration.md),
after the [ChatGPT file-read check](connect-chatgpt.md) succeeds. This exercise
runs one harmless command that prints a marker. It does not modify project files.

## Grant command execution deliberately

Stop the demo's foreground `serve` with `Ctrl+C`. In **that demo's** `config.toml`,
set `execution_enabled = true` under `[machines.local]`. Leave
`workspace_write_enabled = false`; typed file writes are not needed here.

Restart with the same paths:

```sh
openmachine --config "$DEMO/config.toml" --state-dir "$DEMO/state" serve
```

This is a native-account command grant, not a sandbox restricted to the project
directory. Only proceed on an account and workspace appropriate for testing.
The client may separately ask you to approve the command.

## Ask for the command and its original result

```text
Use OpenMachine on machine local, workspace project. Inspect the current
workspace and relevant execution Skill. I authorize one command in that exact
workspace: python3 -c 'print("openmachine-ready")'. First resolve python3 without
running it. Save one execution ID, run the command with a 30-second timeout,
and read that original execution until its result is known. Do not write files,
install anything or change configuration. Report the ID, actual output, status,
exit code and cleanup result. Do not rerun it after a lost reply.
```

The command's tool arguments use an argv array, not a shell string:

```json
["python3", "-c", "print(\"openmachine-ready\")"]
```

The assistant supplies the observed workspace `cwd`, current HEAD where present
and a fresh execution ID. It must use the actual discovered interpreter when
the selected runtime PATH does not resolve `python3`.

## Check success

The original receipt must show `status=succeeded`, exit code `0`, output
`openmachine-ready` followed by a newline, and verified cleanup. Save the
execution ID. Merely seeing an accepted start or a wrapper exit is insufficient.

You can then ask for a read-only retrieval using that **same ID**. It should
return the original result, not launch a new command. A client timeout does not
prove that the command failed or never ran.

## Try a durable multi-stage task next

After separately enabling typed workspace writes, this is a bounded example:

```text
In the same disposable project, create one durable OpenMachine task to create
hello.txt with exactly 'hello from OpenMachine' plus a newline, then verify its
actual contents and SHA256. Do not overwrite an existing hello.txt. Read the
applicable Skills and bind the task to the observed workspace. Keep the original
task, change and execution IDs; finish only after verification. If interrupted,
report the existing task ID rather than creating a replacement.
```

This second example needs its own explicit write grant and task contract.
[Tasks](../features/tasks.md) explains the lifecycle and recovery. The first
one-command check does not require a fabricated multi-stage contract.

After testing, disable the demo grants or stop its server. Do not delete a state
directory or environment while original commands still depend on it.
