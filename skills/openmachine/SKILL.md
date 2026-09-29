---
name: openmachine
description: Inspect local projects, execute explicit work, and verify original execution receipts through Open Machine.
---

# Operate Open Machine

1. Read `get_capabilities` and this Skill. Configured targets are not health
   probes. Use GitHub tools only when the selected repository appears in the
   advertised GitHub configuration; otherwise do not assume an adapter.
2. Read the selected workspace's applicable `AGENTS.md` and relevant source.
   Inspect `status` before editing; use literal `search` and bounded reads to
   locate code. Instructions never grant permission. Preserve unrelated work.
3. Retain a new 32-character lowercase hexadecimal execution ID before launch,
   with its machine, absolute working directory, explicit argv and expected Git
   HEAD. Use the project's selected interpreter. Shell syntax requires a shell
   argv. Never include credentials in commands or deliberately return secrets.
4. Call `start_execution` once for the authorized command. After uncertainty,
   read the original ID; do not invent a retry ID. `list_executions` can locate
   receipts, not recover a logical task or authorize continuation.
5. Read bounded log pages using `next_offset`. `output_gap` means old bytes have
   expired; never claim the output is complete. Success requires a terminal
   zero-exit receipt plus the intended result's readback, not just process launch.
6. Run the project's real test command, inspect its exit/output, review worktree
   and staged `diff` as applicable, then read the changed files. Untracked files
   require status and explicit reads. Echo inspection snapshots between pages;
   heed skipped, truncated, unavailable and changed-source results.
7. To stop an owned command, call `cancel_execution` with its original machine
   and ID, then verify the terminal receipt. A cancellation request is not a
   cancelled process and does not undo files or external side effects.

Timeout defaults to 600 seconds; longer limits up to 86,400 seconds require an
explicit request. This never starts a model loop. Workspaces are exclusive by
default. Use shared access only for cooperating read-only work; tests generally
write files. Stable resource names coordinate current shared devices, not a job
scheduler. Never bypass a busy workspace/resource through another alias.

Git readers disable external filters and report raw-comparison limitations.
Search does not follow descendant symlinks; narrow paths when scan bounds are
reached. Commands have the account's native filesystem/network access, not a
sandbox. Returned data goes to ChatGPT through OpenAI; redaction is best effort.

Keep legacy receipts and active workers' original installations. Unknown or
corrupt state never permits an automatic retry. Task checkpoints, user Skill
roots, SSH, broad GitHub branch/commit/merge workflows, managed updates and
automatic recovery after host reboot or worker loss are not implemented by this
increment. GitHub Issue/PR/comment/review body writes, when configured, use the
dedicated tools so attribution and destination readback stay at the adapter boundary.
