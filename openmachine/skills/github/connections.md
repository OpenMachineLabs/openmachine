# Native GitHub account connections

[Domain guide](SKILL.md)

## Account connections

Connection protocol **2** provides one runtime-owned native account lifecycle on
existing private MCP. New accounts use GitHub App Device Flow; `existing_gh`
onboarding and the gh request backend are retired. Runtime requests require a
selected native connection and an explicit transport; there is no CLI fallback.
Saved gh connections, login attempts, task bindings and receipts remain readable.
Their stored status describes history, not current execution support; capabilities
report `legacy_gh: retired`. Requests through an old binding or an unbound historical
GitHub task fail with `integration_legacy_connection` before provider dispatch.
No account migration, automatic reconnection or mutation replay occurs.

Repository CI may still use GitHub CLI with its own Actions credentials. This is
independent of runtime account connections, as is an operator's Git credential helper.

| Operation | Contract |
| --- | --- |
| `list_integrations` | Passive supported methods, saved accounts, observation time and renewal-owner state; not a live provider health probe. |
| `connect_integration` | Original 32-hex request ID, `provider=github`, `method=device_code`, optional original native connection ID for reauthorization. Returns the public GitHub URL/code; repeat requests read the same attempt. |
| `read_integration_login` | Original login ID; no network access, refresh or restart. |
| `cancel_integration_login` | Cancel only an unfinished attempt; do not disconnect an already established account or claim provider revocation. |
| `disconnect_integration` | Connection ID and observed authorization revision. Persist the disabled binding before owned credential cleanup; report cleanup failure separately. |

OpenMachineLabs owns the official Open Machine GitHub App once for all ordinary
users. Its public client ID is built into OM; users do not register an App or supply
OAuth configuration. The official App keeps **Device Flow** and **Expire user
authorization tokens** enabled. No client secret, App private key or PAT import is
needed. App installation/resource approval and user consent are separate. OM returns
the official installation URL alongside Device Flow so users install the same public
App on each target account or organization instead of creating their own App. Users
complete the returned URL/code on any device; this runtime needs no browser or GUI.
Do not claim repository access merely because user identity was verified: existing
GitHub operations still check configured and provider resource permissions.

Credentials stay in their automatically selected pinned backend: macOS system
Keychain, otherwise OM's private file store. File mode uses private 0700 directories
and 0600 atomic files; permissions are not encryption. Unavailable or locked Keychain
never silently falls back. Credential references,
private device codes and tokens do not enter public results, logs or task records.

These connection controls use their own original IDs and revisions, not
`task_context`. The existing Runtime owns at most four login workers. Caller disconnection does
not cancel admitted work. Successful connections survive compatible restarts;
interrupted unfinished attempts report failure without reconstructing temporary
secrets. Snapshot reads suffice; there is no new event bus, UI, server or executor.
Saved metadata is bounded to 32 accounts, 128 attempts and 128 KiB. Full/corrupt
stores are explicit failures, not automatic deletion or replay of old request IDs.

Account selection is stable per operation/task. No global active token is changed.
Per-account coordination serializes refresh and disconnect; new logins use unique
credential slots, so late results cannot overwrite another account or undo a
cancel/disconnect. New work accepts native bindings only; old task identities and
serialized evidence are not migrated to a different authentication backend.

Normal requests refresh access tokens near expiry. The same owner checks refresh
grant deadlines every six hours and renews within the final 30 days, including
otherwise idle accounts. No high-frequency keepalive or always-open network
connection is needed. GitHub currently returns eight-hour access tokens and
six-month rotating refresh grants; routine successful rotation renews both.

The original credential is marked before exchange, then the complete replacement
is persisted and identity-verified before use. A lost rotating-token response or
uncommitted replacement is not silently replayed. Definitive revocation, expired
grants, prolonged shutdown/offline periods, host loss and inaccessible storage can
still require reauthorization. This is renewable authentication, not a promise of
permanent unrevokable credentials. A confirmed 401 permits one bounded auth retry;
uncertain business writes still require the existing destination reconciliation.

## GitHub account binding

New GitHub task contracts resolve native `github_connections` per repository and retain
`connection_id`, numeric `account_id` and `authorization_revision`. Format-2 through
format-4 operation locators retain that binding for dispatch and destination recovery.
Changing the default, disconnecting or reconnecting cannot silently change a task's
account. Explicit connection selectors must match the accepted binding.

Format-1 records and format-2 gh bindings retain their original serialization,
request hashes, connection identifiers and receipts. Reading this history is passive.
The gh request backend is retired: claims, continuation and remote reconciliation
that need its GitHub authority fail with `integration_legacy_connection`, including
unbound format-1 GitHub tasks. Neither an explicit native selector nor a new default
can transfer that authority. Start separately authorized native work only after
resolving any original uncertain effect; retirement does not prove its outcome or
permit replay. Local pause/cancel controls remain available under their existing guards.

Empty optional fields are omitted from legacy canonical bytes. New creation retries
compare the original caller request, not subsequently resolved connection state.
Routine native credential rotation preserves authorization revisions. Account
recovery does not replay a GitHub mutation or renew task authority.
