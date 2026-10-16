# Install OpenMachine Core from source

On Apple Silicon macOS, the shorter route is the [npm installer](npm-macos.md).
This guide builds the released Python Core from an exact Git tag on macOS or
Linux. It does not start a service, configure a tunnel, grant write permission
or connect ChatGPT.
Complete the [Python, SQLite and Git checks](../introduction/requirements.md)
first. The following commands run in a terminal on your controller.

## Build the released wheel

Choose a parent directory that does not already contain an `openmachine` clone:

```sh
git clone --branch v0.2.21 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
git rev-parse HEAD
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.21-py3-none-any.whl
.venv/bin/openmachine --version
```

For this release, `git rev-parse HEAD` must report
`6014a3129b989427c7053ff426424963b47e6d4f`. The CLI must report version `0.2.21`.
A locally built wheel may report an unknown installed commit; the source checkout
SHA is build provenance, not proof of an installed runtime's recorded commit.

Use this tag's `requirements.lock` for both the build and runtime dependencies.
Do not mix a wheel from one version with another version's lock. The wheel
includes built-in Skills and direct adapters, not tests or your private state.

## Make the CLI available in this terminal

From the clone root:

```sh
export PATH="$PWD/.venv/bin:$PATH"
openmachine --help
```

This changes only the current shell. In another terminal, use the absolute path
to `.venv/bin/openmachine` or set that terminal's PATH deliberately. Do not move
or delete an environment while a service or original worker still uses it.

## Verify the installation boundary

`--help` and `--version` establish that the selected CLI can run. They do not
validate your configuration, SQLite task store, workspace access or connection.
Next, create a disposable workspace in
[Initial configuration](configuration.md).

## Common failures

| Symptom | Check |
| --- | --- |
| Clone requests authentication or returns not found | Repository access and the exact official URL; do not put tokens in a command. |
| Python version rejected or `task_sqlite_unsupported` | The interpreter and its linked SQLite engine, including the interpreter used by the service. |
| Dependency download fails | Network, package-index policy and this exact release's lock. Do not silently unpin dependencies. |
| `openmachine` is not found | Use `.venv/bin/openmachine` from the clone or its absolute path. |
| Another version appears | Check which executable your shell selected; source checkout version is not installed version. |

Release notes and artifacts are listed on the
[release page](https://github.com/OpenMachineLabs/openmachine/releases/tag/v0.2.21).
This source-build route is independent of the npm installer.
