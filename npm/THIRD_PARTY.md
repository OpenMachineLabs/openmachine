# Third-party programs and corresponding source

The npm package includes independent third-party binary programs and a Python
environment. The Open Machine MIT LICENSE does not relicense those programs.

## Git 2.53.0 (GPLv2)

The packaged Git executable and helper programs originate in
dugite-native v2.53.0-3, built from Git source commit
67ad42147a7acc2af6074753ebd03d904476118f (tag v2.53.0).
The package accompanies machine-readable Git source, including its Makefile,
in third_party_sources/git-2.53.0.tar.xz. The build instructions and exact
macOS ARM64 flags are in third_party_sources/dugite-native-f49d009.tar.gz
(script/build-macos.sh, dependencies.json and script/package.sh).
The full Git GPLv2 terms and copyright notices are in the Git source COPYING
and the dugite-native source LICENSE.md. The npm wrapper makes no changes to
those upstream Git source files.

## Bundled Git helpers and Python

- Git LFS v3.7.1: MIT license, Go-derived third-party notices and upstream code:
  third_party_sources/git-lfs-v3.7.1.tar.gz.
- Git Credential Manager v2.7.3: MIT license, upstream code and dependencies:
  third_party_sources/gcm-v2.7.3.tar.gz. Its native NOTICE remains in
  runtime.tar.gz under runtime/git/libexec/git-core/NOTICE.
- Python 3.13.16: the Python license remains within runtime.tar.gz under
  runtime/python/lib/python3.13/LICENSE.txt. The build recipes for the pinned
  standalone Python distribution are in
  third_party_sources/python-build-standalone-20261003.tar.gz.
- Pinned Python dependencies: runtime.tar.gz preserves requirements.lock, all
  dependency wheels in runtime/wheels, and each distribution's license files
  under runtime/python/lib/python3.13/site-packages/*.dist-info/licenses.

All five accompanying archives and their upstream URLs, source identities and
SHA-256 hashes are in release-sources.json. The producer verifies the complete
archives and the packager checks their digests before building the npm tarball.
These materials support recipient access to corresponding source code and
rebuild inputs; a release maintainer must still review all license obligations,
including native components and transitive third-party notices, before public
registry publication. Hashes and GitHub build provenance are not a legal
redistribution approval or Apple Developer code signing.
