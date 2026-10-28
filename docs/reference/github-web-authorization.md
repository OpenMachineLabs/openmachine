# Private GitHub web authorization

Core accepts GitHub App user credentials through its existing owner-only Unix
service and executes GitHub operations locally. Browser authorization and the
App client secret belong to the external authorization broker. These version-1
interfaces are native owner contracts, not MCP tools or credential import APIs.
The public `connect_integration` Device Flow remains available and unchanged.

## Local transport and identity

Use the selected Core's existing `STATE/mcp.sock`. Verify its configured path,
private parent, socket type, owner and mode before connecting. A native client
must bind a new `STATE/web-<32 lowercase hex>.sock` address with mode `0600`
before connecting. Core checks the actual Unix peer UID, this exact private
parent and client socket identity. Unbound clients, TCP, browser `Origin`,
forwarding headers and non-local Host values cannot use the private routes.
After connecting and before sending any body, the native client must verify the
server's native peer UID and recheck the originally pinned state-directory and
Core socket identities. A changed endpoint must fail before credentials are sent.
Remove only the client's own socket after the response; never replace an
existing path. Platform Unix-socket path limits apply; failure has no unbound
or alternate-Core fallback. This is an OS-account owner boundary, not isolation
from arbitrary programs already acting as that native owner.

All new HTTP bodies are strict JSON objects with `v: 1`; unknown or duplicate
fields and incorrect types are rejected. GET requests use exactly `?v=1`.
POST requests have no query and use `Content-Type: application/json`. Bodies
are bounded to 32 KiB. Responses use `Cache-Control: no-store`. Error responses
contain a bounded error code, never a submitted body, provider diagnostic or
token. Native adapters must also suppress request/response body logging.

Read `GET /internal/github/web/identity?v=1` to obtain
`{v, core_binding}` from this verified local Core. The opaque binding combines
an identifier retained in the existing connection store with the private local
state directory identity. It survives restart, differs between independent Core
instances, and rejects copied/replaced state at another local identity. A caller
cannot choose a filesystem path as a Core binding.

Local user approval must precede `begin`. The broker separately binds the
verified OM principal, device, device key and grant revision to this exact Core
and original intent. The OM sign-in account and GitHub account may differ.
The private Core boundary neither grants remote device consent nor enlarges
existing account/repository policy.

## Begin, complete and observe

| Method and path | Exact request fields | Successful response fields |
| --- | --- | --- |
| `POST /internal/github/web/begin` | `v`, `intent_id`, `core_binding`; optional paired `expected_connection_id`, `expected_authorization_revision` | `v`, `intent_id`, `core_binding`, `expires_at_ms`, `status: "pending"` |
| `POST /internal/github/web/complete` | `v`, `intent_id`, `claim_id`, `github_account_id`, `tokens` | `v`, `intent_id`, `connection_id`, `authorization_revision`, `status: "connected"` |
| `GET /internal/github/web/status/{intent_id}?v=1` | Original ID in path; version in query | `v`, `intent_id`, `core_binding`, `expires_at_ms`, `status`; optional `error_code`; connected receipts also include `connection_id`, `authorization_revision` |
| `POST /internal/github/web/cancel` | `v`, `intent_id` | Same bounded original status receipt |

`intent_id` and `claim_id` are independently generated 32-character lowercase
hex identifiers. Save each original ID before dispatch. New connections omit
both expected binding fields. Reauthorization requires both observed fields,
the same GitHub account, and an inactive connection; an active connection is
never silently replaced. The existing account-derived `connection_id` stays
stable. Reauthorization retains the connection's existing repository policy in
addition to current operator policy.

`tokens` has exactly these five fields:

```json
{
  "access_token": "<secret GitHub App user access token>",
  "refresh_token": "<secret rotating refresh grant>",
  "expires_in": 28800,
  "refresh_token_expires_in": 15897600,
  "token_type": "bearer"
}
```

These are placeholders documenting a private native body, never an AI tool
argument. Token strings stay inside the broker/Core process boundary and the
existing pinned Keychain or private credential store. Core independently reads
GitHub `/user`, verifies the claimed numeric account and local account pins,
and saves/readbacks credentials before reporting Connected. Broker installation
verification does not replace Core's existing repository permission checks.

An intent expires ten minutes after its original creation. Repeating a matching
pending `begin` returns its original deadline; it cannot extend approval.
Core retains `pending`, `claimed`, `connected`, `cancelled`, `expired`,
`rejected` or `outcome_unknown`. Browser/claim progress before delivery belongs
to the authorization broker and does not imply a connected Core.

Core durably consumes an intent and claim before token validation, GitHub IO
or credential publication. `complete` never imports a consumed claim twice.
Observe a lost response through `status` and `list_integrations`; a failed,
cancelled, expired or interrupted completion requires fresh user authorization.
An original Connected receipt describes that completion; `list_integrations`
reports the connection's current state after later disconnect/reauthorization.
Cancellation does not disconnect an already connected account or revoke GitHub
consent. Pending approval survives restart; an interrupted consumed claim is
reported as `outcome_unknown` and cannot be resumed or replayed.

## Fixed local refresh broker

For web credentials only, Core connects to `STATE/github-web-broker.sock` and
sends `POST /internal/github/web/refresh`. It verifies the exact private socket
owner, permissions, inode and native peer before sending secrets, and binds its
own ephemeral owner-only client address as above. There is no configurable URL,
redirect, arbitrary provider endpoint or alternate device. The broker adapter
must verify this native client and selected Core binding, then use its existing
signed exact-device Cloud transport. It must recheck principal/device/grant
ownership and revocation and must never persist or log token pairs.

The request has exactly:

```text
v, refresh_id, connection_id, github_account_id, core_binding,
expected_authorization_revision, refresh_token
```

The successful no-store response has exactly:

```text
v, refresh_id, access_token, refresh_token, expires_in,
refresh_token_expires_in, token_type
```

The local broker must return HTTP `200`, `Content-Type: application/json`, an
explicit `Content-Length` and `Cache-Control: no-store`. Core rejects redirects,
repeated headers, transfer encoding (including chunking), and content compression.
Response headers are bounded to 8 KiB and the JSON body to 16 KiB; the response
must arrive within Core's ten-second deadline. A framing or deadline failure
has the same no-retry, reauthorization outcome as a lost rotation response.

Core generates and durably saves a new original `refresh_id` and `refreshing`
phase before dispatch. It matches the response's `refresh_id`, rechecks the
retained Core/account/revision bindings, independently verifies the rotated
token's GitHub actor, and durably saves and reads back the rotated pair before
use. A changed revision cannot
overwrite newer credentials. A missing broker, offline Cloud, timeout, malformed
or lost response, revocation, failed write or interrupted rotation requires
reauthorization. Core never retries the exchange or restores the previous pair
after possible rotation.

An unexpired local web access token remains usable without Cloud. Core requests
web renewal only on expiry or a definite first authentication rejection; idle
maintenance does not make a valid local token depend on Cloud availability.
An uncertain business mutation is never repeated to refresh authentication.
Legacy credentials omit the source field and default to `device_code`, retaining
their existing direct GitHub refresh behavior. Native Git clone/push credentials
remain separate; OAuth tokens are never injected into shell commands.

Web authority uses connection-store format 3 and service state contract 23.
Formats 1/2 remain readable with their original compatibility fingerprints.
Managed activation rejects an older reader that could ignore retained web
authority. Tests use disposable owner sockets and broker/GitHub collaborators;
they do not establish a real browser, external broker or client end-to-end journey.
