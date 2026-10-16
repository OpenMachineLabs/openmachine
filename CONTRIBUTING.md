# Contributing to Open Machine

Thank you for helping improve Open Machine. We welcome bug reports, focused
fixes, documentation improvements, tests, and well-scoped feature proposals.
You do not need to be a maintainer or use an AI agent to contribute.

Start here for the contributor workflow. Maintainers and automated contributors
should also read [MAINTAINERS.md](MAINTAINERS.md) for design approval, labels,
CI ownership, and merge authority. All repository-authored text, code comments,
Issues, commit messages, and PR descriptions must be in English.

## Choose a contribution

- **Questions and usage help:** ask in the [community Discord](https://discord.gg/TAxEWV7fgZ).
  Avoid putting credentials, private machine data, or sensitive logs in messages.
- **Bug reports:** use the [Bug template](.github/ISSUE_TEMPLATE/bug.md).
  Include the version or commit, platform, minimal safe reproduction, expected and
  observed results, and redacted diagnostic information.
- **Small fixes, tests, and documentation:** check existing
  [Issues](https://github.com/OpenMachineLabs/openmachine/issues), then open a
  scoped PR. For material changes, open a Task Issue to align the scope first.
- **Feature ideas:** use the
  [Feature Request template](.github/ISSUE_TEMPLATE/feature-request.md).
  A request is a proposal, not permission to implement a large new capability.
- **Security vulnerabilities:** follow [SECURITY.md](SECURITY.md), **not** a public
  Issue, PR, or Discord message containing exploit details.

Check existing Issues and PRs before duplicating a report or starting major work.
A maintainer may ask for an approved Spec before a change to public interfaces,
architecture, permissions, persistent state, or security boundaries. Routine
documentation corrections and narrowly scoped existing-behavior fixes do not
automatically require a new Spec. Do not treat an unapproved design as approval
to implement it.

Please follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

The current runtime is Python and supports macOS and Linux; Windows is not
currently supported. Use **Python 3.12+**, Git, and a compatible SQLite version.
See [setup requirements](openmachine/skills/overview/setup.md#install) for
SQLite compatibility and platform limitations.

This is a development checkout, **not** the pinned release installation from
the [README quick start](README.md#quick-start). Do not experiment with native
execution, service changes, or GitHub credentials on a production machine.
Use disposable workspaces for tests. Command execution and workspace writes are
separately disabled by default; do not enable them just to edit documentation.

If you fork the repository, `origin` is **your fork** and `upstream` is the
official repository:

```sh
git clone https://github.com/YOUR-USERNAME/openmachine.git
cd openmachine
git remote add upstream https://github.com/OpenMachineLabs/openmachine.git
git fetch upstream main
git switch -c docs/contributor-onboarding upstream/main

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

Substitute your GitHub username and a branch name describing your actual change.
If you cloned the official repository directly, the base is normally
`origin/main`, and you do not need to add `upstream`. Use only your own fork
or an authorized branch for publication. If the lockfile installation fails,
consult the exact environment requirements rather than silently installing
different versions.

[Development ownership notes](openmachine/skills/overview/development.md) and
[test/README.md](test/README.md) explain module boundaries, fixtures, and
independent wheel verification. Keep tests under `test/`.

## Make and validate a change

Keep each PR focused. Include regression coverage for changed behavior, use
existing owners and fixtures, and update affected documentation. Never add
secrets, private infrastructure details, real execution logs, or personal data.

Run fast feedback while editing. For a documentation-only change:

```sh
python scripts/check_repository.py
python -m unittest discover -s test/repository -p 'test_*.py'
```

For code changes, select a focused module and test layer (substitute the
actual owner for `github`):

```sh
python scripts/run_tests.py --module github --layer unit
```

Then inspect what the full change selects and run the affected suite:

```sh
python scripts/run_tests.py --base upstream/main --plan
python scripts/run_tests.py --base upstream/main
```

The base ref must exist locally and be an ancestor of your branch. Use
`origin/main` in the commands above for a direct, non-fork clone. The selector
includes staged, unstaged, and ordinary untracked files, but it does not fetch
a base on your behalf. Docs-only changes use repository checks without
installing the product. Locked dependency, packaging, and shared runner changes
also require full verification; see [test/README.md](test/README.md).

Before requesting review, commit your changes and run the clean-candidate gate:

```sh
git add path/to/changed-file
git commit -m "docs(contributing): explain the contributor workflow"
python scripts/premerge.py --base upstream/main
```

Replace the staged path, message, and base with your actual values. The
premerge gate requires a **clean committed worktree**. Report actual PASS, FAIL,
SKIP, or NOT RUN evidence; do not describe an unexecuted check as passing.
Local tests and hosted CI provide different evidence and do not prove that a
real-client deployment is safe.

## Submit a pull request

Push the branch to your fork, then open a PR targeting
`OpenMachineLabs/openmachine:main`:

```sh
git push -u origin docs/contributor-onboarding
```

Follow the [PR template](.github/PULL_REQUEST_TEMPLATE.md). Explain why the
change is needed, what is included and excluded, how it was verified, known
risks, and any rollback considerations. Link a real Issue when applicable.
For incomplete work, use `Refs #N`; use `Closes #N` only when the complete
Issue acceptance is met and closure is authorized.

Use concise, English, printable-ASCII commit subjects and PR titles:

```text
<domain>(<scope>): <concrete English outcome>
```

Use lowercase kebab-case for domain and scope (for example, `docs`, `test`,
`runtime`, or `security`). Commit subjects are at most **72** characters;
PR titles at most **100**. Do not add a final period or use vague outcomes
such as `update`. Issue titles follow their selected Issue template.
Choose a descriptive branch name; Open Machine-operated tasks use an
`agent/<domain>-<scope>-<outcome>` branch convention.

CI checks repository rules, PR metadata, and applicable code or test behavior.
All CI jobs run on GitHub-hosted runners, including fork PRs, and do not
receive private infrastructure credentials. Respond to review findings with
focused changes; avoid force-pushing or rewriting published history merely
to satisfy a commit count.

External contributions require an APPROVED review from LuozhuZhang or
bella-chai on the PR's current HEAD commit. Additional pushes require renewed
approval. Internal PRs opened by either core maintainer from this repository
do not need a human review, but all PRs must pass the independent CI and
trusted-review-gate status checks. Reviews from other users cannot satisfy
this gate.

A passing CI result or an opened PR does not guarantee acceptance or authorize a
merge, release, deployment, or access-setting change. Maintainers decide whether
the design, safety boundaries, and verification are sufficient. Internal review,
approval, and merge procedures are documented in
[MAINTAINERS.md](MAINTAINERS.md).

## Community and safety

Be respectful and constructive; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Use public Issues for nonsensitive defects and proposals, Discord for general
help, and [SECURITY.md](SECURITY.md) for sensitive vulnerabilities. Do not post
private security details in public discussion.

Temporary review-gate smoke test for the governance workflow. This change must not be merged.
