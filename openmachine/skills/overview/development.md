# Repository development

[Domain guide](SKILL.md)

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

Follow [AGENTS.md](../../../AGENTS.md), [CONTRIBUTING.md](../../../CONTRIBUTING.md), the
[Spec index](../../../spec/README.md) and [test/README.md](../../../test/README.md). Repository prose
remains English. Current code/tests describe implementation; historical Issues
and PRs retain evidence, not instructions to repeat completed milestones.

```sh
python3 scripts/run_tests.py --base origin/main --plan
python3 scripts/premerge.py --base origin/main
```
