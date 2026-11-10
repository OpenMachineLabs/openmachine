// Package connector joins one Open Machine device to a private network served
// by a Tailscale-protocol control server, and carries TCP between peers that
// the control server permits.
//
// The control server decides which peers and ports are reachable, and the
// caller may narrow that further. A Node only accepts or dials connections
// whose remote end is a current peer the caller allows, and closes them when
// that peer leaves the network map or the caller no longer allows it. Log
// upload to Tailscale is disabled for the whole process.
package connector

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"os"
	"sync"
	"time"

	"github.com/OpenMachineLabs/openmachine/connector/internal/tailnet"
)

type (
	Peer       = tailnet.Peer
	PeerStatus = tailnet.PeerStatus
	Status     = tailnet.Status
)

// Config describes one node.
type Config struct {
	Dir        string // private state directory; created owner-only if missing
	ControlURL string // http or https URL of the control server
	Hostname   string
	AuthKey    string               // one-use registration key; ignored once Dir holds an identity
	Logf       func(string, ...any) // optional; nil discards logs
	// Allow, if set, narrows what the control server permits: a connection
	// with peer is admitted and kept only while it returns true. Call
	// Revalidate after its answers change. It may be called concurrently and
	// should return quickly.
	Allow func(peer Peer) bool
}

// Node is a running network node. Close it to leave the network.
type Node struct {
	net    *tailnet.Server
	allow  func(Peer) bool
	cancel context.CancelFunc
	done   chan struct{}

	mu    sync.Mutex
	conns map[*Conn]struct{}
}

// Conn is a TCP connection to a verified peer.
type Conn struct {
	net.Conn
	peer   Peer
	remote net.Addr
	node   *Node
}

// Peer returns the remote peer as identified by the control server.
func (c *Conn) Peer() Peer { return c.peer }

// RemoteAddr returns the address the peer was verified at. Unlike the
// netstack's, it stays set once the connection is closed or reset.
func (c *Conn) RemoteAddr() net.Addr { return c.remote }

func (c *Conn) Close() error {
	c.node.mu.Lock()
	delete(c.node.conns, c)
	c.node.mu.Unlock()
	return c.Conn.Close()
}

const whoIsTimeout = 5 * time.Second

// Start registers or resumes the node and waits until it is connected.
func Start(ctx context.Context, c Config) (*Node, error) {
	u, err := url.Parse(c.ControlURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("connector: control URL must be an absolute http or https URL")
	}
	if c.Hostname == "" {
		return nil, errors.New("connector: hostname is required")
	}
	if err := privateDir(c.Dir); err != nil {
		return nil, err
	}
	logf := c.Logf
	if logf == nil {
		logf = func(string, ...any) {}
	}
	s, err := tailnet.Start(ctx, c.Dir, c.ControlURL, c.Hostname, c.AuthKey, logf)
	if err != nil {
		return nil, err
	}
	watch, cancel := context.WithCancel(context.Background())
	allow := c.Allow
	if allow == nil {
		allow = func(Peer) bool { return true }
	}
	n := &Node{net: s, allow: allow, cancel: cancel, done: make(chan struct{}), conns: map[*Conn]struct{}{}}
	run, err := s.WatchRemovals(watch, n.Revalidate)
	if err != nil {
		cancel()
		s.Close()
		return nil, err
	}
	go func() {
		defer close(n.done)
		if err := run(); err != nil {
			// Without removals we cannot revoke connections, so fail closed.
			logf("connector: %v; refusing connections", err)
			n.closeConns()
		}
	}()
	return n, nil
}

func privateDir(dir string) error {
	if dir == "" {
		return errors.New("connector: state directory is required")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	fi, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	if !fi.IsDir() || fi.Mode().Perm()&0o077 != 0 {
		return fmt.Errorf("connector: state directory %s must be an owner-only directory", dir)
	}
	return nil
}

// Listen accepts TCP connections on port from current peers.
func (n *Node) Listen(port uint16) (net.Listener, error) {
	ln, err := n.net.Listen(port)
	if err != nil {
		return nil, err
	}
	return listener{ln, n}, nil
}

type listener struct {
	net.Listener
	node *Node
}

// Accept returns a *Conn. Connections from addresses that are not current
// peers, or that Allow refuses, are closed and skipped.
func (l listener) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		addr := c.RemoteAddr() // nil once the peer has reset it
		if addr == nil {
			c.Close()
			continue
		}
		p, err := l.node.whoIs(addr.String())
		if err != nil {
			c.Close()
			continue
		}
		if tc, err := l.node.track(c, p, addr); err == nil {
			return tc, nil
		}
	}
}

// Dial connects to a current peer that Allow permits. Addresses outside the
// network are refused rather than dialed through the host network.
func (n *Node) Dial(ctx context.Context, addr netip.AddrPort) (*Conn, error) {
	// WhoIs unmaps IPv4-mapped IPv6 but tsnet's dial routing does not, so
	// check, dial and track one canonical address.
	addr = netip.AddrPortFrom(addr.Addr().Unmap(), addr.Port())
	p, err := n.whoIs(addr.String())
	if err != nil {
		return nil, fmt.Errorf("connector: %s is not a current peer: %w", addr, err)
	}
	if !n.allow(p) {
		return nil, fmt.Errorf("connector: %s is not allowed", addr)
	}
	c, err := n.net.Dial(ctx, addr)
	if err != nil {
		return nil, err
	}
	return n.track(c, p, net.TCPAddrFromAddrPort(addr))
}

var errClosed = errors.New("connector: node is closed")

// track indexes c under its verified peer p, or closes it.
func (n *Node) track(c net.Conn, p Peer, addr net.Addr) (*Conn, error) {
	tc := &Conn{Conn: c, peer: p, remote: addr, node: n}
	n.mu.Lock()
	if n.conns == nil {
		n.mu.Unlock()
		c.Close()
		return nil, errClosed
	}
	n.conns[tc] = struct{}{}
	n.mu.Unlock()
	// A removal or narrowing applied before indexing is caught here; later ones by Revalidate.
	if q, err := n.whoIs(addr.String()); err != nil || q.ID != p.ID || !n.allow(q) {
		tc.Close()
		return nil, errors.Join(errors.New("connector: peer changed or not allowed while connecting"), err)
	}
	return tc, nil
}

func (n *Node) whoIs(addr string) (Peer, error) {
	ctx, cancel := context.WithTimeout(context.Background(), whoIsTimeout)
	defer cancel()
	return n.net.WhoIs(ctx, addr)
}

// Revalidate closes the connections whose peer no longer owns the remote
// address or that Allow no longer permits. Removals from the network map are
// applied automatically.
func (n *Node) Revalidate() {
	n.mu.Lock()
	conns := make([]*Conn, 0, len(n.conns))
	for c := range n.conns {
		conns = append(conns, c)
	}
	n.mu.Unlock()
	for _, c := range conns {
		if p, err := n.whoIs(c.remote.String()); err != nil || p.ID != c.peer.ID || !n.allow(p) {
			c.Close()
		}
	}
}

// closeConns closes every connection and refuses new ones.
func (n *Node) closeConns() {
	n.mu.Lock()
	conns := n.conns
	n.conns = nil
	n.mu.Unlock()
	for c := range conns {
		c.Conn.Close()
	}
}

// Status reports this node's state and its current peers.
func (n *Node) Status(ctx context.Context) (Status, error) { return n.net.Status(ctx) }

// Close closes every connection and leaves the network. The node's identity
// stays in its state directory.
func (n *Node) Close() error {
	n.cancel()
	<-n.done
	n.closeConns()
	return n.net.Close()
}
