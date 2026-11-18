package daemon

import (
	"context"
	"crypto/x509"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// dropping accepts each connection and closes it at once, as a VPN does with IPv6 it tunnels
// without forwarding.
func dropping(t *testing.T) string {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	return ln.Addr().String()
}

// scripted dials first ("tcp") to first and IPv4-only ("tcp4") to second, counting each.
func scripted(first, second string, dials map[string]*atomic.Int32) func(context.Context, string, string) (net.Conn, error) {
	return func(ctx context.Context, network, _ string) (net.Conn, error) {
		dials[network].Add(1)
		target := first
		if network == "tcp4" {
			target = second
		}
		return (&net.Dialer{}).DialContext(ctx, "tcp", target)
	}
}

func cloudServer(t *testing.T, handler http.HandlerFunc) (*httptest.Server, *x509.CertPool) {
	srv := httptest.NewTLSServer(handler)
	t.Cleanup(srv.Close)
	roots := x509.NewCertPool()
	roots.AddCert(srv.Certificate())
	return srv, roots
}

func post(client *http.Client) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// example.com is the test server's certificate name; the scripted dialer ignores it.
	req, _ := http.NewRequestWithContext(ctx, "POST", "https://example.com/api/devices/network/enroll", strings.NewReader("{}"))
	res, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	body, err := io.ReadAll(res.Body)
	return string(body), err
}

func counters() map[string]*atomic.Int32 {
	return map[string]*atomic.Int32{"tcp": {}, "tcp4": {}}
}

func TestCloudClientTriesIPv4OnceWhenTheFirstHandshakeFails(t *testing.T) {
	var requests atomic.Int32
	srv, roots := cloudServer(t, func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		io.WriteString(w, "enrolled")
	})
	dials := counters()
	client := &http.Client{Transport: daemon.CloudTransport(scripted(dropping(t), srv.Listener.Addr().String(), dials), roots)}
	body, err := post(client)
	if err != nil || body != "enrolled" {
		t.Fatalf("post = %q, %v; want the IPv4 answer", body, err)
	}
	if dials["tcp"].Load() != 1 || dials["tcp4"].Load() != 1 || requests.Load() != 1 {
		t.Fatalf("dials tcp=%d tcp4=%d, requests=%d; want one of each", dials["tcp"].Load(), dials["tcp4"].Load(), requests.Load())
	}
}

func TestCloudClientNeverRedialsAWorkingOrStartedConnection(t *testing.T) {
	var requests atomic.Int32
	srv, roots := cloudServer(t, func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Header.Get("X-Drop") != "" {
			// The request reached Cloud; its effect is unknown, so it must never be resent.
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
			return
		}
		io.WriteString(w, "ok")
	})
	addr := srv.Listener.Addr().String()

	dials := counters()
	client := &http.Client{Transport: daemon.CloudTransport(scripted(addr, addr, dials), roots)}
	if body, err := post(client); err != nil || body != "ok" {
		t.Fatalf("post = %q, %v", body, err)
	}
	if dials["tcp4"].Load() != 0 {
		t.Fatalf("a working first connection was dialed again over IPv4")
	}

	requests.Store(0)
	dials = counters()
	client = &http.Client{Transport: daemon.CloudTransport(scripted(addr, addr, dials), roots)}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "POST", "https://example.com/api/devices/network/enroll", strings.NewReader("{}"))
	req.Header.Set("X-Drop", "1")
	if _, err := client.Do(req); err == nil {
		t.Fatal("a dropped reply was reported as an answer")
	}
	if requests.Load() != 1 || dials["tcp4"].Load() != 0 {
		t.Fatalf("requests=%d tcp4=%d; a request that reached Cloud was resent", requests.Load(), dials["tcp4"].Load())
	}
}

func TestCloudClientFailsWhenIPv4CannotConnectEither(t *testing.T) {
	_, roots := cloudServer(t, func(http.ResponseWriter, *http.Request) { t.Error("no request may arrive") })
	dials := counters()
	client := &http.Client{Transport: daemon.CloudTransport(scripted(dropping(t), dropping(t), dials), roots)}
	if _, err := post(client); err == nil {
		t.Fatal("a Cloud that no address reaches answered")
	}
	if dials["tcp"].Load() != 1 || dials["tcp4"].Load() != 1 {
		t.Fatalf("dials tcp=%d tcp4=%d; want one each", dials["tcp"].Load(), dials["tcp4"].Load())
	}
}

func TestCloudClientHasNoTimeoutForTheLeaseWebSocket(t *testing.T) {
	if daemon.CloudClient.Timeout != 0 {
		t.Fatal("the lease's WebSocket needs a client without a timeout")
	}
}
