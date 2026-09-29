# Tests

| Directory | Responsibility | Entry point |
| --- | --- | --- |
| `system/` | Dependency-free repository and metadata checks | `python3 -m unittest discover -s test/system -p 'test_*.py'` |
| `unit/` | Configuration, readers, execution receipts and failure behavior | Installed-environment command below |
| `contract/` | Real MCP SDK discovery, private socket, coding workflow, cancellation and restart | Installed-environment command below |

Build and install the wheel as described in the root README. Run product checks
outside the checkout so source imports cannot hide missing wheel files:

```sh
REPO="$PWD"
PYTHON="$REPO/.venv/bin/python"
cd /tmp
"$PYTHON" -m unittest discover -s "$REPO/test/unit" -p 'test_*.py'
"$PYTHON" -m unittest discover -s "$REPO/test/contract" -p 'test_*.py'
```

The system suite remains standard-library-only. Product tests use the locked
runtime dependencies, disposable directories, short local processes and private
Unix sockets. CI does not use production projects, live SSH, GPUs, GitHub credentials,
Codex or inference APIs. No live account is needed for CI.

Local MCP tests are **not** real ChatGPT/Tunnel acceptance. Connect a separately
authorized upstream tunnel to the private socket and run the README's disposable
coding workflow from ChatGPT. Record that result against the candidate
commit and actual account surface. Missing access, client refresh or a real
write receipt remains `NOT RUN`, not `PASS`.

Keep all tests under root `test/`. Add a group only with behavior that needs it;
do not add placeholder suites or a second test framework.

The local workflow checks use small disposable Git projects, short owned process
groups and a few MiB of output. The 24-hour upper bound is validated with a short
command, not a 24-hour wait. No GPU or production canary is required.

Skills, task persistence and explicit edits use small private fixtures in the
existing unit/contract groups. Tests distinguish never-initialized state from
missing history, cover stale/torn saves and original execution links, and use
separate SDK sessions to verify no duplicate file/test execution. Worst-case
JSON escaping is exercised with one bounded text file. This is local protocol
evidence, not actual cross-conversation ChatGPT acceptance; record that separately
on the refreshed candidate connection before marking M2 accepted.

SSH unit checks cover configured routing, bounded structured transport, identity
and file preconditions, controller-only task references, shared target locks,
format-3 cleanup observations and dependency-free deterministic worker bundles.
Mocked transport is explicitly not live SSH evidence. The SDK checks verify
remote metadata and local independence without contacting an unconfigured host.

M3 live acceptance uses an explicitly prepared Linux CPU-only disposable project
and the exact installed worker artifact. Record real SSH file/test/diff, same-ID
reconnection without replay, owned-child cancellation, alias contention and
important prerequisite failures. Separately exercise the refreshed tools from
actual ChatGPT; neither native adapter calls nor SDK sessions substitute for it.
Preserve original executions and unrelated fixture work. No production canary,
private CI runner, long benchmark or automatic service migration is required.
