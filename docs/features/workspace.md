# Workspace files and worktrees

A workspace names an explicitly configured root on one machine. Typed workspace
tools read, search and modify files with source hashes and identity checks. They
are useful for focused code and documentation changes with recoverable receipts.

## Prerequisites and configuration

Register an existing absolute root as shown in
[configuration](../configuration/reference.md). Reads need no command grant.
For typed edits, set `workspace_write_enabled = true` on the selected machine,
then apply the configuration through the appropriate manual or managed workflow.
SSH writes additionally require a prepared worker and verified identity.

## Read and search first

Tool call shapes below are for the connected assistant, not shell commands:

```text
read_workspace_file(machine="local", workspace_name="project",
                    path="README.md", start_line=1, line_count=80)
inspect_workspace(machine="local", workspace_name="project",
                  action="search", path="src", query="TODO")
```

Retain the whole-source `sha256`, observed HEAD and `workspace_identity`. For
subsequent source pages, echo the original hash and use the returned byte cursor
or valid next-line boundary. For search/status pages, echo the snapshot. Literal
search is bounded and creates no index; an incomplete no-match result proves
nothing about unexamined files. Filters support segment `*`, `?` and character
classes, not recursive `**`.

## Make a guarded change

```text
In machine local, workspace project, change the unique line 'Status: draft' in
notes.md to 'Status: reviewed'. Read the file and applicable instructions first.
Preview the exact edit against its observed hash, HEAD and workspace identity.
Apply only that change under this authorization, save its change ID, then read
back the file and verify the new hash. Stop if the source changed or the match
is absent or ambiguous. Do not run commands or commit anything.
```

`edit_workspace_file` previews unique non-overlapping replacements. Whole-file
creation/replacement uses `write_workspace_file`; multi-file changes use
`apply_workspace_patch` with the advertised `om_patch_v1` format. A preview has
`dry_run=true` and no change ID. A durable application has a saved original change
ID, exact preconditions and optionally the approved preview-plan hash. An empty
expected file hash means observed **absence**, not an empty existing file.

Verify `read_workspace_change` and the actual resulting bytes/hash. File receipts
are not test results. A multi-file patch publishes in order, not as a transaction:
on `partial` or `outcome_unknown`, inspect the original receipt and current files
before deciding what remains. Do not retry the whole patch or invent a rollback.

## Choose a worktree when isolation helps

Inspect registered worktrees and their ownership before choosing one. An explicitly
authorized `create_worktree` uses an observed base commit, new destination,
workspace identity and execution ID. Read that original execution to recover
its result. Subsequent reads, edits and command `cwd` must select the new
registered worktree; shell `cd` inside a command does not change its original lock.
Listing a worktree does not authorize using or deleting somebody else's work.

## Limits and troubleshooting

UTF-8 source/result files are bounded to 2 MiB; write/edit/patch payloads to
128 KiB; source pages to 32 KiB. Patches touch at most 32 paths and use explicit
parent creation. Credential paths, Git metadata, unsafe links, special files and
nested repositories are refused. Hash checks are cooperative protection, not a
kernel compare-and-swap against external editors.

The [file guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/workspace/files.md)
and [worktree guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/workspace/worktrees.md)
own exact limits and recovery rules. See also [Tasks](tasks.md) for binding
multi-stage file work to its original evidence.
