// Package test proves the connector against pinned, unmodified Headscale and
// DERP servers on loopback. Set OM_HEADSCALE and OM_DERPER to their binaries;
// without them the network proof is skipped and must be reported NOT RUN.
package test

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/connector"
	"github.com/OpenMachineLabs/openmachine/connector/internal/tailnet"
)

// Importing the connector, before any node starts, makes tsnet dial control
// and relays through the interface that routes to them, such as a proxy's
// utun, rather than the default physical one.
func TestBindsToInterfaceByRoute(t *testing.T) {
	if v := os.Getenv("TS_BIND_TO_INTERFACE_BY_ROUTE"); v != "true" {
		t.Fatalf("TS_BIND_TO_INTERFACE_BY_ROUTE = %q, want true", v)
	}
}

func TestStartRejectsUnsafeConfig(t *testing.T) {
	dir := t.TempDir()
	shared := filepath.Join(dir, "shared")
	if err := os.Mkdir(shared, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, c := range map[string]connector.Config{
		// An empty URL would make tsnet fall back to Tailscale's control server.
		"missing control URL":  {Dir: dir, Hostname: "n"},
		"relative control URL": {Dir: dir, Hostname: "n", ControlURL: "127.0.0.1:1"},
		"missing hostname":     {Dir: dir, ControlURL: "http://127.0.0.1:1"},
		"missing state dir":    {Hostname: "n", ControlURL: "http://127.0.0.1:1"},
		"shared state dir":     {Dir: shared, Hostname: "n", ControlURL: "http://127.0.0.1:1"},
	} {
		if n, err := connector.Start(context.Background(), c); err == nil {
			n.Close()
			t.Errorf("%s: Start succeeded", name)
		}
	}
}

// The policy allows only users a -> b on port 7000; everything else is denied.
const policy = `{"acls": [{"action": "accept", "src": ["a@"], "dst": ["b@:7000"]}]}`

func TestNetworkProof(t *testing.T) {
	headscale, derper := os.Getenv("OM_HEADSCALE"), os.Getenv("OM_DERPER")
	if headscale == "" || derper == "" {
		t.Skip("NOT RUN: set OM_HEADSCALE and OM_DERPER to the pinned upstream binaries")
	}
	h := startNetwork(t, headscale, derper)

	a := h.join("a", "a", "7000")
	b := h.join("b", "b", "7000,7001")
	c := h.join("c", "c", "7000")
	toB := b.addr(7000)

	t.Run("direct", func(t *testing.T) {
		waitFor(t, 30*time.Second, "a direct path to b", func() bool {
			return a.do("echo "+toB) == "ok" && a.peer(b.name).Direct != ""
		})
		t.Logf("a -> b direct via %s", a.peer(b.name).Direct)
	})

	t.Run("dial stays on the private network", func(t *testing.T) {
		if got := a.do("echo " + mapped(toB)); got != "ok" {
			t.Fatalf("a -> %s = %q, want the same peer as %s", mapped(toB), got, toB)
		}
		host := hostListener(t)
		for _, to := range []string{host.addr, mapped(host.addr)} {
			// This error comes only from the peer check, before any dial.
			if got := a.do("echo " + to); !strings.Contains(got, "is not a current peer") {
				t.Errorf("a -> host %s = %q, want refusal before dialing", to, got)
			}
		}
		if n := host.accepts.Load(); n != 0 {
			t.Fatalf("host listener saw %d connections; non-peers must be refused before dialing", n)
		}
		// A peer can leave between the check and the dial; tsnet then dials
		// the host network. Dialing a host address directly reproduces that.
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		s, err := tailnet.Start(ctx, t.TempDir(), h.control, "guard", h.key("c"), func(string, ...any) {})
		if err != nil {
			t.Fatal(err)
		}
		defer s.Close()
		fallback := hostListener(t)
		c, err := s.Dial(ctx, netip.MustParseAddrPort(fallback.addr))
		if err == nil {
			c.Close()
			t.Fatal("tailnet.Dial returned a host-network connection")
		}
		if !errors.Is(err, tailnet.ErrHostDial) {
			t.Fatalf("tailnet.Dial = %v, want the host-dial guard to refuse it", err)
		}
		// The connect completed, but the listener counts it asynchronously.
		waitFor(t, 5*time.Second, "the host listener to see upstream's fallback dial", func() bool {
			return fallback.accepts.Load() > 0
		})
	})

	t.Run("forced DERP", func(t *testing.T) {
		// Upstream's DERP-only knob disables UDP when the node binds, so it
		// applies to these two processes only.
		d := h.join("d", "a", "", "TS_DEBUG_ALWAYS_USE_DERP=true")
		e := h.join("e", "b", "7000", "TS_DEBUG_ALWAYS_USE_DERP=true")
		toE := e.addr(7000)
		// The relay-only nodes first need a home region.
		waitFor(t, 30*time.Second, "d reaching e", func() bool { return d.do("echo "+toE) == "ok" })
		if p := d.peer(e.name); p.Direct != "" || p.Relay != region {
			t.Fatalf("d -> e path = direct %q relay %q, want relay %q only", p.Direct, p.Relay, region)
		}
		d.quit()
		e.quit()
	})

	t.Run("undeclared port or peer denied", func(t *testing.T) {
		for _, tc := range []struct {
			from *node
			to   string
		}{
			{a, b.addr(7001)}, // b listens, but the policy does not declare the port
			{c, toB},          // c is not a source the policy allows
			{b, a.addr(7000)}, // the rule is one-way
		} {
			if got := tc.from.do("echo " + tc.to); !strings.HasPrefix(got, "err") {
				t.Errorf("%s -> %s = %q, want denial", tc.from.name, tc.to, got)
			}
		}
	})

	t.Run("narrowing a visible peer closes its connections", func(t *testing.T) {
		self := a.status().Self
		if got := b.peer(a.name).NodeKey; got != self.NodeKey || !slices.ContainsFunc(h.nodes(), func(n hsNode) bool { return n.NodeKey == got }) {
			t.Fatalf("b sees a's node key %q, a has %q", got, self.NodeKey)
		}
		if got := a.do("hold " + toB); got != "ok" {
			t.Fatalf("hold: %s", got)
		}
		time.Sleep(time.Second)
		b.drain()
		a.drain()
		if got := b.do("deny " + self.NodeKey); got != "ok" {
			t.Fatalf("deny: %s", got)
		}
		b.event("accepted-closed a", 5*time.Second)
		a.event("held-closed", 5*time.Second)
		if got := a.do("echo " + toB); !strings.HasPrefix(got, "err") || b.peer(a.name).ID == "" {
			t.Fatalf("a -> b after narrowing = %q (a still a peer of b: %v)", got, b.peer(a.name).ID != "")
		}
		b.do("deny")
		// A dial to a peer the dialer does not allow never reaches it.
		a.do("deny " + a.peer(b.name).NodeKey)
		if got := a.do("echo " + toB); !strings.HasPrefix(got, "err") {
			t.Fatalf("a dialed a peer it does not allow: %q", got)
		}
		select {
		case e := <-b.events:
			t.Fatalf("b saw %q from a dial a does not allow", e)
		case <-time.After(time.Second):
		}
		a.do("deny")
		waitFor(t, 5*time.Second, "a reaching b again", func() bool { return a.do("echo "+toB) == "ok" })
	})

	t.Run("a connection reset underneath keeps its verified address", func(t *testing.T) {
		// The netstack reports no remote address for it any more; revalidating must not fail on that.
		if got := a.do("drop " + toB); got != toB {
			t.Fatalf("drop = %q, want %s", got, toB)
		}
		if got := a.do("echo " + toB); got != "ok" {
			t.Fatalf("a -> b after a dropped connection = %q", got)
		}
	})

	t.Run("restart keeps identity", func(t *testing.T) {
		before := b.status().Self
		count := len(h.nodes())
		b.quit()
		b = h.start(b.name, "", "7000,7001")
		after := b.status().Self
		if after.ID != before.ID || fmt.Sprint(after.Addrs) != fmt.Sprint(before.Addrs) {
			t.Fatalf("identity changed: %+v -> %+v", before, after)
		}
		if n := len(h.nodes()); n != count {
			t.Fatalf("control has %d nodes after restart, want %d", n, count)
		}
		waitFor(t, 15*time.Second, "a reaching restarted b", func() bool { return a.do("echo "+toB) == "ok" })
	})

	t.Run("removing a node closes its connections", func(t *testing.T) {
		if got := a.do("hold " + toB); got != "ok" {
			t.Fatalf("hold: %s", got)
		}
		time.Sleep(time.Second)
		b.drain()
		a.drain()
		start := time.Now()
		h.hs("nodes", "delete", "--identifier", h.nodeID(a.name))
		b.event("accepted-closed a", 15*time.Second)
		t.Logf("b closed a's connection %v after removal", time.Since(start).Round(time.Millisecond))
		a.event("held-closed", 15*time.Second)
		t.Logf("a closed its own connection %v after removal", time.Since(start).Round(time.Millisecond))
	})

	t.Run("no external log upload", func(t *testing.T) {
		if got := h.egress(); len(got) != 0 {
			t.Fatalf("connector nodes sent external requests: %v", got)
		}
		// Control: the same probe must catch a registered stock tsnet node's
		// default upload.
		before := len(h.egress())
		h.daemon("rawnode", h.bin("rawnode"), append(h.nodeEnv[:len(h.nodeEnv):len(h.nodeEnv)], "TS_AUTHKEY="+h.key("c")),
			"-dir", h.path("raw"), "-control", h.control)
		waitFor(t, 60*time.Second, "stock tsnet log upload attempt", func() bool {
			for _, r := range h.egress()[before:] {
				if strings.Contains(r, "log.tailscale.com") {
					return true
				}
			}
			return false
		})
	})
}

const region = "om-test"

type network struct {
	t         *testing.T
	dir       string
	headscale string
	config    string
	control   string
	nodeEnv   []string // environment for node processes
	built     map[string]string

	mu       sync.Mutex
	requests []string
	nodeLogs []string
}

func startNetwork(t *testing.T, headscale, derper string) *network {
	h := &network{t: t, dir: t.TempDir(), headscale: headscale, built: map[string]string{}}
	hsPort, derpPort, stunPort := freePort(t, "tcp"), freePort(t, "tcp"), freePort(t, "udp")
	h.control = "http://127.0.0.1:" + hsPort

	// derper creates and serves a self-signed certificate for 127.0.0.1;
	// clients pin its hash. It admits only clients Headscale verifies.
	certs := h.path("certs")
	h.daemon("derper", derper, nil, "-c", h.path("derper.json"), "-a", "127.0.0.1:"+derpPort, "-http-port", "-1", "-stun-port", stunPort,
		"-certmode", "manual", "-certdir", certs, "-hostname", "127.0.0.1",
		"-verify-client-url", h.control+"/verify", "-verify-client-url-fail-open=false")
	var pinned string
	waitFor(t, 15*time.Second, "derper certificate", func() bool {
		b, err := os.ReadFile(filepath.Join(certs, "127.0.0.1.crt"))
		if blk, _ := pem.Decode(b); err == nil && blk != nil {
			sum := sha256.Sum256(blk.Bytes)
			pinned = "sha256-raw:" + hex.EncodeToString(sum[:])
		}
		return pinned != "" && dialable("127.0.0.1:"+derpPort)
	})

	h.write("derp.yaml", fmt.Sprintf(`regions:
  900:
    regionid: 900
    regioncode: %s
    regionname: OM loopback
    nodes:
      - name: 900a
        regionid: 900
        hostname: 127.0.0.1
        ipv4: 127.0.0.1
        ipv6: none
        certname: %s
        derpport: %s
        stunport: %s
`, region, pinned, derpPort, stunPort))
	h.write("policy.hujson", policy)
	h.config = h.write("config.yaml", fmt.Sprintf(`server_url: %[1]s
listen_addr: 127.0.0.1:%[2]s
metrics_listen_addr: 127.0.0.1:%[3]s
grpc_listen_addr: 127.0.0.1:%[4]s
noise:
  private_key_path: %[5]s/noise_private.key
prefixes:
  v4: 100.64.0.0/10
  v6: fd7a:115c:a1e0::/48
  allocation: sequential
derp:
  server:
    enabled: false
  urls: []
  paths: [%[5]s/derp.yaml]
  auto_update_enabled: false
disable_check_updates: true
database:
  type: sqlite
  sqlite:
    path: %[5]s/db.sqlite
log:
  level: warn
policy:
  mode: file
  path: %[5]s/policy.hujson
dns:
  magic_dns: false
  override_local_dns: false
  nameservers:
    global: []
unix_socket: %[5]s/headscale.sock
unix_socket_permission: "0700"
# Leave log upload to each client, so only the connector can disable it.
logtail:
  enabled: true
`, h.control, hsPort, freePort(t, "tcp"), freePort(t, "tcp"), h.dir))
	h.daemon("headscale", headscale, nil, "-c", h.config, "serve")
	waitFor(t, 30*time.Second, "headscale health", func() bool {
		r, err := http.Get(h.control + "/health")
		if err == nil {
			r.Body.Close()
		}
		return err == nil && r.StatusCode == http.StatusOK
	})
	for _, u := range []string{"a", "b", "c"} {
		h.hs("users", "create", u)
	}

	// Connector nodes send any non-loopback HTTP(S) through this recorder,
	// which refuses it. Control and DERP stay on loopback and bypass proxies.
	proxy, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go http.Serve(proxy, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.mu.Lock()
		h.requests = append(h.requests, r.Method+" "+r.Host)
		h.mu.Unlock()
		http.Error(w, "egress refused by test", http.StatusForbidden)
	}))
	t.Cleanup(func() { proxy.Close() })
	// This test process imports the connector, whose init sets
	// TS_NO_LOGS_NO_SUPPORT here; nodes must not inherit it, or a clean
	// result would not show that the node binary disables upload itself.
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "TS_NO_LOGS_NO_SUPPORT=") {
			h.nodeEnv = append(h.nodeEnv, kv)
		}
	}
	h.nodeEnv = append(h.nodeEnv, "HTTPS_PROXY=http://"+proxy.Addr().String())
	return h
}

func (h *network) path(name string) string { return filepath.Join(h.dir, name) }

func (h *network) write(name, content string) string {
	p := h.path(name)
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		h.t.Fatal(err)
	}
	return p
}

func (h *network) egress() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.requests...)
}

// daemon runs a background process whose output is kept in <name>.log and
// printed if the test fails. A nil env inherits this process's environment.
func (h *network) daemon(name, bin string, env []string, args ...string) *exec.Cmd {
	cmd := exec.Command(bin, args...)
	cmd.Env = env
	h.logTo(cmd, name)
	if err := cmd.Start(); err != nil {
		h.t.Fatal(err)
	}
	h.t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
	return cmd
}

func (h *network) logTo(cmd *exec.Cmd, name string) {
	f, err := os.Create(h.path(name + ".log"))
	if err != nil {
		h.t.Fatal(err)
	}
	cmd.Stderr = f
	if cmd.Stdout == nil {
		cmd.Stdout = f
	}
	h.t.Cleanup(func() {
		f.Close()
		if h.t.Failed() {
			b, _ := os.ReadFile(f.Name())
			lines := strings.Split(strings.TrimSpace(string(b)), "\n")
			h.t.Logf("--- %s.log (tail)\n%s", name, strings.Join(lines[max(0, len(lines)-30):], "\n"))
		}
	})
}

func (h *network) hs(args ...string) []byte {
	cmd := exec.Command(h.headscale, append([]string{"-c", h.config, "--force", "-o", "json"}, args...)...)
	out, err := cmd.Output()
	if err != nil {
		h.t.Fatalf("headscale %v: %v\n%s%s", args, err, out, stderr(err))
	}
	return out
}

func stderr(err error) []byte {
	if e, ok := err.(*exec.ExitError); ok {
		return e.Stderr
	}
	return nil
}

type hsNode struct {
	ID      uint64 `json:"id"`
	Name    string `json:"name"`
	NodeKey string `json:"node_key"`
	User    struct {
		ID   uint64 `json:"id"`
		Name string `json:"name"`
	} `json:"user"`
}

func (h *network) nodes() []hsNode {
	var nodes []hsNode
	if err := json.Unmarshal(h.hs("nodes", "list"), &nodes); err != nil {
		h.t.Fatal(err)
	}
	return nodes
}

func (h *network) nodeID(name string) string {
	for _, n := range h.nodes() {
		if n.Name == name {
			return fmt.Sprint(n.ID)
		}
	}
	h.t.Fatalf("no node %q", name)
	return ""
}

func (h *network) userID(name string) string {
	var users []struct {
		ID   uint64 `json:"id"`
		Name string `json:"name"`
	}
	if err := json.Unmarshal(h.hs("users", "list"), &users); err != nil {
		h.t.Fatal(err)
	}
	for _, u := range users {
		if u.Name == name {
			return fmt.Sprint(u.ID)
		}
	}
	h.t.Fatalf("no user %q", name)
	return ""
}

func (h *network) bin(name string) string {
	if p, ok := h.built[name]; ok {
		return p
	}
	p := h.path("bin-" + name)
	if out, err := exec.Command("go", "build", "-o", p, "./testdata/"+name).CombinedOutput(); err != nil {
		h.t.Fatalf("build %s: %v\n%s", name, err, out)
	}
	h.built[name] = p
	return p
}

// key creates a one-use registration key for user.
func (h *network) key(user string) string {
	var key struct {
		Key string `json:"key"`
	}
	if err := json.Unmarshal(h.hs("preauthkeys", "create", "--user", h.userID(user), "--expiration", "10m"), &key); err != nil || key.Key == "" {
		h.t.Fatalf("preauth key for %s: %v", user, err)
	}
	return key.Key
}

// join registers a new node for user.
func (h *network) join(name, user, listen string, env ...string) *node {
	return h.start(name, h.key(user), listen, env...)
}

// start runs a node process from its state directory.
func (h *network) start(name, authKey, listen string, env ...string) *node {
	cmd := exec.Command(h.bin("node"), "-dir", h.path("node-"+name), "-control", h.control, "-hostname", name, "-listen", listen)
	cmd.Env = append(append(h.nodeEnv[:len(h.nodeEnv):len(h.nodeEnv)], "OM_TEST_AUTH_KEY="+authKey), env...)
	in, err := cmd.StdinPipe()
	if err != nil {
		h.t.Fatal(err)
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		h.t.Fatal(err)
	}
	h.logTo(cmd, "node-"+name)
	if err := cmd.Start(); err != nil {
		h.t.Fatal(err)
	}
	h.t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
	n := &node{t: h.t, name: name, in: in, replies: make(chan string, 16), events: make(chan string, 64)}
	go func() {
		s := bufio.NewScanner(out)
		for s.Scan() {
			kind, line, _ := strings.Cut(s.Text(), " ")
			if kind == "=" {
				n.replies <- line
			} else {
				n.events <- line
			}
		}
		close(n.replies)
	}()
	if got := n.reply(90 * time.Second); got != "ready" {
		h.t.Fatalf("node %s did not start: %q", name, got)
	}
	return n
}

type node struct {
	t       *testing.T
	name    string
	in      io.Writer
	replies chan string
	events  chan string
}

func (n *node) reply(timeout time.Duration) string {
	select {
	case r, ok := <-n.replies:
		if !ok {
			n.t.Fatalf("node %s exited", n.name)
		}
		return r
	case <-time.After(timeout):
		n.t.Fatalf("node %s did not reply within %v", n.name, timeout)
		return ""
	}
}

func (n *node) do(cmd string) string {
	fmt.Fprintln(n.in, cmd)
	return n.reply(20 * time.Second)
}

func (n *node) quit() { fmt.Fprintln(n.in, "quit") }

func (n *node) status() connector.Status {
	var st connector.Status
	if err := json.Unmarshal([]byte(n.do("status")), &st); err != nil {
		n.t.Fatalf("status of %s: %v", n.name, err)
	}
	return st
}

func (n *node) addr(port int) string {
	for _, a := range n.status().Self.Addrs {
		if a.Is4() {
			return fmt.Sprintf("%s:%d", a, port)
		}
	}
	n.t.Fatalf("node %s has no IPv4 address", n.name)
	return ""
}

func (n *node) peer(name string) connector.PeerStatus {
	for _, p := range n.status().Peers {
		if p.Name == name {
			return p
		}
	}
	n.t.Fatalf("node %s does not see peer %s", n.name, name)
	return connector.PeerStatus{}
}

func (n *node) drain() {
	for {
		select {
		case <-n.events:
		default:
			return
		}
	}
}

func (n *node) event(want string, timeout time.Duration) {
	deadline := time.After(timeout)
	for {
		select {
		case e := <-n.events:
			if e == want {
				return
			}
		case <-deadline:
			n.t.Fatalf("node %s: no %q within %v", n.name, want, timeout)
		}
	}
}

func waitFor(t *testing.T, timeout time.Duration, what string, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(timeout); !ok(); time.Sleep(250 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
	}
}

// mapped returns the IPv4-mapped IPv6 form of an IPv4 "ip:port".
func mapped(addr string) string {
	ap := netip.MustParseAddrPort(addr)
	return netip.AddrPortFrom(netip.AddrFrom16(ap.Addr().As16()), ap.Port()).String()
}

type hostLn struct {
	addr    string
	accepts atomic.Int32
}

// hostListener counts TCP connections that reach the host network.
func hostListener(t *testing.T) *hostLn {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	h := &hostLn{addr: l.Addr().String()}
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			h.accepts.Add(1)
			c.Close()
		}
	}()
	return h
}

func dialable(addr string) bool {
	c, err := net.DialTimeout("tcp", addr, time.Second)
	if err == nil {
		c.Close()
	}
	return err == nil
}

func freePort(t *testing.T, network string) string {
	var addr net.Addr
	if network == "udp" {
		c, err := net.ListenPacket("udp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		addr = c.LocalAddr()
		c.Close()
	} else {
		l, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		addr = l.Addr()
		l.Close()
	}
	_, port, _ := net.SplitHostPort(addr.String())
	return port
}
