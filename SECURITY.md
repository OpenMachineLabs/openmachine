# Security Policy

Open Machine Core starts Open Machine SSH sessions as the machine's user. A Full
Access session can do anything that user can; Read Only and Workspace sessions
are confined by macOS Seatbelt. Core is a single-operator access layer, **not** a
multi-tenant security boundary. A sandbox escape, a write outside a Workspace
folder or a read of a denied area from a restricted session is in scope.

## Supported versions

Maintainers assess reports affecting the current `main` branch and the latest
published release. Open Machine is still an initial-development project; older
tags may not receive security backports. This section is not a guarantee of
response or patch timelines.

## Report a vulnerability privately

**Do not create a public Issue or PR containing an exploit, proof of concept,
secrets, host information, or private logs.**

When this repository is public and GitHub private vulnerability reporting is
enabled, select **Security → Advisories → Report a vulnerability** on the
[repository](https://github.com/OpenMachineLabs/openmachine). That form sends
the report privately to maintainers. Its availability depends on the repository
setting; this file does not enable it.

If private reporting is unavailable, use the
[Security Contact Request template](https://github.com/OpenMachineLabs/openmachine/issues/new?template=security-contact.md)
**only to request a secure private contact method**. This creates a public
Issue, not a confidential vulnerability report. Do not include any
technical vulnerability details, logs, credentials, exploit steps, hostnames,
or private files in that request. Wait for a verified private channel before
sharing the technical report.

In a private report, provide the affected version or commit, reproduction
steps using disposable resources, security impact, relevant configuration
conditions, and redacted evidence. Do not test against other people's machines
or accounts without authorization.

Maintainers will evaluate scope and impact and coordinate a fix and disclosure
where appropriate. Please avoid publishing exploit details until a coordinated
disclosure has been agreed. No bug bounty or reward program is promised.

For nonsensitive bugs, use the [Bug template](.github/ISSUE_TEMPLATE/bug.md).
