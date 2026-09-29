# Open Machine

An open-source runtime for AI to operate your machines.

## Current increment

`0.1.0a1` is the first implementation slice of [Spec #2](https://github.com/OpenMachineLabs/openmachine/issues/2),
not the completed or published v0.1.0 release. ChatGPT plans; this package executes
explicit calls. There are no model API calls, hidden agents, private Brain imports,
Codex workers or organization-specific settings.

Implemented: an independently installable Python package, safe configuration,
`init`/`doctor`/`serve`, private Streamable HTTP MCP, a bundled operating Skill,
bounded local readers, and durable local execution with result readback.

Deferred: the full Git coding loop, cancellation and rotating logs, task
checkpoints, user Skill roots, SSH/GitHub adapters, and managed service
installation/update/rollback. No empty adapter or future-feature modules are
shipped. Real ChatGPT/Tunnel acceptance is a separate account-bound gate; local
tests do not establish it.

## Install

Use Python 3.12 or newer on macOS or Linux. Run from a version-pinned source
checkout; no public registry name or release is assumed:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.1.0a1-py3-none-any.whl
.venv/bin/openmachine --version
```

The wheel includes only the core package and bundled Skill, not tests, private
state or arbitrary checkout files. Workers use the installed package, never a
source-checkout import. The baseline is MCP SDK 1.30.0, Pydantic 2.13.5, Uvicorn
0.38.0 and Hatchling 1.27.0 for building. See [requirements.lock](requirements.lock)
for the complete pinned dependency set.

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
files and leaves execution **disabled**. Explicitly set `execution_enabled = true`
for a local machine only when native account access is intended. Restart this
service after changing configuration.

Machine/workspace names are operator-owned; see [examples/config.toml](examples/config.toml).
Only local transport is accepted here. Unknown fields and future adapter
settings are rejected, not ignored. `doctor` checks configuration and directory
existence, not effective file access, service readiness, tunnels or ChatGPT.
Verify an actual file read through MCP before calling an environment ready.

`serve` listens only at `<state-dir>/mcp.sock`, with directory mode 0700 and socket
mode 0600. It opens no TCP port, manages no tunnel and installs no OS service.
An occupied socket is never replaced. After an abrupt exit, verify that no owner
is using a stale socket before manually removing it. Graceful shutdown removes
only this instance's socket. Existing state with broader permissions is rejected,
never silently chmodded. Keep socket paths within the host's Unix-socket limit.

## Connect the official tunnel

Manage upstream `tunnel-client` separately. Its documented local binding is:

```text
--mcp.server-url=channel=main,url=http://localhost/mcp,unix-socket=/absolute/path/to/state/mcp.sock
```

Use `localhost` as the logical HTTP host; SDK Host/Origin protection remains
enabled. Follow [OpenAI's Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for runtime credentials, tunnel/workspace association and actual account
permissions. Keep credentials in upstream-supported local secret references,
never in tool arguments, repositories or pasted commands. Configuration reference:
[upstream revision 49c9394](https://github.com/openai/tunnel-client/blob/49c93943eb5d6eec2dee3c9060f7889db2ca8fcd/docs/configuration.md).
This identifies the inspected reference, not a live-tested tunnel binary.

Do not reconfigure an existing production bridge to test this increment. A
separately authorized private ChatGPT connection must initialize, list tools and
perform one real harmless write. Refresh ChatGPT tool metadata when schemas
change. A socket or a tool list does not prove that write tools work.

## Tool contracts and first acceptance

| Tool | Current input and result |
| --- | --- |
| `get_capabilities` | Version, configuration hash, configured machines/workspaces and implemented limits; health remains `not_probed`. |
| `inspect_workspace` | `machine`, `workspace_name`, `action=files/read`, relative `path`, `offset`, `limit`; lists at most 200 entries or reads 64 KiB. Read offsets are raw bytes; list offsets are visible entries. |
| `inspect_skills` | `action=list/read`; bundled Skill identity/hash and optionally text. No user-directory scan. |
| `start_execution` | `machine`, retained 32-hex `execution_id`, existing absolute `cwd`, explicit `argv`, optional `timeout_seconds` and `expected_head_sha`; durable identity and state. |
| `read_execution` | Original `machine`/`execution_id`, byte `offset`, `max_bytes`; state, exit status, context, bounded output and next offset. |

From ChatGPT, read capabilities, the bundled Skill and the disposable project's
`AGENTS.md`. Retain an execution ID, then explicitly authorize an argv such as
`["/absolute/path/to/python", "-c", "from pathlib import Path; Path('hello.txt').write_text('hello')"]`.
Poll the original ID until its terminal receipt reports exit zero, then read
`hello.txt` through `inspect_workspace` and confirm its contents. No other bridge
may perform the write claimed as Open Machine's live acceptance.

Commands return promptly and run under a separate worker. Reusing an execution
ID with an identical normalized request returns its original record; changed
arguments are rejected. After a lost acknowledgement, query the original ID.
A missing worker or invalid receipt is unknown/corrupt, never success or an
automatic relaunch. Service restart does not itself terminate the worker.

This PR caps commands at 600 seconds, also the default. Cooperating writes lock
the canonical Git worktree or non-Git directory; an expected Git HEAD is checked
before launch. No dependency installation, branch reset or environment selection
occurs implicitly. Locks do not control manual edits or other runtimes. Commands
receive no stdin and must not require prompts. Timeout cleanup covers the owned
process group; intentionally detached processes, sleep, session teardown and host
reboot are not recovery promises.

Console retention keeps the **first 4 MiB**, drains later output without killing
the command and reports `log_truncated`. This is not PR2's rotating-log contract.
Reads return at most 64 KiB; explicit artifacts should hold durable full output.
A zero command exit does not mark a logical task complete. Cancellation, 24-hour
commands and automatic resumption are not claimed for this increment.

## Trust boundary

This is a trusted single-operator native runtime, not a multi-tenant executor
or OS sandbox. Enabled commands retain account-level filesystem/network access
beyond reader scopes. Dedicated readers reject known credential paths and
escaping resource paths while allowing useful dotfiles. Redaction is best effort,
not protection against a malicious command. The OS account can modify its own
state; receipts are not tamper-proof.

Returned text/logs go to ChatGPT through OpenAI. Self-hosted does not mean all
data stays local. A Skill never grants write authority. Do not expose this
listener publicly or treat a tunnel ID as a complete access policy. This PR
performs no production transition, service replacement or OS permission changes.

## Development and review

Repository-authored documentation, comments, Issues, PRs, commits, labels and CI
remain English. Follow [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md)
and the [Spec index](spec/README.md).

```sh
python3 -m unittest discover -s test/system -p 'test_*.py'
python3 scripts/check_repository.py
```

See [test/README.md](test/README.md) for installed-wheel checks. CI uses no private
infrastructure or cloud credentials. Record PASS, FAIL and NOT RUN separately,
including real-account acceptance. Human review and explicit merge authorization
remain required.

## License

[MIT](LICENSE).
