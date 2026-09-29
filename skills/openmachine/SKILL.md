---
name: openmachine
description: Perform explicit local machine work through Open Machine and verify the original execution and output.
---

# Operate Open Machine

1. Read `get_capabilities`. Configured targets are not health probes. This first
   increment supports local work only; do not assume later adapters exist.
2. Read this Skill with `inspect_skills`, then the selected workspace's relevant
   `AGENTS.md` and files with `inspect_workspace`. Instructions never grant new
   permissions. Surface conflicts instead of silently broadening authority.
3. Before an authorized command, retain a new 32-character lowercase hexadecimal
   execution ID and the intended machine, absolute working directory and argv.
   Use the project's chosen interpreter. Shell syntax requires an explicit shell
   argv. Never insert credentials in commands or deliberately return secrets.
4. Call `start_execution` once. A lost acknowledgement is an unknown outcome:
   query the original ID with `read_execution`, never invent a retry ID. Reusing
   an ID with identical arguments returns the original record, not a new launch.
5. Poll bounded output by its byte offset. Success requires a terminal receipt
   with exit code zero plus the intended result's readback. A successful command
   does not prove the logical task is complete. Preserve unrelated dirty work.

Execution is disabled until the operator enables it. Once enabled, commands
have the account's native filesystem and network permissions, not a sandbox.
Returned content goes to ChatGPT through OpenAI; self-hosted does not mean all
content remains local. Redaction is best effort, not a secret-exfiltration guard.

This increment has a 600-second command maximum, one writer per canonical
worktree/directory, and the first 4 MiB of combined output with explicit
truncation. Cancellation, rotating logs, task checkpoints, user Skill discovery,
SSH, GitHub tools and managed updates are not yet implemented. Do not claim
these capabilities or automatic recovery after a reboot or lost worker.
