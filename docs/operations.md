# Operation reference

[Overview](../README.md) | [Service](service.md) | [Tasks](tasks.md)

## Tools

| Tool | Current input and result |
| --- | --- |
| `get_capabilities` | Version, configuration hash, configured machines/workspaces and implemented limits; health remains `not_probed`. |
| `inspect_workspace` | `machine`, `workspace_name`, `action=files/read/search/status/diff/worktrees`, relative `path`, `offset`, `limit`; search adds literal `query`, diff adds `staged`, paged search/Git reads accept `expected_snapshot`; optional `path_glob` enables filtered file discovery/search. Optional `worktree` selects one authorized registered absolute root. |
| `inspect_skills` | `list/read/files`, exact discovered `skill`, relative resource `path`, query/range and `expected_snapshot`; bundled and explicit user sources only. |
| `read_workspace_file` | Machine/workspace/path, byte offset and optional whole-file SHA256, or `start_line`/`line_count`; 2 MiB source, 32 KiB UTF-8-safe pages, raw hash/context and explicit absence. |
| `write_workspace_file` | Complete bounded text, observed HEAD/hash; optional `change_id` requires `expected_workspace_identity` and retains the original change result. No-ID behavior stays legacy. |
| `edit_workspace_file` | Exact `old_text/new_text` replacements in one original file, observed workspace identity/HEAD/hash; empty ID for `dry_run`, original caller-generated ID for apply; optional approved plan SHA. |
| `apply_workspace_patch` | Strict `om_patch_v1` text, exact path/hash preconditions, observed workspace identity/HEAD; optional bounded parent creation and preview-plan binding. One original change ID records ordered add/update/delete/move effects, not an atomic batch. |
| `read_workspace_change` | Original machine/change ID; historical receipt and owner observation, with optional separate current hashes. Never applies, repairs or initializes history. |
| `find_tasks` | Exact task/anchor filters or title/ID keywords, range and expected index generation; candidates never authorize continuation. |
| `create_task` | Stable task/update IDs, origin, explicit bounded contract and ordered stages; creates a non-runnable canonical record, not an execution grant. |
| `read_task` | Original task ID; canonical record with truthful support/index status, or the verified legacy checkpoint. |
| `save_task` | Task ID, expected revision, title, checkpoint body and exact anchors; compare-and-save with idempotent last-write retries. |
| `start_execution` | Original 32-hex ID, machine, absolute `cwd`, explicit `argv`; optional timeout, expected HEAD, `workspace_access`, `resource_keys`, an existing `task_id`, `termination_grace_seconds`, explicit `tty=false/true` and optional `terminal_size={rows,cols}`. |
| `create_worktree` | Machine/workspace, absent absolute `destination`, explicit local `base_ref`, original 32-hex `execution_id`, source `expected_workspace_identity`; optional absent `new_branch` or detached default, source `worktree` and current `task_context`. Observe and cancel through the existing execution tools. |
| `read_execution` | Original machine/ID, absolute byte offset and optional `wait_seconds=0..5`; observed state, command/stop/cleanup facts, output/presentation flags and next offset. Waiting never changes command lifetime. |
| `list_executions` | Machine, opaque ID cursor and page size up to 100; receipt summaries and unreadable records, without raw commands/logs. Stable ID order is not chronological or a logical-task lookup. |
| `cancel_execution` | Original machine/ID; records a cancellation request for the owning worker. Read again for the final outcome; requesting cancellation is not proof of termination. |
| `control_execution` | Original machine/ID, observed `expected_control_seq`, `action=write_stdin/interrupt/resize`, optional exact `text` or `terminal_size`; one retained control receipt, without consuming output. |
| `github_read` | Conditional on `[github]`; identity/status, scoped objects, pinned source and paged PR/check/discussion observations; never dispatches arbitrary endpoints. |
| `github_issue` | Create/update a scoped Issue, including open/closed state, with field readback and retained provenance. |
| `github_pull_request` | Conditional on `[github]`; create/update an enabled Pull Request or mark an observed Draft PR ready for review, with Created via attribution on eligible new artifacts. |
| `github_comment` | Conditional on `[github]`; create/update a top-level Issue/PR comment with Posted via attribution on eligible new posts. |
| `github_review` | Submit an exact-HEAD summary after complete gate observation, or update its body; self-review is COMMENT, not independent approval. |
| `github_review_thread` | Resolve/unresolve one PR review thread by its observed GitHub thread ID, scoped to an exact PR HEAD with state readback. |
| `github_merge` | Explicit current-HEAD merge after complete checks/discussions and clean native policy; verify the actual destination commit. |
| `control_task` | Current task/plan revisions and epoch, original update ID and a typed claim/release/pause/resume/cancel/plan/decision/finish transition. |

Capabilities report configured behavior, not live health. Machine/workspace names
select existing owners; no alias fallback or implicit permission changes occur.
Supervised effects carry the current context described in [task operations](tasks.md),
without transferring domain ownership.

## Workspace

Read relevant source with `read_workspace_file`; retain the whole-source raw
SHA256, `context.workspace_identity` and HEAD. Identity includes root/branch, not
just commit: same-commit branch switches or root replacement invalidate old intent.
Confirmed non-Git/unborn roots have no HEAD; unavailable Git is not permission to
ignore it. Empty expected SHA means observed absence, not an empty file.

| Limit | Value |
| --- | --- |
| Source and resulting file | 2 MiB, UTF-8 without NUL |
| Full write, combined edit strings or patch | 128 KiB payload |
| Exact replacements / total patch hunks | 64 |
| Patch file paths, counting both move endpoints | 32 |
| Aggregate prepared before/after bytes | 8 MiB |
| Explicit parent creation | 16 directories, eight missing levels per path |
| Read page / diff text | 32 KiB, further bounded by encoded response |
| Change receipt / retained identities | 64 KiB / 10,000; no automatic eviction |
| Workspace result / MCP body or SSH envelope | 512 KiB / 1 MiB, including escaping and duplicated MCP representations |

### Source windows and discovery

Byte mode uses raw `offset`. A 1-based `start_line` requires `offset=0`;
`line_count` defaults to 200 and accepts 1..500. Line numbers are metadata, not
inserted source text. `range_complete` describes the available requested range,
including EOF; `has_more` describes the whole file. Past-EOF returns empty text,
file-size offset and null line bounds. For `line_truncated`, continue in byte mode
using `next_offset` and the original SHA, never skip to the next line. `next_line`
appears only at a known boundary. BOM, CRLF and final-newline bytes are preserved.

Redaction uses bounded complete source context before page/snippet slicing.
Hashes/cursors describe original bytes, not displayed or redacted lengths.
A whole-file hash does not mean a partial source was fully reviewed. Read nested
`AGENTS.md` explicitly; its reported path is not implicit prompt loading.

Listing/status/search pages allow up to 200 entries. Their offsets count entries;
file/diff offsets count bytes. Echo `snapshot` as `expected_snapshot` for search,
Git and filtered discovery. Changed snapshots reject continuation. Unfiltered
listing retains filesystem ordering and its mtime warning. Encoded-page shortening
advances by returned entries only; observations are not atomic transactions.

Literal search accepts a single line of 1..256 characters, scans up to 2,000
entries/8 MiB/1,000 matching lines, and creates no persistent index. Without
`path_glob`, original traversal and its 1 MiB per-file scan remain. A relative
filter enables Git tracked-plus-ordinary-untracked discovery, excluding ignored
and nested-repository content, or traversal in confirmed non-Git roots, with
2 MiB per-file scans. Direct permitted ignored-file reads remain available.
Segment `*`, `?` and character classes are case-sensitive, never cross `/`, and
`**` is unsupported. Filters apply to files/search only, not mutations/read/diff.
Git enumeration failure is not a traversal fallback. Partial scans, clipped names,
changed sources and skipped credentials/links/binary/oversized/unreadable files
are explicit; no matches in incomplete coverage is not proof of absence.
An incomplete final scanned line is omitted to preserve redaction context.

Git capture remains five seconds/1 MiB. Status separates staged/unstaged/untracked
files; `staged=true` diff compares the index, including staged deletions.
Untracked content needs explicit reads. Renames are add/delete; submodule contents
are not inspected. Readers disable optional index writes, pager, fsmonitor,
external diff, textconv and clean/process filters. Configured filters produce a
raw-comparison warning. Narrow the scope when capture is incomplete.

### On-demand worktrees

The AI chooses whether isolation helps the current task. Neither a new Chat, a PR,
nor a dirty status allocates a worktree. Inspect task intent, original changes and
`action=worktrees` first. Reuse a suitable existing tree, continue in the current
tree when its unfinished work belongs to the task, and create only for independent
work that would otherwise interfere. A registry entry is not proof of cleanliness,
ownership or permission to continue another task. Find related tasks through stable
project/PR anchors and read their actual targets before reusing a candidate.

File inspection, reads, writes, exact edits and patches accept an optional absolute
`worktree`. It selects one call's root without changing configuration or another
Chat's directory. The configured source must be a Git top-level root. The selected
directory must be registered to the same Git common directory and already lie in
a configured root on that machine. Subdirectory grants do not expand to a whole
repository; sibling paths receive no implicit grant. Read selected context and
applicable instructions again, and put that selector, identity and HEAD in the
existing task's revised target before editing. Commands use that exact `cwd`.

Creation is one supervised execution with an original ID and fixed Git argv. The
explicit `base_ref` resolves once to a commit already present locally. Choose the
intended PR branch for continuation or an inspected default-branch ref for new
independent work. There is no automatic fetch, branch switching, dirty-file copy
or stash. A nonempty `new_branch` must be absent; empty creates a detached tree.
Destination parents must already exist inside current machine grants, with no
symlink components and outside any existing Git working tree. Creating a nested
tree would write into that tree while bypassing its file lock. Existing or registered
destinations are preserved. The bounded
command disables hooks, fsmonitor, submodule recursion, sparse checkout and up to
eight configured external filter drivers. Conditional Git includes are rejected
because a new tree can activate different executable filters. This operation does
not change Git configuration to resolve that limitation. Bounded worktree operations
also reject partial/promisor repositories before object inspection, preventing
Git from implicitly fetching missing objects.

The runtime locks the destination and a common-Git resource, so a busy source file
lock does not prevent isolation. It holds shared reservations on the destination's
configured parent scopes, excluding their active writers while allowing separate
creations to share a container. Ordinary commands and file writes retain their
existing worktree locks and permissions. Source identity, destination parent and
fixed base are rechecked before launch; success additionally verifies the registered
result and returns `context.created_worktree`. This coordinates OM operations,
not external editors or every possible native Git command. Worktrees share refs
and Git metadata and do not remove later merge conflicts, port or database conflicts.

Retain `execution_id`; identical intent observes the original result even if the
source or ref has since changed. A lost reply, cancellation, partial directory or
failed verification never authorizes a replacement creation. Read the original
execution and inspect the destination. Creation uses receipt format 6; ordinary
commands remain format 5 and previous records remain readable. Task targets with
a selector and typed creation require state contract 17; matching SSH endpoints
use protocol 7. No separate worktree registry, task allocation service or cleanup
daemon is introduced. Explicit safe Git removal remains a native command after
checking task references, uncommitted/unpushed work and current users; there is no
automatic deletion or force removal.

### Exact edits and durable writes

Every `old_text` must have one unique occurrence in the same original bytes,
including overlapping starts. Generated replacement text cannot become a later
match. Reject ambiguous/overlapping spans, stale context/hash or invalid text.
Untouched bytes stay exact; new text is explicit, without normalization.
The whole writer supports absent/empty files and deliberate full replacements.
With `change_id`, strict preconditions and durable receipts apply; without an ID,
legacy `already_current` behavior remains and has no durable recovery guarantee.

Preview requires `dry_run=true` and empty `change_id`; it allocates no record,
directory, staging file or execution. Apply requires a saved 32-lowercase-hex
change ID and may bind the approved `expected_plan_sha256`. All preconditions
are recomputed; preview grants no permission and holds no approval-time lease.
`workspace_write_enabled` is independent of command permission; omitted/null
inherits the legacy command grant. Preview/scoped receipt reads need no write grant.

Use `read_workspace_change` after lost replies. An identical retained request
returns original evidence even after later third-party edits; changed intent
conflicts. `observe_current=true` adds current hashes without changing history.
An abandoned attempted effect stays unknown. Missing/corrupt history does not
prove non-admission. Receipts retain hashes, context and effects, not source backups,
raw edit payloads or reconstructable historical diffs. Deletion loses deduplication.

Local/SSH writes share one target publisher and canonical workspace lease.
Non-Git commands and edits use one unambiguous configured root; aliases and idle
PTYs cannot bypass exclusion. Descendant links, hardlinks, special files, unsafe
paths, nested repositories and credential/Git metadata are refused. Ordinary POSIX
mode is preserved; unsupported special metadata is rejected where detectable,
not copied or elevated. Publication stages adjacent bytes, rechecks source/parent
identity, uses no-clobber creation or checked atomic replacement, syncs and reads
back. Success needs a persisted final receipt. Checks/rename are not kernel CAS
against external editors. No hidden formatter, shell, test, LSP, commit or rollback.

### Coordinated patches

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

Use `om_patch_v1` only when advertised. Preconditions cover exactly every touched
path, including both move endpoints. Control lines use exact LF markers; this is
not unified diff or shell syntax. Update hunks begin with bare `@@`, require
nonempty original context/removals and a real change, and match unique ordered,
non-overlapping original lines. `*** End of File` is exact, without fallback.
No fuzzy/regex/whitespace matching, line hints, move chains/cycles, overwritten
destinations or ambiguous case/Unicode-equivalent path spellings are accepted.
Pure moves may omit hunks; zero-line adds create empty files.

Updates preserve BOM, untouched terminators and final-newline presence. Added
lines use the first source LF/CRLF separator or LF. A formerly unterminated line
becoming interior gains the necessary separator, shown in the diff. Empty output
has no newline; retained BOM remains. Bare-CR updates require exact edits rather
than patches; pure moves preserve bytes. Nonempty adds use LF plus final newline.

`create_parents` defaults false and permits only declared destination ancestors.
Created directories use 0755 subject to umask, with actual mode/identity recorded;
there is no general mkdir/chmod or recursive cleanup. Preflight checks all paths,
bytes and record budgets before effects. Each effect records attempting before IO
and its observation before the next effect. Moves publish/verify the absent
destination before rechecking/removing source; failure may leave both files.

A later failure leaves prior applied effects intact. `partial` is a known applied
prefix with no uncertain attempt; `outcome_unknown` retains known facts but cannot
establish every attempt or receipt durability. Unattempted suffixes stay unattempted.
No automatic rollback/replay occurs. Current `current.files` is not historical
proof. Run validation explicitly, retain change/execution IDs and verify touched
hashes before/after; a file receipt or old test PASS is not current acceptance.

## Execution

Commands run in separate original workers. Save the 32-hex ID before launch.
Identical normalized retries return original records; changed intent conflicts.
Resource names normalize as a set. Missing worker evidence is unknown, corrupt
receipts are errors, and neither allows relaunch. Restarting the controller does
not itself cancel an original command or replace its interpreter/archive.

Timeout defaults to 600 seconds; explicit requests allow up to 86,400 seconds.
Grace is null or a finite 0..30 seconds: null means five seconds for new work and
the original value on retry, including legacy 0.5 seconds. Default pipes have
closed stdin and combined stdout/stderr. Exclusive canonical-workspace leases
remain default; `shared` is only a cooperating read-only declaration, not enforced
isolation. Up to 16 stable named resources coordinate across workspaces.

The worker alone publishes lifecycle facts. Stop requests retain the first reason
and cannot relabel an already-observed natural exit. `stopping` differs from
command-exited-but-cleanup-pending `finishing`. A TERM handler returning zero can
still be cancelled/timed out; launch failure differs from program exit 127.
Output drains during grace and bounded final cleanup. Native containment is the
verified POSIX session (Linux waitid/WNOWAIT, macOS kqueue), not deliberately
detached sessions or an OS sandbox. SSH uses a separate systemd scope boundary.
Unknown ownership/cleanup never becomes success or arbitrary PID adoption.

Output rotates through four 1 MiB segments without killing commands. Absolute raw
byte cursors survive rotation; expired data reports `output_gap` and the oldest
retained offset. Keep full output in explicit producer artifacts, not this cache.
Formats 1..5 and legacy prefix logs remain readable without rewriting; absent
historical facts stay null and old workers retain their original capabilities.

### Observation and terminal controls

`read_execution(wait_seconds=0..5)` waits only for observation. Unread bytes, gaps,
terminal state or unavailable ownership return promptly; a future offset is invalid.
A preparing worker without logs is not automatically lost. Quiet `wait_expired`
is not command timeout. At most 16 waiting readers are admitted without a queue;
immediate reads and cancellation retain independent capacity. Cancelling/disconnecting
a waiter ends only its observation, never the command, lease or another cursor.
Lock contention uses the same finite read budget. SSH carries the wait in one
bounded exchange, not per-poll reconnects or a five-second network guarantee.

Read `output_decode_replaced`, `output_redacted`, `output_controls_escaped` and
`output_presentation_truncated` separately from retention/IO loss. Visible escaping
and replacement mean concatenated display pages need not reconstruct source.
Follow raw `next_offset`; read a smaller page at the same source offset when
presentation omitted consumed bytes. Results/SSH envelopes are bounded to 1 MiB,
including JSON escaping and MCP text/structured duplication. Mandatory metadata
overflow is an error, not a silently incomplete success.

`tty=true` explicitly requests a worker-owned controlling terminal; dimensions
are integer rows/cols 1..1000, default 24x80. Pipe mode rejects dimensions.
Initial mode/size are immutable start identity; resize changes current size only.
PTY failure has no pipe fallback. Read `control_ready`, supported actions, current
sequence and last receipt before control. Old workers are not adopted. Overlong
control paths reject PTY before launch; pipes may report controls unavailable.

Controls require the observed sequence (0..2^53-2) and one action: `write_stdin`
sends 1..16384 UTF-8 bytes without NUL/surrogates on PTY, adding no newline;
`resize` takes dimensions only; `interrupt` takes neither and signals the verified
foreground group without cancelling the execution. Pipe controls support interrupt
only. Launch consent is not blanket consent for later input; never send secrets
or automate confirmations. Input fingerprints omit bodies, but terminal echo can
enter retained logs. An idle PTY retains workspace/resource leases.

One private socket carries 128 KiB control frames with a two-second worker budget.
One pending intent is durable before effects, and only its last receipt is retained.
Exact duplicates return retained evidence; conflicts/retired sequences never replay.
Applied means OS acceptance, not application completion. Partial input is a known
prefix, not permission to send a suffix. Lost replies reconcile by original ID and
sequence; unresolved effects block new control but preserve authorized cancellation.
Slow controls do not block output/timeout/cancel. No shared temporary socket fallback,
terminal UI, control takeover or restoration of dead-process memory is promised.

## Account connections

Connection protocol **2** provides one runtime-owned native account lifecycle on
existing private MCP. New accounts use GitHub App Device Flow; `existing_gh`
onboarding and the gh request backend are retired. Runtime requests require a
selected native connection and an explicit transport; there is no CLI fallback.
Saved gh connections, login attempts, task bindings and receipts remain readable.
Their stored status describes history, not current execution support; capabilities
report `legacy_gh: retired`. Requests through an old binding or an unbound historical
GitHub task fail with `integration_legacy_connection` before provider dispatch.
No account migration, automatic reconnection or mutation replay occurs.

Repository CI may still use GitHub CLI with its own Actions credentials. This is
independent of runtime account connections, as is an operator's Git credential helper.

| Operation | Contract |
| --- | --- |
| `list_integrations` | Passive supported methods, saved accounts, observation time and renewal-owner state; not a live provider health probe. |
| `connect_integration` | Original 32-hex request ID, `provider=github`, `method=device_code`, optional original native connection ID for reauthorization. Returns the public GitHub URL/code; repeat requests read the same attempt. |
| `read_integration_login` | Original login ID; no network access, refresh or restart. |
| `cancel_integration_login` | Cancel only an unfinished attempt; do not disconnect an already established account or claim provider revocation. |
| `disconnect_integration` | Connection ID and observed authorization revision. Persist the disabled binding before owned credential cleanup; report cleanup failure separately. |

OpenMachineLabs owns the official Open Machine GitHub App once for all ordinary
users. Its public client ID is built into OM; users do not register an App or supply
OAuth configuration. The official App keeps **Device Flow** and **Expire user
authorization tokens** enabled. No client secret, App private key or PAT import is
needed. App installation/resource approval and user consent are separate. OM returns
the official installation URL alongside Device Flow so users install the same public
App on each target account or organization instead of creating their own App. Users
complete the returned URL/code on any device; this runtime needs no browser or GUI.
Do not claim repository access merely because user identity was verified: existing
GitHub operations still check configured and provider resource permissions.

Credentials stay in their automatically selected pinned backend: macOS system
Keychain, otherwise OM's private file store. File mode uses private 0700 directories
and 0600 atomic files; permissions are not encryption. Unavailable or locked Keychain
never silently falls back. Credential references,
private device codes and tokens do not enter public results, logs or task records.

The existing Runtime owns at most four login workers. Caller disconnection does
not cancel admitted work. Successful connections survive compatible restarts;
interrupted unfinished attempts report failure without reconstructing temporary
secrets. Snapshot reads suffice; there is no new event bus, UI, server or executor.
Saved metadata is bounded to 32 accounts, 128 attempts and 128 KiB. Full/corrupt
stores are explicit failures, not automatic deletion or replay of old request IDs.

Account selection is stable per operation/task. No global active token is changed.
Per-account coordination serializes refresh and disconnect; new logins use unique
credential slots, so late results cannot overwrite another account or undo a
cancel/disconnect. New work accepts native bindings only; old task identities and
serialized evidence are not migrated to a different authentication backend.

Normal requests refresh access tokens near expiry. The same owner checks refresh
grant deadlines every six hours and renews within the final 30 days, including
otherwise idle accounts. No high-frequency keepalive or always-open network
connection is needed. GitHub currently returns eight-hour access tokens and
six-month rotating refresh grants; routine successful rotation renews both.

The original credential is marked before exchange, then the complete replacement
is persisted and identity-verified before use. A lost rotating-token response or
uncommitted replacement is not silently replayed. Definitive revocation, expired
grants, prolonged shutdown/offline periods, host loss and inaccessible storage can
still require reauthorization. This is renewable authentication, not a promise of
permanent unrevokable credentials. A confirmed 401 permits one bounded auth retry;
uncertain business writes still require the existing destination reconciliation.

## GitHub

Use native GitHub authentication with explicit exact grants:

```toml
[github]
account = "my-account"
read_repositories = ["owner/reference"]
write_repositories = ["owner/project"]
read_owners = []
write_owners = []
attribution = true
```

Write grants include reads; empty scope grants none. Owner/repository matching
is case-insensitive and exact, not wildcard/substring. Legacy `repositories`
cannot mix with explicit read/write scope fields. Account pinning checks API
identity, not Git author metadata; no automatic login, account switch, credentials
copy or Git-config repair occurs. Missing GitHub must not block local/SSH work.

`doctor --github [--repository owner/project]` explicitly probes authentication
and access; capabilities remain unprobed metadata. Read PR head/base before paged
files/reviews/comments/threads/merge context, echo both revisions on continuation,
and use a full commit ref for checks. Follow next-page/cursor and coverage flags.
Read separate review-comment pages for thread bodies. Missing API patches require
complete pinned files or native Git, not a claim that no change occurred.

Pinned file reads verify tree modes/blob bytes, excluding symlinks, submodules,
credentials and binary data. Pages are 16 KiB UTF-8-safe with full hash/cursors,
sources up to 1 MiB. Provider API/results are bounded to 2 MiB/256 KiB. The PR file
API's 3,000-file ceiling and partial check/review/thread evidence remain explicit.

Writes verify all submitted fields, object identity and acting account. Prefer observed
`expected_updated_at` and exact PR HEAD; preflight/write is not atomic CAS, and
legacy omission retains its weaker contract. Pinned creation uses a same-repo branch.

`github_pull_request(action="ready_for_review")` requires a positive PR number
and exact observed `expected_head_sha`; `expected_updated_at` is optional. It
accepts no title, body, state, branch or metadata edits. The operation checks that
the PR is open and Draft before calling GitHub's fixed
[`markPullRequestReadyForReview`](https://docs.github.com/en/graphql/reference/pulls#markpullrequestreadyforreview)
mutation. GitHub accepts only the PR node ID, with no conditional HEAD or timestamp
input. A concurrent push can therefore take effect before the transition; this
operation does not atomically pin or approve a commit. A successful readback must
still match the observed HEAD, PR node and actor and show an open, non-Draft PR.
Changed or unavailable readback remains unverified, even if the transition already
occurred; reconcile the original destination without replaying the write.

For a review conversation, first read `github_read(action="threads")` and inspect
its finding. `github_review_thread(action="resolve"|"unresolve", repository,
number, thread_id, expected_head_sha, expected_base_sha)` requires the PR to
remain open, verifies thread membership and the opposite current state, then
uses GitHub's specific GraphQL mutation. The resulting state, PR HEAD and actor
are checked again. An uncertain response is not safe to retry automatically;
original task evidence must be reconciled instead. This does not waive any review.

Review complete evidence before an exact-head review and authorized merge.
The gate allows at most 1,000 check/review/thread records per family, chooses latest
check ID per app/name and latest status per context, and rejects gaps, current
failed/pending checks, unresolved threads, effective change requests and unmet
native policy. Zero checks is `no_checks`, not PASS. Self-review COMMENT is not
independent approval. No bypass, auto-merge, queue or stacked-PR orchestration.
Only `merged_verified` proves the original commit reached the destination branch.

Creates are non-idempotent. Preserve original locators; uncertain/unverified
writes and merges require destination reads, never automatic retries or new IDs.
Git working-tree operations stay native execution. HTTPS Git may use a command-scoped
existing `gh auth git-credential` helper without changing global config or remotes.
Never place tokens in URLs/logs or copy controller credentials to another target.
The complete provider remains in `integrations/github/`. The native account owner
is shared by its existing API operations. A native Git credential helper, Projects
management and a general plugin SDK are not included in this increment.

### Attribution

`[github].attribution` is the only watermark setting: a strict boolean, default
`true` for public and private repositories. `false` stops automatic footers on
new bodies. Read its effective value at `get_capabilities.github.attribution`.

Issue/PR bodies use `Created via`; top-level comments and review summaries use
`Posted via`. The [packaged Skill](../skills/openmachine/SKILL.md#github-attribution)
owns the exact text and caller rules; the writers share the two constants in
`integrations/github/operations.py`.

Updates preserve existing footers even when disabled, without branding legacy
content. The switch neither removes nor rewrites caller-supplied text.

Apply changes through the [service configuration lifecycle](service.md), not hot
reload. A future Desktop switch must reuse this boolean and lifecycle; no GUI or
separate settings API is provided here.

## SSH

The supported target baseline is Linux, Python 3.12+ as `python3`, Git and an
available systemd 255+ user manager. The controller uses existing system OpenSSH,
authentication and verified host keys, with no password prompt or agent forwarding.
No privilege, host-key, account-session, route or daemon changes are implicit.

```sh
openmachine worker-bundle --directory ./workers
```

Explicitly copy the returned checksum-named `.pyz` to an operator-selected versioned
target path and verify its SHA256. The curated archive contains only target-capable
standard-library code. Never overwrite an archive used by original work.
Configuration begins read-only; replace the synthetic checksum and actual paths:

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

`doctor --machine build` probes Python, manager, directories and hashed host/account/
canonical-home identity. Initial `identity_verified=false` is expected; verify the
intended target before setting `ssh_identity` and enabling the intended grant.
Restart after configuration changes. Default doctor never contacts hosts or resolves
their paths on the controller. Unavailable identity is not local fallback.

Use the same tools with the configured machine. Arguments travel as bounded JSON
on SSH stdin, not shell interpolation. Hashes, Git checks and file publication run
on the target. Every alias to the same target account shares canonical home/state,
worktree locks and original IDs. Checkpoints stay on the controller; new task-linked
starts validate them there, while original-ID recovery does not require that
checkpoint to survive. No worker upload/install is hidden inside reads or probes.

Workers retain their archive in transient user scopes. SSH disconnect/controller
restart does not own their lifetime. Cancellation and completion clean the owned
scope, including child process groups. `finishing` persists until cleanup is
observed; unavailable scope/manager evidence is unknown. Reads do not stop units;
repeated authorized cancellation may request pending scope cleanup. Scope lifetime
is command timeout plus effective grace and 30 seconds; systemd stop budget is
grace plus seven seconds. Transport uses one attempt, ten-second connection and
30-second exchange budgets, not hard real-time guarantees under kernel/IO stalls.

The user manager must outlive the intended command (existing login or established
lingering policy). A probe during login does not prove last-login survival.
OM never enables lingering or reconstructs work after manager teardown, reboot,
disk loss, account tampering or deliberate cgroup escape. Preserve original target
state, archives and interpreters. Scope observations enrich returned copies, not
old worker receipts. New calls use matching protocol-7 endpoints; updates retain
original compatible read/cancel routes rather than silently provisioning them.

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
An empty read selector returns the bundled `SKILL.md`. Use `action=files` for its
packaged resources and `action=read, path="worktrees.md"` for the detailed Worktree
workflow. Only the listed bundled files are exposed. User edits are live
on the next read; changing configured roots follows service restart. Instructions
never confer grants, install tools or execute embedded resources automatically.

## Codex browser delegation

`start_codex_task` delegates an explicit browser/UI subtask, not the whole Chat
workflow. Supply a local configured `machine`, workspace `cwd`, original
`execution_id`, bounded `goal`, `start_url` and exact `allowed_origins`. `mode`
is browser-only; `browser` is auto/iab/chrome/edge. Timeout is 1-1800 seconds.
The existing local execution grant, lifecycle gate, task claim and native workspace
scope apply. Structured contracts include `start_execution` (the underlying
operation) and use the same operation/execution ID; no second task record exists.

The adapter discovers the enabled native `node_repl` bridge in CODEX_HOME/config.toml
and reads the browser Skill from that bridge's installed desktop-app resources.
It uses `codex` on PATH when present; a packaged service without the developer's
shell PATH uses `codex-cli` beside that same configured native bridge, either as
an executable or the directory containing `CodexCLI.app/Contents/MacOS/codex`.
It does not scan other apps, install anything, copy browser libraries,
read authentication files, or change configuration. A compatible native desktop
bridge and a signed-in Codex installation are prerequisites, not supplied by CLI
installation alone. The child uses an empty temporary instruction home and
`project_doc_max_bytes=0`, so global and project AGENTS are not inherited. Only an
original private file-backed `auth.json` is referenced by a symlink; the adapter
never reads or copies its contents. Native Codex owns authentication and renewal.
The temporary home is removed on ordinary completion without deleting the original
auth file. Missing or unsafe file-backed login (including keyring-only setups)
blocks this adapter; it never changes credential stores or creates a new login.
Discovery metadata is passive; actual setup may be blocked.
Only the configured native browser bridge is delegated; shell, connectors,
subagents, memory and other plugins are disabled for this child. Native bridge
access is not constrained by the CLI's read-only shell sandbox. Navigation origins
are instructions plus observed checks, not a firewall or cross-chat isolation.

Use `read_codex_task` for semantic status and final native page evidence. Generic
`read_execution` success only says the wrapper finished; Chat reviews the report
before accepting a stage. `blocked`/`needs_confirmation` require the indicated
resolution, never implicit permission. Missing or mismatched evidence is unknown.
`cancel_execution`, original log/readback, timeouts and the `browser:codex` resource
lease are reused. The lease coordinates this OM runtime only, not manual browsing
or independent Codex jobs. Cancellation is not rollback of browser side effects.
No automatic Chat resume, desktop computer mode or alternative backend is added.
