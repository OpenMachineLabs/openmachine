# Security Policy

## Scope

This repository holds what runs on your machines: `om`, device identity, Core
and the network library. Report a flaw in any of them here. Report a flaw in
the closed Mac app, the Console or the service the same way.

In scope, for example: a session that escapes Read only or Workspace, a
connection admitted without a current grant, a rule or record that `om` skips
for a command it should cover, a way to make `om` or device identity act for
another account, or a token reaching a machine.

## What the engine guarantees, and what it does not

- **Access levels.** Core starts every OM SSH session as the machine's user.
  Read only and Workspace sessions run in macOS Seatbelt or Linux bubblewrap.
  Full access is that user's authority, **not a sandbox**. Core is a
  single-operator access layer, not a multi-tenant security boundary.
- **Read only and Workspace limit writes.** Such a session can still read
  most of the user's files and reach the network. On macOS it can read other
  processes' environments, so it can use another Read only or Workspace
  session's GitHub access, which only reads; a Full access session's needs a
  proof it cannot read.
- **Pages and models on a Mac.** On macOS, `om`'s address `localhost:11400`
  and the ports of opened pages answer any user of that Mac; on Linux, only
  `om`'s own user.
- **The machine enforces.** Rules, Read only and Workspace, and command
  recording are enforced by `om` and Core on the machine that runs the
  command. The Console's "applied" is that machine's own report. Whoever
  controls a machine, including a Full access session on it, can change what
  it enforces and reports.
- **Rules read the command line only.** Not what a script or program does, what
  a variable, wildcard or pipe supplies, or where `cd` went; not a terminal, a
  shell fed line by line, a remote editor's server, SFTP or forwards, which
  only a lock stops. Rules are a guard against mistakes, not against a
  determined user.
- **Logs.** A machine signs its records with its device key. A Full access
  session can use that key, so a valid signature names the machine, not the
  person. Output, what is typed in a terminal and the files a session reads or
  writes are never recorded; a command line is recorded only where the
  account turned recording on, with the secrets `om` recognizes removed on the
  machine.
- **GitHub.** No GitHub token is stored on a machine: the service adds it to
  each request. A session given GitHub access reaches every repository the
  account's GitHub connection can reach; there is no per-repository
  allow-list. Only Full access pushes and opens pull requests.
- **A Mac sets its own access level**, with a request signed by its device
  key. Code running as that Mac's user can do the same.
- **Mac preview limitation.** The app starts `om` from a file the user can
  write, without verifying it again, so other code running as the same macOS
  user can act with the app's macOS permissions. See
  [docs/mac-app.md](docs/mac-app.md).

How each part works: [docs/om.md](docs/om.md) and [docs/device.md](docs/device.md).

## Supported versions

Maintainers assess reports against `main` and the latest release. Older
versions may not receive fixes. This is not a guarantee of response or patch
timelines.

## Report a vulnerability privately

**Do not open a public Issue or PR containing an exploit, proof of concept,
secrets, host information or private logs.**

Select **Security → Advisories → Report a vulnerability** on the
[repository](https://github.com/OpenMachineLabs/openmachine). The report goes
privately to maintainers.

If that form is unavailable, use the
[Security Contact Request template](https://github.com/OpenMachineLabs/openmachine/issues/new?template=security-contact.md)
**only to ask for a private contact method**. It creates a public Issue: put
no vulnerability details, logs, credentials, exploit steps, hostnames or
private files in it, and wait for a private channel.

In the private report, give the affected version or commit, reproduction steps
using disposable resources, the impact, relevant configuration and redacted
evidence. Do not test against other people's machines or accounts.

Maintainers evaluate scope and impact and coordinate a fix and disclosure.
Please do not publish details until disclosure is agreed. There is no bug
bounty.

For bugs that are not sensitive, use the [Bug template](.github/ISSUE_TEMPLATE/bug.md).
