# Tests

## Isolated macOS first-use check

For a native installation change, opt in with an explicitly trusted, owner-built
payload and its reviewed manifest digest:

```sh
OM_NATIVE_FIRST_USE=1 \
OM_NATIVE_PAYLOAD=/absolute/trusted/payload \
OM_NATIVE_PAYLOAD_SHA256=reviewed_manifest_sha256 \
.venv/bin/python -m unittest \
  test.resilience.service.test_maintenance_process.NativeFirstUseTests
```

This uses a fresh temporary configuration and state directory. It checks
`configure`, offline installation, original-operation readback, `status` and the
installed access launcher's `capabilities`. It does not change the account's
installation. Failed state and original IDs are retained with the exact CLI
replies for diagnosis; successful runs remove only their own fixtures.

The native case is skipped unless explicitly enabled. It proves a fresh local
installation, not Code sessions, Cloud authorization or signed distribution.
Reuse unchanged passing evidence; reserve those separate acceptance checks for
changes that need them.

## Choose the required scope

Use the owning module during development, then verify the complete changed
candidate before requesting review or merge:

```sh
python3 scripts/run_tests.py --module access --layer unit
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
installing dependencies, builds the candidate wheel and runs the required test
files once against it, installed outside the checkout. Its import check ensures
product modules come from that wheel. Repository tests remain dependency-free, and the test package
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

The initial ownership migration, release version bumps, changes to locked
dependencies or packaging, and changes to the shared runner or code-quality
workflow require full verification. Ordinary module, fixture, test or ownership additions select their
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

A changed test runs that test directly. Shared fixture changes run their declared
consumers, including consumers reached through another helper; package initializer
changes include the affected package's tests and fixture consumers. Changed
ownership entries select the union of old and new mappings. Deleted or renamed
sources use the baseline and current manifests together.

Every changed or deleted source of a surviving module must retain its own mapped
coverage; it cannot borrow another module's tests as proof. Retiring a whole owner
removes its sources, its tests and its manifest entry together, and what it
deleted then needs no coverage: no behavior remains. Anything it leaves behind is
unowned and fails. Full mode does not bypass missing ownership or
retained-coverage checks.

Keep each assertion with the module and layer that own its risk. Tests import
shared fixtures from `test.support`, never another test case. Split mixed files
by behavior rather than merely moving a large file to a different directory.
Real lifecycle tests preserve unique crash, cancellation, corruption, permission
and no-replay scenarios; protocol conformance does not multiply every business
lifecycle by every supported protocol. Deleting repeated coverage requires an
explicit retained-coverage map in the PR.

All Python tests remain under root `test/`; no additional test framework, network
account or GPU is required. Private disposable directories, short owned processes
and local Unix sockets are fixtures, not production projects or live
client acceptance.

## Compatibility and packaging

The import check owns every installed production module and checks absolute,
relative, function-local and re-export edges plus cycles. It also holds the
access launcher to `openmachine`, `openmachine.access`, `openmachine.foundation`
and `openmachine.config`. Type-only imports do not pull unexpected runtime
dependencies into unrelated owners. Explicit checks do not model all implicit
initialization/dynamic imports; full-source review covers those limits. Do not
add a dynamic import to evade them.

Preserve the `openmachine.access`, `openmachine.cli` and
`openmachine.service.maintenance` module entry points: the Code host, Desktop and
the pinned management entry run them. State and lock paths, file ownership and
the original-operation records are compatibility contracts.

## Evidence boundaries

Disposable state and short owned processes are not native acceptance. Restricted
session tests need macOS Seatbelt; on other hosts they are skipped, not passed.
Run installation checks only on authorized disposable resources, never against
an account's real Core state.
