package daemon

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net"
	"net/http"
	"time"
)

// CloudClient is the client of every request this device makes to Cloud: enrollment, the Code
// lease and the GitHub broker's proofs. It has no timeout, since a lease's WebSocket lives on;
// each request bounds itself with its context.
var CloudClient = &http.Client{Transport: CloudTransport(nil, nil)}

// CloudTransport dials with Happy Eyeballs, as Go does by default, and, when the connection or its TLS handshake
// fails, dials once more over IPv4 only. A VPN that tunnels IPv6 without forwarding it accepts the
// IPv6 TCP connection and then drops it, so Happy Eyeballs picks it and never tries IPv4. Nothing of
// the request is written before the handshake completes, so the second dial never resends one.
// Like the Connector, it never takes a proxy from the environment. dial and roots replace the
// system dialer and certificate roots when not nil.
func CloudTransport(dial func(ctx context.Context, network, addr string) (net.Conn, error), roots *x509.CertPool) *http.Transport {
	t := &http.Transport{MaxIdleConns: 10, IdleConnTimeout: 90 * time.Second, TLSHandshakeTimeout: 10 * time.Second,
		ExpectContinueTimeout: time.Second}
	if dial == nil {
		dial = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	}
	handshake := func(ctx context.Context, network, addr string) (net.Conn, error) {
		host, _, err := net.SplitHostPort(addr)
		if err != nil {
			return nil, err
		}
		raw, err := dial(ctx, network, addr)
		if err != nil {
			return nil, err
		}
		// HTTP/1.1 only: the lease's WebSocket upgrades over it, and Cloud serves both.
		conn := tls.Client(raw, &tls.Config{ServerName: host, RootCAs: roots, NextProtos: []string{"http/1.1"}, MinVersion: tls.VersionTLS12})
		ctx, cancel := context.WithTimeout(ctx, t.TLSHandshakeTimeout)
		defer cancel()
		if err := conn.HandshakeContext(ctx); err != nil {
			raw.Close()
			return nil, err
		}
		return conn, nil
	}
	t.DialTLSContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		conn, err := handshake(ctx, network, addr)
		if err == nil || ctx.Err() != nil || network != "tcp" {
			return conn, err
		}
		if conn, err4 := handshake(ctx, "tcp4", addr); err4 == nil {
			return conn, nil
		}
		return nil, err
	}
	return t
}
