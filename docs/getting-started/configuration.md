# Initial configuration

Start with a disposable workspace and **read-only** grants. The commands below
use the CLI installed in [Installation](installation.md), in the same terminal.
They deliberately use separate config/state paths rather than an existing service.

## Create a small workspace

```sh
DEMO="$(mktemp -d /tmp/om-demo.XXXXXX)"
mkdir "$DEMO/project"
git init "$DEMO/project"
printf 'OpenMachine connection check\n' > "$DEMO/project/README.md"
openmachine --config "$DEMO/config.toml" --state-dir "$DEMO/state" \
  init --workspace "$DEMO/project"
openmachine --config "$DEMO/config.toml" --state-dir "$DEMO/state" doctor
```

Retain the value of `DEMO` locally for the later steps; do not replace it with a
new directory halfway through the tutorial. This Git repository can be unborn:
it does not need a commit or user identity to run the file-read check.

`init` creates a private configuration and does not overwrite an existing file.
It registers the workspace as **`project`** on machine **`local`**, regardless
of the directory's basename. `doctor` should report valid configuration and no
unavailable local workspace. State may not exist before the first runtime start.

## Understand the configuration

The generated file contains these settings, with your actual absolute root:

```toml
schema_version = 1

[machines.local]
transport = "local"
execution_enabled = false
workspace_write_enabled = false

[workspaces.project]
machine = "local"
root = "/absolute/path/to/disposable-project"
```

`execution_enabled` controls native commands; `workspace_write_enabled` controls
typed file edits. They are independent. Read-only discovery does not require
either grant. Do not use a whole home directory as a tutorial workspace.

To allow a first command later, stop this manual runtime, change only
`execution_enabled` to `true` in this demo configuration and restart it. Enable
typed writes separately only when needed. `init --enable-commands` and
`init --enable-workspace-writes` are first-creation alternatives, not commands
for overwriting an existing configuration.

## Start the private MCP server

```sh
openmachine --config "$DEMO/config.toml" --state-dir "$DEMO/state" serve
```

Keep this process running while connecting and testing. It listens on
`$DEMO/state/mcp.sock`, not on a TCP port. `Ctrl+C` stops this foreground server;
it is not cancellation of already admitted user commands.

The next step is [Connect to ChatGPT](connect-chatgpt.md). Do not run a second
manual server or managed service against the same state directory.

## Existing installations

Default paths are `~/.config/openmachine/config.toml` and
`~/.local/state/openmachine/`. Global options go **before** the subcommand.
For an existing managed installation, use its explicit configuration/update
workflow rather than editing or restarting another active service by accident.
See [configuration reference](../configuration/reference.md) and
[Service](../features/service.md).
