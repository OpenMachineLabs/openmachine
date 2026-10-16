# Open Machine npm installer (macOS Apple Silicon)

This directory is the npm distribution wrapper for the existing Python Core.
It is not a rewrite of Core, a generic platform installer or an MCP tunnel.

The **published release tarball** embeds one exact macOS ARM64 offline runtime,
including Python, Git, the Core wheel and locked dependencies. There are no
npm installation lifecycle scripts. npm installation only unpacks the package:

    npm install -g openmachine@latest
    openmachine

The first run prompts for an **existing project directory** with a harmless
readable file such as README.md. It uses Core management contract 1 to configure
read-only access, install a retained managed runtime, then start the macOS
user service. Machine commands and file editing require separate grants.
For unattended initial configuration:

    openmachine setup --workspace /absolute/project --probe README.md

Run 'openmachine status', 'openmachine start' or 'openmachine stop' afterwards.
Use 'openmachine core ...' to call a native Core CLI command. The ChatGPT
connection and upstream MCP tunnel are not installed or approved by npm.

This wrapper does not change an existing managed Core installation, repair
incomplete history, auto-enable login startup, or replace the retained runtime
when npm updates its own executable. The initial install operation has a
persisted original ID at ~/.config/openmachine/npm-install.json.

## Preparing a publishable npm tarball

Do not publish this source directory with an absent runtime. First build a
trusted Core wheel from a pinned commit and assemble the offline payload using
'scripts/build_runtime_payload.py' as documented in
'openmachine/skills/service/runtime-payload.md'. From the repository root:

    node npm/scripts/package.cjs /absolute/runtime-macos-arm64 /absolute/output

The producer copies exact source wrapper files and packs the runtime as a
single 'runtime.tar.gz' to preserve Python/Git symlinks and file modes. It pins
both archive and manifest digests, runs the same verification as first launch,
then verifies the final npm tarball. The tarball is the input for a separately authorized npm publish.

The release builder still needs provenance, redistribution review and a
real macOS first-run acceptance test before publication. A checksum copied
from an untrusted server is not independent signature verification.
