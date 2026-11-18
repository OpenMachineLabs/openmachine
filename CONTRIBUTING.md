# Contributing to Open Machine

We welcome bug reports, feature requests, documentation, tests and focused
fixes. You do not need an AI agent to contribute. Read our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Report a bug or request a feature

- **Questions:** ask on [Discord](https://discord.gg/TAxEWV7fgZ).
- **Bugs:** open a [Bug report](.github/ISSUE_TEMPLATE/bug.md) with safe steps
  to reproduce, what you expected and saw, the version and the platform.
- **Features:** open a [Feature Request](.github/ISSUE_TEMPLATE/feature-request.md)
  describing the problem and the behavior you want. Discuss a large change
  there before you build it.
- **Security vulnerabilities:** follow [SECURITY.md](SECURITY.md), never a
  public Issue, pull request or chat.

Reports are welcome in any language. Prefer English for text in the
repository; Unicode examples and data are fine.

## Set up

You need Git, Python 3.12 or newer and, for `om`, the Go version in
[`go.mod`](go.mod) and Node.js 24, which one of its tests and
`scripts/verify-logs.mjs` run. Restricted Core sessions need macOS Seatbelt
or, on Linux, a bubblewrap that can build the sandbox; their tests are skipped
elsewhere. Windows is not supported.

```sh
git clone https://github.com/YOUR-USERNAME/openmachine.git
cd openmachine
git remote add upstream https://github.com/OpenMachineLabs/openmachine.git
git fetch upstream main
git switch -c fix/your-change upstream/main

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock -r device/requirements.lock
```

Test with disposable state: a `HOME` under a short path such as `/tmp` (macOS
limits Unix socket paths to 104 bytes), never your real
`~/.config/openmachine` or `~/.local/state/openmachine*`. Do not test against
other people's machines, accounts or credentials.

## Test your change

Keep the change narrow, add a regression test for a behavior change, and
update the documentation it affects. Never commit secrets, private logs or
personal data.

| Change | Check |
| --- | --- |
| `om` (`cmd/`, `daemon/`, `linux/`) | `gofmt -l cmd daemon linux test`, `go vet ./...`, `go test ./...` |
| Device identity (`device/`) | `python -m unittest discover -s test/device -p 'test_*.py'` |
| `scripts/install.sh` | `python -m unittest discover -s test/linux -p 'test_install_script.py'` |
| `scripts/verify-logs.mjs` | `node --test test/verifier/verify-logs.test.mjs` |
| The network library (`connector/`) | its [README](connector/README.md#test). `om` builds against the version `go.mod` names, not this folder; to try a change in both, use an uncommitted `go work init . ./connector` |
| A dependency | `python scripts/check_licenses.py` (needs both lock files installed), and add the new license if it is allowed |
| Core (`openmachine/`) | `python scripts/run_tests.py --base upstream/main`; [test/README.md](test/README.md) explains the layers |
| Documentation and repository files | `python scripts/check_repository.py` |

Before review, commit and run the same gate CI runs for Core:

```sh
python scripts/premerge.py --base upstream/main
```

`scripts/build_bundle.py` builds the bundle of `om` and device identity that
the Mac app and the Linux release carry;
[docs/device.md](docs/device.md#setup-and-the-bundle) says how. Report actual
PASS, FAIL and NOT RUN results.

## Sign off your commits

Every commit in a pull request must carry a `Signed-off-by:` line with the
name and email of its author. It certifies that you wrote the change or have
the right to submit it under the project's license, as the
[Developer Certificate of Origin](https://developercertificate.org/) states.
`git commit -s` adds it; `git rebase --signoff upstream/main` adds it to
commits you already made. The `dco` check fails without it. There is no
contributor license agreement.

## Open a pull request

Push your branch and open a pull request against
`OpenMachineLabs/openmachine:main`. Use the
[template](.github/PULL_REQUEST_TEMPLATE.md): why, what changed, how you tested
it and any material risk, with a related Issue if there is one. Choose a clear
title, such as `fix(access): refuse a replaced directory`.

Maintainers review design, safety, tests and CI before they merge. A passing
check is not approval. Contributions are licensed under
[Apache-2.0](LICENSE).
