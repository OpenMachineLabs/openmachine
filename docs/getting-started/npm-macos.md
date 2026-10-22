# Install with npm (Apple Silicon Mac)

**Requirements:** macOS 13+ on Apple Silicon (ARM64), Node.js 20+.
This is the standard consumer install and update path. The current npm package
targets Apple Silicon macOS; source installation remains an advanced developer
path for other platforms until their npm packages are available.

## Install

```sh
npm install -g openmachine@latest
openmachine
```

The npm install only unpacks the package. On first run, `openmachine` asks for
an existing project folder containing a readable file such as `README.md`.
It then configures a local Core runtime and starts a managed macOS user service.

For non-interactive setup, use an existing folder and file:

```sh
openmachine setup --workspace /absolute/project --probe README.md
```

## Verify

```sh
openmachine status
```

The service should report `ready`. Machine commands and workspace edits remain
**off by default** unless you explicitly grant them.

The npm `openmachine` command manages setup, service status and Core updates.
Use `openmachine core ...` only for advanced Core management commands. Updating
the npm package alone does **not** replace an existing managed Core runtime;
run `openmachine update` as described in [Updates](updates.md).

## Background service and login startup

Core runs as a macOS user service and can keep running after you close the
terminal. Use `openmachine start`, `openmachine stop` and `openmachine status`
to control and inspect it.

```sh
openmachine startup on
openmachine startup off
```

These commands enable or disable startup when you log in. They do not start or
stop the current service. A deliberate `openmachine stop` remains in effect
until `openmachine start`, even with login startup enabled. First setup leaves
login startup disabled. This is a user-login service, not a promise of availability
while the Mac is asleep, shut down or logged out.

## Connect to ChatGPT

The installer does not configure an MCP tunnel, connect a ChatGPT account or
approve client tools. Follow [Connect to ChatGPT](connect-chatgpt.md), then
[verify a real read](verification.md).
