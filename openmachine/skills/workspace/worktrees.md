# Choose and reuse a worktree

[Domain guide](SKILL.md)

## Selection, creation and recovery

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

## Task target binding

Omitting `worktree` retains the configured root. Empty selectors stay absent from
canonical history and request fingerprints. A task does not reserve a checkout
for its lifetime, and `find_tasks` neither searches target paths nor proves tree
ownership. Explicitly authorize `create_worktree` in the current contract and use
its execution ID as the operation ID. A source HEAD advance never substitutes a
different pinned base.

After verified creation or reuse, revise the same task to its observed selector,
identity and HEAD. This material contract change requires all retained stages to
be invalidated under the [plan revision rules](../tasks/control.md#admission-and-outstanding-effects).
Original receipts remain historical; new acceptance needs current-plan evidence.
File selectors and command `cwd` must match the revised target. Unresolved local
effects still block plan replacement; another tree cannot abandon unknown work.
Independent tasks retain existing operation locks without a global checkout-owner
ledger. Older strict readers reject unsupported selectors instead of silently
writing the default root.
