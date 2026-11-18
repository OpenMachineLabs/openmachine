# The Mac app

The Open Machine app for Mac is closed source. It is the window, the menu bar
and the installer. What your Mac allows (network, identity, permission, rules
and logs) is decided by the open engine in this repository, which the app
reaches only through the local interfaces below. This page says what the app
does and does not do, and how to check the engine files on your Mac against
their inventories.

## What is open

| Lives in the open engine | Part |
| --- | --- |
| Private network, OM SSH, proxy, Services, Models | [`om`](om.md) |
| Rules matching, log collection and redaction | `om` |
| GitHub relay client; no GitHub token on the machine | `om` |
| Device key, sign-in, sign-out, revocation, writing this Mac's permission | [device identity](device.md) |
| Checking and switching the engine bundle | device identity |
| Read only and Workspace, sandboxed with macOS Seatbelt; Full access, which is not sandboxed | Core |

The app uses six local interfaces, all defined here:

| Interface | Defined in | The app uses it to |
| --- | --- | --- |
| `om`'s owner-only socket, `~/.local/state/openmachine-om/daemon.sock`: `status`, `watch`, `folders` | [`daemon/daemon.go`](../daemon/daemon.go) | show whether this Mac is connected, the machines list, this Mac's permission and your account; check Workspace folders |
| Device identity's commands `setup`, `companion` (sign-in, sign-out, `policy`, `remote-access` and reads), `inspect`, `upgrade` | [`device/openmachine_device/cli.py`](../device/openmachine_device/cli.py) | set up, sign in and out, set this Mac's permission, pass on your On or Off, switch bundles |
| Core's `manage --contract 1`: from the payload the app carries to install and update Core, and while no binding is saved; otherwise from the `manage.py` Core installs | [`openmachine/cli.py`](../openmachine/cli.py), [`openmachine/service/`](../openmachine/service) | install and update Core, read its status and follow its operations |
| `remote-access.json` | [`remote_access.py`](../device/openmachine_device/remote_access.py) | pass on your On or Off |
| The bundle's and Core's payload's `manifest.json` | [`scripts/build_bundle.py`](../scripts/build_bundle.py), [`scripts/build_runtime_payload.py`](../scripts/build_runtime_payload.py) | check the bundle's files and links before using it; read Core's version, commit and target before installing it |
| `om daemon -core-python PATH -core-state DIR -core-config FILE` | [`cmd/om/main.go`](../cmd/om/main.go) | start `om` with Core's interpreter, configuration and state |

Core's `access` launcher is run by `om`, with the interpreter the app names; the
app never runs it.

## What the app does and does not do

### Runs `om`

- The app registers a launchd agent, `app.openmachine.om`, from inside its own
  bundle. launchd runs the app's executable as that agent, which starts `om` and
  stays its parent. macOS therefore counts `om`'s access as the app's: the macOS
  permissions you give the app also cover `om`.
- `~/Library/Application Support/OpenMachine/Code/agent.json` names the `om` to
  start and Core's interpreter, configuration and state. The app writes it only
  after checking the bundle and Core (below), and restarts the agent when it
  changes. launchd starts the agent again whenever `om` exits. Quitting the app
  does not stop `om`.
- When the agent starts `om`, it checks only `agent.json` itself: your own
  regular file, not a link, that no one else can write, at most 64 KiB, with
  only known fields and absolute paths. It does not check `om` again. See
  [Limitation of this preview](#limitation-of-this-preview).
- Once registered, the agent starts at every login and launchd keeps it
  running, whether your connection is on or off; while it is off, `om` stays
  offline. When you turn the connection on, the app also asks macOS to open the
  app at login.
- It runs only from `/Applications` or `~/Applications`. Opened elsewhere, it
  asks to move itself there: it copies itself, clears the copy's download
  quarantine flag and keeps the copy only if its signature verifies. An older
  version of the app there goes to the Trash, as does the same version still
  flagged as downloaded when macOS runs this one from a temporary location; any
  other app stays. If the copy there is running, or is the same or a newer
  version, the app opens that copy instead, without asking.
- It checks its own code signature before it installs Core, sets up the bundle
  or registers the agent.

### Selects and checks the engine bundle

- The app carries one bundle: `om`, device identity, the Python that runs device
  identity, and `manifest.json`, which lists the SHA-256 of every file. Device
  identity keeps a copy in
  `~/Library/Application Support/OpenMachine/Connector/versions/<hash>/`, where
  `<hash>` is the SHA-256 of its `manifest.json`.
- The app records the bundle in use by that hash. Before every device identity
  command, and before it writes `agent.json`, it checks the bundle: the
  manifest's hash, every file's SHA-256 and every link, nothing missing or
  added, built for Apple silicon. It uses the kept copy when there is one; if
  that copy fails, the app refuses it rather than fall back to its own.
- It runs device identity from that bundle, or from the bundle it carries to
  set up or switch bundles, each checked first, with a cleared environment, and
  passes to its window only a fixed list of fields from the replies. It accepts
  only the service's own addresses from device identity, and opens only the
  matching Console.
- It switches to the bundle it carries only through device identity's
  `upgrade`, only when you turn the connection on, and never to an older
  version.

### Keeps your On or Off

Your choice is kept in `remote-access.json` (`format`, `enabled`, `revision`).
Device identity reads it with its own checks
([`remote_access.py`](../device/openmachine_device/remote_access.py)) and treats
a file it cannot read safely as Off; `om` follows device identity.

### Installs Core

- At its first launch, the app installs Core from the payload it carries with
  `manage --contract 1`: `configure`, then `install` with the SHA-256 of the
  payload's `manifest.json`. Core checks the payload itself. A Core already
  installed is kept, unless it has the version and commit of the payload the
  app carries but another signature, as after a signed rebuild: then turning
  the connection on updates it to the carried payload. Core's configuration is
  `~/.config/openmachine/config.toml` and its state
  `~/.local/state/openmachine`.
- It keeps Core's binding in `attachment.json`: the paths of Core's management
  entry, interpreters, configuration and state, and the SHA-256 of the
  management interpreter and of `manage.py`. It checks them before every
  management call.
- It does not decide this Mac's permission. Your choice of Read only, Workspace
  or Full access goes to device identity's `policy` action once Core has checked
  the Workspace folders through `om`, and Core enforces it. The app keeps no
  copy.

### Updates

- The app contains an updater for itself, Core and the bundle. Releases ship it
  off, with no update address and no signing key: it makes no update request
  and installs nothing from the network.
- The menu shows a notice when this repository has a stable release newer than
  your Core. With the updater off, the notice installs nothing.

### Network

The app's own requests are these two:

- It reads this repository's latest Release from `api.github.com`: every five
  minutes until a read succeeds, then once a day. It sends no credentials or
  cookies, only a User-Agent with the app's version, follows no redirects, and
  reads only the release's tag, its link and whether it is a draft or a
  prerelease.
- It loads your account picture from the address your account gives, only over
  HTTPS from GitHub's or Google's image servers. Its window can load nothing
  else from the network.

It opens the Console's Machines page and sign-in page in your browser; the
browser makes those requests.

It never calls the service and holds no credential for it: no device key and no
token. Device identity proves itself to the service with the device key it
keeps in `~/.local/state/openmachine-connector`.

### Diagnostic log

`~/Library/Logs/co.openmachine.desktop.preview/connection.log` gets a line for
each connection problem: UTC time, app version, a fixed event name and a short
error code, with no paths, accounts or contents. It is owner-only. At 256 KiB
it is renamed `connection.log.1`, so at most two files exist. The app never
reads or sends it; you may send it to the developers yourself.

### Files the app keeps

Only you can read them, except the socket. `co.openmachine.desktop.preview` is
the preview app's identifier; `…` below is
`~/Library/Application Support/co.openmachine.desktop.preview`.

| File | Holds |
| --- | --- |
| `…/companion/attachment.json` | Core's binding |
| `…/companion/setup.json` | the Core install or update: its operation ID, action, the payload's hash, paths and phase, and for an update the selection Core admitted it under |
| `…/companion/connector-reference.json` | the bundle in use, by its hash, and the path of the bundle the app carries; IDs of pending setup, sign-in, sign-out, connection operations and bundle switch, and of the account and sign-in they concern |
| `…/companion/remote-access.json` | your On or Off |
| `…/companion/remote-access-owner.json`, `…/companion/remote-access-operations/` | connection operations, by ID |
| `…/companion/updates.json` | update records; not written while the updater is off |
| `~/Library/Application Support/OpenMachine/Code/agent.json` | what the agent starts |
| `~/Library/Logs/co.openmachine.desktop.preview/connection.log` | the diagnostic log |
| `~/Library/WebKit/co.openmachine.desktop.preview/` | the window's web data |
| `~/Library/Caches/co.openmachine.desktop.preview/WebKit/` | the window's network cache |
| `~/Library/Preferences/co.openmachine.desktop.preview.plist` | the last folder you chose in the Workspace folder picker |
| a socket in `/tmp` named after the identifier, with `_` for each `.`, ending `_si.sock` | keeps the app to one copy; made with default permissions |

The window keeps your appearance (Light, Dark or System) in its local storage.

## Limitation of this preview

The agent starts the `om` that `agent.json` names without checking it, and any
program running as your macOS user can change `agent.json`, the kept bundle
and Core's files.
Such a program can have its own code started by the agent, with the macOS
permissions you gave the app, such as access to your Documents, Desktop and
Downloads folders and to the local network. The app checks the bundle when it
writes `agent.json`, not each time the agent starts. This preview accepts that.
Report security problems as [SECURITY.md](../SECURITY.md) says.

## Check the engine files on your Mac

The bundle and Core's payload each hold an inventory, `manifest.json`, that
lists their files. Find the files in use:

```sh
agent="$HOME/Library/Application Support/OpenMachine/Code/agent.json"
bundle="$(dirname "$(plutil -extract daemon raw "$agent")")"
core="$(plutil -extract python raw "$agent")"; core="${core%/python/bin/python3}"
```

`$bundle` is normally
`~/Library/Application Support/OpenMachine/Connector/versions/<hash>` and
`$core` is `~/.local/state/openmachine/service/payloads/<hash>`; each `<hash>`
is the SHA-256 of the `manifest.json` inside. The app carries its own copies, whose
inventories are
`OpenMachine.app/Contents/Resources/runtime/connector.app/Contents/Resources/bundle/manifest.json`
and `OpenMachine.app/Contents/Resources/runtime/payload/manifest.json`. The
copies in use can be from an earlier app version.

Read their versions. On a Mac, `om` has no `version` command; it has one only on
Linux.

```sh
plutil -extract version raw "$bundle/manifest.json"    # om and device identity
plutil -extract core.version raw "$core/manifest.json"
plutil -extract core.commit raw "$core/manifest.json"  # the public commit
```

Check every file against its `manifest.json`:

```sh
inventory() {  # what manifest.json lists: sums, links or names
  osascript -l JavaScript - "$1" <<'JS'
function run([mode]) {
  const m = JSON.parse($.NSString.stringWithContentsOfFileEncodingError("manifest.json", $.NSUTF8StringEncoding, null).js);
  const links = Object.assign({}, m.links), lines = [];
  for (const [name, entry] of Object.entries(m.files)) {
    const item = typeof entry === "string" ? { sha256: entry } : entry;
    if (item.link !== undefined) links[name] = item.link;
    else if (mode === "sums" && item.sha256) lines.push(item.sha256 + "  " + name);
    else if (mode === "names" && !item.directory) lines.push(name);
  }
  for (const [name, target] of Object.entries(links))
    if (mode !== "sums") lines.push(mode === "links" ? name + " -> " + target : name);
  return lines.join("\n");
}
JS
}
check() (
  cd "$1" || exit
  inventory sums | shasum -a 256 -c --quiet && echo "Every listed file matches."
  inventory links | while IFS= read -r line; do
    [ -z "$line" ] || [ "$(readlink "${line%% -> *}")" = "${line#* -> }" ] || echo "LINK DIFFERS: ${line%% -> *}"
  done
  find . ! -type d ! -path ./manifest.json | cut -c3- | sort | comm -13 <(inventory names | sort) -
)
check "$bundle"
check "$core"
```

A file that differs prints `FAILED`, and a link that points elsewhere
`LINK DIFFERS`. Any other path printed is a file the inventory does not list;
there should be none.

Passing shows that every file matches its inventory; it cannot show that the
inventory itself is unchanged. It does not show that anyone can rebuild the
files bit for bit, and we make no such claim: Mac builds sign every native file (`om`, the Python runtime, native
libraries and native code in Core's wheels) with our Developer ID before writing
the inventory, so an unsigned build of the same commit gives other hashes for
those files. [`scripts/build_bundle.py`](../scripts/build_bundle.py) builds the
bundle and, given a signing identity, signs it.
[`scripts/build_runtime_payload.py`](../scripts/build_runtime_payload.py) builds
Core's payload unsigned; a release script outside this repository signs it and
writes its inventory again.
