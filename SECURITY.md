# Security Policy

Open Machine can run commands and modify files on machines where the operator
has explicitly granted those capabilities. It is a trusted single-operator native
runtime, **not** an OS sandbox or a multi-tenant security boundary. Review
[setup and trust boundaries](openmachine/skills/overview/setup.md) before treating
unexpected behavior as a vulnerability.

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

If the private reporting option is unavailable, open a
[public Issue](https://github.com/OpenMachineLabs/openmachine/issues/new/choose)
**only to request a secure private contact method**. Do not include any
vulnerability details in that request. Wait for a verified private channel before
sharing the technical report.

In a private report, provide the affected version or commit, reproduction
steps using disposable resources, security impact, relevant configuration
conditions, and redacted evidence. Do not test against other people's machines
or accounts without authorization.

Maintainers will evaluate scope and impact and coordinate a fix and disclosure
where appropriate. Please avoid publishing exploit details until a coordinated
disclosure has been agreed. No bug bounty or reward program is promised.

For nonsensitive bugs, use the [Bug template](.github/ISSUE_TEMPLATE/bug.md).
