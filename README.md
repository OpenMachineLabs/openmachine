# Open Machine

An open-source runtime for an existing Chat to operate your machines. Chat controls
the work; OM provides explicit workspace, command, task, GitHub and browser tools
with bounded reads and original operation receipts.

Version `0.2.19` is an initial-development release. Build from its exact tag;
current `main` may contain unreleased work. A source checkout does not update an
installed runtime or a client's tool catalog.

## Get started

Use Python 3.12+ and Git on macOS or Linux. Build with the release's pinned
requirements, then choose an existing disposable workspace:

```sh
git clone --branch v0.2.19 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.19-py3-none-any.whl
.venv/bin/openmachine init --workspace /absolute/path/to/disposable-project
.venv/bin/openmachine doctor
.venv/bin/openmachine serve
```

Command execution and typed workspace writes are independently disabled by default.
`serve` exposes only a private Unix socket; it installs no service or tunnel.
Read [setup](openmachine/skills/overview/setup.md) for grants, configuration,
installation boundaries and the official tunnel route. Managed services support
logged-in macOS controllers; Linux supports manual serving and configured SSH
workers. Windows and managed Linux services are not included.

## Operating guides

Start with [the short operating Skill](openmachine/skills/overview/SKILL.md) or the
[Skills directory](openmachine/skills/README.md). The runtime exposes its catalogue
and direct tool-to-guide routes through `inspect_skills`; detailed guides load only
when needed.

- [Workspace](openmachine/skills/workspace/SKILL.md): source, exact edits and worktrees.
- [Execution](openmachine/skills/execution/SKILL.md): original commands and terminal control.
- [Tasks](openmachine/skills/tasks/SKILL.md): create, continue, control and verify work.
- [GitHub](openmachine/skills/github/SKILL.md): native accounts and scoped collaboration.
- [Browser](openmachine/skills/browser/SKILL.md): explicitly authorized UI subtasks.
- [Service](openmachine/skills/service/SKILL.md): lifecycle, SSH and offline payloads.

## Development and trust

Follow [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), the
[Spec index](spec/README.md) and [test/README.md](test/README.md).
[Development notes](openmachine/skills/overview/development.md) describe module
ownership. Current code/tests own implemented behavior; historical Issues retain
approval and evidence. Source, installed tests, deployment and real-client
acceptance are distinct.

OM is a trusted single-operator native runtime, not an OS sandbox or a multi-tenant
executor. Commands retain account access beyond reader scopes. Exclusions and
best-effort redaction are not a secret vault; returned files/logs reach ChatGPT
through OpenAI. Do not expose the listener to untrusted users or treat a tunnel ID
as a complete access policy. Installation and native grants require authorization.

[MIT license](LICENSE).
