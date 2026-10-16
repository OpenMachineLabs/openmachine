# Browser subtasks

Core can delegate a **bounded browser/UI goal** to a compatible native Codex
installation while ChatGPT remains the parent controller. This is useful for
inspecting an actual rendered page or a permitted web workflow that needs UI
evidence. It is not general desktop control or coding-agent delegation.

## Prerequisites

Use a configured **local** machine with command execution enabled, an authorized
workspace and a compatible signed-in Codex installation with its enabled native
`node_repl` browser bridge. The adapter discovers that configured bridge and its
installed browser Skill; it does not install a browser, library, plugin or login.

The supported authentication setup must include a safe existing file-backed
Codex login. A missing/unsafe login, including a keyring-only setup unsupported
by this adapter, is a blocker, not permission to copy credentials or change stores.
CLI presence and passive discovery metadata do not prove usable browser access.

No browser section is added to Core TOML. The actual tool supports browser mode
only and browser selectors `auto`, `iab`, `chrome` or `edge`, subject to the
installed native bridge's capabilities and permissions.

## Give a precise goal

```text
Use OpenMachine's browser adapter only to inspect https://example.com in a new
task-owned tab. Allow only the https://example.com origin. Report the visible
page title and heading with native page evidence. Do not sign in, submit forms,
download files, change settings or inspect unrelated tabs. Stop on a permission
prompt or navigation outside the allowed origin.
```

The assistant supplies `start_codex_task` with the exact start URL, allowed
origins, self-contained goal, local workspace, original execution ID and a bounded
timeout. A structured parent task admits the underlying execution. Allowed origins
are instructions and observed checks, **not** a network firewall or isolation
from another browser user.

## Confirm the actual result

Read the same ID using `read_codex_task`. Review semantic status, the final native
page evidence and any pending action. Generic `read_execution` exit zero means
the wrapper finished, not that the intended browser action succeeded.

`blocked` and `needs_confirmation` require their stated resolution. A new
authorized follow-up retains the parent task and exact tab context but uses a
new operation ID. Do not replay an uncertain form submission or infer a purchase,
message or configuration change from a successful controller exit.

## Limits and recovery

Timeouts are bounded to 1-1,800 seconds. Cancel through the original execution,
then inspect the receipt; cancellation is not rollback of browser effects. The
runtime's browser resource lease does not coordinate manual browsing or unrelated
Codex sessions. Remote SSH browsers and desktop computer mode are not supplied.

The [browser operating guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/browser/reference.md)
owns precise bridge, login, tab-evidence and delegation boundaries.
