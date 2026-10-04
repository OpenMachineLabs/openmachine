# Tests

## Run the installed candidate

Use the repository's locked dependencies and build/install an independent wheel.
Run outside the checkout so source imports cannot hide missing package files:

```sh
REPO="$PWD"
python3 -m venv .venv
PYTHON="$REPO/.venv/bin/python"
"$PYTHON" -m pip install -r requirements.lock
"$PYTHON" -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
"$PYTHON" -m pip install --no-deps --force-reinstall dist/openmachine-*.whl
python3 -m unittest discover -s test/system -p 'test_*.py'
python3 scripts/check_repository.py
cd /tmp
"$PYTHON" -m unittest discover -s "$REPO/test/unit" -p 'test_*.py'
"$PYTHON" -m unittest discover -s "$REPO/test/contract" -p 'test_*.py'
```

Use a fresh build directory or select the exact wheel when multiple versions exist.
For focused work, select one existing capability file with `-p`. All tests remain
under root `test/`; no additional framework, network account or GPU is required.
Private disposable directories, short owned processes and local Unix sockets are
fixtures, not production projects or live SSH/ChatGPT acceptance.

## Coverage by responsibility

| Group | Important behavior |
| --- | --- |
| `unit/test_codex.py` | Browser scope, native evidence, original execution reuse, cancellation, setup failure and private result boundaries. |
| `unit/test_runtime.py` | Configuration, passive discovery, workspace/Git reading, shared resources, retained logs and dependency boundaries. |
| `unit/test_execution.py`, `test_interactive.py` | Launch versus exit 127, signals/grace/cleanup, original-ID conflicts, waiting, pipe/PTY controls, lost replies, backpressure and cancellation isolation. |
| `unit/test_workspace_changes.py` | Exact edits, original-byte matching, preview purity, grants, hashes/identities, leases, record ownership, races and no replay. |
| `unit/test_workspace_reads.py` | Raw/line windows, long Unicode lines, source-context redaction, filtered discovery, incomplete coverage and cursor/snapshot continuity. |
| `unit/test_workspace_patch.py` | Strict add/update/delete/move grammar, full preflight, parents, exact BOM/terminators/EOF, ordered effects, partial/unknown failures and bounded records. |
| `unit/test_continuity.py` | Legacy checkpoint/index integrity, real original-ID association, Skill resources and whole writes. |
| `unit/test_tasks.py`, `test_task_admission.py` | Canonical publication, bounded index rebuild, process-loss boundaries, claims/epochs, stale intent, scope, plan invalidation, capacity and terminal acceptance. |
| `unit/test_task_observer.py`, `test_task_ssh.py` | Absent-client original results, pause/terminal races, durable read budgets, actual slot ownership, stale snapshots and shared target recovery. |
| `unit/test_service.py` | Selection, reader compatibility, before-stop rejection, configuration restoration, first-upgrade failures, bounded supervision, persistent Stop/login policy, exact legacy definitions, stable management CLI, original maintenance phases and unchanged recovery budgets. |
| `unit/test_ssh.py`, `test_github.py` | Pinned target transport/archive isolation; exact provider scope/account, complete paged evidence, merge gates and original-destination reconciliation. |
| `contract/test_mcp*.py` | Installed private-socket SDK/wire contracts, workflow/restart continuity and independent admission; split by execution, workspace, patches and tasks; retired Events methods are rejected. |
| `system/test_repository.py` | Standard-library repository hygiene, templates, metadata and commit checking. |

`support_tasks.py`, `support_execution.py`, `support_workspace.py` and
`support_mcp.py` contain shared fixture helpers, not TestCase subclasses or tests.
Tests do not import another test case to borrow setup or assertions. Keep a
behavior in its owning group; a lower-level unit, wire boundary and real restart
can cover distinct risks even when their names overlap. Fault injection targets
the real owner, not a facade re-export. Never weaken assertions just to pass.

## Compatibility and packaging

The import check owns every installed production module and checks absolute,
relative, function-local and re-export edges plus cycles. Type-only imports do not
pull controller dependencies onto SSH targets. Explicit checks do not model all
implicit initialization/dynamic imports; isolated assembly/archive startup and
full-source review cover those limits. Do not add a dynamic import to evade them.

Build both wheel and sdist-derived wheel, compare their shipped Python/Skill files
with the source and installed candidate, and verify the new reference guides are included in the
sdist. Preserve the console and execution/service module entrypoints. The curated
SSH archive runs with `-I -S` without controller app/tasks/service/GitHub/MCP/Pydantic.
Its actual worker fixture verifies inherited leases and an original counter=1.
New source members change artifact hashes, not protocol/state semantics by assertion.

Use independently installed old/new readers against disposable original records:
legacy and structured tasks including terminal-only data, workspace formats 1/2,
execution/control receipts, SSH/managed recovery and retired Events archives. Check exact bytes,
identity and outcome before/after; missing/unsupported history must not become an
empty store, replay or permissive rollback. A matching version string alone is not
an artifact identity. State/lock paths, inode ownership and acquisition order are
compatibility contracts, not cosmetic implementation details.

## Frozen MCP contract

[contract/mcp_contract.json](contract/mcp_contract.json) retains the released baseline
and explicitly approved schema deltas. It is an independent expected catalog, not
regenerated from a candidate to hide drift. Compare all four GitHub/SSH enablement
configurations and both supported protocols without normalizing actual results.
Duplicate tools, pagination, changed annotations or missing fields are failures.
Discovery contacts no GitHub/SSH host. Existing behavior checks own results/errors,
limits, source integrity and lifecycle semantics not captured by schema snapshots.

An authorized contract change requires exact baseline/candidate provenance and
review of its deliberate fixture delta. Internal file moves require no fixture
change. Deleting a duplicate test requires an explicit retained-coverage map;
keep unique fault, corruption, permission, control and restart scenarios.

## Evidence boundaries

Mocked launchctl is not native service acceptance; private-socket SDK sessions are
not real ChatGPT conversations; synthetic SSH transport is not live target evidence.
A true transition uses distinct immutable candidates, preserved original workers/
records/configuration and verified restoration or explicit uncertainty. Run such
checks only on authorized disposable resources, never production services or NAS/GPU
workloads. Quiescent SDK upgrades remain stop -> upgrade -> original read -> resume,
not a blanket zero-downtime promise. Preserve original target session/linger policy.

Work Events delivery and discovery probes are removed. The installed MCP checks
reject Events methods on both supported protocols and retain the frozen ordinary
tool catalog. Service tests preserve an inert private archive and reject missing,
unsafe or reactivating readers. Existing absent-caller, claim, no-replay and restart
checks retain ordinary Chat task-continuation coverage without an Events adapter.

Historical milestone receipts remain in Specs
[#18](https://github.com/OpenMachineLabs/openmachine/issues/18),
[#28](https://github.com/OpenMachineLabs/openmachine/issues/28),
[#13](https://github.com/OpenMachineLabs/openmachine/issues/13),
[#32](https://github.com/OpenMachineLabs/openmachine/issues/32) and
[#29](https://github.com/OpenMachineLabs/openmachine/issues/29), with their PRs.
Reuse applicable evidence; do not repeat completed milestones or call historical
passes new-candidate acceptance. Source merge, release, deployment and real-client
acceptance require their own explicit authority and verification.

M2 advances only the persistent service contract from 13 to 14 and adds management
contract 1. The corresponding explicit state-contract assertions change; the frozen
MCP tool catalog does not. Service tests exercise the installed management module
and actual CLI entry, with mocked native mutations. Unix-socket or launchctl
restrictions must be reported as blockers, not converted to native acceptance.
