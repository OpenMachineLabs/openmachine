# Contributing to Open Machine

We welcome bug reports, documentation improvements, tests, focused fixes and feature
proposals. You do not need to use an AI agent to contribute.

## Find the right starting point

- **Questions:** use [Discord](https://discord.gg/TAxEWV7fgZ).
- **Bugs:** open a [Bug report](.github/ISSUE_TEMPLATE/bug.md) with a safe
  reproduction, expected/actual results, version and platform.
- **Features:** open a [Feature Request](.github/ISSUE_TEMPLATE/feature-request.md)
  describing the problem and desired behavior.
- **Small fixes and docs:** check existing [Issues](https://github.com/OpenMachineLabs/openmachine/issues)
  and open a focused pull request.
- **Larger changes:** discuss the approach in an Issue before implementation.
  Changes to public interfaces, architecture, permissions or persisted state may
  need an approved [Spec](.github/ISSUE_TEMPLATE/spec.md).
- **Security vulnerabilities:** use [SECURITY.md](SECURITY.md), never a public
  Issue, pull request or community chat containing exploit details.

Read our [Code of Conduct](CODE_OF_CONDUCT.md). Bug reports and feature requests
are welcome in any language. Repository-authored documentation, code comments,
commit messages and technical PR descriptions must remain in English.

## Set up a development checkout

The Python runtime supports macOS and Linux, not Windows. You need Python 3.12+,
Git and a compatible SQLite version; see the
[setup requirements](openmachine/skills/overview/setup.md#install).

For a fork-based contribution:

```sh
git clone https://github.com/YOUR-USERNAME/openmachine.git
cd openmachine
git remote add upstream https://github.com/OpenMachineLabs/openmachine.git
git fetch upstream main
git switch -c docs/your-change upstream/main

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

Replace the username and branch name. If you cloned the official repository
instead of a fork, use `origin/main` as the base. Do not substitute unpinned
dependencies if installation fails.

Use disposable workspaces for execution tests; do not test changes against
production machines, services or credentials. Commands and workspace writes
require separate explicit grants.

## Make and test your change

Keep the change narrowly scoped, include regression tests for behavior changes,
and update relevant documentation. Never commit secrets, private logs or personal
data. [Test ownership and validation details](test/README.md) explain the layers.

For documentation and repository-only changes:

```sh
python scripts/check_repository.py
python -m unittest discover -s test/repository -p 'test_*.py'
```

For a focused code change (replace `github` with the owning module):

```sh
python scripts/run_tests.py --module github --layer unit
```

Before review, inspect and run the affected candidate checks:

```sh
python scripts/run_tests.py --base upstream/main --plan
python scripts/run_tests.py --base upstream/main
```

Then commit your work and run the clean-candidate gate:

```sh
git add path/to/changed-file
git commit -m "docs(contributing): clarify the contributor workflow"
python scripts/premerge.py --base upstream/main
```

The base must exist locally and be an ancestor of the change. Use `origin/main`
for a direct clone. Packaging, locked dependencies and shared runner changes
require [full verification](test/README.md#full-verification-and-cache-identity).
Report actual PASS, FAIL and NOT RUN results rather than assuming a test passed.

## Open a pull request

Push your branch and open a PR against `OpenMachineLabs/openmachine:main`.
Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) to explain the reason,
the change, how you tested it and any material risks. Link an Issue when relevant.
A small documentation correction does not need a separate Spec or rollback plan.

Use a descriptive commit subject such as `fix(workspace): handle missing files`
or `docs(readme): clarify installation`. Commit subjects follow
`domain(scope): outcome`, use printable ASCII and fit within 72 characters.
Prefer concise English PR descriptions; titles may be up to 100 characters.
Respond to review comments with focused updates.

Maintainers review design, safety, tests and CI before deciding on a merge.
A passing check is not automatic merge approval. Maintainer-specific decisions
and publication rules live in [MAINTAINERS.md](MAINTAINERS.md).
