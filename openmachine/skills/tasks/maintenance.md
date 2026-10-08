# Task lookup and legacy checkpoints

[Domain guide](SKILL.md)

## Lookup and explicit maintenance

`find_tasks` gives candidates by exact ID/anchor or keywords; even a unique keyword
match requires checking the record. Exact anchors may be ambiguous. Read original
records before acting and retain index generations between pages.

```sh
openmachine --config /absolute/config.toml --state-dir /absolute/private/state rebuild-task-index
```

Maintenance validates at most 50 records/2 MiB per page; continue with its observed
`--expected-generation` until complete. New work requires complete lookup; original
result publication and stopping do not. At most 10,000 entries are considered,
without eviction or migration. Missing legacy metadata, established locks, unsafe
paths, orphans and corrupt records block repair rather than permit guessed state.
The index is derived lookup, not a second progress ledger. Reads never repair it.

## Legacy checkpoints

`save_task` creates at expected revision zero and updates the observed revision.
Identical latest-write retries remain unchanged; anchors are additive and an empty
list preserves existing locators. Titles/anchors/revision/hash metadata live in
the index, checkpoint prose only in Markdown. Checkpoints are bounded to 32 KiB,
index to 2 MiB. Save concise goals, exclusions, authority, worktree/branch, original
handles/cursors, relevant outcomes and next action; never secrets, transcripts,
hidden reasoning or bulk source/logs.

Each file replacement is durable/atomic; checkpoint plus index is not a two-file
transaction. Torn pairs, missing established locks, lost directories, stale hashes
or generations fail rather than reinitialize. Reconnect by reading the same task,
current user instructions, Git and original operation receipts; reuse valid results
and perform only remaining authorized work. A changed instruction retains earlier
evidence and observes or explicitly stops incompatible original work. A checkpoint
restores neither hidden model state nor new authority, and schedules no model loop.

A retained local start rejection is not a lost worker. A recorded pre-dispatch
`resource_busy`, its free original launch lock and an absent execution directory
prove a failed-before-launch result. The original task operation remains with its
ID and request hash; no execution receipt is invented. Remote, ambiguous, live or
partial destinations remain unresolved. This correction never replays an effect.
