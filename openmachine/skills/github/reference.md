# GitHub operations and verification

[Domain guide](SKILL.md)

On this page:

- [GitHub](#github)
- [Attribution](#attribution)
- [Scoped uncertainty and historical evidence](#scoped-uncertainty-and-historical-evidence)

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

Read organization Projects via github_read actions projects, project, project_fields,
project_items and project_item. The repository owner identifies the organization;
number is the Project number, and project_item uses ref=item node ID. Project lists
use next_cursor (not page). github_project(action="add_item", project_number,
issue_number) adds a repository Issue, or pull_request_number adds a repository
PR, to the organization Project (select exactly one). action="update_field"
takes project_number, item_id, field_id, and one value key: text, number, date,
singleSelectOptionId or iterationId. Iteration choices are returned under
project_fields configuration.iterations. Pass expected_value to detect an observed
field change. Field updates reject draft or other-repository items, and uncertain
Project writes exclude further writes to the same organization Project across
repository aliases. Both writes require scoped permissions and verified readback.

Create a Release document with `github_release(action="create", repository, tag_name,
name, body, draft=True, prerelease=False)`. Notes must be nonempty; the default
is a draft, and `draft=False` explicitly publishes. The operation checks
bounded authorized Release listings (including drafts), rejects used tags or
incomplete absence evidence, retains task markers, and verifies the created
Release ID, tag, notes, flags and acting account via destination readback. A lost
response is not permission to call create again: use the original task/operation
ID and read the original tag. It does not deploy or update an OM runtime.
GitHub may create a missing Git tag at the repository default branch on creation;
check the intended tag/commit before authorizing the call.
No update/delete/upload-assets interface is exposed.

Read Releases with github_read(action="releases") or action="release", ref=exact
tag name; use action="file_history", path=relative document path, optional ref
for a branch/tag, to enumerate past commits. Read an old document with
action="file", ref=the selected full commit SHA, path=same document. REST lists
use numbered pages.

Writes verify all submitted fields, object identity and acting account. Prefer observed
`expected_updated_at` and exact PR HEAD; preflight/write is not atomic CAS, and
legacy omission retains its weaker contract. Use `expected_fields` for every
replaced field when unrelated activity can change `updated_at`; labels/assignees
are complete replacement sets, including explicit empty sets. A supplied timestamp
remains enforced and exact HEAD is independent. Avoid metadata changes without
acceptance value. Pinned creation uses a same-repo branch.

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
original task evidence must be reconciled instead. It does not require a Review.

Inspect complete evidence before an optional exact-head review or authorized merge.
No submitted GitHub Review, human approval, or self-review is required by Open
Machine to merge. An optional COMMENT self-review can document useful findings;
it is not independent approval.
The gate allows at most 1,000 check/review/thread records per family, chooses latest
check ID per app/name and latest status per context, and rejects gaps, current
failed/pending checks, unresolved threads, effective change requests and unmet
native policy. Zero checks is `no_checks`, not PASS.
No bypass, auto-merge, queue or stacked-PR orchestration.
Only `merged_verified` proves the original commit reached the destination branch.

New supervised merges retain their exact HEAD, original destination branch and
hashes of explicitly supplied commit text before dispatch (state contract 19).
The existing explicit task reconciliation can then verify a merged destination
after a lost reply or runtime restart, without recovering the original Chat's
arguments. Later commits on the destination are allowed when the actual merge
commit remains its ancestor. Changed HEAD, destination, supplied text or unavailable
ancestry evidence leaves the original outcome unresolved. No request is resent.
This verifies the requested destination outcome, not which client caused it.
Rebase merges preserve individual source commit messages. Custom `commit_title`
or `commit_message` with `method="rebase"` is rejected before mutation; omit them
instead of expecting a generated merge message.

Passive `read_task` and the local execution observer do not reconcile GitHub.
An explicit `control_task` stage decision observes outstanding original results;
if that observation advances the task revision, read the new revision before
submitting the decision again. Old merges without retained binding still need
the original arguments and operation ID through the existing same-ID path.
A changed base before dispatch is a known-no-effect rejection: re-read the
candidate, assess the intervening changes and current gates, then submit corrected
intent only within existing authority. Never treat a lost response as that case.

Creates are non-idempotent. Preserve original locators; uncertain/unverified
writes and merges require destination reads, never automatic retries or new IDs.
For a verified pre-send connection failure, credential contention or rate limit,
retain the known-no-effect receipt and restore the prerequisite within the same
task's authority and budget. Honor `retry_after_seconds` and, for exhausted primary
quota, `rate_limit_reset_epoch_seconds`. Missing hints grant no immediate retry;
permission denial requires its cause to be fixed. Re-read current guards before
submitting corrected new intent with a new operation ID; never treat an uncertain
write as this no-effect case.

Git working-tree operations stay native execution. HTTPS Git may use a command-scoped
existing `gh auth git-credential` helper without changing global config or remotes.
Never place tokens in URLs/logs or copy controller credentials to another target.
The complete provider remains in `integrations/github/`. The native account owner
is shared by its existing API operations. A native Git credential helper and a general plugin SDK are not included in this increment.

## Attribution

Read `get_capabilities.github.attribution`, the effective `[github].attribution`
boolean (default `true` for public/private repositories). No per-client override.
When enabled, new Issue/PR bodies use:

```markdown
---

Created via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

New top-level comments and review summaries use:

```markdown
---

Posted via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

Let typed writers append these exact footers once; do not add other signatures,
tool/model explanations, versions, badges or slogans. Any separately authorized
route uses the same setting and text, never an authentication/permission bypass.
Verify the destination body and footer before reporting publication. `false`
stops new automatic attribution; preserve existing footers and hidden task markers
on edits. Do not rebrand legacy content, alter unrelated historical publications
or decorate inline review comments. The typed writers share the exact constants in
`integrations/github/operations.py`. Apply a setting change through the authorized
[service configuration lifecycle](../service/management.md#exact-updates-and-compatible-rollback),
not hot reload or a per-client switch.

## Scoped uncertainty and historical evidence

Format 4 separates original operation identity from the resources an effect can
change. The native GitHub writer routes share a repository lease during admission,
dispatch and publication. Retained uncertainty then excludes only conflicting
resources across tasks, anchors and account selectors. Issue/PR metadata shares
the same repository-and-number namespace. The original operation ID still never
dispatches again.

| Unresolved effect | Exclusion retained |
| --- | --- |
| Labels or assignees replacement | The same field; disjoint body/title, reviews and merge can continue |
| PR state or draft transition | Overlapping state/draft changes, submitted reviews and merge |
| Submitted review | The same review collection and dependent merge/state transitions |
| Review body replacement | The same review body; it does not resubmit the review decision |
| Merge | Another merge or dependent state change; omitted commit messages also depend on PR title/body |
| Comment mutation | The same comment or unresolved parent collection; an unchecked parent argument cannot narrow a comment ID |
| Missing GitHub creation destination | A conservative repository exclusion until original evidence identifies the destination |
| Release creation | The exact repository and tag; other Release tags are independent |
| Uncertain execution or partial workspace effect | Conflicting work on its original target; existing worker and workspace leases remain authoritative |

An old operation without a scope stays conservative. The explicit
`bind_effect_scope` control takes its original operation ID, GitHub method, complete
normalized arguments and a reason. It recomputes the original request hash with
the original plan and source hash, derives scope from the actual owner arguments,
and appends an immutable witness. The original operation, locator, outcome and
receipt stay unchanged. A matching request proves scope, not non-execution. No
current destination snapshot can prove a historical request was never dispatched.
The witness input is bounded at 128 KiB; only its fingerprint, compact scope,
principal and timestamp are retained, not raw bodies or parameters.

Scope binding rechecks the current owner, authority, workspace identity and native
grants. A changed local HEAD does not block this evidence-only action; new local
effects and stage acceptance still require the accepted HEAD. Plan revision pins
each unresolved GitHub operation's own repository and native account binding.
Explicit changes to unrelated repositories remain possible under ordinary scope
validation and stage invalidation.

For a retained 0.2.11 task, first inspect the running server's capabilities and
the actual `control_task` schema. The scoped reader supports record formats 1..4;
`bind_effect_scope` is a `control_task.transition.action`, not a separate tool.
The original 0.2.11 runtime lacks this action even when newer source or Skill text
is present. Update through the managed compatibility checks only under explicit
installation authority, then refresh the client's tool catalog. Do not drop
`task_context` to work around an older catalog.

Read the original task and destination before continuing. Resume only under
existing user authority, claim the same task, and retain its original operation ID.
A format-3 record may upgrade through an authorized control without replacing its
original operations. Format-1/2 records cannot enroll historical effect scope;
retain their original receipts and uncertainties. Retired gh history cannot be
transferred to a native account. Supply the exact complete normalized original
arguments to `bind_effect_scope`; if they are unavailable, retain the conservative
scope. To reconcile the outcome, call the original GitHub method with those same
arguments and operation ID: this path reads the destination and never dispatches
again. Scope binding alone leaves the outcome unknown. A missing, changed or
unverifiable destination leaves the original uncertainty intact.

New owner failures retain a failure code and effect classification. An explicit
business preflight rejection is failed/known-no-effect without a reconciling read
that could replace the result. A dispatched or acknowledged write with insufficient
evidence remains unknown. Passive unproven observations preserve the original
failure evidence. Same-ID verified reconciliation never resubmits the request.

Verified historical GitHub results are frozen. They establish the original effect;
subsequent bot comments, timestamps and metadata do not replace their digest.
Current acceptance remains separate: each merge reads the current HEAD/base,
protections, checks, review decisions and discussions. A stage can accept nonempty,
successful evidence independent of all unresolved scopes, while the unresolved
IDs remain visible. Finish still blocks every original unresolved effect.
