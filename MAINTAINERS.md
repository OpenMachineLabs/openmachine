# Maintainer guide

This document preserves the repository's internal design, governance, CI, and
publication rules. External contributors should start with
[CONTRIBUTING.md](CONTRIBUTING.md); the approval and safety boundaries below
still apply to all changes.

## Repository boundary

The Spec index binds approved implementation slices. Add files, dependencies
and tests only for the current approved behavior. The current runtime uses
Python; additional languages or runtime scope require explicit approval.
Keep all repository-authored content in English, including commit and PR bodies.

## Design and research

| Work | Title | Purpose |
| --- | --- | --- |
| Spec | `[SPEC] <domain>(<scope>): <outcome>` | An implementable design delta. |
| Feature request | `[REQUEST] <domain>(<scope>): <outcome>` | A community-proposed capability or usability improvement. |
| Bug report | `[BUG] <domain>(<scope>): <problem>` | An observed defect with reproduction details. |
| Survey / Research | `[SURVEY] research(<scope>): <question>` | Investigation, comparison, or synthesis. |
| Experiment | `[SURVEY] experiment(<scope>): <question>` | Bounded measurements and their conclusions. |
| Benchmark | `[BENCHMARK] eval(<scope>): <comparison>` | A reproducible comparison under a frozen protocol. |
| Roadmap | `[ROADMAP] <name>` | Long-term direction across separately approved Specs and tasks. |
| Ordinary task | `<domain>(<scope>): <outcome>` | A bounded bug, improvement, or maintenance task. |

Use the Issue templates. Research is covered by **Survey**; do not create a
parallel `[RESEARCH]` workflow or a separate results Issue for the same question.
A Survey records sources and revisions, facts, inference, unknowns, a budget,
a stopping condition, and conclusions. `keep`, `adapt`, `reject`, and `unknown`
are valid conclusions when supported; an unstarted investigation is not complete.
A Benchmark additionally freezes inputs, candidates, scorer, metrics and
denominators, environment, and invalidation rules before comparison.

A Spec states requirements, non-goals, affected interfaces and paths, acceptance,
important failure behavior, open questions, and stopping conditions. Mark
irrelevant fields as not applicable rather than inventing architecture. It gains
design authority only when a maintainer explicitly approves its scope and its
row in [spec/README.md](spec/README.md) records `direct_issue_contract` and the
approval link. A complete current user design may authorize a small change
without another Spec. Never invent an approval or a placeholder Issue number.

Keep Spec and Survey bodies in GitHub Issues; the repository holds only their
stable index. Add `spec/<issue-number>/` only for necessary design attachments;
product code must not consume these attachments as runtime configuration.
After acceptance and merge, code and tests own implemented behavior. Subsequent
Specs describe deltas rather than permanently mirroring the code.

Keep design, implementation, and acceptance status distinct. Use `Refs #N` for
partial work; use `Closes #N` only when all scoped acceptance is met and closure
is authorized. Historical `[Program]` Issue titles remain valid when those Issues are edited or
reopened; new Issue submissions must use `[ROADMAP]`.

A Roadmap, Survey, Benchmark, or Feature Request does not
authorize implementation, production changes, expensive runs, or purchases.

## Git and review

Commit subjects, PR titles, and ordinary Issue titles use printable ASCII:

```text
<domain>(<scope>): <concrete English outcome>
```

Use lowercase kebab-case for domain and scope. Choose a meaningful domain such
as `governance`, `docs`, `ci`, `test`, `architecture`, `runtime`, `security`, or
`refactor`; `eval` is reserved for Benchmark Issues. Do not use vague outcomes
such as "update" or append a period. Subjects are at most 72 characters; Issue
and PR titles are at most 100. The bracketed prefixes above are Issue-only.
A maintainer-authorized squash uses `<PR title> (#<number>)`, at most 72 characters.

Use `agent/<domain>-<scope>-<outcome>` for a task branch. Keep related changes
reviewable and atomic; do not rewrite published history to meet a commit count.
Commit bodies briefly state `Why`, `Scope`, `Validation`, `Boundary`, and `Lane`,
with actual results rather than expected PASS claims. Use the PR template as
presentation guidance and link the design or identify the current user request
as the design source. Explain the problem and outcome, scope, actual validation,
and material risks in the form that fits the change. Fixed PR section names are
advisory; inspect the actual candidate instead. Retain one `- Issue: #N` field when
linking a Spec so the post-merge Development automation can find it.

Inspect the entire candidate and its validation evidence, not only its final
commit. Internal Open Machine review evidence is separate from the repository's
GitHub approval policy. An optional COMMENT self-review cannot satisfy approval.

### Trusted maintainer approval gate

The immutable GitHub user IDs 70309026 (LuozhuZhang) and 191080179 (bella-chai)
are the only trusted maintainer reviewers. Their own PRs from branches in this
repository need no human approval. Every other PR, including a fork opened by a
maintainer, needs an APPROVED review from at least one of these IDs for its exact
current HEAD commit. An approval of an older HEAD or a dismissed review is not
valid. An outstanding trusted CHANGES_REQUESTED review blocks the gate.

The default-branch workflow publishes commit status om/trusted-review-gate.
GitHub Rulesets must require that status from the GitHub Actions integration,
as well as the independent repository/runtime/metadata CI checks. Configure
zero ordinary required approvals and disable required Code Owner review: otherwise
maintainer-owned PRs would be forced into unnecessary human review. Never put
maintainers or bots on a broad bypass list. A separate unprivileged PR/review
workflow only sends a signal; a privileged default-branch workflow independently
re-reads GitHub's PR author, HEAD and reviews. A 15-minute scheduled sweep
reconciles missed events, and workflow_dispatch can re-evaluate one PR. External
PR code, workflow definitions, artifacts, descriptions and labels are never
executed or accepted as review authority in the privileged job.

Review exemption never exempts any PR from CI or other rules. GitHub Auto-merge
may be used once repository policy and required checks are active, but no
Actions GITHUB_TOKEN workflow is authorized to merge PRs. An Open Machine
merge remains subject to its separately required user authorization and current
native checks. This policy does not authorize releases or deployments.

Creating a PR does not authorize merging; an actual merge still requires explicit
user authorization and the current native GitHub policy and CI checks.
Do not enable auto-merge, publish a release, or alter repository access settings
as part of an ordinary code change.

## Labels and planning

[labels.json](.github/labels.json) defines the small managed label set. Use one
primary label per Issue or PR: `spec`, `research`, `benchmark`, `bug`,
`implementation`, `feature`, `feature-request`, `maintenance`, `documentation`,
or `security`. Surveys, including experiments, use `research`; implementation
increments use `implementation`. Roadmaps and other feature work use `feature`;
Feature Request issues use only `feature-request`. Labels classify work, not
approval or completion. Check the live label during review.

The label workflow creates or updates only these managed labels after the
manifest is merged to `main`, or on an explicit manual run against `main`.
It never deletes unrelated labels. New or changed templates and label synchronization take effect through GitHub's
default-branch mechanisms after merge; a PR alone does not update `main`.

No external Project, release field, milestone, assignee, or private organization
workflow is assumed. Add planning integration only when it has an actual owner.
Do not add an assignee or rewrite unrelated metadata merely to fill a template.

## Checks and evidence

Use a module's focused tests while editing, then the shared premerge entry point
for the clean committed candidate:

```sh
python3 scripts/run_tests.py --module github --layer unit
python3 scripts/run_tests.py --base origin/main --plan
python3 scripts/premerge.py --base origin/main
```

The focused unit command verifies current checkout source. The default module
and premerge paths select the affected module's layers and explicit boundaries,
build one independent wheel, and test that candidate outside its checkout.
Repository and whitespace checks run before environment preparation. See
[test/README.md](test/README.md) for ownership, fixture, deletion and layer rules.

The base must exist locally and be an ancestor of the candidate. Development
selection includes staged, unstaged and ordinary untracked changes; premerge
requires a clean committed worktree. Unknown source ownership, invalid mappings,
missing retained coverage and zero required tests fail before expensive work.
A documentation-only plan runs repository checks without product installation.

Use `--full` for complete verification. Dependency, packaging and global runner
changes also require the full suite. Ordinary module/test additions and ownership
edits use the affected entries, including old and new boundaries. `--force`
invalidates cache reuse for the selected scope; it does not expand that scope.

A local PASS is reusable only for the exact HEAD, resolved base, test selection,
scope and verification fingerprint. Source-only and dirty-worktree results are
not cached. Full and affected evidence cannot be confused, and hosted CI obtains
its own result. Pull-request-internal commit subjects are not a merge gate;
published history should not be rewritten to satisfy presentation policy. The
final commit landing on main is still validated by the push event.

The dependency-free repository-only checks remain:

```sh
python3 -m unittest discover -s test/repository -p 'test_*.py'
python3 scripts/check_repository.py
```

Before publishing a PR, check its exact draft title and body with the same
metadata validator used by CI:

```sh
python3 scripts/check_repository.py --mode metadata --event-name pull_request --event-path /absolute/pr-event.json
```

The JSON file contains `{"pull_request": {"title": "...", "body": "..."}}`, with
the actual draft strings. Both explicit event arguments are required together;
`issues` uses an `issue` object. Metadata mode reads only that event data and
does not run source checks, contact GitHub, or publish anything. PR titles,
English text and a nonempty valid body remain hard checks; suggested section
names produce presentation warnings. A passing body is not proof of meaningful
acceptance, sufficient review or authorization.

The repository suite tests the checker, selector and runner and verifies source
dependency direction. The checker validates candidate text hygiene, relative
Markdown file links, the label manifest, and naming and English-language rules
for supported GitHub events. `--mode source` checks repository files and the
final commit range for a push event, without reading PR or Issue metadata.
The default `--mode all` preserves the combined local entry point. The checker
rejects CJK text as a common accidental language switch; it does not detect all
non-English prose or prove English grammar.
Review remains responsible for English content, meaningful acceptance,
authorization and the live primary label. Local link checks validate file
targets, not URL availability or heading anchors.

All CI jobs use GitHub-hosted Ubuntu 24.04, including fork PRs, pushes,
documentation builds and default-branch automation. Runner selection is fixed
in the workflows instead of relying on repository variables or private machines.
Quality and metadata jobs retain pinned actions, read-only permissions, no
credentials supplied to candidate code, and a five-minute budget. The quality
workflow's code checks use the same premerge pipeline and exact GitHub event
candidate with explicit source mode. PR and Issue metadata checks run only in
their separate workflow and concurrency identity, including each new PR head; a
metadata-only edit cannot replace a failed code check with a same-named skipped
result or cancel code verification. A missing PR heading does not block code
verification, and a corrected body can pass metadata checks without a new source
commit. Changing a PR's base still requires code verification against the new
candidate.

Only separate default-branch metadata workflows have write access: label sync
and post-merge Spec Development linking use `issues: write`. Never use
`pull_request_target` to execute candidate code, interpolate Issue or PR text
into shell commands, or route write-privileged jobs to private self-hosted runners.
Branch protection remains a separate maintainer setting.

Add focused tests for actual product behavior; do not create empty suites or
directories. Record measured timings and actual PASS, FAIL, SKIP and NOT RUN
results. A local pass is not a hosted-CI pass, and neither proves runtime,
provider, security, performance or production acceptance.

## Public repository maintenance

Keep all workflows on GitHub-hosted runners without private infrastructure.
Enable GitHub private vulnerability reporting for the public repository and
verify that the private security reporting route is visible. Check branch
protection, required checks and community links as separate repository settings;
merging documents alone does not configure them. See [SECURITY.md](SECURITY.md)
and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
