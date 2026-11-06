# GitHub Configuration Instructions

Read the root [AGENTS.md](../AGENTS.md),
[CONTRIBUTING.md](../CONTRIBUTING.md), and
[MAINTAINERS.md](../MAINTAINERS.md) first.

Keep templates and automation in English. Reuse the managed label manifest;
do not add status, priority, or architecture taxonomies without a current need.

Quality checks must stay read-only, bounded, and runnable on GitHub-hosted
runners without private infrastructure. Primary repository, runtime and required
native jobs pin `ubuntu-24.04`; additional real native acceptance jobs may pin
`ubuntu-22.04` and `ubuntu-26.04`. Runner labels must be explicit and reviewed;
repository variables must not select private machines.

Repository hygiene remains dependency-free; a separate product job may install
the locked public runtime dependencies. Pin external actions to full commit SHAs and disable persisted checkout credentials. Read event JSON as data;
never interpolate untrusted titles or bodies into executable commands.

Do not run candidate code with `pull_request_target` or write tokens.

Label writes belong only to the separate, default-branch workflow and
must preserve unrelated labels. Changes to permissions or external writes need
explicit maintainer authorization, not a mandatory GitHub Review.
Passing CI does not grant merge authority.
