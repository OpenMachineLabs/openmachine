# Open Machine

An open-source runtime for AI to operate your machines.

## Current increment

`0.1.0a2` is the local coding workflow increment of [Spec #2](https://github.com/OpenMachineLabs/openmachine/issues/2),
not the completed or published v0.1.0 release. ChatGPT plans; this package executes
explicit calls. There are no model API calls, hidden agents, private Brain imports,
Codex workers or organization-specific settings.

Implemented: an independently installable Python package, safe configuration,
`init`/`doctor`/`serve`, private Streamable HTTP MCP, a bundled operating Skill,
bounded local search and Git status/diff, durable local execution, cancellation,
execution lookup, rotating logs, and cooperative workspace/resource locks.

Deferred: task checkpoints, user Skill roots, SSH/GitHub adapters, and managed service
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
.venv/bin/python -m pip install --no-deps dist/openmachine-0.1.0a2-py3-none-any.whl
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

## Tool contracts and local coding acceptance

| Tool | Current input and result |
| --- | --- |
| `get_capabilities` | Version, configuration hash, configured machines/workspaces and implemented limits; health remains `not_probed`. |
| `inspect_workspace` | `machine`, `workspace_name`, `action=files/read/search/status/diff`, relative `path`, `offset`, `limit`; search adds literal `query`, diff adds `staged`, paged search/Git reads accept `expected_snapshot`. |
| `inspect_skills` | `action=list/read`; bundled Skill identity/hash and optionally text. No user-directory scan. |
| `start_execution` | Original 32-hex ID, machine, absolute `cwd`, explicit `argv`; optional timeout, expected HEAD, `workspace_access`, and `resource_keys`. |
| `read_execution` | Original machine/ID and absolute byte offset; observed state, exit code, context, bounded output and next offset. |
| `list_executions` | Machine, opaque ID cursor and page size up to 100; receipt summaries and unreadable records, without raw commands/logs. Stable ID order is not chronological or a logical-task lookup. |
| `cancel_execution` | Original machine/ID; records a cancellation request for the owning worker. Read again for the final outcome; requesting cancellation is not proof of termination. |

From ChatGPT, read capabilities, the operating Skill and the disposable project's
`AGENTS.md`. Inspect Git status, search and read the relevant source. Use an
explicit execution to make the authorized change, then another to run the
project's test command with its selected interpreter. Inspect the original
receipts and actual exit/output, review both staged and unstaged diffs as needed,
and read back the changed files. Preserve unrelated dirty work. Local SDK tests
prove the protocol workflow, not this account-bound ChatGPT acceptance. Record
the exact tested source and refresh changed tool metadata in a new conversation.

### Workspace inspection bounds

File/diff reads return at most 64 KiB. Listing/status/search pages contain up to
200 entries; offsets count entries except file/diff offsets, which count raw
bytes. Echo `snapshot` as `expected_snapshot` between search/status/diff pages;
a changed snapshot rejects continuation. These inspections are not atomic source
snapshots: recheck status when external writers may have changed the workspace.

Literal search scans at most 2,000 entries and 8 MiB per call, at most 1 MiB per
file and 1,000 matching lines. It does not follow descendant symlinks or build an
index. Skipped credentials, binary, oversized and unreadable files are explicit;
`complete=false` is not a complete negative search. Narrow the path when limits
are reached. Git commands have a five-second/1 MiB capture bound; a captured
prefix may be paged, but `truncated=true` requires a narrower path for completeness.

Git status distinguishes staged, unstaged and untracked changes. Diff defaults
to the worktree; `staged=true` compares the index, including staged deletions.
Untracked files require status plus explicit reads. Submodule contents are not
inspected. Git readers disable optional index writes, fsmonitor, external diff,
textconv and clean/process filters. Configured filters produce a warning that
this is a raw worktree comparison. Credential-path exclusions apply to all reader
actions; these scopes are not an operating-system sandbox.

### Execution lifecycle and compatibility

Commands return promptly and run under a separate worker. Reusing an execution
ID with an identical normalized request returns its original record; changed
arguments are rejected. Resource names are normalized as a set. After a lost
acknowledgement, query the original ID rather than launch again. Missing worker
evidence is `outcome_unknown`; corrupt receipts are errors, never success or
permission to relaunch. Service restart does not itself terminate the worker.

The default timeout remains **600 seconds**. A caller may explicitly request up
to **86,400 seconds**; this does not schedule model reasoning or autonomous work.
The default exclusive lease coordinates a canonical Git worktree or non-Git
directory. Explicit `shared` mode is only a cooperating read-only declaration,
not enforced filesystem isolation. Tests remain exclusive unless genuinely
read-only. Up to 16 stable resource names acquire exclusive leases across
workspaces. Busy resources are reported, never bypassed. Git must be available
for trustworthy workspace identity; no environment selection or installation is
implicit. Locks do not coordinate manual edits or another runtime/state store.

Only the worker writes lifecycle receipts. Cancellation writes a separate durable
request; completed executions keep their original terminal result. An observed
normal completion wins over a later cancellation. Cancellation/timeout clean up
the owned process group, not arbitrary PIDs or independent services, and do not
undo previous side effects. Graceful worker interruption is distinct from failed,
timed-out and cancelled work. Intentionally detached processes, host reboot,
sleep, disk loss and session teardown are not recovery promises. Linux user-scope
and remote-session guarantees remain later SSH/service acceptance, not this slice.

Output rotates through **four 1 MiB segments** without stopping the command.
Readers use absolute byte offsets across rotations; expired offsets report
`output_gap` and resume at the oldest retained byte. Follow `next_offset` even
when a UTF-8 character is split. Full durable output belongs in an explicit
artifact, not this bounded console cache. A zero process exit alone does not mark
a logical task complete.

Format-1 (`0.1.0a1`) receipts and prefix-only logs remain readable without rewriting
history. New receipts identify format 2. Legacy active workers cannot acquire the
new cancellation protocol; their original installed runtime and records must be
preserved. This is read compatibility, not managed upgrade/rollback or permission
to downgrade a format-2 state store. Tasks and automatic resumption remain deferred.

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
