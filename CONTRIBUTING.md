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
  Issue, pull request or community channel containing exploit details.

Read our [Code of Conduct](CODE_OF_CONDUCT.md). Bug reports and feature requests
are welcome in any language. Prefer English for repository-authored prose; Unicode examples and data are valid.

## Set up a development checkout

You need Python 3.12+ and Git. Restricted sessions need macOS Seatbelt or, on
Linux, a bubblewrap that can build the sandbox; their tests are skipped elsewhere. Windows is not supported.

For a fork-based contribution:

```sh
git clone https://github.com/YOUR-USERNAME/openmachine.git
cd openmachine
git remote add upstream https://github.com/OpenMachineLabs/openmachine.git
git fetch upstream main
git switch -c fix/your-change upstream/main

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

Replace the username and branch name. If you cloned the official repository
instead of a fork, use `origin/main` as the base. Do not substitute unpinned
dependencies if installation fails.

Run tests with disposable state: a `HOME` under a short path such as `/tmp`
(macOS limits Unix socket paths to 104 bytes), never a real
`~/.config/openmachine` or `~/.local/state/openmachine*`. Do not test changes
against production machines, services or credentials.

## Make and test your change

Keep the change narrowly scoped, include regression tests for behavior changes,
and update relevant documentation. Never commit secrets, private logs or personal
data. [Test ownership and validation details](test/README.md) explain the layers.

For documentation and repository-only changes:

```sh
python scripts/check_repository.py
python -m unittest discover -s test/repository -p 'test_*.py'
```

For a focused code change (replace `access` with the owning module):

```sh
python scripts/run_tests.py --module access --layer unit
```

Before review, inspect and run the affected candidate checks:

```sh
python scripts/run_tests.py --base upstream/main --plan
python scripts/run_tests.py --base upstream/main
```

Commit the reviewed changes. The clean-candidate gate below is available for local
verification; CI uses the same runner, so do not repeat an unchanged full suite
locally when its current CI result is available:

```sh
git add path/to/changed-file
git commit -m "docs(contributing): clarify the contributor workflow"
python scripts/premerge.py --base upstream/main
```

The base must exist locally and be an ancestor of the change. Use `origin/main`
for a direct clone. Packaging, locked dependencies and shared runner changes
require [full verification](test/README.md#full-verification-and-cache-identity).
Report actual PASS, FAIL and NOT RUN results rather than assuming a test passed.

## Module owners

| Owner | Responsibility |
| --- | --- |
| `access/` | The Code launch boundary `python -I -m openmachine.access`: `capabilities`, `session` and `folders` (`__main__.py`); session requests, lifetimes and Full Access admission (`sessions.py`); the Linux session environment (`linux/environment.py`); the Workspace folder rule and worktree grants (`rules.py`) and their Git observation (`git.py`); what the restricted backends share, the spawner with its launch frames, relay and exit report (`restricted.py`); the macOS Seatbelt profile (`macos/seatbelt.py`) and restricted sessions (`macos/sessions.py`); the Linux bubblewrap sandbox (`linux/sandbox.py`) and restricted sessions (`linux/sessions.py`). |
| `service/` | Management on macOS and Linux: payload targets and verification, installation, the selection, original operations and their retention. |
| `cli.py` | The `manage` command. |
| `config.py` | The machines-only configuration. |
| `foundation/` | Private state, locks and safe errors; no domain ownership. |

| Command | Caller | Purpose |
| --- | --- | --- |
| `python -I -m openmachine.access capabilities` | Code host | Core's version, session kinds and policies |
| `python -I -m openmachine.access session --control-fd N` | Code host | Admit one session channel |
| `python -I -m openmachine.access folders` | Code host, for Desktop | Check Workspace folders: one JSON request on stdin |
| `python -I -B -m openmachine.cli --config C --state-dir S manage --contract 1 <action>` | Desktop | `status`, `configure`, `install`, `update`, `operation`, `continue` |

The configuration holds one machine, `[machines.local]` with `transport = "local"`.
Desktop installs and updates Core from a verified offline runtime payload that
`scripts/build_runtime_payload.py` assembles for its host's target (`--target`); a Linux
payload carries a static `bwrap`, built there with meson, ninja, pkg-config and the static libcap. The README artwork in `assets/` is
the banner PNG and self-contained SVGs animated with CSS, with no scripts.

Access imports only `openmachine`, `openmachine.access`, `openmachine.foundation`
and `openmachine.config`. Source moves never move persistent state or replace
stable locks.

## Choose work from current evidence

Before starting a candidate or declaring a blocker, inspect the relevant
worktrees and the owning Issue, PR and CI evidence. A missing PR, empty search or
clean tree does not prove nobody is working there. Reconcile stale status
against newer evidence for the same prerequisite, target and revision.

Separate implementation, integration, acceptance and deployment. A pending
check blocks only actions that need its result, so an unrelated pending
acceptance gate does not block development. For a real blocker, name the missing
prerequisite, the affected action and the condition that would unblock it. Check
overlapping edits and shared services before acting; worktrees isolate files,
not every resource.

## Complete the authorized delivery

Carry the requested outcome through verification. Merge, release and deployment
need actual authorization, not words in a plan. Correct a proven terminal
no-effect rejection under a new ID. For a missing reply, a partial effect or an
unknown outcome, read the original ID and its destination before any new effect,
and never replay it. Preserve unrelated edits and stable locks.

Continue through ordinary tool and test failures when safe recovery stays in
scope; observe an existing command or CI run instead of restarting it. Compare
every requested deliverable with actual evidence before finishing. If blocked,
report the evidence, the safe recovery attempted and the remaining work.

## Open a pull request

Push your branch and open a PR against `OpenMachineLabs/openmachine:main`.
Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) to explain the reason,
the change, how you tested it and any material risks. Link an Issue when relevant.
A small documentation correction does not need a separate Spec or rollback plan.

Choose a clear final squash title, for example `fix(access): refuse a replaced
directory`. Naming and formatting conventions are advisory; CI does not audit
development commit messages or require fixed Issue/PR headings. Explain the
change, validation and material limitations, and respond to review findings.

Maintainers review design, safety, tests and CI before deciding on a merge.
A passing check is not automatic merge approval. Maintainer-specific decisions
and publication rules live in [MAINTAINERS.md](MAINTAINERS.md).
