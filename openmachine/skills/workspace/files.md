# Read and change workspace files

[Domain guide](SKILL.md)

On this page:

- [Workspace](#workspace)
- [Source windows and discovery](#source-windows-and-discovery)
- [Exact edits and durable writes](#exact-edits-and-durable-writes)
- [Coordinated patches](#coordinated-patches)

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
| Workspace result / MCP body | 512 KiB / 1 MiB, including escaping and duplicated MCP representations |

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

Native writes use one target publisher and canonical workspace lease.
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
*** Update File: notes/old.txt
*** Move to: notes/new.txt
*** Delete File: notes/obsolete.txt
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

## Project catalog

Require `get_capabilities.project_catalog.protocol == 1` before using
`inspect_projects`. This optional local-only reader changes no existing workspace
operation, grant, configuration or persistent state.

Omit `workspace_name` to page configured roots for the named machine. Supply it
to list direct child directories under that approved root; `path` defaults to
`.`. Lists contain metadata only, sorted by workspace name or relative path.
Echo `snapshot` with `next_offset`; changed listings return `source_changed`.
Credential paths, descendant symlinks and non-directories are excluded. A row
with `error` has no usable identity; unavailable Git is never a plain folder.

Use `action="inspect"`, a workspace and a relative path to select a directory.
Retain the exact `(machine, workspace_name, relative_path, target_id)` tuple.
Revalidate it with `expected_target_id` before use; never substitute a new ID
after rejection. The opaque digest binds the configured/canonical root, every
descendant directory's device/inode and Git root/control/common-directory
identities. It excludes branch, HEAD and ordinary content/timestamps. Replacement,
movement, retargeting or unverifiable metadata requires explicit reselection.
This is an observed filesystem identity, not a permanent UUID, bearer grant,
reservation or atomic fence against an uncooperative native process.

Clients must enforce approved target scope and current native authority at
dispatch and result delivery. A parent selection does not authorize a nested
repository/worktree automatically. Existing `workspace_identity` and source/HEAD
guards remain mandatory; `target_id` is not a replacement. Native commands remain
OS-account operations, not a sandbox.

No catalog call registers workspaces. A subfolder can remain bound to its parent,
avoiding overlapping non-Git registrations. When an operation requires an
independent workspace (notably a nested repository), use the existing locally
approved candidate-configuration management update with its confirmation hashes
and original receipt, then select the resulting workspace. A remote selection
cannot approve an external discovery root or widen local grants.

Limits: 50 rows per page, 2,000 raw directory entries per scan (including excluded
entries), 1,024 UTF-8 path bytes, 32 relative components and 64 KiB JSON metadata.
Oversized directories fail explicitly, not with a misleading complete list.
Select a narrower known directory or reduce the page size for a large response.
There is no recursive crawl, content index, background job or ID store.
