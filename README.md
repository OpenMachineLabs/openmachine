# Open Machine

An open-source runtime for AI to operate your machines.

## Current increment

`0.1.0a5` extends the accepted local coding, Skills and task workflow with
configured Linux SSH machines for [Spec #2](https://github.com/OpenMachineLabs/openmachine/issues/2) M3.
The same workspace/file/execution tools use a thin SSH adapter and a versioned
standard-library worker. Tasks and Skills stay on the controller; execution
receipts, logs and locks stay on the target. No model calls, private controller,
remote MCP daemon or public port is introduced.

The optional GitHub body/provenance slice remains unchanged. Complete GitHub
collaboration, managed services, update/rollback, production transition and
v0.1.0 release acceptance remain separate. A source merge or local SDK pass does
not establish actual ChatGPT/SSH acceptance; the PR/Spec records that evidence.

## Install

Use Python 3.12 or newer on macOS or Linux. Run from a version-pinned source
checkout; no public registry name or release is assumed:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.1.0a5-py3-none-any.whl
.venv/bin/openmachine --version
```

The wheel includes the core, direct adapters and bundled Skill, not tests, private
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
for a machine only when native account access is intended. Restart this
service after changing configuration.

Machine/workspace names are operator-owned; see [examples/config.toml](examples/config.toml).
Local and explicitly configured SSH transports are supported. Optional `[github]` configuration lists
exact repositories enabled for the dedicated collaboration tools. Omit `attribution` for
the default policy (public repositories on, private repositories off), or set it explicitly
to `true` / `false`. Open Machine uses the existing `gh` login and stores no GitHub token.
Unknown fields are rejected, not ignored. `doctor` checks configuration and directory
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
| `inspect_skills` | `list/read/files`, exact discovered `skill`, relative resource `path`, query/range and `expected_snapshot`; bundled and explicit user sources only. |
| `read_workspace_file` | Machine/workspace/path, byte offset and optional whole-file SHA256; UTF-8-safe pages, raw hash, Git context and explicit absence. |
| `write_workspace_file` | Machine/workspace/path, complete text, required observed HEAD and SHA256; one verified replacement, not a command. |
| `find_tasks` | Exact task/anchor filters or title/ID keywords, range and expected index generation; candidates never authorize continuation. |
| `read_task` | Original task ID; verified checkpoint, revision and lookup references. |
| `save_task` | Task ID, expected revision, title, checkpoint body and exact anchors; compare-and-save with idempotent last-write retries. |
| `start_execution` | Original 32-hex ID, machine, absolute `cwd`, explicit `argv`; optional timeout, expected HEAD, `workspace_access`, `resource_keys` and an existing `task_id`. |
| `read_execution` | Original machine/ID and absolute byte offset; observed state, exit code, context, bounded output and next offset. |
| `list_executions` | Machine, opaque ID cursor and page size up to 100; receipt summaries and unreadable records, without raw commands/logs. Stable ID order is not chronological or a logical-task lookup. |
| `cancel_execution` | Original machine/ID; records a cancellation request for the owning worker. Read again for the final outcome; requesting cancellation is not proof of termination. |
| `github_issue` | Conditional on `[github]`; create/update an enabled Issue, preserving existing Open Machine attribution on edits. |
| `github_pull_request` | Conditional on `[github]`; create/update an enabled Pull Request with Created via attribution on eligible new artifacts. |
| `github_comment` | Conditional on `[github]`; create/update a top-level Issue/PR comment with Posted via attribution on eligible new posts. |
| `github_review` | Conditional on `[github]`; create an exact-HEAD review summary or update its body; inline review comments are not decorated. |

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
sleep, disk loss and session teardown are not recovery promises. Local execution retains this process-group contract; configured SSH execution uses
the target user-scope lifecycle described below.

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
to downgrade a format-2 state store. Task references default to empty for older receipts; old records are never rewritten. Automatic resumption remains excluded.

## Explicit file editing

For an ordinary source change, read the exact file with `read_workspace_file`,
then write its complete desired text with `write_workspace_file`. Supply the
observed `expected_head_sha` and whole-file `expected_sha256`. Empty HEAD explicitly
means the workspace has no HEAD; empty SHA256 means the file is absent, not empty.
An existing empty file has the normal SHA256 of zero bytes. Both preconditions
are required arguments. Identical destination content returns `already_current`.

Files are at most 128 KiB of UTF-8 text; read pages are at most 32 KiB and retain
character boundaries. Follow `next_offset` until `has_more=false`, echoing the
original SHA256. Do not overwrite a file from incomplete or redacted content.
The fixed 1 MiB MCP request bound accommodates worst-case JSON escaping of a
bounded file; it does not allow a larger file or unbounded commands.

Writes require the selected machine's existing `execution_enabled` setting and
remain inside its named configured workspace. They share the original execution
worktree lock, reject credential/Git-control paths, traversal, descendant symlinks,
hardlinks and special files, and require existing parents. Publication preserves
ordinary file mode, atomically installs the destination, fsyncs it and reads back.
No shell, content execution, deletion, directory creation, permission-change API,
Git mutation or network call is supplied by the file writer. Tests and authorized
Git commands still use `start_execution`.

These are single-file operations, not multi-file transactions or an OS sandbox.
Cooperative locks cannot fence manual editors or hostile same-account processes.
The final hash check and rename are separate operations, not a kernel-supported
compare-and-swap. Pause external writers to the same file; an external write in
that interval may be overwritten despite the earlier checked hash. No portable
mandatory editor fencing is promised by this increment.
`write_outcome_unknown` requires destination readback before any retry. Honest
write/destructive/idempotence annotations describe actual effects; they do not
replace platform approval or guarantee fewer OpenAI blocks. Never disguise or
reroute a refused operation to evade its checks.

## Configured SSH machines

The first SSH target baseline is Linux, Python 3.12+ available as `python3`, Git,
and a reachable systemd 255+ user manager. The controller uses system OpenSSH and
the operator's existing SSH configuration, authentication and verified host keys.
It requests no interactive password, forwards no agent, installs no OS service,
and never changes host keys, privileges, user-session policy or network routes.
A missing prerequisite is an error, not a local or privileged fallback.

Prepare one immutable worker explicitly from the installed controller package:

```sh
openmachine worker-bundle --directory ./workers
```

The returned `<sha256>.pyz` contains only the existing execution, workspace, log
and error owners plus the thin SSH entry, using Python's standard library. Copy
that exact artifact to an operator-selected versioned directory on the target
using your authorized provisioning workflow. Keep its checksum filename and
verify the copied SHA256. Never overwrite a worker still used by an execution.
There is no installation or upload hidden inside ordinary tools or `doctor`.

Add a target with execution disabled, the existing SSH alias and the real archive
path. The checksum below is synthetic; replace it with the returned filename:

```toml
[machines.build]
transport = "ssh"
ssh_alias = "my-build-host"
ssh_worker = "/home/alex/.local/share/openmachine/workers/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.pyz"
execution_enabled = false

[workspaces.remote-project]
machine = "build"
root = "/home/alex/project"
```

Run `openmachine doctor --machine build` to explicitly probe that host. It reports
Python, user-manager availability, remote directory availability and a hashed
`target_identity` derived from the host identity, account and canonical account
home. Initial `identity_verified=false` and a nonzero diagnosis exit are expected.
After verifying the intended existing SSH identity, explicitly copy the returned
hash into that machine's `ssh_identity` and enable execution only when intended.
An identity change rejects the operation before workspace access. Restart the
controller after configuration changes. Default `doctor` never contacts remote
hosts or checks remote paths against the controller filesystem.

Use the same MCP tools with `machine="build"`; no second SSH tool family exists.
File content and command arguments travel as bounded JSON on SSH stdin, not as
interpolated business shell commands. File/HEAD/hash checks and Git inspection run
on the target. Tool annotations disclose configured remote access; refresh
ChatGPT metadata and exercise the affected tools after activation.

Every alias to the same target account uses its canonical account home and one
fixed remote `~/.local/state/openmachine/` store, not an alias-specific directory.
Canonical target identities own execution IDs; canonical worktrees and resource
names own locks. Alternate aliases cannot create a second cooperating writer.
Controller checkpoints are not copied remotely: a new task-linked start verifies
the checkpoint on the controller, then stores only its reference remotely.
Original execution lookup does not depend on a surviving controller checkpoint.

Each execution worker enters its own transient user scope and retains its exact
archive version. SSH disconnects and controller restarts do not own its lifetime.
Cancellation and normal completion clean the entire owned scope, including
children that changed process groups. A durable process receipt is reported as
`finishing` until scope cleanup is observed; a missing user manager is unknown,
not success. Reads never stop units. Reissuing cancellation can request cleanup
of a finishing owned scope. Scope lifetime is bounded by the command timeout
plus 30 seconds, with a two-second systemd stop bound. These are owned scopes,
not a scheduler or installed background agent.

New remote receipts use format 3 so older runtimes reject rather than misread
the scope-cleanup contract. Existing local format-1/2 receipts remain readable;
local writes retain format 2. This is not managed update or downgrade support.
Preserve original archives, receipts and target state. Account-session teardown,
reboot, disk loss, arbitrary same-account tampering and deliberately moving a
process out of its cgroup are not recovery guarantees. Native access and locks
remain a trusted single-operator contract, not a sandbox or multi-controller
security boundary.

## User Skills

Optionally add explicit existing sources to configuration:

```toml
[skills]
roots = ["/absolute/path/to/my-skills"]
```

A source is either a Skill directory with `SKILL.md`, or a collection whose
immediate child directories contain Skills. There is no recursive account or
agent-installation scan. Configure a deeper collection explicitly when needed.
Configured root links and whole-Skill directory links resolve to canonical
identities; duplicate sources are deduplicated, equal names are not. Resources
inside a selected Skill do not follow descendant links or escape its boundary.
Useful dotfiles remain readable while credential paths are excluded.

`inspect_skills` returns source paths, hashes, pagination and warnings. Discovery
inspects at most 2,000 collection entries per call; incomplete or inaccessible
sources are explicit, not a successful empty result. `read` without a selector
still returns the bundled operating Skill. User Skill edits appear on the next
read; changing configuration roots follows the existing service restart flow.
A Skill is instructions/resources, not a tool, credential or grant of authority.

## Durable task continuity

A logical task may refer to multiple executions. One private store owns it:

```text
<state-dir>/tasks/index.json          # lookup metadata and integrity references
<state-dir>/tasks/<task-id>/task.md   # material progress, evidence and next action
```

`save_task` creates at revision zero and updates only the observed revision. It
serializes saves under one stable store lock; an identical retry of the latest
write returns unchanged. Lookup anchors are additive: an omitted or empty list
preserves prior references rather than silently removing task locators. Titles
and exact anchors live only in the index with
revision/hash metadata; execution status and checkpoint prose are not duplicated.
Checkpoints are at most 32 KiB and the index at most 2 MiB. Keep only goals,
exclusions, authorization references, worktree/branch, original execution IDs and
log offsets, reusable evidence, failed approaches and the next safe action.
Never save credentials, hidden reasoning, transcripts or bulk logs/source.

Each file replacement is atomic and durable, but the checkpoint and index are
not a two-file transaction. An interrupted update may leave a mismatch. Missing
indexes or established lock files, orphan directories, changed hashes and
inconsistent generations fail
explicitly; read the original record and reconcile deliberately rather than
initializing over history. The persistent store lock also distinguishes a lost
task directory from a never-initialized store. No automatic repair or replay is
provided. State permissions are private; same-account records are not tamper-proof.

Continuation is deliberate: find/read the original task, recheck current user
instructions, Git and original execution receipts, reuse still-valid results,
then perform only the missing step and save material progress. A keyword result
is a candidate even when unique; exact anchors may be ambiguous. An execution's
optional `task_id` verifies the checkpoint for a new launch but never updates
progress. Existing-ID reconciliation does not depend on a still-readable task
store. Old receipts without the field remain readable without migration.

Save a command's original ID before launch, so a lost acknowledgement can be
reconciled. A changed instruction preserves prior evidence and observes or
cancels the original owned execution, verifying termination before new work.
A checkpoint does not restore hidden chat context, provide new approval, schedule
models or mark a task complete when a process merely exits zero.

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
