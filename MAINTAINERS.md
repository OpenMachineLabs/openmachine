# Maintainer guide

This guide covers design authority, repository governance, review and release
boundaries. External contributors should start with [CONTRIBUTING.md](CONTRIBUTING.md).

## Design and scope

Code and tests describe implemented behavior. The [Spec index](spec/README.md)
links the approved scope and original decision; it is not a second implementation
status ledger. Avoid unrelated changes, speculative modules and new dependencies
without a demonstrated need.

Use the repository's Issue templates according to the work:

| Type | Use |
| --- | --- |
| Spec | Material public-interface, architecture, permission or persistence change |
| Feature Request | Community proposal; discussion is not implementation approval |
| Bug | Reproducible observed defect |
| Survey / Research | Evidence, alternatives or a bounded experiment |
| Benchmark | Frozen comparison protocol, reproducible metrics and limitations |
| Roadmap | Long-term direction across independent increments |
| Task | Scoped maintenance, documentation or implementation work |

An implementable Spec records requirements, boundaries, acceptance and relevant
failure behavior. It becomes approved only with explicit maintainer authorization
and an approval link recorded as `direct_issue_contract` in
[spec/README.md](spec/README.md). A sufficiently explicit current user request
can authorize a small change without another Spec. No draft, Survey, Roadmap or
Feature Request silently authorizes implementation, production work or purchases.

Store Spec and Survey details in the original GitHub Issues, not duplicate files.
Add `spec/<issue-number>/` only for necessary attachments; product code must not
treat them as runtime configuration. Historical approval never substitutes for
current permission to merge, release or deploy.

## Git, review and publication

Commit subjects use `<domain>(<scope>): <concrete English outcome>`, printable
ASCII, lowercase kebab-case domain/scope and a 72-character maximum. Avoid vague
outcomes like `update`. Ordinary internal Task/Spec/Survey/Benchmark titles follow
their templates, while public Bug and Feature Request titles may use natural
language and Unicode. PR title style is advisory; baseline title hygiene still
applies. Prefer a descriptive `agent/<domain>-<scope>-<outcome>` branch name for
Open Machine-operated tasks.

Keep one focused candidate and review the **full diff**, not merely the last
commit. Include real test outcomes, known limitations, affected security boundaries
and an Issue/Spec link where applicable. Use `Refs #N` for incomplete work and
`Closes #N` only when all acceptance is complete and closing is authorized.

A self-review COMMENT can provide findings, but is not independent approval.
Opening a PR, passing local tests or passing CI does not authorize a merge.
Merge, release, deployment, repository access changes and auto-merge require
separate explicit authority and current native GitHub policy checks. Do not
rewrite published history just to satisfy a presentation convention.

## Labels and automation

[labels.json](.github/labels.json) is the managed label source. Use the appropriate
primary label: `spec`, `research`, `benchmark`, `bug`, `implementation`,
`feature`, `feature-request`, `maintenance`, `documentation` or `security`.
Labels describe work, not approval. Do not invent a new taxonomy or change
unrelated metadata merely to fill a template.

The separate default-branch label workflow manages only declared labels and does
not delete unrelated ones. GitHub Projects, milestones and assignees are optional;
no private organization workflow is a prerequisite for community contributions.

## Testing and evidence

Follow [test/README.md](test/README.md) for the canonical module ownership,
test-layer selection, wheel verification, cache identity and compatibility checks.
Useful entry points for an affected candidate are:

```sh
python3 scripts/run_tests.py --base origin/main --plan
python3 scripts/premerge.py --base origin/main
```

The second command needs a clean committed worktree. Locked dependencies, packaging
and shared runner changes require the full suite. A local PASS is not hosted CI
evidence and neither proves deployment, native service or real-client acceptance.
Do not remove necessary regression coverage or lower safety gates to obtain PASS.

Repository hygiene uses `scripts/check_repository.py`. Commit formats remain
strict, while ordinary community Bug/Feature Request Issue bodies need not use
formal Spec sections or English. The source checker preserves secret-boundary,
link and file hygiene checks. PR section names are presentation advice, not
proof of acceptance.

CI jobs run on GitHub-hosted Ubuntu 24.04 with pinned actions. Quality checks
use read-only permissions and cannot run candidate code under write-privileged
`pull_request_target`. Metadata-only changes must not cancel, replace or mask
code verification. Only separate trusted default-branch automations perform
limited metadata writes. See [.github/AGENTS.md](.github/AGENTS.md) for workflow
rules. Branch protection and required checks are separate GitHub settings.

## Public repository maintenance

Keep private infrastructure, credentials, raw logs and personal data out of
the public repository. Verify private vulnerability reporting, branch protection,
required checks and community links before relying on them; documentation alone
does not enable repository settings. See [SECURITY.md](SECURITY.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

Publishing any npm package additionally requires the exact artifact, provenance,
clean-user native acceptance and third-party redistribution review described in
[npm/README.md](npm/README.md) and [npm/THIRD_PARTY.md](npm/THIRD_PARTY.md).
