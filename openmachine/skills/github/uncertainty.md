# Scoped GitHub uncertainty and historical evidence

[Domain guide](SKILL.md) | [Common operations](reference.md)

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
| Review thread resolve/unresolve | The exact proven PR thread; merge remains dependent until an audited resolve goal is verified |
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

Older Tasks may lack `control_task`'s `bind_effect_scope` transition:
inspect current schemas and upgrade only with explicit authority. Never omit
`task_context` or rewrite original history.

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
IDs remain visible. Finish blocks unresolved effects except independently verified
resolve goals described below; it never reclassifies their original receipts.

### Audited review-thread goal recovery

When the original resolve acknowledgement is missing, call the existing
`github_review_thread` method with the exact normalized original arguments and
original operation ID. This is an original-result read, never a second mutation.
Core first tries the original acknowledgement. If it cannot establish historical
success, a read-only target check may retain a separate `goal_witness` while the
returned write status and original operation remain unknown.

Only a one-way `resolve` intent with an exactly matching request fingerprint,
source, repository, PR, thread, HEAD and native account binding is eligible. The
read must prove resolved state, stable HEAD/base destination and observer identity.
Missing arguments, unbound legacy identity, unknown `unresolve`, incomplete
provider evidence and conflicting live/unknown effects cannot create this proof.
Another client may have resolved the thread: the witness proves the current goal,
not who performed the original write. Do not describe it as recovered success.

The witness is an immutable, versioned Task evidence event tied to the unchanged
original operation digest. Only Tasks receiving this evidence advance to format 5;
formats 1..4 remain readable without rewriting historical bytes. Compatibility
checks require state contract 24 and format-5 support once that format is retained;
an older reader must refuse it rather than ignore the new semantics.

A witnessed resolve no longer excludes a merge of the same PR, HEAD, base branch
and native binding. It never permits another uncertain write to that same thread.
Merge still independently checks current HEAD/base, CI, reviews, every thread and
GitHub protection rules. Stage decisions and completion re-read the goal under
current authority, source and repository exclusion; reopened threads invalidate
acceptance. Base commit advancement is revalidated on the same destination branch,
not accepted from a historical snapshot. Read `read_task(section="evidence")` for
the witness and `section="operations"` with the original ID for unchanged history.

This path runs only under a valid current claim and authorization. It does not
renew authority, bypass Stop or a foreground end-of-turn boundary, schedule AI
work, or repeat the original write. Independent safe work may continue in the
current turn; an unknown historical result alone is not a reason to ask the user
to repeat an already authorized continuation.
