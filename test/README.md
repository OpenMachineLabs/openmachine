# Tests

## Choose the required scope

Use the owning module during development, then verify the complete changed
candidate before requesting review or merge:

```sh
python3 scripts/run_tests.py --module github --layer unit
python3 scripts/run_tests.py --base origin/main --plan
python3 scripts/run_tests.py --base origin/main
python3 scripts/premerge.py --base origin/main
```

The first command runs the selected pure unit tests against the current checkout
source, using the project's already installed dependencies. It is fast focused
feedback, not installed-candidate or complete-module acceptance. Omitting
`--layer unit` runs the module's declared layers and boundaries against a newly
built, independently installed wheel.

`--base` compares the entire candidate range with an explicit local ancestor.
Development selection also includes staged, unstaged and ordinary untracked
changes; execution tests the current worktree bytes. Both sides of renames and
deletions remain visible. Selection does not fetch refs, guess a base, silently
ignore unknown paths, or treat an empty product selection as behavioral coverage.
Resolve a missing base or update the candidate's base relationship explicitly.

`--plan` prints the changed paths, owning modules, selected files, layers and
reasons without installing dependencies or running tests. A recognized
documentation-only change runs repository checks without creating a product
environment. An explicit module or layer with no tests is an error.

`premerge.py` requires a clean committed worktree. It uses the same selector and
runner as development and CI, checks whitespace and repository rules before
installing dependencies, builds the direct and sdist-derived candidate wheels,
compares their canonical Skill package bytes, and runs the required test files
once against the installed direct wheel outside the checkout. Its import check ensures product modules come
from that wheel. Repository tests remain dependency-free, and the test package
is loaded without putting the checkout ahead of the installed product.

All entry points report environment preparation and test-layer timings, test
counts, skips and the slowest cases. These measurements are evidence for the
current run; a directory name or a target budget is not a performance result.

## Full verification and cache identity

Request the complete layered suite explicitly when needed:

```sh
python3 scripts/premerge.py --base origin/main --full
python3 scripts/run_tests.py --full
```

The initial ownership migration, changes to locked dependencies or packaging,
and changes to the shared runner or code-quality workflow require full
verification. Ordinary module, fixture, test or ownership additions select their
affected checks. An ownership-manifest edit selects the changed entries' old and
new tests and boundaries; it does not automatically select every module.
`--full` cannot be combined with a focused module or layer.

A successful local premerge result is cached only inside Git metadata for the
exact HEAD, resolved base, scope, selected test set, candidate bytes, ownership
manifest and Python/verification fingerprint. Source-unit results and dirty
worktrees never populate this cache. An affected PASS cannot satisfy a full
request. `--force` reruns the selected scope; it does not itself request full
verification. A failed or interrupted fresh attempt invalidates its earlier
cached PASS. HEAD, base, candidate files, index and selection are rechecked
before publishing success.

Hosted CI always obtains its own evidence. Pull-request verification binds the
checked-out merge candidate and both event-pinned parents; main pushes bind the
exact before/after range. Manual workflow dispatch requests full verification.
PR-internal commit subjects remain outside the merge gate. PR metadata is checked
on pull-request events, and the final commit landing on main is checked on push.

## Directory and ownership rules

| Directory | Responsibility |
| --- | --- |
| `unit/<module>/` | Pure parsing, validation, transformations and state decisions; no real server, process lifecycle or timed waiting. |
| `component/<module>/` | Module behavior with local files, state stores and application collaborators. |
| `skill-library/<skill-name>/` | Standalone Skill behavior tests; keep each Skill in its own named folder, with exact paths in `ownership.json`. |
| `contract/mcp/<module>/` | Wire encoding, catalog/schema, routing, result and error boundaries. |
| `resilience/<module>/` | Real owned processes, crash/restart, cancellation, lost replies and retained-state recovery. |
| `repository/` | Repository hygiene, dependency direction, test selection and runner behavior. |
| `support/` | Shared fixtures with explicit consumers; no TestCase subclasses or test methods. |

[ownership.json](ownership.json) is the executable ownership map. Every
production Python file has one explicit module owner, and every test file has
one owning module. The source entries use exact file paths so a newly added
production file cannot be absorbed by a broad pattern without review.

Each module lists its own `tests` and explicit `boundary_tests` owned elsewhere.
The selector adds those concrete tests once; it does not recursively expand an
import graph or run every consumer module. Shared foundation, configuration and
application changes must name the affected consumer boundaries in the manifest.

All runtime, YAML bindings and Markdown under `openmachine/skills/` are canonical
shipped inputs owned by `skills`. Each has an exact ownership entry. Editing a
guide selects installed resource and MCP navigation coverage, including rebuilding
the wheel, even though its content is Markdown.

A changed test runs that test directly. Shared fixture changes run their declared
consumers, including consumers reached through another helper; package initializer
changes include the affected package's tests and fixture consumers. Changed
ownership entries select the union of old and new mappings. Deleted or renamed
sources use the baseline and current manifests together.

Every changed or deleted source must retain its own mapped coverage. Removing
an entire owner while changing another module cannot borrow that other module's
tests as proof. When retiring an owner, keep its retained absence or replacement
boundary explicitly registered until the design change is accepted. Full mode
does not bypass missing ownership or retained-coverage checks.

Keep each assertion with the module and layer that own its risk. Tests import
shared fixtures from `test.support`, never another test case. Split mixed files
by behavior rather than merely moving a large file to a different directory.
Real lifecycle tests preserve unique crash, cancellation, corruption, permission
and no-replay scenarios; protocol conformance does not multiply every business
lifecycle by every supported protocol. Deleting repeated coverage requires an
explicit retained-coverage map in the PR.

All tests remain under root `test/`; no additional test framework, network
account or GPU is required. Private disposable directories, short owned processes
and local Unix sockets are fixtures, not production projects or live
SSH/ChatGPT acceptance.

## Compatibility and packaging

The import check owns every installed production module and checks absolute,
relative, function-local and re-export edges plus cycles. Type-only imports do not
pull controller dependencies onto SSH targets. Explicit checks do not model all
implicit initialization/dynamic imports; isolated assembly/archive startup and
full-source review cover those limits. Do not add a dynamic import to evade them.

The installed runner builds both wheel and sdist-derived wheel in its existing
temporary environment and compares every canonical Skill `.py`, `.yaml` and `.md`
file with source bytes. Missing, extra, changed, duplicate and retired `_skills`
resources fail the gate. Installed resource coverage compares the installed copy
with the same source. Behavioral tests execute once after this payload parity
check. Preserve the console and execution/service module entrypoints. The curated
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

[contract/mcp/mcp_contract.json](contract/mcp/mcp_contract.json) retains the released baseline
and explicitly approved schema deltas. It is an independent expected catalog, not
regenerated from a candidate to hide drift. Compare all four GitHub/SSH enablement
configurations and both supported protocols without normalizing actual results.
Duplicate tools, pagination, changed annotations or missing fields are failures.
Discovery contacts no GitHub/SSH host. Existing behavior checks own results/errors,
limits, source integrity and lifecycle semantics not captured by schema snapshots.

An authorized contract change requires exact baseline/candidate provenance and
review of its deliberate fixture delta. Internal file moves require no fixture
change. Deleting a duplicate test requires an explicit retained-coverage map;
keep unique fault, corruption, permission, control and restart scenarios. The
retired `gh`-CLI outage fixture is covered by native HTTP fault/size/diagnostic
checks in `test/component/github/test_transport.py`, native GitHub typed-boundary
tests, `test_tool_contract_without_github`, and installed-candidate catalog discovery.
`test/component/connections/test_connections.py` covers passive historical gh
records, explicit retirement errors and rejection of native account substitution.

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
