# Contributing

## Repository boundary

The Spec index binds approved implementation slices. Add files, dependencies
and tests only for the current approved behavior. The first runtime slice uses
Python; later scope remains subject to explicit approval.
Keep all repository-authored content in English, including commit and PR bodies.

## Design and research

| Work | Title | Purpose |
| --- | --- | --- |
| Spec | `[SPEC] <domain>(<scope>): <outcome>` | An implementable design delta. |
| Survey / Research | `[SURVEY] research(<scope>): <question>` | Investigation, comparison, or synthesis. |
| Experiment | `[SURVEY] experiment(<scope>): <question>` | Bounded measurements and their conclusions. |
| Benchmark | `[BENCHMARK] eval(<scope>): <comparison>` | A reproducible comparison under a frozen protocol. |
| Program | `[Program] <name>` | An optional roadmap across several tasks. |
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
is authorized. A Program, Survey, or Benchmark does not authorize implementation,
production changes, expensive runs, or purchases.

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
with actual results rather than expected PASS claims. Use the PR template and
link the design or identify the current user request as the design source.

Review the entire candidate, not only its final commit. Governance, CI
permissions, architecture, security, deployment, and public-contract changes
need explicit maintainer review. A self-review is not independent approval.
Creating a PR does not authorize merging; leave it open when review is requested.
Do not enable auto-merge, publish a release, or alter repository access settings
as part of an ordinary code change.

## Labels and planning

[labels.json](.github/labels.json) defines the small managed label set. Use one
primary type label per Issue or PR: `spec`, `research`, `benchmark`, `bug`,
`implementation`, `feature`, `maintenance`, `documentation`, or `security`. Surveys,
including experiments, use `research`; implementation increments use `implementation`;
Programs and ordinary feature requests use `feature`. Labels classify work;
they do not imply approval or completion. Check the live label during review.

The label workflow creates or updates only these managed labels after the
manifest is merged to `main`, or on an explicit manual run against `main`.
It never deletes unrelated labels. Templates and label synchronization become
available through GitHub's default-branch mechanisms after merge; merely opening
this scaffold PR does not install them on `main`.

No external Project, release field, milestone, assignee, or private organization
workflow is assumed. Add planning integration only when it has an actual owner.

## Checks and evidence

```sh
python3 -m unittest discover -s test/system -p 'test_*.py'
python3 scripts/check_repository.py
```

The system suite tests the repository checker itself; product checks and their
installed-wheel entry points are documented in `test/README.md`. The checker validates tracked
text hygiene, relative Markdown file links, the label manifest, and the naming
and English-language backstop for supported GitHub events and new commits.
It rejects CJK text as a common accidental language switch; it does not detect
all non-English prose or prove English grammar. Review remains responsible for
English content, meaningful acceptance, authorization, and the live primary label.
Local link checks validate file targets, not URL availability or heading anchors.

Quality CI uses GitHub-hosted runners, pinned action revisions, read-only
permissions, no credentials supplied to candidate code, and a five-minute
budget. Only the separate default-branch label workflow has `issues: write`.
Never use `pull_request_target` to execute candidate code, interpolate Issue or
PR text into shell commands, or introduce self-hosted machines for these checks.
Branch protection is a separate maintainer setting, not enabled by this PR.

Add focused product tests with actual product behavior; do not create empty
suites for nonexistent components. Record important unexecuted checks as
`NOT RUN`. A local pass is not a hosted-CI pass, and neither proves runtime,
provider, security, performance, or production acceptance.
