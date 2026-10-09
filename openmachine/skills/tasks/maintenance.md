# Task lookup and legacy checkpoints

[Domain guide](SKILL.md)

## Lookup and explicit maintenance

`find_tasks` gives candidates by exact ID/anchor or keywords; even a unique keyword
match requires checking the record. Exact anchors may be ambiguous. Read original
records before acting and retain index generations between pages.

```sh
openmachine --config /absolute/config.toml --state-dir /absolute/private/state rebuild-task-index
```

SQLite indexes commit with their authoritative rows. `rebuild-task-index` now
performs an explicit integrity/coverage check; it does not reconstruct missing
authority. There is no 2-MiB catalog or 10,000-task ceiling. Ordinary reads do not
initialize, migrate or repair a store. Missing databases or established locks,
unsafe paths and corrupt evidence fail closed.

Task records and new requests use only the current Task contract. Retired Chat
recovery settings are not accepted by the current runtime. Before deploying it
onto an older installation, explicitly audit and convert any historical Task
authority outside the running service, with a verified private backup and
original operation/event evidence preserved. A failed or incomplete cutover must
leave the old installation and its authoritative database unchanged. Do not
reset or silently omit other Tasks to pass readiness checks.

Managed service activation stops the controller before importing old task formats
1–4 and Markdown. The migration validates all original bytes and IDs, commits
normalized evidence, then archives the old directory under the stable lock before
activating SQLite. The import manifest uses POSIX path-string order, including
prefix-related task IDs such as `task-one` and `task-one-c1`. A corrected candidate
can reprepare an unfenced failed import from the still-authoritative legacy bytes.
Activation preflight reads that original store while the imported database is
inactive, so newly recorded unresolved work still blocks activation. Conflicting
active stores remain invalid. The retained lock prevents old binaries from
recreating their JSON store. Once fenced, a failed start retains the same candidate and activation
intent; continue that original maintenance operation instead of rolling back to
an old writer. Original paused/unknown state never becomes execution permission.

Backups use the Task owner's SQLite online-backup operation, including live WAL
content. A backup is published only after its copied write authority is disabled.
Do not copy a live database alone, delete WAL files, or make a restored older copy
writable without accounting for effects after its watermark. This increment has
no automatic write-enablement or effect replay for restored copies.

## Legacy checkpoints

`save_task` creates at expected revision zero and updates the observed revision.
Identical latest-write retries remain unchanged; anchors are additive and an empty
list preserves existing locators. Checkpoint body and metadata now commit together
in SQLite; checkpoint prose remains bounded to 32 KiB. Save concise goals,
exclusions, authority, worktree/branch, original
handles/cursors, relevant outcomes and next action; never secrets, transcripts,
hidden reasoning or bulk source/logs.

Retain original revisions and IDs after a lost commit acknowledgement. Missing
established locks, lost databases or corrupt payloads never reinitialize history.
Reconnect by reading the same task,
current user instructions, Git and original operation receipts; reuse valid results
and perform only remaining authorized work. A changed instruction retains earlier
evidence and observes or explicitly stops incompatible original work. A checkpoint
restores neither hidden model state nor new authority, and schedules no model loop.

A retained local start rejection is not a lost worker. A recorded pre-dispatch
`resource_busy`, its free original launch lock and an absent execution directory
prove a failed-before-launch result. The original task operation remains with its
ID and request hash; no execution receipt is invented. Remote, ambiguous, live or
partial destinations remain unresolved. This correction never replays an effect.
