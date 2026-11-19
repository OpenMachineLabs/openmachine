# om

`om` (`cmd/om`, packages `daemon/` and `linux/`) is the program on each
machine. It joins the private network with the [network library](../connector),
holds this machine's authority from Cloud, serves OM SSH and runs every command
through Core's access launcher. On a Mac the app runs its daemon as the user's
LaunchAgent; on Linux `om` is also the product (below). It runs as the user
who installed it, never as root on macOS; on Linux, root may install it too.
Before sign-in the daemon waits.

Cloud is the authority: it decides who may reach which machine, at which
access level, with which rules. The machine enforces that decision, and
rechecks it locally before every admission. When in doubt `om` fails closed:
missing, stale, malformed or unknown state admits nothing new.

## Lease

Each machine holds its authority as a lease over a signed WebSocket to Cloud,
which names this protocol in `X-OM-Protocol: 4`; Cloud refuses another with
`426`, which `om status` reports as `update_required`.

- A lease lists the machine's grant and access policy, and two sets of other
  devices: `sources`, which may connect to this machine, and `hosts`, which this
  machine may connect to.
- A source carries its grant revision, `allow` (`github` only for a device of
  the machine's own personal account; `models`), `limit` (`read_only` holds its
  sessions to Read only; `models` lets it reach the models and nothing else)
  and `home`, an opaque key for that person on this machine. Entries are
  decoded strictly: an unknown field or `limit` drops the source or host, and
  an unknown field of the target admits nothing new and closes nothing, so an
  older `om` cannot ignore a later restriction.
- A lease runs at most 60 seconds from when its renewal was sent and is renewed
  every 20 seconds. Replies apply in the order sent, so no reply extends or
  rolls back authority.
- A new admission needs a current lease and is checked locally, without a round
  trip; with no lease and Cloud unreachable it fails at once. Losing Cloud, or
  a lease running out, stops new admissions only: admitted sessions and their
  work continue, and a revocation then applies on reconnection, before anything
  new is admitted.
- When a lease no longer lists a revision, `om` closes the connections, channels
  and forwards bound to it, admits nothing new until it has, and then
  acknowledges the lease. A revoked or signed-out device is told it has no
  authority, which closes all its connections. No change kills a process, but
  closing a connection hangs up its terminal, as a lost SSH connection does.
- Each renewal reports whether `om` can serve, which Core version it runs,
  which access levels Core can enforce and the machine's listening programs.
- The lease also carries the account view (devices, names, aliases, access
  levels and states), which `om` keeps in memory and streams to this user's
  processes on its socket (`watch`). It is display data: it admits and closes
  nothing.
- Cloud may ask an Online Linux machine to update now: one frame that names
  nothing to install. The machine runs its own `om update`, which installs only
  a newer release its release key signed. A move between accounts arrives the
  same way, with a one-time secret that approves one sign-in.

## OM SSH

`om` serves SSH only on the private network's port for it, never on the host
network.

- Every transport, session channel and `-L` stream is a new admission, checked
  locally: this computer online, a current lease listing the peer's verified
  node key among its `sources`, and the daemon's own OS user. A session runs
  under the machine's access policy, or Read only when its source's `limit`
  says so; `om` passes the source's `home` to Core, which speaks access
  protocol 4. One network filter admits only the node keys of `sources` and
  `hosts`, in both directions.
- `-L` dials the machine's own network, never the private network, and needs
  Full access; otherwise it is refused and recorded. Remote forwarding, agent
  and X11 forwarding, streamlocal and other subsystems are refused.
- A computer is online only while device identity is enabled and, where an
  intent is bound (the app's Connect, or `om up`), that intent is on. Going
  offline closes every OM SSH connection into or out of it within a second, and
  none opens until it is online again; refusals say why on the client's
  standard error.
- A host is reached as `<alias>.om`, from the aliases its lease lists. `om`
  writes its own `ssh_config` with strict host-key checking, and adds one
  `Include` of it at the top of `~/.ssh/config`, once, on its first lease that
  lists a host. It leaves a symbolic-link `~/.ssh/config` untouched, never adds
  a removed line back, and never touches the SSH configuration of a device with
  no hosts. While that `Include` is in place, any other `.om` name reaches
  `om`, which refuses it, never DNS.
- A machine registers its SSH host key once; another key needs recovery.
- `om`'s state, `~/.local/state/openmachine-om`, is denied by Core to
  restricted sessions.

Until the machine's network node listens, the `status` of the owner-only
socket says why the last join step failed, for example `network_unavailable`,
`cloud_unavailable` or `network_unreachable`; `om status` on Linux shows
Connecting, or Update required. The `status` reply's `in_use` is true while an
SSH connection or stream into or out of the machine is open. Where its
supervisor discards standard error, as launchd does, `om` logs to `daemon.log`
in its state directory; the network node's lines there are rate-limited and
carry no keys, URL queries or home path.

## Pages and models on localhost

`om` listens on `127.0.0.1:11400` and `[::1]:11400`, and on Linux serves only
its own OS user.

- `localhost:11400/<alias>/<port>` opens that port of a host. Each opened port
  gets a port of its own on `127.0.0.1`, over an OM SSH connection of its own,
  as `-L` does, once the person has agreed to that page; until then nothing
  reaches the machine. A page's server gets only the cookies that page set.
  The browser keeps one set of cookies for `127.0.0.1`, whatever the port, so
  another opened page's script can read the cookies a page's script can.
- A machine lists the models of its local model servers and serves them on the
  channel `models@openmachine.app`, one HTTP request each, to a source whose
  `allow` has `models`: its list, or an inference `POST` passed to the server
  of the model its body names; nothing else reaches a server. On the source,
  `http://127.0.0.1:11400/v1` lists the models of its own servers and its hosts
  and routes by `model`. It answers no website: only requests with no `Origin`,
  its own chat page at `http://127.0.0.1:11400/`, or an origin that is not
  `http(s)`.

## Rules

Rules are what an account blocks on its machines. They arrive in the lease, so
its revision covers them; Core is not told.
[`test/vectors/rules.json`](../test/vectors/rules.json) holds the built-in
rules with their text and the cases every matcher must pass.

- `om` checks a new session after admitting it and before Core runs it: an
  `exec` by its command line, read as a POSIX shell reads it; a terminal, a
  shell fed line by line, `sftp`, the models and a Full access `-L` stream only
  by a lock. A line too long or too nested to read is refused. A stopped
  session starts nothing, exits 126 with the rule's text on standard error and
  is recorded as a `block`. Rules `om` cannot read admit nothing new. A change
  closes nothing: the next command is checked under the lease current then.
- A rule reads only the command line. [SECURITY.md](../SECURITY.md) says what
  that does not cover. A script sent whole (`bash -s`), whose input ends within
  300 ms and 65,536 bytes, is checked as a line.

## Logs

`om` records each connection, refusal, command start and end, block and forward
in a queue in its private state, signed with the device key and written to disk
before Core runs the command, and sends the queue to Cloud. It keeps a record
until Cloud confirms it, and sends it again otherwise; past 64 MiB on disk it
deletes the queue's oldest file, and Cloud records the gap as lost records.

- The lease's `target.record` is `null`, or the notice `om` prints, where the
  account records commands on that machine. Only then does a record hold the
  command, or the script sent to a shell, with the secrets `om` recognizes
  removed on the machine. Output, what is typed in a terminal and the files a
  session reads or writes are never recorded.
- [`test/vectors/logs.json`](../test/vectors/logs.json) is the record format
  both sides test against. `node scripts/verify-logs.mjs <export>` checks the
  chain and signatures of an account's log export on your own computer.

## GitHub

Sessions reach GitHub through the account's GitHub connection. No token is on
the machine, and `gh` is not used.

- Only a session whose source's `allow` has `github` gets the routing. A shell
  or exec session then gets process-scoped Git configuration in its
  environment, from a Core that lists `github` as `https`: remotes spelled
  `https://github.com/`, `git@github.com:` or `ssh://git@github.com/` go to the
  machine's loopback broker, with `om git-credential` as the only credential
  helper. No global or repository configuration changes. Under Full access the
  user's own credentials still work for anything that bypasses the routing.
- The broker serves TLS with a key made in memory at each start, which the
  routing pins, so whatever else later holds its port fails the handshake. A
  Git whose libcurl cannot pin ignores the pin.
- A session's capability ends with the session. Each Git command and
  `om github` call runs under a 15-minute Cloud ticket for one repository,
  which Cloud mints only against a fresh device-key proof and rechecks on every
  request. Only Cloud adds the token.
- Only Full access pushes and opens pull requests; Read only and Workspace
  fetch and read. Only GitHub's `info/refs` and two RPC endpoints of
  `github.com/OWNER/REPO` are forwarded. A push may update branches only, never
  the default branch as GitHub reports it at push time; a change of the default
  branch meanwhile races this check. Once a push may have reached GitHub, any
  failure but a refusal is reported as an unknown outcome and never retried.
- `om github` creates, views and lists pull requests and reads checks and
  statuses; nothing else.

## Linux

On Linux `om` is the product, as the app is on a Mac. Its commands are
`om up`, `down`, `status`, `logout`, `update`, `set auto-update on|off`,
`uninstall` and `version`. It calls only Core's `manage`, device identity's
`configure`, `inspect`, `setup`, `companion`, `upgrade` and `remove`, and the
daemon's socket; only `om uninstall` deletes their files.

- **State.** `headless.json`, which `om`'s commands write, and the connection
  intent `remote-access.json`, which only `om up` and `om down` write, are in
  the daemon's state directory.
- **No replay.** An operation ID is recorded in `headless.json` before its
  effect is sent. The next command that acts on that owner reads it back by
  that ID first. A Core operation in `recovery_required` blocks every later
  one; `om` reports it and says to reinstall.
- **Trust.** A release runs only after its pointer and manifest verify against
  the release public key `om` carries (`linux/update.go`) and its archive
  matches the manifest's SHA-256. `scripts/install.sh` carries the hashes of
  the release it installs and runs nothing that does not match. A release
  already unpacked and verified in `om`'s state, as by an interrupted install
  or update, is run without hashing it again. A key from the Console reaches
  `om install` in the environment and device identity on its input; it is in no
  state file.
- **Move.** `om move` signs in with a new key and the move's secret, and
  replaces the stored sign-in only once Cloud approved the new one, so a
  refused or expired move leaves the machine as it was.
- **Limit.** A key join returns no `stale_keys`, so stored devices that are no
  longer current stay stored until a browser sign-in (`om up` without a key)
  forgets them, or `om uninstall`; Cloud refuses a sign-in that lists more than
  16 stored keys.

## Test

```bash
go test ./...                          # om, with stub Core, device identity and systemd for linux/
python -m unittest discover -s test/linux -p 'test_install_script.py'
node --test test/verifier/verify-logs.test.mjs
```
