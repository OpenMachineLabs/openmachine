# Current limitations

This guide describes released Core **v0.2.21**, not a roadmap. The release tag
and the live runtime's version/commit are the starting points for compatibility.

## Product and distribution

Core is a native runtime, not a chat UI, a hosted cloud service, a general
knowledge database or a multi-user control plane. These guides do not require
a companion desktop application. No Windows or managed Linux service is promised.

The release provides source and release notes. Do not assume a PyPI package,
Homebrew formula, npm installer, signed DMG or attached wheel exists. Follow the
[exact-tag installation](../getting-started/installation.md).

The Core MIT license does not grant access to ChatGPT, Codex, GitHub or a tunnel.
Client account eligibility, organization policy and individual tool approval
remain separate. A private custom MCP connection is not public plugin distribution.

## Persistence is not autonomous conversation

Tasks and operation receipts survive compatible controller restarts. Core can
observe already admitted results without the original caller, but it does not
automatically wake ChatGPT or decide and execute the next task stage. Work Events
and automatic Chat recovery are not supported paths in this release.

There is no guarantee through reboot, lost state, deleted worker archives,
interpreter removal, logout, sleeping machines or deliberate process escape.
An execution that timed out or was cancelled may still have produced effects.
Cancellation and software rollback do not undo files, messages or deployments.

## Security and evidence

Workspace read scopes are not an OS sandbox for native commands. A command runs
with the native account's permissions. Core is not a tenant-isolation system.
Browser origin restrictions guide and check delegation; they are not a firewall.

Discovery and output are bounded. An incomplete search is not proof of absence;
an exit code of zero is not proof of business success; a local socket handshake
is not ChatGPT acceptance. Redaction and credential-path exclusions are not a
guarantee that arbitrary output contains no sensitive data.

## Release versus main

At the documentation baseline, `main` contains post-v0.2.21 client-call diagnostic
changes from PR #156. In particular, the server catalog fingerprint and enriched
invalid-call diagnostic fields must not be required from the v0.2.21 release.
The supported troubleshooting approach is to compare the actual client schema
with the actual server catalog and preserve the original error.

Always inspect the running version's Skills. A merged change does not update an
installation or refresh a client's approved tools. See
[updates](../getting-started/updates.md) and [troubleshooting](../troubleshooting.md).
