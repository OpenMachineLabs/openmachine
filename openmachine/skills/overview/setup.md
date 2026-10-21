# Install, configure and discover Skills

[Domain guide](SKILL.md)

On this page:

- [Install](#install)
- [Configure and run](#configure-and-run)
- [Connect the official tunnel](#connect-the-official-tunnel)
- [Skills](#skills)
- [Platform and acceptance](#platform-and-acceptance)
- [Release update notices](#release-update-notices)
- [Client-call diagnostics](#diagnose-client-calls-and-interrupted-work)

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. Task storage requires SQLite 3.51.3 or newer, or the patched 3.50.7+
(3.50.x) or 3.44.6+ (3.44.x) releases. Build from the exact release tag, not an unpinned default branch:

```sh
git clone --branch v0.2.23 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.23-py3-none-any.whl
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
[SSH](../service/ssh.md) before target preparation. Version `0.2.23` is an initial
development release, not a stable 1.x promise. State contract 21, SSH protocol 7
and worktree receipt format 6 are compatibility contracts, not release versions;
ordinary format-5 and historical receipts remain readable. Inspect installed
capabilities and actual readers separately. Source merge does not update a live
installation, and tests do not prove real ChatGPT acceptance.

## Diagnose client calls and interrupted work

After an authorized runtime update, compare the actual client tool definitions
with this server's `tools/list`. A source checkout, service version or reconnect
does not prove that a client refreshed its approved catalog. In particular, check
`control_task` renewal and `read_task` section/cursor inputs before continuation.
Use the client's supported tool-refresh or rescan flow when those definitions
differ, then verify a fresh conversation. Core cannot update that client snapshot.

`get_capabilities.mcp_catalog` fingerprints the server's advertised descriptors,
sorted by name and encoded as sorted compact UTF-8 JSON plus LF. The digest covers
names, descriptions, schemas and annotations; it is not a client-match or health
claim. It changes when configuration changes the advertised tools. Discovery
performs no target probe and does not expose request arguments or credentials.

A Core `invalid_tool_call` includes a safe diagnostic source, reason, phase and
catalog digest. `current_call_dispatch=not_attempted` applies only to that rejected
call before its handler, never to an earlier request with the same operation ID.
A handler failure reports dispatch as unknown: read the original task/execution or
destination before retrying. Structured domain errors retain their original codes.
Missing replies alone prove neither no effect nor a platform refusal.

Distinguish catalog mismatch, Core permission denial, task-authority expiry,
transport failure and an explicit client/platform refusal. Never call all of them
"blocked by the platform". Preserve the original error and IDs. A bounded wait
ending is not a command timeout or cancellation; original work may still run.
Read its original result before continuing. Task receipt observation does not wake
Chat or authorize the next operation. See [task continuation](../tasks/continue.md).
Do not relabel mutating tools as read-only, expand grants, remove task context or
use another executor to evade a denial. A Core fix cannot promise that a platform
will approve every request.

## Release update notices

While serving, Core checks the official public GitHub latest-release endpoint in
an independent asynchronous worker. `get_capabilities.updates` only reads the
worker's cached view; it never waits for GitHub or starts an installation. Checks
are enabled by default, no more than once per 24 hours per state directory while
its valid cache is retained, including failures and restarts. A future-dated
attempt after a clock rollback is rechecked and starts a new interval rather than
waiting for the old date to return. Initial reads may be
`unchecked`; continue work and let later ordinary capability reads see the result.

Only published, non-prerelease `vMAJOR.MINOR.PATCH` tags participate. Comparison is
numeric, not lexical; equal versions, locally newer versions and unsupported
local version strings never suggest downgrading. A development commit with the
same release version is not independently tracked. Maintainers must publish a
new versioned Release for a new notice; pushing to main is insufficient.

The notice supplies `status`, current/latest versions, the canonical release URL,
check/next-check timestamps, `stale` and a bounded failure reason. Unavailable or
stale metadata is not proof of being current. A private repository, absent public
release, rate limit, timeout or invalid response cannot block ordinary tools.
The fixed unauthenticated HTTPS request sends no project data, credentials,
installed version or machine identifier; GitHub still sees ordinary connection
metadata such as the source IP. Redirects and environment proxy inheritance are
disabled. No release prose, download or remote command is interpreted.

To disable checks, place this root setting before any TOML tables and apply the
configuration through the existing [configuration lifecycle](../service/management.md#exact-updates-and-compatible-rollback):

```toml
schema_version = 1
check_for_updates = false
```

Disabled checking starts no worker and reads/writes no cache. Default/omitted
settings preserve the legacy configuration fingerprint; older readers cannot
parse an explicitly added setting. `release-check.json` in the selected private
state directory is bounded disposable metadata, never Task or update authority.
Malformed data can be replaced; unsafe or unwritable cache disables checks for
that service lifetime without modifying original work. Check responses are capped
at 64 KiB with an eight-second async deadline, separate from normal tool budgets.

The operating guide asks Chat to mention a fresh available release once per
conversation, not repeatedly interrupt or poll for it. This is advisory data,
not a guaranteed Chat notification or a compatibility/security verdict. Upgrade
only with user authorization using the [existing installation-specific procedure](../service/management.md#exact-updates-and-compatible-rollback).
Keep active work and original receipts protected. Updating Core and refreshing a
client's stored tool catalog remain separate; this checker performs neither.
Install a version containing this feature once before expecting future notices.
