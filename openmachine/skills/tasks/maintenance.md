# Task maintenance

[Domain guide](SKILL.md)

`find_tasks` returns candidates, not an authorization. Verify the original Task,
IDs and index generation before acting. `rebuild-task-index` checks integrity;
it does not restore missing authority. Reads never initialize or repair state.

## First-release storage

Only Task schema 3 is writable. Earlier pre-release databases and file-based
Task stores block activation; preserve their bytes and inspect/backup them before
any separately authorized developer reset. There is no implicit migration,
reset, rollback writer or historical owner takeover.

Online SQLite backups include WAL content and remain write-fenced on restore
until effects after the backup watermark are reconciled. Do not copy a live
database alone or delete its WAL.

## Legacy checkpoints

`save_task` uses expected revision zero for creation; identical retries are
unchanged and anchors are additive. Keep short goals, exclusions, authority,
original IDs, outcomes and next action (no secrets, transcripts or bulk logs).

After a lost reply, read the original Task, change or execution ID; never
replay uncertain effects. `resource_busy` before launch is known-no-effect
only with the original retained rejection and absent execution receipt.
Missing locks, databases and corrupt records never authorize reinitialization.
