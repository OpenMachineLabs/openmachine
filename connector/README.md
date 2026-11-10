# Open Machine network connector

`github.com/OpenMachineLabs/openmachine/connector` joins one device to a private
network served by a Tailscale-protocol control server and carries TCP between
the peers that server permits. It wraps the stock
[`tsnet.Server`](https://pkg.go.dev/tailscale.com/tsnet); upstream details stay
in `internal/tailnet`.

```go
n, err := connector.Start(ctx, connector.Config{
	Dir:        stateDir,   // owner-only; holds the node identity
	ControlURL: controlURL, // required: there is no default control server
	Hostname:   "device",
	AuthKey:    oneUseKey,  // used only for the first registration
})
ln, err := n.Listen(port)           // Accept returns *connector.Conn
c, err := n.Dial(ctx, peerAddrPort) // only current peers are dialed
st, err := n.Status(ctx)            // self, peers, direct or relay path
err = n.Close()
```

The control server decides which peers and ports are reachable; the optional
`Config.Allow` narrows that further. A node accepts or dials only connections
whose remote end is a current, allowed peer, reports that peer from the
control server's identity, including its verified `NodeKey`, and closes the
connection when the peer leaves the network map or, after `Revalidate`, when
`Allow` no longer permits it. A dial that upstream would hand to the host
network, such as one racing a peer's removal, fails instead. Restarting from
the same directory keeps the node's identity.

Importing the package disables Tailscale log upload and binds each dial to the
interface that routes to its destination, so control and relays still work
behind a VPN or proxy TUN. Both settings are process-wide and inherited by child
processes: `TS_NO_LOGS_NO_SUPPORT=true` and `TS_BIND_TO_INTERFACE_BY_ROUTE=true`.
The library does not use `Server.Sys()`, `go:linkname`, `wgengine` or
`magicsock` directly, and enables no Funnel, Serve, exit node, subnet route or
TUN device.

## Test

The module builds and tests on its own, with no private sibling code:

```sh
go test ./...
```

`test/proof_test.go` runs two-node proofs against pinned, unmodified Headscale
and DERP servers on loopback. Without `OM_HEADSCALE` and `OM_DERPER` set to those
binaries, it is skipped and must be reported as not run. The proof uses
Headscale v0.29.7 and `derper` at the `tailscale.com` version in `go.mod`:

```sh
bin="$(mktemp -d)"
GOBIN="$bin" go install github.com/juanfont/headscale/cmd/headscale@v0.29.7
GOBIN="$bin" go install "tailscale.com/cmd/derper@$(go list -m -f '{{.Version}}' tailscale.com)"
OM_HEADSCALE="$bin/headscale" OM_DERPER="$bin/derper" go test ./...
```

## License

MIT; see [LICENSE](LICENSE). Dependencies keep their own licenses; see
[third-party licenses](THIRD_PARTY_LICENSES.md).
