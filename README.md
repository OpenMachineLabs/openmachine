# Open Machine

An open-source runtime for AI to operate your machines.

## v0.2.14

This release adds on-demand Git worktree discovery, guarded creation and explicit
selection for workspace tools. Worktree isolation remains a task decision; creation
pins a local base commit and preserves the original checkout and dirty files.
Original execution receipts retain creation outcomes without implicit retries.

Expired continuation claims now return `renewal_required` without forcing the
current Chat turn to end. New effects remain blocked until the caller explicitly
renews the same retained claim. Operating guidance covers lease checks around long
external work and bounded recovery from observation-only revision conflicts.
Pause, cancellation, authority expiry, uncertain work and foreground wait limits
retain their existing stop behavior.

Runtime GitHub requests now require native GitHub App connections. Historical
`gh` task and connection records remain readable, but their retired backend cannot
execute or reconcile requests; new native work requires separate authorization.

Enabled recovery remains bounded by readiness, original receipts, finite retries
and pre-dispatch revalidation. Offline hosts, missing login, human decisions and
uncertain or exhausted delivery remain stop conditions. State contract 17, SSH
protocol 7 and worktree execution format 6 are compatibility contracts, not release
versions; ordinary format-5 and historical execution receipts remain readable.
See [task operations](docs/tasks.md) and [tool contracts](docs/operations.md).

Version `0.2.14` is an initial-development release, not a stable 1.x API promise.
Build from its exact tag; later `main` commits may be unreleased. Inspect installed
capabilities separately: a source checkout or release does not update an installation.

Managed services support logged-in macOS controllers. Linux supports manual
`serve` and configured SSH workers; managed Linux services and Windows are not
included. Native execution is disabled by default and is **not a sandbox** once
enabled. Returned files and logs travel to ChatGPT through OpenAI.

## Install

Use Python 3.12 or newer on macOS or Linux, with Git available for workspace
identity. Build from the exact release tag, not an unpinned default branch:

```sh
git clone --branch v0.2.14 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.14-py3-none-any.whl
.venv/bin/openmachine --version
```

Release notes use a compact Changes/Changelog format and do not attach installers
or wheels. Build from the exact tag with its committed lock file as shown above;
no PyPI, Homebrew or npm publication is implied.

The wheel includes the core, direct adapters and bundled Skill, not tests, private
state or arbitrary checkout files. Workers use the installed package, never a
source-checkout import. MCP SDK remains at 2.2.0; dependency pins are unchanged.
Use [requirements.lock](requirements.lock) as the complete build/runtime dependency
source rather than mixing packages from different release tags.

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
files and leaves execution **disabled** unless `--enable-commands` is explicit. `--enable-workspace-writes` is an independent first-run grant. By default, `init` explicitly sets
`workspace_write_enabled = false`. Enable `execution_enabled = true` only for
intended native commands, and `workspace_write_enabled = true` only for typed
file edits. Existing configurations omitting the new field inherit their old
execution grant; an explicit false overrides that inheritance. Restart this
service after an authorized configuration change.

Machine/workspace names are operator-owned; see [examples/config.toml](examples/config.toml).
Local and explicitly configured SSH transports are supported. Optional `[github]`
configuration enables the scoped collaboration tools described in the operation reference. Omit it to
keep GitHub disabled. New accounts use the official Open Machine GitHub App Device
Flow and automatically pinned local credential storage; historical gh task bindings
remain readable, but their retired backend cannot execute requests.
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

[Product management](docs/service.md#product-management) is the primary interface
for supported installations: explicit start/stop, login policy, immutable updates,
compatible rollback and original-operation readback. `manage stop` persists Stop;
legacy `service stop` only unloads the current job. Neither cancels user workloads.

[Offline runtime preparation](docs/runtime-payload.md) retains a verified
Python/Core/Git environment without starting it. This release provides the source
support, not a signed product installer or automatic-update feed.

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

Git worktrees are optional, selected per operation. The AI inspects task context
and existing trees, reuses a suitable location, and calls `create_worktree` only
when independent work needs isolation. Creation keeps one execution ID and leaves
the original directory and uncommitted files in place. No tree is allocated per
Chat or PR. See [on-demand worktrees](docs/operations.md#on-demand-worktrees) for
path grants, task targets, recovery and the explicit cleanup boundary. This
capability uses state contract 17, SSH protocol 7 and creation receipt
format 6; ordinary execution receipts remain format 5.

[Workspace operations](docs/operations.md#workspace) cover source windows, exact
edits and `om_patch_v1`. Whole-plan preflight is not a multi-file transaction;
partial/unknown results preserve the original ID and known applied prefix.

## GitHub collaboration

[Connection operations](docs/operations.md#account-connections) cover native GitHub
Device Flow, pinned credentials, renewal, cancellation, disconnect and stable task
bindings. All runtime GitHub requests use the official GitHub App; retained `gh`
history cannot authorize requests or move to a native account. Installation/resource
approval and user consent are still required.
Automatic renewal is not a promise of permanent authorization.

GitHub attribution uses `Created via` for Issue/PR bodies and `Posted via` for
comments/reviews. `[github].attribution = false` disables new automatic footers;
edits preserve existing provenance.

[GitHub operations](docs/operations.md#github) use exact scopes and the existing
account, current-head review gates and destination readback. A self-review is not
independent approval, and a publication response is not verified merge evidence.

## Configured SSH machines

[SSH operations](docs/operations.md#ssh) use explicitly prepared immutable workers
and pinned target identity. Preserve original archives; no local fallback occurs.

## Codex browser subtasks

[Browser delegation](docs/operations.md#codex-browser-delegation) adds two tools
while reusing execution IDs, cancellation and task continuity. Chat remains the
controller; installed native Codex handles only the assigned browser goal.
No browser UI, plugin manager or second runtime is installed by Open Machine.

## User Skills

[Skills](docs/operations.md#skills) use explicit live sources, never account scans.
Instructions and resources do not install tools or confer permissions.

## Durable task continuity

[Task operations](docs/tasks.md) explain contracts/claims, legacy checkpoints,
original-result recovery, index maintenance and explicit Chat continuation. A
persisted handoff, readback, claim and actual next intent are different facts.

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
| `integrations/` | Complete GitHub and SSH implementations; no second engine. |

Workspace preparation and record ownership are separate from application.
Task models remain in `records.py`; pure decisions live in `transitions.py`.
`app/task_observer.py` owns threads/leases; `task_observation.py` owns receipt
observation facts, and `task_attention.py` derives bounded foreground stop views.
Service remains independent of Desktop. Existing
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
python3 scripts/run_tests.py --base origin/main --plan
python3 scripts/premerge.py --base origin/main
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
