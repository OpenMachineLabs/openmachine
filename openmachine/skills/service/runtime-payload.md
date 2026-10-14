# Packaged runtime

On this page:

- [One payload, one installer](#one-payload-one-installer)
- [Produce the payload](#produce-the-payload)
- [Prepare without activating](#prepare-without-activating)
- [Validation boundary](#validation-boundary)

Core provides an offline runtime preparation path. Preparation does not register
or start a service, change its selected version, or alter persistent startup policy.
The existing wheel/requirements lifecycle commands remain supported.

## One payload, one installer

The release producer supplies a relocatable Python distribution already containing
the selected Core and its dependencies, portable Git, the complete dependency
wheelhouse, the Core wheel, pinned requirements, and redistribution notices. This
makes the ordinary Core CLI available before user installation; no separate
bootstrap interpreter, installer implementation, or developer tool is required.
Only `python -m` is supported for this relocatable bootstrap. Its generated
build-path entry-point scripts are not distributed.

The payload's `bin/git` entry binds Git's bundled helpers and templates relative
to its retained directory; adding only the native Git binary to PATH is not
sufficient. The underlying Git binaries and libraries are kept in their original
layout. Normal Git arguments are forwarded unchanged, without shell evaluation.

`manifest.json` format 1 binds the target (Apple Silicon, macOS 13+), exact Core
version/source commit, Python/Git builds and upstream archive digests, and every
payload file/directory/symlink. Files bind content, size, and executable status;
links must resolve inside the payload. The trusted manifest digest is part of the
product's artifact identity. Changing an interpreter build or a dependency changes
the installation identity even when the Core/Python version string stays the same.

**Trust comes before execution.** A digest is not a signature. A product must
verify its app/updater signature before running the bundled Python, and supply the
manifest digest from that trusted artifact. Developer use requires an explicitly
trusted build. Never execute a downloaded interpreter merely to establish whether
its own unauthenticated manifest is trustworthy. Signing/notarization and public
product distribution are outside this preparation API.

## Produce the payload

On an Apple Silicon build machine, build and install the selected Core wheel in
the repository's isolated build environment, then run:

```sh
.venv/bin/python scripts/build_runtime_payload.py \
  --wheel /absolute/build/openmachine-0.2.20-py3-none-any.whl \
  --requirements /absolute/source/requirements.lock \
  --commit "$(git rev-parse HEAD)" \
  --cache /absolute/build/runtime-inputs \
  --output /absolute/build/runtime-macos-arm64
```

The supplied commit must be the source used to build that wheel, not an unrelated
checkout HEAD. The script checks that the installed payload implementation matches
the wheel. `scripts/runtime-inputs.json` pins Python and Git; there is no latest
release discovery. Downloads happen only in this build step. Dependency wheels
must match CPython 3.13 / macOS 13 / arm64; source builds and index fallback during
local preparation are prohibited. The same `payload.inventory` / `verify` logic
seals and consumes the result. A failed build/copy is preserved, not overwritten.

The output is a bundleable directory plus a printed manifest SHA256;
it is not a signed DMG. Preserve upstream notices and dependency licenses. Product
publication must also satisfy corresponding-source/redistribution obligations for
all bundled components; a source URL alone is not a declaration of legal clearance.
Validate the advertised OS floor and native signing with the final distributed artifact.

## Prepare without activating

After selecting an explicitly trusted payload and an existing validated Core
configuration, invoke its bundled CLI (or an already installed compatible CLI):

```sh
"$PAYLOAD/python/bin/python3" -I -B -m openmachine.cli \
  --config "$CONFIG" --state-dir "$STATE" \
  prepare-runtime --payload "$PAYLOAD" --sha256 "$TRUSTED_MANIFEST_SHA256"
```

All three paths are absolute. A new configuration can be created with the existing
`init --workspace` command; execution and typed writes remain disabled until
separately authorized.

Preparation uses the existing service operation lock. It verifies the full payload,
retains a private read-only copy, checks the retained Python/Git versions, then
uses `service/install.py` to create an isolated venv at its final path. Pip has an
isolated environment, no package index/cache or source-build fallback, and installs
only the verified local wheelhouse. Existing candidate state readers validate the
installed environment before its installation record is committed.

```text
STATE/service/payloads/<manifest-sha256>/  retained Python, Git and input files
STATE/service/versions/<identity>/        final venv and installation.json
```

The JSON result reports `status: prepared`, the installation/payload identities,
actual version, runtime path, retained `launch_python`, and `path_environment` with
portable Git first. `selection: unchanged` and `service: not_started` are explicit:
this is not service readiness, activation, transport readiness, or ChatGPT acceptance.
The lifecycle owner consumes these retained paths through `manage install/update`. Do not point
a LaunchAgent or original worker at the replaceable source App.

Retained copies and previous environments are not moved, overwritten, or garbage
collected. Incomplete preparation remains a diagnostic failure, not permission to
repeat installation into the same directory. Matching completed identities are
reused. Artifact checks and compatible activation remain separate; user workloads,
configuration, native connections, task observation and its budgets are not
migrated or reset. Read-only payload permissions prevent ordinary cache writes,
not tampering by the same native account.

## Validation boundary

Focused integrity/installer tests live in `test/component/service/test_payload.py` alongside
existing lifecycle tests. Native acceptance must additionally build the real
payload, prepare outside the checkout with package-index/network access denied,
exercise installed CLI/MCP file readiness and portable Git, and verify retained
interpreter/worker paths after removing the donor App path. Use a disposable
configuration/state/workspace, never an active service or production job.

Preparation evidence does not establish GUI onboarding, persistent-stop behavior,
signed distribution, automatic updates, or plugin availability.

Managed product installation/update now uses [management contract 1](management.md#product-management),
which records the original operation before independent preparation and invokes
the verified incoming implementation from retained paths. `prepare-runtime` remains
a preparation-only primitive; it neither selects a service nor creates update consent.
