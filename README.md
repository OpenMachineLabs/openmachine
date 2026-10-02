# Open Machine

An open-source runtime for AI to operate your machines.

## v0.2.6

Open Machine v0.2.6 keeps the accepted v0.2.5 runtime contracts while simplifying
their implementation and documentation. PR #60 splits the macOS service lifecycle
into focused state, checks, install, runtime and transition owners. PR #61 reduces
historical guidance and mixed responsibilities across workspace, tasks, GitHub,
Events and tests without changing the public tool contract or persisted formats.

Sources/results are bounded to 2 MiB per file; full-content/edit/patch payloads
remain 128 KiB. Single-file receipts retain format 1, patches use format 2, and
current readers accept both. State contract 10 and SSH protocol 6 are compatibility
contracts, not release versions. Execution retains format 5 and opt-in PTY control.
Inspect installed capabilities; a checkout or version label alone is not evidence
that the client exposes the current tools.

Version `0.2.6` is an initial-development release, not a stable 1.x API promise.
Spec #32 remains the MCP SDK 2.2.0 migration baseline. Retained task, recovery and
Events history requires a compatible state reader before a managed update or
rollback. Automatic continuation is accepted only on the recorded Work route;
other clients, cross-chat isolation and production deployment remain separate.

Managed services support logged-in macOS controllers. Linux supports manual
`serve` and configured SSH workers; managed Linux services and Windows are not
included. Native execution is disabled by default and is **not a sandbox** once
enabled. Returned files and logs travel to ChatGPT through OpenAI.

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. Build from the exact release tag, not an unpinned default branch:

```sh
git clone --branch v0.2.6 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.6-py3-none-any.whl
.venv/bin/openmachine --version
```

Release notes use a compact Changes/Changelog format and do not attach installers
or wheels. Build from the exact tag with its committed lock file as shown above;
no PyPI, Homebrew or npm publication is implied.

The wheel includes the core, direct adapters and bundled Skill, not tests, private
state or arbitrary checkout files. Workers use the installed package, never a
source-checkout import. Current source pins MCP SDK 2.2.0, HTTPX2 2.13.1,
JSON Schema 4.26.0, Pydantic 2.13.5, Uvicorn 0.38.0 and Hatchling 1.27.0 for
building. See [requirements.lock](requirements.lock) for the complete locked set.
v0.2.6 remains on MCP SDK 2.2.0. v0.2.3 introduced this SDK baseline; the
v0.2.2 tag remains on SDK 1.30.0 and retains its own matching lock.

## Configure and run

Start with an existing disposable workspace; replace the example path:

```sh
.venv/bin/openmachine init --workspace /absolute/path/to/disposable-project
.venv/bin/openmachine doctor
.venv/bin/openmachine serve
```

Default configuration: `~/.config/openmachine/config.toml`. Private state:
`~/.local/state/openmachine/`. Global `--config` and `--state-dir` options go
before the subcommand. `init` creates mode-0600 configuration without overwriting
files and leaves execution **disabled**. `init` also explicitly sets
`workspace_write_enabled = false`. Enable `execution_enabled = true` only for
intended native commands, and `workspace_write_enabled = true` only for typed
file edits. Existing configurations omitting the new field inherit their old
execution grant; an explicit false overrides that inheritance. Restart this
service after an authorized configuration change.

Machine/workspace names are operator-owned; see [examples/config.toml](examples/config.toml).
Local and explicitly configured SSH transports are supported. Optional `[github]`
configuration enables the scoped collaboration tools described in the operation reference. Omit it to
keep GitHub disabled. Open Machine uses the existing `gh` login and stores no token.
Unknown fields are rejected, not ignored. Plain `doctor` checks configuration and
directory existence, not effective file access or network readiness. Managed
`doctor --service` verifies the selected runtime, MCP identity and an actual
permitted file read; neither command claims tunnel or ChatGPT readiness.

`serve` listens only at `<state-dir>/mcp.sock`, with directory mode 0700 and socket
mode 0600. It opens no TCP port, manages no tunnel and installs no OS service.
An occupied socket is never replaced. After an abrupt exit, verify that no owner
is using a stale socket before manually removing it. Graceful shutdown removes
only this instance's socket. Existing state with broader permissions is rejected,
never silently chmodded. Keep socket paths within the host's Unix-socket limit.


## Manage the macOS service

[Service operations](docs/service.md) covers immutable installation, start/stop,
status, exact candidate updates, compatible rollback and recovery. A service stop
unloads the current job; it is not permanent login disable or workload cancellation.

## Connect the official tunnel

Manage upstream `tunnel-client` separately. Its inspected local binding is:

```text
--mcp.server-url=channel=main,url=http://localhost/mcp,unix-socket=/absolute/path/to/state/mcp.sock
```

Keep logical Host `localhost` and Host/Origin protection. Follow the
[Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
for the selected route's credentials and eligibility; keep secrets in supported
local references, not pasted commands. A socket/handshake is not actual ChatGPT
acceptance. Never reconfigure a production bridge to test a candidate.

## Tool contracts and local coding acceptance

Read capabilities, the operating Skill and applicable project `AGENTS.md` files.
Inspect source/Git state, apply an authorized exact change, then explicitly run
its validation command. Read original receipts and verify touched files, unrelated
work and actual exit/output. Application, testing, publication, deployment and
client acceptance are separate outcomes.

The [operation reference](docs/operations.md) documents existing tools and limits.
The [operating Skill](skills/openmachine/SKILL.md#complete-the-authorized-delivery)
explains completion and original-ID recovery. A task read's
`resume_authorized: false` means it adds no authority, not that current explicit
user consent was revoked. A lost response never authorizes replay.

## Explicit file editing

[Workspace operations](docs/operations.md#workspace) cover source windows, exact
edits and `om_patch_v1`. Whole-plan preflight is not a multi-file transaction;
partial/unknown results preserve the original ID and known applied prefix.

## GitHub collaboration

Current source includes Spec #62 PR1's runtime-owned existing-`gh` connection
management: explicit attach/list/disconnect, account-bound GitHub requests and
new task bindings. [Connection operations](docs/operations.md#account-connections)
work through the existing private MCP; no GUI or new server is required. Native
GitHub OAuth and refresh remain PR2. This source increment is not a new release
or evidence that an existing installation has been updated.

[GitHub operations](docs/operations.md#github) use exact scopes and the existing
account, current-head review gates and destination readback. A self-review is not
independent approval, and a publication response is not verified merge evidence.

## Configured SSH machines

[SSH operations](docs/operations.md#ssh) use explicitly prepared immutable workers
and pinned target identity. Preserve original archives; no local fallback occurs.

## User Skills

[Skills](docs/operations.md#skills) use explicit live sources, never account scans.
Instructions and resources do not install tools or confer permissions.

## Durable task continuity

[Task operations](docs/tasks.md) explain contracts/claims, legacy checkpoints,
original-result recovery, index maintenance and task handoff Events. Notification,
readback, claim and actual next intent are different facts.

## Development and review

| Owner | Responsibility |
| --- | --- |
| `server.py`, `cli.py` | Explicit schemas, serialization and command entries. |
| `bootstrap.py`, `app/` | Enabled-component assembly, lifecycle/task admission and target routing. |
| `execution/` | Original commands, pipe/PTY controls, cleanup, receipts and bounded logs. |
| `workspace/` | Source/Git observations, preparation, change records and guarded publication. |
| `tasks/` | Canonical schemas, pure transitions, lookup and legacy checkpoint integrity. |
| `service/` | Installation, compatibility/readiness, supervision and activation/restoration. |
| `foundation/` | Shared IO, private state, locks and safe errors; no domain ownership. |
| `integrations/` | Complete GitHub, SSH and Events implementations; no second engine. |

Workspace preparation and record ownership are separate from application.
Task models remain in `records.py`; pure decisions live in `transitions.py`.
`app/task_observer.py` owns threads/leases; `task_observation.py` owns observation
policy and read-only views. Service remains independent of Desktop. Existing
`python -m openmachine.execution` and `python -m openmachine.service check-state`
entries remain intact.

Dependencies point inward. Execution uses `workspace.git` only for identity;
workspace and tasks do not import each other. SSH uses the same local capability
code in a curated standard-library-only archive, excluding controller app/tasks,
service and providers. Application admission spans dispatch, not worker lifetime.
Source moves never move persistent state, replace stable locks or authorize replay.

Follow [AGENTS.md](AGENTS.md), [CONTRIBUTING.md](CONTRIBUTING.md), the
[Spec index](spec/README.md) and [test/README.md](test/README.md). Repository prose
remains English. Current code/tests describe implementation; historical Issues
and PRs retain evidence, not instructions to repeat completed milestones.

```sh
python3 -m unittest discover -s test/system -p 'test_*.py'
python3 scripts/check_repository.py
```

## Trust boundary

This is a trusted single-operator native runtime, not a multi-tenant executor or
OS sandbox. Commands retain account access beyond reader scopes. Exclusions and
best-effort redaction are not a secret vault or tamper-proof audit system.
Returned files/logs travel to ChatGPT through OpenAI; self-hosted does not mean all
data stays local. Do not expose this listener to untrusted users or treat a tunnel
ID as a complete access policy. No automatic production migration or permission
grant occurs. Record live acceptance separately from installed tests and CI.

## License

[MIT](LICENSE).
