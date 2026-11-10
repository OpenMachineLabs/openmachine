# GitHub Configuration Instructions

Read the root [AGENTS.md](../AGENTS.md),
[CONTRIBUTING.md](../CONTRIBUTING.md), and
[MAINTAINERS.md](../MAINTAINERS.md) first.

Keep templates and automation in English. Reuse the managed label manifest;
do not add status, priority, or architecture taxonomies without a current need.

Quality checks must stay read-only, bounded, and runnable on GitHub-hosted
Ubuntu 24.04 without private infrastructure. Workflow jobs use fixed
GitHub-hosted runners: `ubuntu-24.04`, and `macos-15` only for the manual
runtime-payload build. Repository variables must not select private machines.

Repository hygiene remains dependency-free; a separate product job may install
the locked public runtime dependencies. Pin external actions to full commit SHAs and disable persisted checkout credentials. Read event JSON as data;
never interpolate untrusted titles or bodies into executable commands.

Do not run candidate code with `pull_request_target` or write tokens.

Label writes belong only to the separate, default-branch workflow and
must preserve unrelated labels. Changes to permissions or external writes need
explicit maintainer authorization, not a mandatory GitHub Review.
Passing CI does not grant merge authority.
