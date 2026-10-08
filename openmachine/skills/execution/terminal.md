# Run and control an original execution

[Domain guide](SKILL.md)

## Execution

Commands run in separate original workers. Save the 32-hex ID before launch.
Supply a program and at most 99 arguments; each string is at most 4,096 characters
and cannot contain NUL. For larger reviewed inputs, use an explicit file or bounded
arguments consumed as data, never shell interpolation.
Identical normalized retries return original records; changed intent conflicts.
Resource names normalize as a set. Missing worker evidence is unknown, corrupt
receipts are errors, and neither allows relaunch. Restarting the controller does
not itself cancel an original command or replace its interpreter/archive.

Timeout defaults to 600 seconds; explicit requests allow up to 86,400 seconds.
Grace is null or a finite 0..30 seconds: null means five seconds for new work and
the original value on retry, including legacy 0.5 seconds. Default pipes have
closed stdin and combined stdout/stderr. Exclusive canonical-workspace leases
remain default; `shared` is only a cooperating read-only declaration, not enforced
isolation. Up to 16 stable named resources coordinate across workspaces.

The worker alone publishes lifecycle facts. Stop requests retain the first reason
and cannot relabel an already-observed natural exit. `stopping` differs from
command-exited-but-cleanup-pending `finishing`. A TERM handler returning zero can
still be cancelled/timed out; launch failure differs from program exit 127.
Output drains during grace and bounded final cleanup. Native containment is the
verified POSIX session (Linux waitid/WNOWAIT, macOS kqueue), not deliberately
detached sessions or an OS sandbox. SSH uses a separate systemd scope boundary.
Unknown ownership/cleanup never becomes success or arbitrary PID adoption.

Output rotates through four 1 MiB segments without killing commands. Absolute raw
byte cursors survive rotation; expired data reports `output_gap` and the oldest
retained offset. Keep full output in explicit producer artifacts, not this cache.
Formats 1..5 and legacy prefix logs remain readable without rewriting; absent
historical facts stay null and old workers retain their original capabilities.

### Find and observe executions

`list_executions` returns receipt summaries and unreadable records, with an opaque
ID cursor and at most 100 entries per page. It exposes no raw commands/logs.
Stable ID order is not chronological or a logical-task lookup; use the original
task/receipt identity instead of picking the newest unrelated execution.

Command starts, worktree creation and execution reads include advisory
`task_feedback`. `unbound` means the receipt names no task; `legacy` means it
references a legacy checkpoint, not structured supervision. `supervised`
requires the exact original execution and machine identity in the named task,
and includes its current revision and existing `foreground` projection.
`unavailable` carries an error code and recovery prompt when task metadata or
the binding cannot be verified. The original status, exit code and output remain
authoritative even when task feedback is unavailable; task observation may lag
the execution. Feedback never claims, renews, accepts or rewrites work.

Independent calls remain supported. If work was meant to continue a structured
task but feedback is unbound, preserve the receipt and reconcile the original
task before the next effect. Do not rerun completed work to fill missing history,
infer ownership from a shared directory, or attach a receipt retroactively.
Honor a verified `foreground.stop_foreground` under the
[bounded waiting rules](../tasks/continue.md#bounded-foreground-waiting).

### Observation and terminal controls

`read_execution(wait_seconds=0..5)` waits only for observation. `max_bytes` is
1..65,536 per read (default 65,536); continue from `next_offset`. Unread bytes, gaps,
terminal state or unavailable ownership return promptly; a future offset is invalid.
A preparing worker without logs is not automatically lost. Quiet `wait_expired`
is not command timeout. At most 16 waiting readers are admitted without a queue;
immediate reads and cancellation retain independent capacity. Cancelling/disconnecting
a waiter ends only its observation, never the command, lease or another cursor.
Lock contention uses the same finite read budget. SSH carries the wait in one
bounded exchange, not per-poll reconnects or a five-second network guarantee.

Read `output_decode_replaced`, `output_redacted`, `output_controls_escaped` and
`output_presentation_truncated` separately from retention/IO loss. Visible escaping
and replacement mean concatenated display pages need not reconstruct source.
Follow raw `next_offset`; read a smaller page at the same source offset when
presentation omitted consumed bytes. Results/SSH envelopes are bounded to 1 MiB,
including JSON escaping and MCP text/structured duplication. Mandatory metadata
overflow is an error, not a silently incomplete success.

`tty=true` explicitly requests a worker-owned controlling terminal; dimensions
are integer rows/cols 1..1000, default 24x80. Pipe mode rejects dimensions.
Initial mode/size are immutable start identity; resize changes current size only.
PTY failure has no pipe fallback. Read `control_ready`, supported actions, current
sequence and last receipt before control. Old workers are not adopted. Overlong
control paths reject PTY before launch; pipes may report controls unavailable.

Controls require the observed sequence (0..2^53-2) and one action: `write_stdin`
sends 1..16384 UTF-8 bytes without NUL/surrogates on PTY, adding no newline;
`resize` takes dimensions only; `interrupt` takes neither and signals the verified
foreground group without cancelling the execution. Pipe controls support interrupt
only. Launch consent is not blanket consent for later input; never send secrets
or automate confirmations. Input fingerprints omit bodies, but terminal echo can
enter retained logs. An idle PTY retains workspace/resource leases.

One private socket carries 128 KiB control frames with a two-second worker budget.
One pending intent is durable before effects, and only its last receipt is retained.
Exact duplicates return retained evidence; conflicts/retired sequences never replay.
Applied means OS acceptance, not application completion. Partial input is a known
prefix, not permission to send a suffix. Lost replies reconcile by original ID and
sequence; unresolved effects block new control but preserve authorized cancellation.
Slow controls do not block output/timeout/cancel. No shared temporary socket fallback,
terminal UI, control takeover or restoration of dead-process memory is promised.
