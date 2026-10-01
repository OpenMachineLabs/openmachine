# Open Machine

An open-source runtime for AI to operate your machines.

## v0.2.3

Open Machine v0.2.3 delivers the MCP SDK 2.2.0 migration from
[Spec #32](https://github.com/OpenMachineLabs/openmachine/issues/32): MCP
`2026-07-28` support with retained `2025-11-25` compatibility, public `MCPServer`
integration, preserved tool contracts, and verified safe stop -> upgrade -> resume continuity.

Sources/results are bounded to 2 MiB per file; full-content/edit/patch payloads
remain 128 KiB. Single-file receipts retain format 1, patches use format 2, and
current readers accept both. State contract 5 and SSH protocol 6 are compatibility
contracts, not release versions. Execution retains format 5 and opt-in PTY control.
Inspect installed capabilities; a checkout or version label alone is not evidence
that the client exposes the current tools.

Version `0.2.3` is an initial-development release, not a stable 1.x API promise.
Spec #32 records completed installed/native/client acceptance, including the
refreshed ChatGPT path. Live SSH migration, Events and Connection Layer work
remain outside this release.

Managed services support logged-in macOS controllers. Linux supports manual
`serve` and configured SSH workers; managed Linux services and Windows are not
included. Native execution is disabled by default and is **not a sandbox** once
enabled. Returned files and logs travel to ChatGPT through OpenAI.

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. Build from the exact release tag, not an unpinned default branch:

```sh
git clone --branch v0.2.3 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.3-py3-none-any.whl
.venv/bin/openmachine --version
```

Release notes use a compact Changes/Changelog format and do not attach installers
or wheels. Build from the exact tag with its committed lock file as shown above;
no PyPI, Homebrew or npm publication is implied.

The wheel includes the core, direct adapters and bundled Skill, not tests, private
state or arbitrary checkout files. Workers use the installed package, never a
source-checkout import. Current source pins MCP SDK 2.2.0, HTTPX2 2.13.1,
JSON Schema 4.26.0, Pydantic 2.13.5, Uvicorn 0.38.0 and Hatchling 1.27.0 for
building. See [requirements.lock](requirements.lock) for the complete locked set.
v0.2.3 is the first tagged release on MCP SDK 2.2.0. The v0.2.2 tag remains
on SDK 1.30.0 and retains its own matching lock.

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
files and leaves execution **disabled**. W1 `init` also explicitly sets
`workspace_write_enabled = false`. Enable `execution_enabled = true` only for
intended native commands, and `workspace_write_enabled = true` only for typed
file edits. Existing configurations omitting the new field inherit their old
execution grant; an explicit false overrides that inheritance. Restart this
service after an authorized configuration change.

Machine/workspace names are operator-owned; see [examples/config.toml](examples/config.toml).
Local and explicitly configured SSH transports are supported. Optional `[github]`
configuration enables the scoped collaboration tools described below. Omit it to
keep GitHub disabled. Open Machine uses the existing `gh` login and stores no token.
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

## Manage the macOS service

Build the wheel above using the matching committed `requirements.lock`. Choose a
short, existing local workspace with a harmless readable file, then initialize
configuration as above. Do not start manual `serve` on the same state. From the
checkout, install the already-built artifact and register its file-read probe:

```sh
.venv/bin/openmachine service install \
  --wheel "$PWD/dist/openmachine-0.2.3-py3-none-any.whl" \
  --requirements "$PWD/requirements.lock" \
  --probe-workspace project --probe-path AGENTS.md
.venv/bin/openmachine service start
.venv/bin/openmachine service status
.venv/bin/openmachine doctor --service
.venv/bin/openmachine service restart
.venv/bin/openmachine service stop
```

Global absolute `--config` and `--state-dir` overrides precede the command. Install
validates but does not start the service or enable execution. Start/restart require
MCP initialization, advertised version/configuration/process identity and the
registered file read to succeed. The probe returns its hash, not file contents.
A service that cannot access that file is not ready even when its process is alive.
Use a stable existing probe file; editing its contents is allowed, deleting it makes
the service unready. The probe must name a configured **local** workspace.

The user LaunchAgent is `~/Library/LaunchAgents/co.openmachine.runtime.<state-id>.plist`.
Its state-derived label prevents separate configured instances from controlling
each other. The stable launcher and immutable environments live below
`<state-dir>/service/`; `service.json` owns the selected/previous installation,
probe, unfinished activation and exact configuration-snapshot references used by
E1 transitions. Configuration, user Skills,
`tasks/`, `executions/`, `workspace-changes/` and their locks retain their owning
capabilities and locations.
The launcher uses the selected base Python at installation time; preserve that
interpreter as well as the version environments. Do not move virtual environments.

`launchd` owns service supervision. Stop removes the loaded job, not its installed
LaunchAgent: it can start again at the next GUI login. The service inherits the
explicitly captured installation `PATH`, not shell startup files or copied
credentials; required Git/gh/SSH utilities must be accessible there. The service
log is private at `<state-dir>/service/service.log`; inspect it locally without
pasting credentials or raw user data. It is not the bounded execution-log store.
No system daemon, privilege grant, tunnel service or remote MCP daemon is installed.

### Update and roll back

Use a trusted local wheel and its matching fully pinned requirements, never an
unqualified `latest` or an in-place upgrade of a running environment:

```sh
.venv/bin/openmachine update \
  --wheel /absolute/path/to/new/openmachine-VERSION-py3-none-any.whl \
  --requirements /absolute/path/to/new/requirements.lock
.venv/bin/openmachine rollback
.venv/bin/openmachine doctor --service
```

Preparation snapshots the selected artifacts, creates a distinct environment at
its final path, checks dependencies and installed version identity, validates the
existing configuration/records with the candidate's own readers, and exercises a
private MCP/file-read probe. It never imports a source checkout or private Brain.
An unhealthy candidate is rejected before stopping the healthy service. Activation
then pauses new cooperating MCP mutations, verifies state again, records its intent,
switches the service and checks the actual managed instance. A failed activation
restores and verifies the prior instance or reports `recovery_required`; it never
claims that rollback succeeded without readback. Update/rollback activate the
selected service, including when it was previously stopped.

Already-started local workers keep their original interpreter, package, process
and receipt. SSH workers keep their original archive and target-side records.
Version environments, including failed candidates, are retained; there is no
automatic cleanup, remote provisioning, workload restart or mutation replay.
Cooperative locks are not a sandbox or a fence against external native commands.
A brief MCP disconnect is possible; reconnect and read the same task/execution IDs.
Do not repeat a test, commit or external write merely because a reply was lost.

For an upgrade from `v0.1.0` to `v0.2.0`, new SSH requests require a matching
`0.2.0` worker. Run `worker-bundle` from the newly installed release, explicitly
copy the checksum-named archive to each enabled target, update its `ssh_worker`
path, and verify the pinned target with `doctor --machine NAME`. Retain previous
archives and interpreters for original executions. Controller update and rollback
never provision or replace remote workers automatically; a rollback also requires
a worker matching the selected controller for new calls.

W1 keeps lifecycle protocol 1 and advances state contract to 4. The transition-capable
CLI recognizes contracts 1/2/3/4/5, but numbers alone never prove compatibility:
the candidate's actual readers must validate retained state. Readiness uses the
selected installation's contract. Retained workspace change history blocks a
candidate without its reader, even if an old check silently ignores that directory.
Completed execution/change receipts still count. Never delete history to permit a
downgrade; `0.1.0a6` and earlier are not managed rollback targets.

W2 uses strict SSH protocol 5 for its read selectors and larger-source semantics;
W1 used protocol 4. The record shape remains format 1. An actual W1 reader rejects
retained ranges beyond 128 KiB before a transition; no receipt is rewritten to
make old software pass. Explicitly prepare the matching endpoint alongside
old worker archives. For a managed upgrade that changes endpoint paths, leave the
active configuration untouched and pass `update --candidate-config /absolute/new.toml`.
The existing service owner snapshots the exact old/new bytes; it validates the
candidate's local and target readers under admission before selecting them. An
offline, unprepared, unpinned or unreadable target defers the transition. Successful
activation selects the explicit configuration; failed activation restores the
actual pre-transition bytes and verifies the old route, including valid edits
since the last activation. A conflicting edit observed during the transition is
preserved with `recovery_required`; these checks do not fence uncooperative editors. Retain the
private `service/configurations/` snapshots with both immutable installations.
Use the transition-capable CLI for these transitions; an old CLI may reject them.

W3 writes patch receipts as format 2 while preserving format-1 single-file
receipts. The actual candidate must read every retained format; a writer-format
number alone is not coverage. A compatible W2 candidate may read a format-1-only
store, but its actual reader rejects format-2 history before service stop.
No existing receipt is rewritten. W3 new calls require an explicitly prepared
protocol-6 endpoint; this source change does not deploy or replace any endpoint.

Before selecting an older controller, the current installed reader also checks
original pinned targets for retained workspace changes and incompatible live
non-Git leases. An unavailable proof blocks the transition before service stop.
Idle PTYs retain their leases; a legacy subdirectory lease must finish under its
original owner before switching to the configured-root mapping. Independent
workspaces remain independent. Edit-only target probing does not require the
execution-specific systemd user manager.

No state migration, remote provisioning or worker relaunch is performed. Rollback
does not undo user work or GitHub effects. An identical update is a no-op, not a
service restart; changing configuration alone is not disguised as such an update.

After an update or restart, read capabilities and the original execution before
sending more terminal input. Retain its output cursor and inspect its current
control sequence/last receipt; a lost reply is not permission to resend input.
A compatible controller can continue a live original PTY, but cannot reconstruct
one after worker/host loss. The old worker's interpreter/archive must remain
available even when a different controller installation is selected.

### Diagnose and recover

| Observation | Required action |
| --- | --- |
| GUI user domain unavailable | Log in as the owning macOS user. No `sudo`, system daemon or silent session/permission change is attempted. |
| MCP works but the probe read fails | Verify the configured path and grant only the required macOS Files and Folders / Full Disk Access permission manually to the responsible executable, or use an explicitly selected accessible workspace. Restart and verify the actual read. |
| Socket left after an abrupt exit | Managed startup acquires the listener lock and removes only a verified owner-only dead socket. A live, changing or unverifiable socket is preserved. Manual `serve` still refuses stale paths. |
| `activation_failed_restored` | The update failed and the previous instance was verified again. Inspect the candidate failure; do not replay the workload. |
| `activation_pending` / `recovery_required` | Preserve `service.json`, both installations and original execution records. Inspect `service status`, then explicitly run `rollback`; it verifies and restores the original target. New MCP writes remain paused until recovery succeeds. |
| Incompatible or corrupt persisted data | Preserve the original records and runtime. Repair the identified inconsistency or use a known compatible candidate; never delete history to make a downgrade pass. |
| Incomplete installation or changed LaunchAgent/launcher | Inspect the original setup and owned paths; installation never overwrites them. Reconcile only task-owned incomplete setup artifacts after verifying no active worker or service uses them. |
| Runtime ready, but ChatGPT unavailable | Diagnose the separately managed official tunnel and actual client connection. Refresh changed tool metadata and retest affected calls; local readiness is not client acceptance. |

There is no automatic recovery from host reboot, sleep, user-session teardown,
base-interpreter removal, disk loss or same-account tampering. For SSH targets,
retain the accepted Linux/Python 3.12+/Git/systemd 255+ user-manager requirements;
this increment grants no session persistence or OS privileges. CI exercises local
contracts without private machines; real macOS service and tunnel acceptance must
be recorded separately against the candidate revision.

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
| `inspect_workspace` | `machine`, `workspace_name`, `action=files/read/search/status/diff`, relative `path`, `offset`, `limit`; search adds literal `query`, diff adds `staged`, paged search/Git reads accept `expected_snapshot`; optional `path_glob` enables filtered file discovery/search. |
| `inspect_skills` | `list/read/files`, exact discovered `skill`, relative resource `path`, query/range and `expected_snapshot`; bundled and explicit user sources only. |
| `read_workspace_file` | Machine/workspace/path, byte offset and optional whole-file SHA256, or `start_line`/`line_count`; 2 MiB source, 32 KiB UTF-8-safe pages, raw hash/context and explicit absence. |
| `write_workspace_file` | Complete bounded text, observed HEAD/hash; optional `change_id` requires `expected_workspace_identity` and retains the original change result. No-ID behavior stays legacy. |
| `edit_workspace_file` | Exact `old_text/new_text` replacements in one original file, observed workspace identity/HEAD/hash; empty ID for `dry_run`, original caller-generated ID for apply; optional approved plan SHA. |
| `apply_workspace_patch` | Strict `om_patch_v1` text, exact path/hash preconditions, observed workspace identity/HEAD; optional bounded parent creation and preview-plan binding. One original change ID records ordered add/update/delete/move effects, not an atomic batch. |
| `read_workspace_change` | Original machine/change ID; historical receipt and owner observation, with optional separate current hashes. Never applies, repairs or initializes history. |
| `find_tasks` | Exact task/anchor filters or title/ID keywords, range and expected index generation; candidates never authorize continuation. |
| `read_task` | Original task ID; verified checkpoint, revision and lookup references. |
| `save_task` | Task ID, expected revision, title, checkpoint body and exact anchors; compare-and-save with idempotent last-write retries. |
| `start_execution` | Original 32-hex ID, machine, absolute `cwd`, explicit `argv`; optional timeout, expected HEAD, `workspace_access`, `resource_keys`, an existing `task_id`, `termination_grace_seconds`, explicit `tty=false/true` and optional `terminal_size={rows,cols}`. |
| `read_execution` | Original machine/ID, absolute byte offset and optional `wait_seconds=0..5`; observed state, command/stop/cleanup facts, output/presentation flags and next offset. Waiting never changes command lifetime. |
| `list_executions` | Machine, opaque ID cursor and page size up to 100; receipt summaries and unreadable records, without raw commands/logs. Stable ID order is not chronological or a logical-task lookup. |
| `cancel_execution` | Original machine/ID; records a cancellation request for the owning worker. Read again for the final outcome; requesting cancellation is not proof of termination. |
| `control_execution` | Original machine/ID, observed `expected_control_seq`, `action=write_stdin/interrupt/resize`, optional exact `text` or `terminal_size`; one retained control receipt, without consuming output. |
| `github_read` | Conditional on `[github]`; identity/status, scoped objects, pinned source and paged PR/check/discussion observations; never dispatches arbitrary endpoints. |
| `github_issue` | Create/update a scoped Issue, including open/closed state, with field readback and retained provenance. |
| `github_pull_request` | Conditional on `[github]`; create/update an enabled Pull Request with Created via attribution on eligible new artifacts. |
| `github_comment` | Conditional on `[github]`; create/update a top-level Issue/PR comment with Posted via attribution on eligible new posts. |
| `github_review` | Submit an exact-HEAD summary after complete gate observation, or update its body; self-review is COMMENT, not independent approval. |
| `github_merge` | Explicit current-HEAD merge after complete checks/discussions and clean native policy; verify the actual destination commit. |

From ChatGPT, read capabilities, the operating Skill and the disposable project's
`AGENTS.md`. Inspect Git status, search and read the relevant source. Use a
typed edit or coordinated patch for the authorized file change, then a separate
execution to run the project's test command with its selected interpreter. Inspect the original
receipts and actual exit/output, review both staged and unstaged diffs as needed,
and read back the changed files. Preserve unrelated dirty work. Local SDK tests
prove the protocol workflow, not this account-bound ChatGPT acceptance. Record
the exact tested source and refresh changed tool metadata in a new conversation.

The [operating Skill](skills/openmachine/SKILL.md#complete-the-authorized-delivery)
explains how callers finish already-authorized work without routine handback.
Task-read `resume_authorized: false` means the read grants no authority, not a
revocation of current user consent. Recognized preflight rejections with no file
effects include a derived `recovery_hint`; existing receipts and guards are
unchanged. This guidance does not implement Spec #29 or restart ended AI turns.

### Workspace inspection bounds

Workspace file/diff pages return at most 32 KiB of text. Listing/status/search
pages contain up to 200 entries; offsets still count entries except file/diff
offsets, which count raw bytes. Final encoded response bounds can shorten an
entry page, but its next offset counts only returned entries. Echo `snapshot`
as `expected_snapshot` between search/status/diff and filtered-discovery pages;
a changed snapshot rejects continuation. Unfiltered listing retains filesystem
order and its mtime warning. Observations are not atomic filesystem transactions.

Literal search scans at most 2,000 entries, 8 MiB and 1,000 matching lines per call.
Omitting `path_glob` retains the existing traversal selection and 1 MiB per-file
scan limit. An explicit relative filter enables Git tracked-plus-ordinary-untracked
discovery (excluding ignored source), or bounded traversal for a confirmed non-Git
root, and a 2 MiB per-file scan limit. Direct permitted reads of ignored files
remain available. Git enumeration failure is not a traversal fallback. Filtered
results are sorted workspace-relative file names; nested repositories/submodules
need a separately configured workspace. `*`, `?` and character classes match each
path segment case-sensitively; `*` never crosses `/`, and `**` is unsupported.
For example, `path_glob="openmachine/workspace/*.py"` narrows files or literal search,
not mutation paths. A path filter is invalid for read/status/diff actions.

Search shares one backend and creates no index. Skipped credentials, links,
binary/oversized/unreadable sources and unsupported names are explicit;
`complete=false` is not a complete negative search. A clipped final logical line
is omitted rather than exposing a snippet without its redaction context. Source
changes, byte/entry limits and clipped NUL-delimited names remain incomplete.
Git commands keep the five-second/1 MiB capture bound. Narrow the path/filter
when capture is incomplete; paging a captured prefix does not finish enumeration.

### Focused source windows

`read_workspace_file` observes a bounded complete source locally, hashes original
bytes and returns only the selected page. `start_line=null` retains raw-byte mode.
A 1-based `start_line` requires `offset=0`; `line_count` defaults to 200 and accepts
1..500. Returned start/end lines and requested range are metadata, never prefixes
inserted into source text. The legacy inspection `read` action uses this same
guarded 2 MiB source reader and 32 KiB pages, retaining its raw offset/chunk hash
fields; it is not an unbounded large-file or binary reader.

`range_complete` means all available bytes of the requested range were covered,
including when that range reaches EOF. A request beyond EOF returns empty text,
raw offset equal to file size and null actual line bounds. `has_more` independently
means bytes remain in the source. An oversized line returns `line_truncated=true`;
continue in byte mode with `next_offset` and the same `expected_sha256`, not the
next line number. `next_line` appears only at a known line boundary with bytes
remaining. UTF-8, BOM, CRLF and final-newline bytes are not normalized.

Redaction is best effort, applied using complete bounded source context before
page/snippet slicing, including multiline key blocks. Source hashes and cursors
never use redacted-text lengths. Response clipping cannot silently skip raw source
or captured hits. Source text remains partial even when its whole-file hash is
known. Read applicable nested `AGENTS.md` paths reported in context explicitly;
those instructions are not automatically loaded or executed.

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

Only the worker publishes command/lifecycle facts. Cancellation writes the same
durable request; repetitions neither extend grace nor relabel an observed natural
exit. E1 distinguishes `stopping`, command-exited-but-cleanup-pending `finishing`,
and the final outcome. A program handling TERM and returning zero remains
`cancelled`/`timed_out` when that reason initiated stopping; `exit_code=0` is not
by itself execution success. Launch failure is distinct from a program exiting 127.

`termination_grace_seconds` is null or a finite number from 0 to 30. New executions
resolve null to **5 seconds**. Identical-ID retries resolve null to the original
value, including **0.5 seconds** for legacy workers; an explicit different value
conflicts. Pipes remain the default, with closed stdin and combined stdout/stderr.
The worker drains output during TERM grace, then forces remaining owned work and
bounds final observation/drain. Command runtime excludes this bounded cleanup.

Native E1 workers retain an unreaped child identity while signaling verified
process groups in its POSIX session; Linux uses waitid/WNOWAIT and macOS uses
kqueue. The native session is not a sandbox: deliberate new sessions and external
supervisors can escape it. The SSH systemd scope is a different containment boundary.
Unknown ownership or cleanup is reported, not converted into success or a PID
adoption. Disk-log failure is an execution infrastructure error; retention gaps and
bounded output incompleteness remain separate from a known command exit. Stop is
not undo, and process completion is not logical-task completion.

Output rotates through **four 1 MiB segments** without stopping the command.
Readers use absolute byte offsets across rotations; expired offsets report
`output_gap` and resume at the oldest retained byte. Follow `next_offset` even
when a UTF-8 character is split. Full durable output belongs in an explicit
artifact, not this bounded console cache. A zero process exit alone does not mark
a logical task complete.

### Bounded observation (E2)

`read_execution(..., wait_seconds=0)` remains an immediate snapshot. A finite
wait from 0 to 5 seconds can wait for unread output or meaningful state changes,
including a zero-output exit. Existing unread data, retention gaps, terminal
state and unavailable ownership return without an additional observation wait;
a future offset is invalid. A preparing worker without a log segment is not
mistaken for a lost worker. `wait_expired=true` means only that no wake condition
was observed before this read's deadline, never that the command timed out.

Waiting runs outside the MCP event loop. Each server admits at most 16 concurrent
waiting reads; excess requests return `reader_busy` without a persistent queue.
Immediate reads do not consume waiting slots; command cancellation has its own
per-request executor rather than competing with immediate reads. Cancelling
an observer releases its own resources when its actual work ends; it never sends
command cancellation, changes leases, or advances another reader's cursor.
Receipt/output lock contention shares the observation's finite budget and can
return `observation_busy`, without rewriting state. The zero-wait path retains a
bounded IO budget. Filesystem/kernel stalls are not hard real-time guarantees.

On SSH, one request carries the wait to the target's same execution reader;
there is no per-poll reconnection. Cancelling that observation ends only the
owned SSH exchange. Network/identity/serialization time remains part of the
separate 30-second transport budget, not a five-second end-to-end latency promise.
Prepare the matching immutable endpoint explicitly and preserve original worker
archives; a newer reader does not replace or relaunch an old worker.

`offset` and `next_offset` always count original source bytes. Presentation reports
`output_decode_replaced`, `output_redacted`, `output_controls_escaped`, and
`output_presentation_truncated` separately from `output_gap`/`output_incomplete`.
Split or invalid UTF-8 may display replacement characters; concatenated display
pages are not promised lossless. ESC, C0/C1 controls and carriage-return overwrite
are escaped visibly; normal newlines/tabs remain. Raw retained logs are unchanged,
and existing redaction is best effort, not universal secret filtering.

Execution MCP results and SSH envelopes are bounded to one MiB, measuring JSON
escaping and MCP's text/structured representations rather than raw log size.
Presentation may shorten output without changing consumed source cursors; an
oversized metadata-only result returns `response_limit`. A truncation flag means
some consumed bytes were not displayed. Read a smaller page from the same source
offset when needed; producer-owned artifacts remain the full-output authority.
MCP request IDs and HTTP headers are outside the execution-result size contract.
E2 adds no event subscription, automatic task continuation or terminal emulator.
E3 extends the persisted contract explicitly as described below.

New local and SSH executions write format 5. Original formats 1/2/3/4 and prefix
logs remain readable without rewriting. Format-4 lifecycle facts remain intact;
facts missing in formats 1/2/3 are null rather than invented; legacy workers keep their original cancellation
capability and guarantees. The original runtime/archive remains their owner.
All execution IDs, lock names, task references and raw-byte log cursors retain
their existing meaning. There is no automatic resume or control takeover.

### Interactive execution (E3)

Set `tty=true` to create a real controlling terminal owned by the original worker,
not by the Chat/MCP or SSH connection. `terminal_size` defaults to `{rows:24,cols:80}`;
both dimensions must be integers from 1 to 1000. Dimensions with `tty=false` are
invalid. Pipe/PTY mode and initial dimensions belong to immutable start identity;
resize changes only the current terminal. PTY failure never silently falls back.

Read the original execution's `control_ready`, `control_actions`, `control.seq`,
last receipt and current `terminal_size` before interacting. A new worker can be
preparing and return `control_not_ready` without reserving a sequence. Old workers
return `unsupported_operation`; a newer controller does not adopt their PIDs or
replace their runtime. Pipe workers with a usable control path support interrupt
only. An overlong AF_UNIX path rejects explicit PTY before launch; default pipes
can run with controls explicitly unavailable. No shared `/tmp` fallback is used.

Call `control_execution` with the observed `expected_control_seq` (0 through
2^53-2). `write_stdin` accepts 1..16384 UTF-8 bytes without NUL or unpaired
surrogates, on PTY only. It adds no newline, shell quoting, normalization or
confirmation. `resize` requires dimensions and no text. `interrupt` accepts
neither; it sends SIGINT to the verified original foreground group (or the pipe
command group). It neither cancels the execution nor guarantees that the program
exits. A REPL can keep its state and continue. There is no portable PTY half-close
operation; control characters are deliberate input, not a guaranteed EOF API.

The worker serializes controls and persists one pending intent in `record.json`
before any effect. Exact retained duplicates return the original result, even
after command exit/socket removal. A conflicting same-sequence payload fails;
older retired sequences return `control_result_unavailable` and never execute.
Only the last receipt is retained, not an input journal. `applied` means bytes
accepted by the OS or a successful signal/ioctl, not application-level completion.
`partial` reports the known byte prefix; do not automatically send its suffix.
A lost response is reconciled by the original ID/sequence. A crash after possible
effect leaves explicit uncertainty, not permission to type or signal again.
Subsequent input/resize is blocked while a control is unresolved; cancellation
remains separately available when original ownership can be verified.

One private owner-only socket carries bounded 128 KiB frames. Framing/effects use
a two-second worker deadline; at most one control is admitted, with no persistent
queue or extra daemon. The local response budget and SSH's 30-second exchange
budget are distinct, not hard real-time guarantees. Slow/partial input does not
block output draining, timeout or cancellation. Reads keep independent raw-byte
cursors and wake on control-state changes; they never consume a shared buffer.

Launching a terminal is not blanket approval for later input. Apply current user
and platform authorization to every control; never automate confirmations or
send credentials. Input bodies are not stored in control receipts, but a program
may echo them into retained output. Hashes/redaction are not secret protection.
An idle terminal keeps its workspace/resource leases; use a separately authorized
independent worktree rather than weakening them. Disconnects preserve a live
worker; actual worker/host death cannot restore terminal memory. No terminal UI,
autonomous resume, MCP Events, new model loop or deployment is included.

## GitHub collaboration

Enable the developer connection using the existing local `gh` account:

```toml
[github]
account = "my-account" # Optional pin; a mismatch fails without switching identities.
read_repositories = ["owner/reference"]
write_repositories = ["owner/project"]
read_owners = []
write_owners = []
# attribution = true
```

Write grants include reads. Owners can be personal accounts or organizations;
matching is case-insensitive and exact, never a substring or wildcard. An empty
scope grants no repository access. Legacy `repositories = ["owner/project"]`
retains its exact read/write meaning, but cannot be mixed with the new scope
fields. Migrate explicitly; omitting a write grant must not widen permission.
The optional account pin checks the actual API actor, not Git author metadata.

```sh
openmachine --config /absolute/path/config.toml doctor --github
openmachine --config /absolute/path/config.toml doctor --github --repository owner/project
```

These are real probes, unlike `get_capabilities`, which reports `not_probed`.
Missing CLI, authentication, access, target identity and network failures are
distinct. Missing GitHub never prevents local or SSH tools from working. Do not
switch `gh` accounts during an operation: before/after checks detect observed
identity drift, not an atomic credential lease. OM neither copies credentials
nor automatically logs in, changes scopes, switches accounts or repairs Git config.

Read `pull_request` first. Candidate `files`, `reviews`, `review_comments`,
`threads` and `merge_context` require its exact `expected_head_sha`; echo both
HEAD and base on subsequent pages. `checks` takes a full commit SHA in `ref`.
Follow `next_page` or `next_cursor`; an empty partial result is not proof of
absence. Thread results include resolution state and a first-comment locator;
read the separately paged `review_comments` for full inline discussion bodies.
Ordinary Issue/PR conversation comments use `comments`.

API patches may be missing or incomplete even when their field is present.
Review complete relevant base/head files with `file`, or use native Git through
existing execution. `file` accepts an exact commit and relative text path, checks
Git tree modes and blob bytes, and rejects credential paths, symlinks, submodules
and binary data. It returns 16 KiB UTF-8-safe byte pages with full raw SHA256,
`next_offset` and `has_more`, for sources up to 1 MiB. API responses are bounded
to 2 MiB and returned observations to 256 KiB after JSON escaping. An oversized
result requires a narrower read, not an invented success. GitHub's PR file API
has a 3,000-file cap; larger candidates expose an explicit coverage gap.

Create/update operations preserve the existing provenance policy: default on
for public repositories and off for private ones; `attribution` overrides it.
Updates preserve an existing footer without branding older artifacts. Readback
checks all submitted fields, object identity, review commit/event and the acting
account. Optional `expected_updated_at` detects stale Issue/PR/comment edits;
this preflight and the API write are not atomic CAS. Legacy calls without this
field retain their previous weaker edit guarantee. Prefer `expected_head_sha`
for PR creation/updates; pinned creation uses a same-repository branch.

The delivery loop is instructions -> inspected change -> tests -> native
commit/push -> PR -> full diff/review context -> submitted HEAD-bound review ->
authorized merge -> destination readback. `merge_context` is read-only. The gate
pages up to 1,000 check, review or thread records per evidence family and rejects
incomplete evidence. It selects the latest check ID per app/name and latest
status ID per context, rather than failing on superseded attempts. Zero actual
checks is `no_checks`, not an unknown or passing CI run. Any current non-passing
check, unresolved thread, effective change request or unmet review requirement
blocks merge. GitHub must independently report clean, mergeable, non-draft policy.

A current-HEAD submitted COMMENT may record authorized self-review; it never
satisfies a required independent approval by itself. A review can describe known
check failures, but cannot be submitted with missing gate evidence. Native branch
protections remain authoritative. No bypass flags, auto-merge, merge queues or
stacked-PR orchestration are implemented. Only `merged_verified` confirms that
the PR's returned merge commit is actually present on the destination branch;
sequential observations do not constitute an atomic GitHub transaction.

Creates are not idempotent. Record intent and the original Issue/PR/comment or
review locator in the existing task checkpoint. On `write_outcome_unknown`,
`write_unverified`, `merge_outcome_unknown` or `merge_unverified`, read the actual
destination before any retry. OM never automatically replays external writes and
adds no second task ledger or publication queue.

Local Git remains native execution. An operator choosing HTTPS Git can reuse the
same existing `gh` connection with a command-scoped credential helper, for example:

```sh
git -c credential.helper= -c 'credential.https://github.com.helper=!gh auth git-credential' push origin HEAD
```

This does not change global Git configuration or rewrite an existing remote.
Never place tokens in remotes, arguments or logs. SSH-connected machines need
their own explicitly authorized Git access; do not copy controller credentials.

The complete provider lives in `integrations/github/`. Its small facade exposes
the existing operations to the application; MCP declares schemas and serializes
results. Future Desktop/headless callers use the same application operations,
not a second GitHub stack. The native GitHub App connection and GUI
remain [Spec #11](https://github.com/OpenMachineLabs/openmachine/issues/11) follow-ups.
This increment is the existing-identity bridge, not one-click native onboarding,
Projects management, a general plugin SDK or a claim that Desktop has shipped.

## Explicit file editing

W1 adds focused edits without resending the whole file. Read relevant source with
`read_workspace_file`, and retain its complete raw-byte `sha256`, `context.head_sha`
and versioned `context.workspace_identity`. The latter identifies the root and
branch, not just its current commit: switching between two branches at the same
commit invalidates old edit intent. Confirmed non-Git and unborn workspaces have
no HEAD; unavailable Git identity is not permission to ignore it.

Call `edit_workspace_file` with `edits=[{"old_text":"return 41","new_text":"return 42"}]`
and the three observed preconditions. Every old fragment must occur exactly once,
including overlapping occurrences. All 1..64 replacements locate the same original
bytes; generated text cannot become a later match. Ambiguity, overlaps, stale
context/source and unsupported paths fail before publication. Untouched bytes,
BOM, line endings and final-newline presence are preserved; new text is explicit.

For preview, use `dry_run=true` and an empty `change_id`. It creates no record,
staging file, directory or command. It returns a bounded proposed diff and plan
SHA, not write authority. For apply, retain a new 32-lowercase-hex `change_id` before
calling, optionally echoing the approved `expected_plan_sha256`. The complete plan
is revalidated; no lock is held while a person reviews the preview.

After a lost reply, use `read_workspace_change(machine, change_id)`. An identical
retained request returns original evidence; a changed request conflicts. Later
third-party content is never restored by an old retry. `observe_current=true`
adds current hashes separately without rewriting historical facts. An inactive
writer with an attempted but unobserved effect is unknown, not success or a replay
request. Missing/corrupt history is not evidence that nothing happened.

`write_workspace_file` remains available for empty files, new files and intentional
whole replacements. An empty source SHA means absence, not empty content. With a
change ID it uses the same durable publisher and strict preconditions. Without an
ID it retains the legacy `already_current` shortcut and explicitly has no durable
recovery guarantee. Never reconstruct a whole file from partial/redacted pages.
Byte cursors retain their units; follow `next_offset` with the original hash.

The machine's `workspace_write_enabled` independently controls typed editing:
true allows it without commands, false denies it even when commands are enabled,
and omitted/null inherits `execution_enabled` for existing configurations.
Preview and scoped historical reads require no write grant. New example/init
configurations explicitly disable both grants. Editing scripts is not a sandbox
for commands that may later execute them; platform approval remains independent.

W2 bounds sources/results to 2 MiB while complete text or combined edit strings
remain 128 KiB. Read pages/diffs stay 32 KiB, records 64 KiB and encoded workspace
responses 512 KiB, including duplicated MCP representations. Each target retains at most
10,000 change identities without automatic eviction. The existing 1 MiB transport
limit still applies. Receipts contain context, request/plan fingerprints and actual
effects, not raw edit payloads or a source archive. Historical diffs are not
reconstructed from live Git state. Retain original IDs; deleting history loses
its deduplication protection.

Local and SSH edits share one target-side publisher and the original worktree
lease. Non-Git commands and edits use the unambiguous configured root. Descendant
links, hardlinks, special files, credential/Git-control paths and nested repositories
are refused. Single-file writers require existing parents; patches may explicitly
create only their declared destination ancestors. Ordinary POSIX mode is retained;
unsupported special metadata is rejected where detected, not copied or elevated.
Publication stages an adjacent file, rechecks bytes/context/parent attachment,
uses no-clobber creation or atomic replacement, then syncs and reads back. A saved
terminal receipt is required for success. Failed or uncertain publication keeps
its original ID and known effects; it never silently retries or rolls back.

These checks are not a kernel CAS against external writers. Pause competing
editors; hash checking and rename remain separate syscalls. There is no formatter,
LSP, shell, implicit test, automatic commit, recursive directory deletion or
multi-file transaction. Run validation explicitly through execution, bind touched post-hashes
and the execution/change IDs in the existing task, and verify applicability after
source drift. File application is not a test PASS or completed task. Actual client,
live SSH and managed-transition evidence remain separate W4 acceptance.

### Coordinated patches (W3)

Use `apply_workspace_patch` only when capabilities advertise `om_patch_v1`.
One call selects one machine and workspace. `preconditions` must cover exactly
all touched file paths, including both move endpoints: a complete raw SHA256 for
an existing file, or `""` for an observed absence. Empty files have the SHA256 of
zero bytes; they are not absent. All original files, resulting bytes, path
relationships, parents and receipt budgets are checked before target effects.

```text
*** Begin Patch
*** Update File: src/example.py
@@
-answer = 41
+answer = 42
*** Add File: test/expected.txt
+42
*** Update File: docs/old.txt
*** Move to: docs/new.txt
*** Delete File: docs/obsolete.txt
*** End Patch
```

This is a strict string grammar, not unified diff, a shell command or `git apply`.
Control lines use LF and exact markers. Update hunks begin with bare `@@`, need
nonempty original context/removals and at least one addition/removal, and match
uniquely against the same original source. Hunks must be source-ordered and not
overlap. `*** End of File` requires an exact EOF match, without fallback. No
whitespace guessing, regex, Unicode punctuation conversion or line-number hints.
A pure move may omit hunks; add may contain zero payload lines for an empty file.
Each file path participates once; move chains/cycles, overwriting destinations,
file/parent conflicts and case/Unicode-equivalent spellings are rejected.

Updates retain the source BOM, untouched line terminators and final-newline
presence. New/replaced lines use the first existing LF/CRLF separator, or LF when
none exists. A retained formerly unterminated final line must gain a separator
when it becomes interior; the proposed diff includes that byte change. Empty
output has no newline and a retained BOM is not removed. Bare-CR separators are
unsupported for patch updates; exact edits and pure byte-preserving moves remain
available. Nonempty adds use LF and a final newline.

Patch text is limited to 128 KiB, each source/result to 2 MiB, all prepared
before/after bytes to 8 MiB, touched file paths to 32 and hunks to 64.
`create_parents=true` permits at most 16 missing destination ancestors and eight
missing levels per path. The default is false. Directories use mode 0755 subject
to the native umask; their actual identity/mode and separate effects are recorded.
There is no general mkdir/chmod interface or automatic directory removal.

`dry_run=true` requires an empty change ID and creates no target directory,
staging artifact, change store or execution. It returns a plan hash and bounded
proposed diff, with the same record budget checked as apply. Applying uses a
saved caller-generated change ID and may bind `expected_plan_sha256`; all source
preconditions are still checked again.

Publication follows declared operation order. Each effect records `attempting`
before IO and its verified observation before the next effect. A move publishes
and verifies its absent destination before rechecking/removing the original;
ordinary source mode/executable bits are preserved. A failure can leave both
paths. Created directories and removal effects are not hidden in a move flag.
A later failure leaves verified earlier effects in place: `partial` means a known
applied prefix with no uncertain attempted effect, while `outcome_unknown` retains
any known prefix but cannot establish at least one attempt or receipt durability.
Later operations remain unattempted. There is no automatic rollback or replay.

Read the original ID after any lost response. Same-ID identical calls return
original history, even after later user edits. For patch receipts,
`observe_current=true` adds bounded `current.files` observations separately from
historical effects. Partial/unknown replies never label the whole proposal as an
applied diff. Hash-only history cannot reconstruct a historical diff or undo files.
Explicit execution remains responsible for tests; patch application is not a test
PASS. Actual refreshed Chat, live SSH transport and native managed transitions
remain separate W4 gates.

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
workspace_write_enabled = false

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
plus effective grace and 30 seconds; systemd's stop budget is grace plus seven
seconds. Preparation, force observation, final drain and publication have finite
budgets, not hard real-time guarantees under kernel/storage stalls. These are
owned scopes, not a scheduler or an installed background agent.

New remote receipts use format 5 and identify `systemd_scope` independently of the
format number. Original format-3/4 workers retain their original scope behavior.
A returned scope observation enriches a copy, never the original worker receipt.
The systemd user manager must remain active for the intended command lifetime,
for example through an existing login or an operator-established lingering policy.
A successful probe during an SSH login does not prove that lifetime: with lingering
disabled, ending the last login can shut down the manager and its scopes. OM does
not change login/linger policy or promise recovery after that shutdown. Tests must
state how this prerequisite was maintained separately from the execution's own
SSH connection, which may disconnect and reconnect.

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
listener publicly or treat a tunnel ID as a complete access policy. Open Machine does not
automatically migrate production workloads or grant OS permissions.

## Development and review

The runtime has one application boundary, local capabilities and explicit integrations:

| Package | Ownership |
| --- | --- |
| `openmachine/app/` | Typed operations, shared lifecycle admission, configured local/SSH routing and controller task-reference checks. |
| `openmachine/bootstrap.py` | Assemble only configured components without probing, executing work or creating state. |
| `integrations/ssh/` | Bounded controller transport, identity-checked target endpoint and immutable worker archives. |
| `integrations/github/` | One gh connection backend, bounded API pagination, pinned review/merge evidence and scoped collaboration with destination reconciliation. |
| `openmachine/execution/` | Local execution admission and observations; pipe/PTY lifecycle and bounded control IPC; persisted record validation and logs. |
| `openmachine/workspace/` | Canonical Git identity/capture; inspection and search results; scoped file reads and guarded publication. |
| `openmachine/tasks/` | Lock-coordinated checkpoint saves/reads; index metadata and checkpoint integrity. |
| `openmachine/foundation/` | Shared filesystem primitives, durable writes, cooperative locks and safe errors; no domain schemas or policy. |

Execution uses `workspace.git` for canonical identity. Task storage depends on
its index validation, never the reverse. Package facades expose the existing
operations; workers retain `python -m openmachine.execution`. These internal
paths do not change MCP/CLI contracts, persisted locations, lock identities or
save ordering. The checkpoint/index pair is still not a two-file transaction.

MCP operations and explicit CLI integration probes use the application assembled
by `bootstrap.py`. Every application mutation acquires the existing shared
lifecycle lease and selected-runtime guard once around dispatch, not for the
background command's lifetime. CLI service management still enters `service.py`
directly. Reads and already-running workers retain their existing behavior.

Machine routing is controller-owned. Local execution/workspace code never calls
SSH; the target endpoint invokes those same local APIs. Original execution IDs
are reconciled before a new task reference is required. A remote pre-read does
not replace the target's final normalized-request comparison. Checkpoints stay
on the controller; files, hashes, worktree locks and execution receipts stay on
the selected target. Transport loss never causes local fallback or replay.

The curated archive contains the endpoint and dependency-free target closure,
not the SSH client, app, controller tasks, service or optional provider code.
Disabled integrations are not imported by application assembly. GitHub's
`operations.py` owns collaboration and provenance, `evidence.py` owns pinned
sources and merge gates, `client.py` owns REST/GraphQL pagination, and
`connection.py` owns the single authenticated gh request and account/grant checks.
Dependencies point toward connection, never back to application or entrypoints.
There is no second executor, task store, login stack or public plugin/DI framework.

The installed-runtime tests enforce explicit dependency ownership and an acyclic
source import graph, including relative imports, local imports and re-exports.
External callers use capability facades rather than worker or record internals;
CLI bundle/serve dispatch and the SSH worker process entry remain explicit.
For a new module, identify its current caller, state owner and allowed dependencies
before adding it. Static checks complement, rather than replace, installed startup,
real workflow and version-transition evidence.

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
