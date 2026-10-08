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
is shared by its existing API operations. A native Git credential helper, Projects
management and a general plugin SDK are not included in this increment.

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
