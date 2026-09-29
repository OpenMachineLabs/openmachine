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
Unix sockets. They do not use production projects, SSH, GPUs, GitHub credentials,
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
