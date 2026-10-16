# Open Machine npm installer (macOS Apple Silicon)

This directory is the npm distribution wrapper for the existing Python Core.
The npm release version equals the bundled Python Core version (initially 0.2.21).
The release packer and prepublish hook both reject mismatches.
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

    python3 scripts/prepare_npm_release_sources.py --cache /absolute/cache --runtime /absolute/runtime-macos-arm64 --output /absolute/sources
    node npm/scripts/package.cjs /absolute/runtime-macos-arm64 /absolute/output /absolute/sources

The producer copies exact source wrapper files and packs the runtime as a
single 'runtime.tar.gz' to preserve Python/Git symlinks and file modes. It pins
both archive and manifest digests, runs the same verification as first launch,
then verifies the final npm tarball. The release form also embeds the five SHA256-pinned source/build archives from release-sources.json and THIRD_PARTY.md. Omitting the third packaging argument is development-only: prepublishOnly refuses a source-less package. The verified tarball is the input for a separately authorized npm publish.

## Release verification and publication boundary

The manual \`npm-package.yml\` job builds current \`main\` for preview and never
publishes. The separate \`npm-release.yml\` job builds from an exact published,
non-prerelease GitHub tag and automatically publishes only for new stable
\`release.published\` events after npm Trusted Publishing is configured. It
never publishes from workflow_dispatch. For the historical \`v0.2.21\` Release,
the tagged Core commit is 6014a3129b989427c7053ff426424963b47e6d4f;
because that tag predates the npm directory, a manual backfill uses the reviewed
installer from main while the bundled Core is built strictly from that tag.
Future tags must contain the installer; no other fallback is permitted.

The preview job runs only from the protected main branch. It attests the exact unpublished openmachine-*.tgz with a
GitHub Actions OIDC/Sigstore SLSA build provenance statement, then uploads
the tarball. After downloading the artifact, verify its digest and identity:

    gh attestation verify /absolute/openmachine-0.2.21.tgz --repo OpenMachineLabs/openmachine

Build provenance is not Apple Developer signing/notarization or a substitute
for a trusted registry publication. Do not release until a maintainer has:

- Checked the accompanying GPLv2 Git source, corresponding pinned dugite-native
  build scripts, the Git-LFS/GCM/Python build sources, and all license notices
  described in THIRD_PARTY.md; independently completed any remaining native
  component and Python dependency redistribution obligations.
- Tested first-run installation and start/stop with a disposable Apple Silicon
  macOS user-service state and both grants disabled by default. For the first
  npm release, the maintainer has explicitly deferred an independent clean-GUI
  login-account test to the post-publication beta; record that limitation.
  A redirected HOME inside the same login session is not independent acceptance.
- Approved an exact tarball, npm publishing identity, and provenance-bearing
  first registry upload; verified the resulting registry digest and installed
  that registry artifact in the same clean-user test environment.

The first registry bootstrap uses the already-attested manual Release candidate
and account-owner 2FA. Once \`openmachine\` exists on npmjs.com, configure its
Trusted Publisher as GitHub Actions, organization \`OpenMachineLabs\`, repository
\`openmachine\`, workflow filename \`npm-release.yml\`, with direct publish allowed.
The workflow uses GitHub-hosted OIDC and npm CLI 11.5.1; no long-lived npm token
or repository secret is needed. Creating a GitHub Release for a stable tag then
automatically publishes the matching tagged-Core npm version, with npm provenance.
The GitHub \`Packages\` sidebar uses a separate registry and requires a scoped
package; \`openmachine\` remains unscoped on npmjs.org with its GitHub repository
link already specified in package.json. The manual preview workflow never
publishes or changes live Core.
