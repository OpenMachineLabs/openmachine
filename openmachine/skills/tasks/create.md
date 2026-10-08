# Create a durable task

[Domain guide](SKILL.md)

## One original task

A task is a logical outcome that can reference several commands or file changes.
Task progress is controller-owned; operations keep their authoritative receipts:

```text
<state-dir>/tasks.db         # current state, original operations and audit
<state-dir>/tasks.db-wal     # SQLite write-ahead log when present
<state-dir>/locks/tasks.lock # stable maintenance and old-writer fence
```

For new multi-stage work needing continuity, derive the smallest contract and
outcome-sized stages from the authorized request and call `create_task` before
consequential work. A hand-written Spec is optional; one-step reads need no task.
Inspect exposed tools before claiming structured support. Continue existing
structured records through `control_task` and legacy records through `save_task`,
without conversion or another journal. A task read adds no execution authority.

Creation binds original request/source identities, objective, scope, exclusions,
acceptance, stop condition, authority references, configured targets, tools and
finite expiry. Unknown fields and caller-authored runtime facts are rejected.
Actual target identity, HEAD and current grants are checked before new admission,
without holding task locks across target/network reads. References do not grant
permissions or prove the referenced source was inspected.

Canonical bytes are sorted compact UTF-8 JSON, no BOM, plus one LF. A stage's
`source_refs` select complete `{locator, sha256}` objects from `contract.sources`,
sorted by locator. Compute their `source_sha256` exactly:

```python
hashlib.sha256((json.dumps(selected, sort_keys=True, ensure_ascii=False,
    separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")).hexdigest()
```

The newline matters; an empty selection hashes `[]\n`. Hash source objects, not
only digest strings or referenced file bytes. Never invent hashes or omit bindings
to silence a validation error.

Creation remains bounded to 48 KiB; current authority is bounded to 64 KiB and
holds immutable stage-decision references. Operations and audit grow independently,
without a task-lifetime byte or entry limit. Single stored entities and page data
remain bounded to 64 KiB. SQLite WAL transactions with full synchronization commit
admission and conflict indexes before domain dispatch. A lost acknowledgement is
uncertain: retain and read the original task/update/operation IDs. Never manufacture
a replacement task or erase history to obtain capacity. No arbitrary overwrite API.

## Choose targets

Read [worktree selection](../workspace/worktrees.md) before allocating or selecting
an optional worktree. Read [GitHub account binding](../github/connections.md#github-account-binding)
for native connection identity. These records identify scope; they do not reserve
a checkout for the task's lifetime or grant new permissions.

Retain necessary instructions and readable inputs within the bounded contract;
hashes cannot restore missing attachments.

If required structured tools are absent, do not install them, claim legacy coverage,
or begin required supervised work without a valid record. Correct rejected contract
inputs or report the blocker; do not substitute a file receipt, command or second
legacy journal for the required structured task.
