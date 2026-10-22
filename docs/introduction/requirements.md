# Requirements and supported platforms

## npm installer (Apple Silicon macOS)

Requires macOS 13+, Apple Silicon (ARM64) and Node.js 24 LTS (recommended) or 22 LTS. The [npm installer](../getting-started/npm-macos.md) bundles Python, Git, Core and its locked dependencies. The checks below apply to manual source installations.

## Manual Core requirements

Use **Python 3.12 or newer**, Git and a writable private state directory under
the account that will run Core. The Python interpreter must also provide a
supported SQLite engine: **3.51.3 or newer**, **3.50.7+ within 3.50.x**, or
**3.44.6+ within 3.44.x**. A recent Python version alone does not prove this.

Run this with the same `python3` you will use to create the virtual environment:

```sh
python3 -c 'import sys, sqlite3; v = sqlite3.sqlite_version_info; print(sys.version); print("SQLite", sqlite3.sqlite_version); assert sys.version_info >= (3, 12), "Python 3.12+ required"; assert v >= (3, 51, 3) or (3, 50, 7) <= v < (3, 51, 0) or (3, 44, 6) <= v < (3, 45, 0), "Unsupported SQLite engine"'
git --version
```

An assertion failure is a prerequisite failure, not a reason to bypass the task
database check. Install an appropriate Python distribution through a trusted
source, then repeat the check. Core does not download an interpreter during
ordinary source installation. The separate offline payload producer is described
under [Service and runtime](../features/service.md).

## Platform matrix

| Platform | Supported path | Important condition |
| --- | --- | --- |
| macOS Apple Silicon (npm) | npm installer and managed user service | macOS 13+, Node.js 24 LTS recommended or 22 LTS; first-run setup required. |
| macOS (source) | Manual Core serving and managed user service | Managed lifecycle requires the owning logged-in GUI user. |
| Linux controller | Manual `openmachine serve` | No managed Linux service is provided by these commands. |
| Linux SSH target | Explicitly prepared worker | Python 3.12+, Git, OpenSSH access and systemd 255+ user manager for command execution. |
| Windows | Not supported by this release | Do not assume a Windows service or Windows SSH worker exists. |
| Offline runtime payload | Apple Silicon, macOS 13+ artifact format | An advanced producer interface, not a downloadable signed installer promise. |

Manual Core support is not a claim that every optional integration works on
every platform. In particular, [Browser](../features/browser.md) requires the
compatible native desktop bridge and login described there.

## Network and account prerequisites

Building from a release needs access to the Git repository and the locked package
dependencies. Before public repository access is enabled, cloning also requires
repository permission; this documentation does not change repository visibility.

ChatGPT needs a supported MCP connection and applicable account/workspace
approval. A private tunnel additionally needs the upstream tunnel permissions,
credentials and correct organization/workspace association. Check these in
[Connect to ChatGPT](../getting-started/connect-chatgpt.md) before treating local
installation as end-to-end readiness.

GitHub collaboration, SSH access and browser control each have separate
prerequisites. Start with read-only configuration and enable only what you need.
