// Package tailnet adapts the stock tsnet.Server. It is the only package that
// imports Tailscale, so upstream types never reach the public API.
package tailnet

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"

	"gvisor.dev/gvisor/pkg/tcpip/adapters/gonet"
	"tailscale.com/client/local"
	"tailscale.com/envknob"
	"tailscale.com/ipn"
	"tailscale.com/tsnet"
)

// These process-wide settings must hold before a server starts. tsnet uploads
// its logs to Tailscale unless the first is set; discarding Logf alone would
// not stop the upload. On macOS, tsnet binds control and relay dials to the
// default physical interface, skipping utun*, unless the second is set. Under
// a VPN or proxy TUN with fake-IP DNS, such as Surge or Clash enhanced mode,
// those addresses route only through its utun, so the node never registers.
func init() {
	envknob.SetNoLogsNoSupport()
	envknob.Setenv("TS_BIND_TO_INTERFACE_BY_ROUTE", "true")
}

// Peer is a node identified by the control server.
type Peer struct {
	ID      string       // stable node ID assigned by the control server
	NodeKey string       // the node's public key as the control server holds it, "nodekey:<hex>"
	Name    string       // host name
	Addrs   []netip.Addr // network addresses
}

// PeerStatus is a peer as currently seen by this node.
type PeerStatus struct {
	Peer
	Online bool
	Direct string // current direct UDP endpoint; empty when relayed or idle
	Relay  string // home relay region code
}

// Status is this node's current view of the network.
type Status struct {
	State string // backend state, such as "Running" or "NeedsLogin"
	Self  Peer
	Peers []PeerStatus
}

type Server struct {
	ts *tsnet.Server
	lc *local.Client
}

// Start runs a node and waits until it is connected. authKey is used only
// when dir holds no registered identity.
func Start(ctx context.Context, dir, controlURL, hostname, authKey string, logf func(string, ...any)) (*Server, error) {
	ts := &tsnet.Server{
		Dir:        dir,
		ControlURL: controlURL,
		Hostname:   hostname,
		AuthKey:    authKey,
		Logf:       logf,
		UserLogf:   logf,
	}
	if _, err := ts.Up(ctx); err != nil {
		ts.Close()
		return nil, err
	}
	lc, err := ts.LocalClient()
	if err != nil {
		ts.Close()
		return nil, err
	}
	return &Server{ts: ts, lc: lc}, nil
}

func (s *Server) Listen(port uint16) (net.Listener, error) {
	return s.ts.Listen("tcp", fmt.Sprintf(":%d", port))
}

var ErrHostDial = errors.New("tailnet: refusing a connection outside the private network")

// Dial connects over the private network only. tsnet hands an address that
// is not a current peer at dial time to the host network instead; that
// connection is closed here and never returned.
func (s *Server) Dial(ctx context.Context, addr netip.AddrPort) (net.Conn, error) {
	c, err := s.ts.Dial(ctx, "tcp", addr.String())
	if err != nil {
		return nil, err
	}
	if _, ok := c.(*gonet.TCPConn); !ok {
		c.Close()
		return nil, ErrHostDial
	}
	return c, nil
}

// WhoIs returns the peer that currently owns addr ("ip" or "ip:port").
func (s *Server) WhoIs(ctx context.Context, addr string) (Peer, error) {
	w, err := s.lc.WhoIs(ctx, addr)
	if err != nil {
		return Peer{}, err
	}
	p := Peer{ID: string(w.Node.StableID), NodeKey: w.Node.Key.String(), Name: w.Node.ComputedName}
	for _, prefix := range w.Node.Addresses {
		p.Addrs = append(p.Addrs, prefix.Addr())
	}
	return p, nil
}

func (s *Server) Status(ctx context.Context) (Status, error) {
	st, err := s.lc.Status(ctx)
	if err != nil {
		return Status{}, err
	}
	out := Status{State: st.BackendState}
	if st.Self != nil {
		out.Self = Peer{ID: string(st.Self.ID), NodeKey: st.Self.PublicKey.String(), Name: st.Self.HostName, Addrs: st.Self.TailscaleIPs}
	}
	for _, p := range st.Peer {
		out.Peers = append(out.Peers, PeerStatus{
			Peer:   Peer{ID: string(p.ID), NodeKey: p.PublicKey.String(), Name: p.HostName, Addrs: p.TailscaleIPs},
			Online: p.Online,
			Direct: p.CurAddr,
			Relay:  p.Relay,
		})
	}
	return out, nil
}

// WatchRemovals subscribes before returning, so no later removal is missed.
// The returned run calls changed whenever a peer leaves the network map or the
// backend state changes, until ctx ends or the watch fails.
func (s *Server) WatchRemovals(ctx context.Context, changed func()) (run func() error, err error) {
	w, err := s.lc.WatchIPNBus(ctx, ipn.NotifyPeerChanges)
	if err != nil {
		return nil, err
	}
	return func() error {
		defer w.Close()
		for {
			n, err := w.Next()
			if err != nil {
				if ctx.Err() != nil {
					return nil
				}
				return errors.Join(errors.New("tailnet: network map watch ended"), err)
			}
			if len(n.PeersRemoved) > 0 || n.State != nil {
				changed()
			}
		}
	}, nil
}

func (s *Server) Close() error { return s.ts.Close() }
