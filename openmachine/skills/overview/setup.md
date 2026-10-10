# Install, configure and discover Skills

[Domain guide](SKILL.md)

On this page:

- [Install](#install)
- [Configure and run](#configure-and-run)
- [Connect the official tunnel](#connect-the-official-tunnel)
- [Skills](#skills)
- [Platform and acceptance](#platform-and-acceptance)

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. Task storage requires SQLite 3.51.3 or newer, or the patched 3.50.7+
(3.50.x) or 3.44.6+ (3.44.x) releases. Build from the exact release tag, not an unpinned default branch:

```sh
git clone --branch v0.2.19 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.19-py3-none-any.whl
.venv/bin/openmachine --version
```

Release notes use a compact Changes/Changelog format and do not attach installers
or wheels. Build from the exact tag with its committed lock file as shown above;
no PyPI, Homebrew or npm publication is implied.

The wheel includes the core, direct adapters and built-in Skills, not tests, private
state or arbitrary checkout files. Workers use the installed package, never a
source-checkout import. MCP SDK remains at 2.2.0.
Use [requirements.lock](../../../requirements.lock) as the complete build/runtime dependency
source rather than mixing packages from different release tags.

## Configure and run

Start with an existing disposable workspace; replace the example path:

```sh
.venv/bin/openmachine init --workspace /absolute/path/to/disposable-project
.venv/bin/openmachine doctor
.venv/bin/openmachine serve
```

Default configuration: `~/.config/openmachine/config.toml`. Private state:
`~/.local/state/openmachine/`. Global `--config` and `--state-dir` options go
before the subcommand. `init` creates mode-0600 configuration without overwriting
files and leaves execution **disabled** unless `--enable-commands` is explicit. `--enable-workspace-writes` is an independent first-run grant. By default, `init` explicitly sets
`workspace_write_enabled = false`. Enable `execution_enabled = true` only for
intended native commands, and `workspace_write_enabled = true` only for typed
file edits. Existing configurations omitting the new field inherit their old
execution grant; an explicit false overrides that inheritance. Restart this
service after an authorized configuration change.

Machine/workspace names are operator-owned; see [examples/config.toml](../../../examples/config.toml).
Local and explicitly configured SSH transports are supported. Optional `[github]`
configuration enables [scoped GitHub collaboration](../github/SKILL.md). Omit it to
keep GitHub disabled. New accounts use the official Open Machine GitHub App Device
Flow and automatically pinned local credential storage; historical gh task bindings
remain readable, but their retired backend cannot execute requests.
Unknown fields are rejected, not ignored. Plain `doctor` checks configuration and
directory existence, not effective file access or network readiness. Managed
`doctor --service` verifies the selected runtime, MCP identity and an actual
permitted file read; neither command claims tunnel or ChatGPT readiness.

`serve` listens only at `<state-dir>/mcp.sock`, with directory mode 0700 and socket
mode 0600. It opens no TCP port, manages no tunnel and installs no OS service.
An occupied socket is never replaced. After an abrupt exit, verify that no owner
is using a stale socket before manually removing it. Graceful shutdown removes
only this instance's socket. Existing state with broader permissions is rejected,
never silently chmodded. Keep socket paths within the host's Unix-socket limit.

## Connect the official tunnel

Manage upstream `tunnel-client` separately. Its inspected local binding is:

```text
--mcp.server-url=channel=main,url=http://localhost/mcp,unix-socket=/absolute/path/to/state/mcp.sock
```

Keep logical Host `localhost` and Host/Origin protection. Follow the
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for the selected route's credentials and eligibility; keep secrets in supported
local references, not pasted commands. A socket/handshake is not actual ChatGPT
acceptance. Never reconfigure a production bridge to test a candidate.

## Skills

Configure explicit existing sources:

```toml
[skills]
roots = ["/absolute/skills"]
```

A root is one Skill directory with `SKILL.md` or an immediate-child collection;
there is no recursive account scan. Canonical whole-directory links are supported,
duplicated sources deduplicate by identity, not name, and resource links cannot
escape the selected directory. Useful dotfiles remain visible; credentials are
excluded. Discovery scans at most 2,000 entries and reports skipped/inaccessible
sources. Follow hashes, snapshots and paging; incomplete discovery is not absence.
An empty read selector returns the short built-in Overview. `action=list` returns
metadata; `action=read, tool="<available-tool-name>"` selects its bound guide.
Use exact returned IDs and `action=files` before reading a relative topic path.
The historical `package:openmachine/_skills/SKILL.md` and default
`path="worktrees.md"` selectors remain aliases to canonical resources, not duplicate
manuals. Only supported packaged resources are exposed. External user edits are live
on the next read; changing configured roots follows service restart. Instructions
never confer grants, install tools or execute embedded resources automatically.

## Platform and acceptance

Managed services support logged-in macOS controllers. Linux supports manual `serve`
and explicitly prepared SSH workers; managed Linux services and Windows are not
included. Read [management](../service/management.md) before lifecycle changes and
[SSH](../service/ssh.md) before target preparation. Version `0.2.19` is an initial
development release, not a stable 1.x promise. State contract 21, SSH protocol 7
and worktree receipt format 6 are compatibility contracts, not release versions;
ordinary format-5 and historical receipts remain readable. Inspect installed
capabilities and actual readers separately. Source merge does not update a live
installation, and tests do not prove real ChatGPT acceptance.
