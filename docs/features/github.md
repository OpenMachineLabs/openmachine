# GitHub integration

Core provides scoped native GitHub operations for repositories, Issues, pull
requests, comments, reviews, merge checks, Projects and Releases. Use them to
connect code changes with verifiable remote outcomes, without giving a task
unbounded access to your account.

## Configure repository scope

Add a GitHub section to the selected Core configuration. Replace these examples
with actual account/repository names and apply the configuration deliberately:

```toml
[github]
account = "my-account"
read_repositories = ["owner/reference"]
write_repositories = ["owner/project"]
read_owners = []
write_owners = []
attribution = true
```

Write scope includes reads. Empty scopes grant no repository access. Prefer
exact repository scope to an owner-wide grant. Do not mix legacy `repositories`
with the explicit read/write forms. Core scope, App resource installation and
GitHub account authorization must all allow the operation.

## Connect the native account

Ask the assistant to inspect `list_integrations`, then start **one** explicitly
authorized `connect_integration` Device Flow with a saved request ID. Complete
the returned GitHub URL/code yourself, and install/approve the official Open
Machine GitHub App for the intended resources using the returned installation
URL. User identity consent alone does not grant repository access.

Read the original login ID with `read_integration_login` until its outcome is
known. Core owns the finite login attempt; passive reads do not start another.
No PAT, App private key, client secret or custom App registration is required.
The retired `existing_gh` backend is not a fallback for current runtime requests.

Native clients can also use the [private web authorization contract](../reference/github-web-authorization.md).
Core retains local approval, independently verifies the authorized account and
saves credentials in the same pinned store. Browser authorization and web-token
refresh use an external broker; ordinary GitHub operations still run directly
on Core. These private interfaces are not public assistant tools.

With the selected absolute `CONFIG` and `STATE`, probe the intended repository:

```sh
openmachine --config "$CONFIG" --state-dir "$STATE" doctor \
  --github --repository owner/project
```

Check the returned account, repository access and error, not just local saved
connection status. Git clone/push authentication is separate from this App
connection; Core does not configure a global Git credential helper.

## Use the integration

```text
Use OpenMachine's native GitHub connection for owner/project. Read the current
repository and open pull requests, then summarize their actual check status.
Do not create, edit, comment, review or merge anything.
```

For a write, authorize the specific destination and intended effect. A PR workflow
normally reads the Issue and source, edits/tests in a workspace, pushes an explicit
branch, creates one PR, then reviews its complete current diff/checks/discussion.
Merge requires the actual current head/base and all applicable GitHub gates.
Neither passing CI nor creating a PR grants merge permission.

## Supported operations and boundaries

| Area | Supported use |
| --- | --- |
| Repository/source | Read metadata, branches, exact commits/files, history, releases and checks. |
| Issues / PRs | Create/update, labels and assignees, PR readiness; read back the same destination. |
| Comments / reviews | Top-level comment create/update/delete, review create/update, thread resolve/unresolve with guards. |
| Merge | Checked merge using explicit current head/base; verify the destination commit. |
| Projects | Read supported organization Projects metadata; add an existing Issue/PR and update a supported field. |
| Releases | Create a Release for an existing verified tag with explicit notes; default is draft. |

This is not an arbitrary GitHub API proxy. It does not supply Git push, repository
administration, asset upload or unlimited Project mutation through these tools.
Pagination and diff truncation must be resolved before review; a partial diff
cannot establish that the whole PR is safe.

## Verify, disconnect and recover

Record original task/operation IDs, account binding, repository and returned
Issue/PR/comment IDs. Read the actual destination after writes. An uncertain
create or merge is not permission to send a duplicate. A self-review comment is
not an independent approval and cannot override branch protection.

`disconnect_integration` uses the observed authorization revision to disable the
local connection and clean up owned credentials. It is not proof of provider-side
grant revocation; manage GitHub's authorization separately when needed.
Credentials use the pinned native backend (macOS Keychain, otherwise private
files), not your task logs. Reauthorization can be necessary after revocation,
host loss or prolonged offline periods.

Default attribution appends the product's footer to supported new GitHub content;
disabling it stops new automatic footers, not removal of existing attribution.
See the [connection guide](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/github/connections.md)
and [GitHub reference](https://github.com/OpenMachineLabs/openmachine/blob/main/openmachine/skills/github/reference.md)
for exact guards, current actions and original-destination recovery.
