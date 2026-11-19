package daemon

import (
	"bufio"
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
	"github.com/coder/websocket"
	"golang.org/x/crypto/ssh"
)

// This computer's entry to a machine's web pages, against a machine that is an SSH server
// in this process. Its port 80 and 8080 are one page, which answers with what it was asked.

// machine is the fake target: OM SSH without the private network.
type machine struct {
	ln     net.Listener
	signer ssh.Signer
	page   string // where its web page listens

	mu      sync.Mutex
	conns   []*ssh.ServerConn
	users   []string
	changed int // channels still to refuse as after a change of authority
	open    int // streams open now
	most    int // and the most there were at once

	services  []daemon.Service // its model servers
	off       bool             // AI models are off for the computer
	answer    string           // what it answers a models channel in place of its models; "hold" is nothing
	asked     int              // models channels it took
	forwarded []int            // the ports its model requests went to
}

// What the machine does with a stream to each port: refuses it, or answers without HTTP.
var (
	refusals = map[uint32]struct {
		reason ssh.RejectionReason
		text   string
	}{
		9001: {ssh.ConnectionFailed, "om: dial tcp [::1]:9001: connect: connection refused"},
		9002: {ssh.Prohibited, "om: ports need Full access on this machine"},
		9003: {ssh.Prohibited, "no-deploys"},
		9004: {ssh.Prohibited, "om: the target cannot reach Cloud, so it refuses new SSH; existing sessions continue"},
		9005: {ssh.ResourceShortage, "om: too many channels on this connection"},
	}
	speaks = map[uint32]string{9006: "SSH-2.0-OpenSSH_9.9\r\n", 9007: ""}
)

func (m *machine) serve() {
	for {
		c, err := m.ln.Accept()
		if err != nil {
			return
		}
		go func() {
			cfg := &ssh.ServerConfig{NoClientAuth: true}
			cfg.AddHostKey(m.signer)
			sc, chans, reqs, err := ssh.NewServerConn(c, cfg)
			if err != nil {
				return
			}
			m.mu.Lock()
			m.conns, m.users = append(m.conns, sc), append(m.users, sc.User())
			m.mu.Unlock()
			go ssh.DiscardRequests(reqs)
			for nch := range chans {
				go m.stream(nch)
			}
		}()
	}
}

func (m *machine) stream(nch ssh.NewChannel) {
	var to struct {
		Host       string
		Port       uint32
		OriginHost string
		OriginPort uint32
	}
	ssh.Unmarshal(nch.ExtraData(), &to)
	m.mu.Lock()
	changed := m.changed > 0
	if changed {
		m.changed--
	}
	models, services, off, answer := nch.ChannelType() == "models@openmachine.app", m.services, m.off, m.answer
	if models && !changed && !off {
		m.asked++
	}
	m.mu.Unlock()
	if models && !changed {
		if off {
			nch.Reject(ssh.Prohibited, "om: AI models are off for you on this machine")
			return
		}
		ch, reqs, err := nch.Accept()
		if err != nil {
			return
		}
		defer ch.Close()
		go ssh.DiscardRequests(reqs)
		if answer == "hold" {
			io.Copy(io.Discard, ch)
			return
		} else if answer != "" {
			http.ReadRequest(bufio.NewReader(ch))
			io.WriteString(ch, answer)
			return
		}
		daemon.ServeModels(ch, services, func(port int) {
			m.mu.Lock()
			defer m.mu.Unlock()
			m.forwarded = append(m.forwarded, port)
		})
		return
	}
	if r, ok := refusals[to.Port]; nch.ChannelType() != "direct-tcpip" || to.Host != "localhost" || changed || ok {
		if changed {
			r.reason, r.text = ssh.Prohibited, "om: Code authority changed; reconnect"
		}
		nch.Reject(r.reason, r.text)
		return
	}
	ch, reqs, err := nch.Accept()
	if err != nil {
		return
	}
	defer ch.Close()
	go ssh.DiscardRequests(reqs)
	m.mu.Lock()
	m.open++
	m.most = max(m.most, m.open)
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		m.open--
		m.mu.Unlock()
	}()
	if first, ok := speaks[to.Port]; ok {
		io.WriteString(ch, first)
		return
	}
	page, err := net.Dial("tcp", m.page)
	if err != nil {
		return
	}
	defer page.Close()
	go func() { io.Copy(page, ch); page.Close() }()
	io.Copy(ch, page)
}

// connections returns how many connections the machine has taken, and closes them when asked, as a change of
// access does.
func (m *machine) connections(end bool) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, c := range m.conns {
		if end {
			c.Close()
		}
	}
	return len(m.conns)
}

// computer is the daemon side: a lease that lists the machine, the view and the streams out.
type computer struct {
	mu      sync.Mutex
	lease   daemon.Lease
	devices []daemon.ViewDevice
	offline error // why Lease refuses
	dial    error // why Dial fails
	to      string
	streams []net.Conn
	pages   []daemon.Page // as the state file keeps them
	approve *daemon.Approval

	services []daemon.Service  // its own model servers
	others   map[string]string // where another machine listens, by alias
	apps     []string          // what each stream was opened for
}

func (c *computer) Lease() (*daemon.Lease, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	l := c.lease
	return &l, c.offline
}
func (c *computer) Devices() []daemon.ViewDevice { return c.devices }
func (c *computer) Approval() *daemon.Approval {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.approve
}
func (c *computer) Key() ([]byte, error) { return []byte(strings.Repeat("k", 32)), nil }
func (c *computer) Pages() ([]daemon.Page, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return slices.Clone(c.pages), nil
}
func (c *computer) SavePages(p []daemon.Page) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.pages = slices.Clone(p)
	return nil
}
func (c *computer) Services() []daemon.Service {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.services
}
func (c *computer) Dial(alias, app string) (net.Conn, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.dial != nil {
		return nil, c.dial
	}
	c.apps = append(c.apps, app+" "+alias)
	to := c.to
	if other, ok := c.others[alias]; ok {
		to = other
	}
	s, err := net.Dial("tcp", to)
	c.streams = append(c.streams, s)
	return s, err
}

// set changes the computer under its lock.
func (c *computer) set(change func()) {
	c.mu.Lock()
	defer c.mu.Unlock()
	change()
}

type entry struct {
	t        *testing.T
	url      string
	computer *computer
	machine  *machine
	asked    atomic.Int32 // requests the machine's page got
}

// The pages newEntry opens first, so given the first ports after the entry's.
const (
	gpu  = "127.0.0.1:11401" // gpu-01.acme:8080
	gpu2 = "127.0.0.1:11402" // gpu-01.acme:80
)

// newEntry starts a machine with a web page, and the entry of a computer that reaches it as gpu-01.acme and
// dev-1. The view names it, a machine of the computer's own and the computer itself.
func newEntry(t *testing.T) *entry {
	t.Helper()
	e := &entry{t: t}
	page := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		e.asked.Add(1)
		if r.URL.Path == "/socket" {
			// The library refuses a WebSocket whose Origin is not its Host: the entry rewrote both.
			c, err := websocket.Accept(w, r, nil)
			if err != nil {
				return
			}
			defer c.CloseNow()
			kind, b, _ := c.Read(r.Context())
			c.Write(r.Context(), kind, append([]byte("echo "), b...))
			return
		}
		for _, c := range r.URL.Query()["set"] {
			w.Header().Add("Set-Cookie", c)
		}
		if to := r.URL.Query().Get("to"); to != "" {
			w.Header().Set("Location", to)
			w.WriteHeader(http.StatusFound)
			return
		}
		body, _ := io.ReadAll(r.Body)
		json.NewEncoder(w).Encode(map[string]any{"method": r.Method, "url": r.URL.String(), "host": r.Host, "origin": r.Header.Get("Origin"),
			"referer": r.Header.Get("Referer"), "cookie": r.Header.Values("Cookie"), "forwarded": r.Header.Get("X-Forwarded-For"), "body": string(body),
			"site": r.Header.Get("Sec-Fetch-Site")})
	}))
	t.Cleanup(page.Close)
	_, key, _ := ed25519.GenerateKey(nil)
	signer, _ := ssh.NewSignerFromKey(key)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	e.machine = &machine{ln: ln, signer: signer, page: page.Listener.Addr().String()}
	go e.machine.serve()
	e.computer = &computer{to: ln.Addr().String(), lease: daemon.Lease{Hosts: []daemon.Host{
		{DeviceID: "dev-gpu", NodeKey: "nodekey:1", Key: string(ssh.MarshalAuthorizedKey(signer.PublicKey())), User: "ubuntu", Aliases: []string{"dev-1", "gpu-01.acme"}},
		{DeviceID: "dev-own", NodeKey: "nodekey:2", Key: string(ssh.MarshalAuthorizedKey(signer.PublicKey())), User: "ubuntu", Aliases: []string{"dev-2", "studio"}},
	}}}
	if err := json.Unmarshal([]byte(`[
		{"device_id":"dev-self","name":"My Laptop","mode":"full_access","alias":"laptop","state":"online"},
		{"device_id":"dev-own","name":"Studio <1>","mode":"workspace","alias":"studio","state":"online"},
		{"device_id":"dev-gpu","name":"GPU <01>","mode":"read_only","alias":"gpu-01.acme","state":"online","team":{"slug":"acme","name":"Acme & Co"}}]`), &e.computer.devices); err != nil {
		t.Fatal(err)
	}
	local := httptest.NewServer(daemon.NewLocal(e.computer, "dev-self", "11400"))
	t.Cleanup(local.Close)
	e.url = local.URL
	if e.open("gpu-01.acme", "8080") != gpu || e.open("gpu-01.acme", "80") != gpu2 {
		t.Fatal("the first pages were given other ports")
	}
	return e
}

// mark is the mark om keeps a page's HttpOnly cookies under, for page@port, under the computer's key.
func mark(page string) string {
	m := hmac.New(sha256.New, []byte(strings.Repeat("k", 32)))
	m.Write([]byte("cookies " + page))
	return fmt.Sprintf("%x", m.Sum(nil)[:4])
}

// open follows the entry's link to the page alias:port and returns the page's host.
func (e *entry) open(alias, port string) string {
	e.t.Helper()
	res, _ := e.do("GET", "localhost:11400", "/"+alias+"/"+port, "", "Sec-Fetch-Dest", "document")
	host, ok := strings.CutPrefix(res.Header.Get("Location"), "http://")
	if res.StatusCode != http.StatusFound || !ok || !strings.HasSuffix(host, "/") {
		e.t.Fatalf("the entry of %s:%s = %d to %q", alias, port, res.StatusCode, res.Header.Get("Location"))
	}
	return strings.TrimSuffix(host, "/")
}

// do sends one request to the entry as host, following no redirect, and returns its answer and body.
func (e *entry) do(method, host, path, body string, header ...string) (*http.Response, string) {
	e.t.Helper()
	req, _ := http.NewRequest(method, e.url+path, strings.NewReader(body))
	req.Host = host
	for i := 0; i < len(header); i += 2 {
		req.Header.Add(header[i], header[i+1])
	}
	res, err := (&http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	return res, string(b)
}

// confirm presses Ask's button for host and returns the cookie it sets.
func (e *entry) confirm(host string) string {
	e.t.Helper()
	res, _ := e.do("POST", host, "/lab?x=1", "om_open=1", "Content-Type", "application/x-www-form-urlencoded", "Origin", "http://"+host)
	cookie := res.Header.Get("Set-Cookie")
	if res.StatusCode != http.StatusSeeOther || res.Header.Get("Location") != "http://"+host+"/lab?x=1" || !strings.HasPrefix(cookie, "om_open_"+strings.TrimPrefix(host, "127.0.0.1:")+"=") ||
		!strings.HasSuffix(cookie, "; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax") {
		e.t.Fatalf("confirming %s = %d, Location %q, Set-Cookie %q", host, res.StatusCode, res.Header.Get("Location"), cookie)
	}
	return strings.TrimSuffix(cookie, "; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
}

// got is what the machine's page was asked, as it answers.
type got struct {
	Method, URL, Host, Origin, Referer, Forwarded, Body, Site string
	Cookie                                                    []string
}

func (e *entry) page(method, host, path, body string, header ...string) got {
	e.t.Helper()
	res, text := e.do(method, host, path, body, header...)
	var g got
	if err := json.Unmarshal([]byte(text), &g); res.StatusCode != http.StatusOK || err != nil {
		e.t.Fatalf("%s %s%s = %d %q", method, host, path, res.StatusCode, text)
	}
	return g
}

// socket opens the WebSocket of host's page.
func (e *entry) socket(ctx context.Context, host, cookie string) *websocket.Conn {
	e.t.Helper()
	ws, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(e.url, "http")+"/socket", &websocket.DialOptions{Host: host,
		HTTPHeader: http.Header{"Cookie": {cookie}, "Origin": {"http://" + host}}})
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { ws.CloseNow() })
	return ws
}

func TestLocalEntry(t *testing.T) {
	e := newEntry(t)
	// The entry gives each page a port of its own, the same one each time.
	for _, c := range [][3]string{
		{"gpu-01.acme", "8080", gpu}, {"GPU-01.Acme", "8080/", gpu}, {"gpu-01.acme", "80", gpu2}, {"dev-1", "65535", "127.0.0.1:11403"}, {"x", "1", "127.0.0.1:11404"},
	} {
		if host := e.open(c[0], c[1]); host != c[2] {
			t.Errorf("/%s/%s went to %s, want %s", c[0], c[1], host, c[2])
		}
	}
	e.open("gpu-01.acme", "8080")
	if pages := e.computer.pages; len(pages) != 4 || pages[0] != (daemon.Page{Page: "gpu-01.acme:8080", Port: 11401}) {
		t.Errorf("the pages kept are %v", pages)
	}
	for _, path := range []string{"/", "/gpu-01.acme", "/gpu-01.acme/0", "/gpu-01.acme/08080", "/gpu-01.acme/65536", "/gpu-01.acme/99999",
		"/-gpu/80", "/a.b.c/80", "/gpu_01/80", "/gpu-01.acme/8080?x=1", "/gpu-01.acme/8080/lab", "//gpu-01.acme/8080"} {
		if res, body := e.do("GET", "localhost:11400", path, ""); res.StatusCode != 404 || body != "Not found.\n" {
			t.Errorf("the entry's %s = %d %q", path, res.StatusCode, body)
		}
	}
	if res, _ := e.do("POST", "localhost:11400", "/gpu-01.acme/8080", ""); res.StatusCode != 404 {
		t.Errorf("a POST to the entry = %d", res.StatusCode)
	}
	// Another website's image or script gives no page a port.
	if res, _ := e.do("GET", "localhost:11400", "/gpu-01.acme/9999", "", "Sec-Fetch-Dest", "image"); res.StatusCode != 403 || len(e.computer.pages) != 4 {
		t.Errorf("an image = %d, with %d pages", res.StatusCode, len(e.computer.pages))
	}
	// A page answers only at its own name and port.
	for host, status := range map[string]int{
		gpu: 200, "127.0.0.1:11405": 404, "127.0.0.1:11700": 404, "127.0.0.1:11399": 404, "localhost:11401": 404, "LocalHost:11401": 404,
		"[::1]:11401": 404, "127.0.0.1": 404, "127.0.0.1:011401": 404, "evil.example:11401": 404, "8080.gpu-01.acme.localhost:11400": 404,
	} {
		res, body := e.do("GET", host, "/", "")
		if res.StatusCode != status || status == 404 && body != "Not found.\n" {
			t.Errorf("%s = %d %q, want %d", host, res.StatusCode, body, status)
		}
	}
	// This computer's own alias is its own localhost, whatever its state.
	e.computer.set(func() { e.computer.offline = daemon.ErrOffline })
	res, _ := e.do("GET", "localhost:11400", "/laptop/3000", "")
	if res.StatusCode != http.StatusFound || res.Header.Get("Location") != "http://localhost:3000/" {
		t.Errorf("this computer = %d to %q", res.StatusCode, res.Header.Get("Location"))
	}
	if e.machine.connections(false) != 0 {
		t.Error("an address reached the machine")
	}
}

// The pages' ports outlast om, and a page opened when all are taken gets the one opened longest ago.
func TestLocalPorts(t *testing.T) {
	e := newEntry(t)
	var full []daemon.Page
	for n := 11401; n <= 11699; n++ {
		full = append(full, daemon.Page{Page: "dev-1:" + fmt.Sprint(n), Port: n})
	}
	// What a state file holds twice, out of range or that no entry gives is left out.
	e.computer.set(func() {
		e.computer.pages = append(slices.Clone(full), daemon.Page{Page: "dev-2:1", Port: 11401}, daemon.Page{Page: "dev-1:11401", Port: 11500},
			daemon.Page{Page: "dev-2:2", Port: 11700}, daemon.Page{Page: "bad_alias:3", Port: 11400})
	})
	local := httptest.NewServer(daemon.NewLocal(e.computer, "dev-self", "11400"))
	defer local.Close()
	e.url = local.URL
	if e.open("dev-1", "11500") != "127.0.0.1:11500" || e.computer.pages[0].Port != 11500 || len(e.computer.pages) != 299 {
		t.Errorf("a kept page = %v", e.computer.pages[:2])
	}
	cookie := e.confirm("127.0.0.1:11401")
	// A new page takes the port of the page opened longest ago, which asks again there.
	if host := e.open("gpu-01.acme", "8080"); host != "127.0.0.1:11699" || len(e.computer.pages) != 299 || e.computer.pages[0].Page != "gpu-01.acme:8080" {
		t.Errorf("a new page in a full table went to %s", host)
	}
	if host := e.open("gpu-01.acme", "80"); host != "127.0.0.1:11698" {
		t.Errorf("the next went to %s", host)
	}
	e.computer.set(func() {
		e.computer.pages = slices.DeleteFunc(e.computer.pages, func(p daemon.Page) bool { return p.Port == 11401 })
	})
	local2 := httptest.NewServer(daemon.NewLocal(e.computer, "dev-self", "11400"))
	defer local2.Close()
	e.url = local2.URL
	if host := e.open("studio", "443"); host != "127.0.0.1:11401" {
		t.Errorf("a freed port went to %s", host)
	}
	if res, body := e.do("GET", "127.0.0.1:11401", "/", "", "Cookie", cookie); res.StatusCode != 200 || !strings.Contains(body, "<h1>Open port 443?</h1>") {
		t.Errorf("another page's cookie at its old port = %d:\n%s", res.StatusCode, body)
	}
}

func TestLocalPeerUID(t *testing.T) {
	tcp := `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:2C88 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1 0000000000000000 100 0 0 10 0
   1: 0100007F:2C88 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000        0 102 1 0000000000000000 20 4 30 10 -1
   2: 0100007F:D431 0100007F:2C88 01 00000000:00000000 00:00000000 00000000  1001        0 103 1 0000000000000000 20 4 30 10 -1
`
	tcp6 := `  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:D432 00000000000000000000000001000000:2C88 01 00000000:00000000 00:00000000 00000000     0        0 201 1 0000000000000000 20 4 30 10 -1
   1: 0000000000000000FFFF00000100007F:D433 0000000000000000FFFF00000100007F:2C88 01 00000000:00000000 00:00000000 00000000  1002        0 202 1 0000000000000000 20 4 30 10 -1
`
	addr := netip.MustParseAddrPort
	for _, c := range []struct {
		proc, peer, local string
		uid               uint32
		found             bool
	}{
		{tcp, "127.0.0.1:54321", "127.0.0.1:11400", 1001, true}, // the browser's socket, not om's end of it
		{tcp, "127.0.0.1:54322", "127.0.0.1:11400", 0, false},
		{tcp6, "[::1]:54322", "[::1]:11400", 0, true},
		{tcp6, "127.0.0.1:54323", "127.0.0.1:11400", 1002, true}, // a socket of both families
		{tcp6, "[::1]:54322", "[::1]:11401", 0, false},
		{"", "127.0.0.1:54321", "127.0.0.1:11400", 0, false},
	} {
		if uid, found := daemon.PeerUID([]byte(c.proc), addr(c.peer), addr(c.local)); uid != c.uid || found != c.found {
			t.Errorf("the peer %s of %s = %d %v, want %d %v", c.peer, c.local, uid, found, c.uid, c.found)
		}
	}
}

// The first visit: om asks, and nothing reaches the machine before the person agrees.
func TestLocalFirstVisit(t *testing.T) {
	e := newEntry(t)
	own := func(res *http.Response) {
		t.Helper()
		if h := res.Header; h.Get("Content-Security-Policy") != "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; frame-ancestors 'none'" ||
			h.Get("Cache-Control") != "no-store" || h.Get("Referrer-Policy") != "same-origin" {
			t.Errorf("om's own answer has headers %v", h)
		}
	}
	res, body := e.do("GET", gpu, "/lab", "")
	own(res)
	for _, want := range []string{"<title>Open port 8080? · Open Machine</title>", "<h1>Open port 8080?</h1>", "<p>It runs on GPU &lt;01&gt;.</p>",
		`<form method="post"><input type="hidden" name="om_open" value="1"><button class="primary" autofocus>Open port 8080</button></form>`,
		"We ask once per page, so no other website can open it without you.", `<img src="data:image/svg+xml;base64,`} {
		if res.StatusCode != 200 || !strings.Contains(body, want) {
			t.Errorf("Ask = %d without %q:\n%s", res.StatusCode, want, body)
		}
	}
	if strings.Contains(body, "<script") || strings.Contains(body, "http://") || strings.Contains(body, "https://") {
		t.Error("Ask loads something")
	}
	// A machine the view does not name is its alias.
	if _, body := e.do("GET", e.open("dev-9", "8080"), "/", ""); !strings.Contains(body, "<p>It runs on dev-9.</p>") {
		t.Errorf("Ask for an unknown machine:\n%s", body)
	}
	if res, body := e.do("HEAD", gpu, "/", ""); res.StatusCode != 200 || body != "" {
		t.Errorf("HEAD = %d %q", res.StatusCode, body)
	}
	form := []string{"Content-Type", "application/x-www-form-urlencoded"}
	for name, c := range map[string]struct {
		method, body string
		header       []string
	}{
		"another method":         {"PUT", "", nil},
		"a post":                 {"POST", "a=1", form},
		"another value":          {"POST", "om_open=2", append([]string{"Origin", "http://" + gpu}, form...)},
		"another type":           {"POST", "om_open=1", []string{"Content-Type", "application/x-www-form-urlencoded; charset=UTF-8", "Origin", "http://" + gpu}},
		"a longer body":          {"POST", "om_open=1&", append([]string{"Origin", "http://" + gpu}, form...)},
		"no Origin":              {"POST", "om_open=1", form},
		"Origin: null":           {"POST", "om_open=1", append([]string{"Origin", "null"}, form...)},
		"another page":           {"POST", "om_open=1", append([]string{"Origin", "http://" + gpu2}, form...)},
		"this page's other name": {"POST", "om_open=1", append([]string{"Origin", "http://localhost:11401"}, form...)},
		"another site":           {"POST", "om_open=1", append([]string{"Origin", "https://example.com"}, form...)},
		"the Origin and more":    {"POST", "om_open=1", append([]string{"Origin", "http://" + gpu + "/"}, form...)},
		"another page's cookie":  {"GET", "", []string{"Cookie", e.confirm(gpu2)}},
	} {
		res, _ := e.do(c.method, gpu, "/", c.body, c.header...)
		own(res)
		if want := map[string]int{"GET": 200}[c.method]; res.StatusCode != want && res.StatusCode != 403 || res.Header.Get("Set-Cookie") != "" {
			t.Errorf("%s = %d, Set-Cookie %q", name, res.StatusCode, res.Header.Get("Set-Cookie"))
		}
	}
	// Not ready comes before Ask.
	e.computer.set(func() { e.computer.offline = daemon.ErrOffline })
	res, body = e.do("GET", gpu, "/", "")
	own(res)
	if res.StatusCode != 503 || !strings.Contains(body, "<h1>Open Machine isn’t ready</h1><p>Check that Open Machine is on and signed in on this computer, then try again.</p>"+
		`<a class="button" href="" autofocus>Try again</a>`) {
		t.Errorf("Not ready = %d:\n%s", res.StatusCode, body)
	}
	if e.machine.connections(false) != 0 || e.asked.Load() != 0 {
		t.Error("something reached the machine before the person agreed")
	}
}

// Once confirmed, a request reaches the page as from the machine's own browser.
func TestLocalForward(t *testing.T) {
	e := newEntry(t)
	cookie, cookie2 := e.confirm(gpu), e.confirm(gpu2)
	// The page sets the cookies its script reads first.
	e.do("GET", gpu, "/?set=theme%3Ddark&set=session%3Dabc&set=last%3D1", "", "Cookie", cookie)
	g := e.page("GET", gpu, "/lab/tree?a=1;b&c=%zz", "", "Cookie", "theme=dark; "+cookie+"; session=abc", "Cookie", "last=1",
		"Origin", "http://"+gpu, "Referer", "http://"+gpu+"/lab?x=1", "X-Forwarded-For", "203.0.113.9")
	if want := (got{Method: "GET", URL: "/lab/tree?a=1;b&c=%zz", Host: "localhost:8080", Origin: "http://localhost:8080", Referer: "http://localhost:8080/lab?x=1",
		Cookie: []string{"theme=dark; session=abc; last=1"}}); fmt.Sprint(g) != fmt.Sprint(want) {
		t.Errorf("the page was asked\n%+v, want\n%+v", g, want)
	}
	// Port 80, with nothing to rewrite and no other cookie.
	g = e.page("POST", gpu2, "/", "om_open=2", "Cookie", cookie2, "Content-Type", "application/x-www-form-urlencoded", "Referer", "https://example.com/")
	if want := (got{Method: "POST", URL: "/", Host: "localhost:80", Referer: "https://example.com/", Body: "om_open=2"}); fmt.Sprint(g) != fmt.Sprint(want) {
		t.Errorf("the page on port 80 was asked\n%+v, want\n%+v", g, want)
	}
	// The browser keeps one set of cookies for 127.0.0.1: the page gets the ones it set, no other page's, program's or om's,
	// and keeps the HttpOnly ones it sets under its mark.
	mine := "om11401_" + mark("gpu-01.acme:8080@11401") + "_"
	res, _ := e.do("GET", gpu, "/?set=sid%3D1%3B+Path%3D%2F%3B+HttpOnly&set=_xsrf%3D2%3B+Path%3D%2F&set=a%3D3%3B+httponly%3B+SameSite%3DLax"+
		"&set=om_open_11401%3Dx&set=om11402_y_sid%3Dx%3B+HttpOnly&set=b%3D4%3B+Path%3D%2FHttpOnly&set=%3Dnameless", "", "Cookie", cookie)
	if set := res.Header.Values("Set-Cookie"); fmt.Sprint(set) != fmt.Sprint([]string{mine + "sid=1; Path=/; HttpOnly", "_xsrf=2; Path=/",
		mine + "a=3; httponly; SameSite=Lax", "b=4; Path=/HttpOnly"}) {
		t.Errorf("the page set the cookies %q", set)
	}
	if g := e.page("GET", gpu, "/", "", "Cookie", mine+"sid=1; om11401_00000000_sid=2; om11402_x_sid=3; om_open_11402=x; "+cookie+"; _xsrf=2; b=4; other=local; om114=4"); fmt.Sprint(g.Cookie) != "[sid=1; _xsrf=2; b=4]" {
		t.Errorf("the page got the cookies %q", g.Cookie)
	}
	// The other page has set none: it gets none of them.
	if g := e.page("GET", gpu2, "/", "", "Cookie", cookie2+"; "+mine+"sid=1; _xsrf=2"); len(g.Cookie) != 0 {
		t.Errorf("another page got the cookies %q", g.Cookie)
	}
	// A cookie for the page's script goes only with the value the page set: another program's at 127.0.0.1 never goes,
	// even under a name the page set elsewhere, nor a value a script changed.
	e.do("GET", gpu, "/?set=sessionid%3Dx%3B+Path%3D%2Fnever", "", "Cookie", cookie)
	for jar, want := range map[string]string{
		"sessionid=LOCAL-SIGN-IN; sessionid=x": "[sessionid=x]", "sessionid=LOCAL-SIGN-IN": "[]", "_xsrf=changed; b=4": "[b=4]", "_xsrf=2": "[_xsrf=2]", "empty=; sessionid=": "[]",
	} {
		if g := e.page("GET", gpu, "/", "", "Cookie", cookie+"; "+jar); fmt.Sprint(g.Cookie) != want && !(want == "[]" && len(g.Cookie) == 0) {
			t.Errorf("with %q the page got %q, want %s", jar, g.Cookie, want)
		}
	}
	// A removal removes the page's own copy too, HttpOnly or not.
	res, _ = e.do("GET", gpu, "/?set=sid%3D%3B+Path%3D%2F%3B+Max-Age%3D0&set=c%3D%3B+Expires%3DThu%2C+01+Jan+1970+00%3A00%3A00+GMT", "", "Cookie", cookie)
	if set := res.Header.Values("Set-Cookie"); fmt.Sprint(set) != fmt.Sprint([]string{mine + "sid=; Path=/; Max-Age=0", "sid=; Path=/; Max-Age=0",
		mine + "c=; Expires=Thu, 01 Jan 1970 00:00:00 GMT", "c=; Expires=Thu, 01 Jan 1970 00:00:00 GMT"}) {
		t.Errorf("the page's removals were %q", set)
	}
	e.do("GET", gpu, "/?set=b%3D%3B+Max-Age%3D0", "", "Cookie", cookie)
	if g := e.page("GET", gpu, "/", "", "Cookie", cookie+"; b=4"); len(g.Cookie) != 0 {
		t.Errorf("a removed cookie went as %q", g.Cookie)
	}
	// Origin: null is the page's own only when the browser says so.
	if g := e.page("GET", gpu, "/", "", "Cookie", cookie, "Origin", "null", "Sec-Fetch-Site", "same-origin"); g.Origin != "null" {
		t.Errorf("a same-origin Origin: null arrived as %q", g.Origin)
	}
	// The Console's link to the front door arrives as typed; any other request from another site stays one.
	nav := []string{"Cookie", cookie, "Sec-Fetch-Site", "cross-site", "Sec-Fetch-Dest", "document"}
	for _, c := range []struct {
		path, want string
		header     []string
	}{
		{"/", "none", nil},
		{"/view?file=x", "cross-site", nil},
		{"/?x=1", "cross-site", nil},
		{"/", "cross-site", []string{"Sec-Fetch-Dest", "iframe"}},
		{"/", "same-site", []string{"Sec-Fetch-Site", "same-site"}},
	} {
		header := slices.Clone(nav)
		for i := 0; i < len(c.header); i += 2 {
			header[slices.Index(header, c.header[i])+1] = c.header[i+1]
		}
		if g := e.page("GET", gpu, c.path, "", header...); g.Site != c.want {
			t.Errorf("GET %s with %q arrived as Sec-Fetch-Site %q, want %q", c.path, c.header, g.Site, c.want)
		}
	}
	if g := e.page("POST", gpu, "/", "", nav...); g.Site != "cross-site" {
		t.Errorf("a cross-site POST / arrived as Sec-Fetch-Site %q", g.Site)
	}
	asked := e.asked.Load()
	for name, header := range map[string][]string{
		"another site":             {"Origin", "https://example.com"},
		"another page":             {"Origin", "http://" + gpu2},
		"this page over https":     {"Origin", "https://" + gpu},
		"a cross-site null":        {"Origin", "null", "Sec-Fetch-Site", "cross-site"},
		"a null that says nothing": {"Origin", "null"},
	} {
		if res, _ := e.do("POST", gpu, "/", "x", append([]string{"Cookie", cookie}, header...)...); res.StatusCode != 403 {
			t.Errorf("%s = %d, want 403", name, res.StatusCode)
		}
	}
	// With a cookie too, Ask's POST is om's to answer.
	e.confirm(gpu)
	if res, _ := e.do("POST", gpu, "/", "om_open=1", "Cookie", cookie, "Content-Type", "application/x-www-form-urlencoded", "Origin", "https://example.com"); res.StatusCode != 403 {
		t.Errorf("a foreign confirming POST with a cookie = %d", res.StatusCode)
	}
	if e.asked.Load() != asked {
		t.Error("a refused request reached the page")
	}
	// A redirect to this port's address stays on the page; any other is the page's own.
	for _, c := range []struct{ host, to, want string }{
		{gpu, "http://localhost:8080/next?x=1#top", "http://" + gpu + "/next?x=1#top"},
		{gpu, "http://127.0.0.1:8080/", "http://" + gpu + "/"},
		{gpu, "http://[::1]:8080", "http://" + gpu},
		{gpu, "HTTP://LocalHost:8080/a%2Fb", "http://" + gpu + "/a%2Fb"},
		{gpu, "http://localhost/next", "http://localhost/next"},
		{gpu, "http://localhost:8081/", "http://localhost:8081/"},
		{gpu, "https://localhost:8080/", "https://localhost:8080/"},
		{gpu, "//localhost:8080/", "//localhost:8080/"},
		{gpu, "http://example.com/", "http://example.com/"},
		{gpu, "/login?next=%2F", "/login?next=%2F"},
		{gpu2, "http://localhost/next", "http://" + gpu2 + "/next"},
		{gpu2, "http://127.0.0.1:80/next", "http://" + gpu2 + "/next"},
		{gpu2, "http://localhost:8080/", "http://localhost:8080/"},
	} {
		res, _ := e.do("GET", c.host, "/?to="+strings.NewReplacer("%", "%25", "#", "%23", "&", "%26").Replace(c.to), "", "Cookie", map[string]string{gpu: cookie, gpu2: cookie2}[c.host])
		if res.StatusCode != http.StatusFound || res.Header.Get("Location") != c.want {
			t.Errorf("on %s, a redirect to %s = %d to %q, want %q", c.host, c.to, res.StatusCode, res.Header.Get("Location"), c.want)
		}
	}
	// A WebSocket passes through.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ws := e.socket(ctx, gpu, cookie)
	ws.Write(ctx, websocket.MessageText, []byte("hello"))
	if _, b, err := ws.Read(ctx); string(b) != "echo hello" {
		t.Errorf("the WebSocket answered %q, %v", b, err)
	}
	// Each page has one connection.
	if n := e.machine.connections(false); n != 2 {
		t.Errorf("two pages opened %d connections", n)
	}
}

// What cannot be forwarded gets om's page for it.
func TestLocalRefusals(t *testing.T) {
	e := newEntry(t)
	on := func(port, alias string) string { return e.open(alias, port) }
	for _, c := range []struct {
		host   string
		dial   error
		status int
		title  string
		text   string
		button bool
	}{
		{on("9001", "gpu-01.acme"), nil, 502, "Nothing on port 9001", "Nothing on GPU &lt;01&gt; is using this port. Check the number, or start the program first.", true},
		{on("9002", "gpu-01.acme"), nil, 403, "You can’t open this", "You have Read only on GPU &lt;01&gt;. Ask an admin of Acme &amp; Co for Full access.", false},
		{on("9002", "dev-1"), nil, 403, "You can’t open this", "You have Read only on GPU &lt;01&gt;. Ask an admin of Acme &amp; Co for Full access.", false},
		{on("9002", "studio"), nil, 403, "You can’t open this", "Studio &lt;1&gt; is set to Workspace. Change it to Full access to open this.", false},
		{on("9003", "gpu-01.acme"), nil, 403, "You can’t open this", "A rule on GPU &lt;01&gt; blocks it.", false},
		{on("9004", "gpu-01.acme"), nil, 502, "Can’t reach GPU &lt;01&gt;", "This computer can’t connect to it.", false},
		{on("9005", "gpu-01.acme"), nil, 502, "Can’t reach GPU &lt;01&gt;", "This computer can’t connect to it.", false},
		{on("9006", "gpu-01.acme"), nil, 502, "This isn’t a web page", "Port 9006 on GPU &lt;01&gt; doesn’t answer as a web page. Use it as a program instead.", false},
		{on("9007", "gpu-01.acme"), nil, 502, "This isn’t a web page", "Port 9007 on GPU &lt;01&gt; doesn’t answer as a web page. Use it as a program instead.", false},
		{on("9008", "gpu-01.acme"), fmt.Errorf("gpu-01.acme.om is %w", daemon.ErrUnreachable), 502, "Can’t reach GPU &lt;01&gt;", "It’s offline. Try again when it’s back.", true},
		{on("9009", "gpu-01.acme"), errors.New("gpu-01.acme.om answered with another network identity"), 502, "Can’t reach GPU &lt;01&gt;", "This computer can’t connect to it.", false},
		{on("8080", "dev-9"), nil, 502, "Can’t reach dev-9", "This computer can’t connect to it.", false},
	} {
		e.computer.set(func() { e.computer.dial = c.dial })
		res, body := e.do("GET", c.host, "/", "", "Cookie", e.confirm(c.host))
		want := "<h1>" + c.title + "</h1><p>" + c.text + "</p>"
		if c.button {
			want += `<a class="button" href="" autofocus>Try again</a>`
		}
		if res.StatusCode != c.status || !strings.Contains(body, want+"</main>") || res.Header.Get("Cache-Control") != "no-store" {
			t.Errorf("%s = %d, want %d with %q:\n%s", c.host, res.StatusCode, c.status, want, body)
		}
	}
	e.computer.set(func() { e.computer.offline = daemon.ErrOffline })
	if res, body := e.do("GET", gpu, "/", "", "Cookie", e.confirm(gpu)); res.StatusCode != 503 || !strings.Contains(body, "<h1>Open Machine isn’t ready</h1>") {
		t.Errorf("Not ready with a cookie = %d", res.StatusCode)
	}
	if e.asked.Load() != 0 {
		t.Error("a refused request reached the page")
	}
}

// A machine of a team that has not approved this computer says why, on a page of a name of the team no host
// has and at the models' address; any other name, and a state om does not know, say what they said.
func TestLocalApproval(t *testing.T) {
	e := newEntry(t)
	e.computer.set(func() { e.computer.dial = errors.New("not a machine this computer can use") })
	const unreachable = "This computer can’t connect to it."
	waiting, blocked := "Acme &amp; Co hasn’t approved this computer yet. A team admin can approve it in the Console, under Team › Personal machines.",
		"Acme &amp; Co blocked this computer. Ask a team admin."
	for _, c := range []struct{ approval, why string }{
		{`{"team":{"slug":"acme","name":"Acme & Co"},"state":"waiting"}`, waiting},
		{`{"team":{"slug":"acme","name":"Acme & Co"},"state":"blocked"}`, blocked},
		{`{"team":{"slug":"acme","name":"Acme & Co"},"state":"paused"}`, ""},
		{`null`, ""},
	} {
		var approve *daemon.Approval
		if err := json.Unmarshal([]byte(c.approval), &approve); err != nil {
			t.Fatal(err)
		}
		e.computer.set(func() { e.computer.approve = approve })
		page := unreachable
		if c.why != "" {
			page = c.why
		}
		for _, p := range []struct{ alias, port, name, text string }{
			{"gpu-02.acme", "8080", "gpu-02.acme", page},
			{"gpu-01.acme", "9010", "GPU &lt;01&gt;", unreachable}, // a host has it
			{"dev-9", "8080", "dev-9", unreachable},
			{"gpu-02.other", "8080", "gpu-02.other", unreachable},
		} {
			host := e.open(p.alias, p.port)
			res, body := e.do("GET", host, "/", "", "Cookie", e.confirm(host))
			if want := "<h1>Can’t reach " + p.name + "</h1><p>" + p.text + "</p></main>"; res.StatusCode != 502 || !strings.Contains(body, want) {
				t.Errorf("with %s, %s = %d, want %q:\n%s", c.approval, p.alias, res.StatusCode, want, body)
			}
		}
		model := `No machine this computer reaches has the model "none".`
		if c.why != "" {
			model += " " + html.UnescapeString(c.why)
		}
		res, text := e.do("POST", "127.0.0.1:11400", "/v1/chat/completions", `{"model":"none"}`)
		var refusal struct{ Error struct{ Message string } }
		if json.Unmarshal([]byte(text), &refusal); res.StatusCode != 404 || refusal.Error.Message != model {
			t.Errorf("with %s, the model = %d %q, want %q", c.approval, res.StatusCode, text, model)
		}
	}
}

// A page's connection is opened again when it ended, when the machine's authority or its host changed, and
// after this computer was offline.
func TestLocalConnection(t *testing.T) {
	e := newEntry(t)
	cookie := []string{"Cookie", e.confirm(gpu)}
	open := func(want int, why string) {
		t.Helper()
		e.page("GET", gpu, "/", "", cookie...)
		e.page("POST", gpu, "/", "x", cookie...)
		if n := e.machine.connections(false); n != want {
			t.Errorf("%s: the machine has taken %d connections, want %d", why, n, want)
		}
	}
	ended := func() {
		t.Helper()
		// The entry learns that a connection ended as the machine closes it.
		time.Sleep(200 * time.Millisecond)
	}
	open(1, "two requests")
	e.machine.connections(true)
	ended()
	open(2, "after the connection ended")
	// The machine refuses a stream as after a change of authority: the entry connects again, once.
	refuse := func(streams int) {
		e.machine.mu.Lock()
		e.machine.changed = streams
		e.machine.mu.Unlock()
		e.machine.connections(true) // with its idle streams, so the next request opens one
		ended()
	}
	refuse(1)
	open(4, "after the authority changed")
	refuse(2)
	if res, body := e.do("GET", gpu, "/", "", cookie...); res.StatusCode != 502 || !strings.Contains(body, "This computer can’t connect to it.") || e.machine.connections(false) != 6 {
		t.Errorf("refused twice = %d over %d connections:\n%s", res.StatusCode, e.machine.connections(false), body)
	}
	open(6, "after two refusals")
	// The host's user changed in the lease.
	user := func(name string) {
		e.computer.set(func() {
			e.computer.lease.Hosts = slices.Clone(e.computer.lease.Hosts)
			e.computer.lease.Hosts[0].User = name
		})
	}
	user("root")
	open(7, "after the host's user changed")
	// It changes again under an open WebSocket, which ends with its connection: no request is needed.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws := e.socket(ctx, gpu, cookie[1])
	user("ubuntu")
	if _, _, err := ws.Read(ctx); ctx.Err() != nil {
		t.Errorf("a WebSocket outlived its host's user: %v", err)
	}
	open(8, "after the user changed under a WebSocket")
	if users := e.machine.users; len(users) != 8 || users[5] != "ubuntu" || users[6] != "root" || users[7] != "ubuntu" {
		t.Errorf("the connections' users are %v", users)
	}
	// Going offline closes the stream, as the daemon's watch does: Not ready, then a new connection.
	e.computer.set(func() {
		e.computer.offline = daemon.ErrOffline
		for _, s := range e.computer.streams {
			s.Close()
		}
	})
	if res, _ := e.do("GET", gpu, "/", "", cookie...); res.StatusCode != 503 {
		t.Errorf("offline = %d", res.StatusCode)
	}
	ended()
	e.computer.set(func() { e.computer.offline = nil })
	open(9, "back online")
}

// A page has at most 30 streams at once, its WebSockets among them: the next request waits for one to end.
func TestLocalStreams(t *testing.T) {
	e := newEntry(t)
	cookie := e.confirm(gpu)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	var sockets []*websocket.Conn
	for range 30 {
		sockets = append(sockets, e.socket(ctx, gpu, cookie))
	}
	asked := e.asked.Load()
	short, done := context.WithTimeout(ctx, 500*time.Millisecond)
	defer done()
	req, _ := http.NewRequestWithContext(short, "GET", e.url+"/", nil)
	req.Host = gpu
	req.Header.Set("Cookie", cookie)
	if res, err := http.DefaultClient.Do(req); err == nil {
		res.Body.Close()
		t.Errorf("with 30 WebSockets open, a request was answered %d", res.StatusCode)
	}
	e.machine.mu.Lock()
	most := e.machine.most
	e.machine.mu.Unlock()
	if e.asked.Load() != asked || most != 30 {
		t.Errorf("the machine had %d streams at once, want 30", most)
	}
	sockets[0].CloseNow()
	e.page("GET", gpu, "/", "", "Cookie", cookie)
	// A request that waits as the page's connection is replaced is answered over the new one.
	e.socket(ctx, gpu, cookie)
	answered := make(chan int, 1)
	go func() {
		req, _ := http.NewRequestWithContext(ctx, "GET", e.url+"/", nil)
		req.Host = gpu
		req.Header.Set("Cookie", cookie)
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			answered <- 0
			return
		}
		res.Body.Close()
		answered <- res.StatusCode
	}()
	time.Sleep(200 * time.Millisecond)
	e.computer.set(func() {
		e.computer.lease.Hosts = slices.Clone(e.computer.lease.Hosts)
		e.computer.lease.Hosts[0].User = "root"
	})
	if status := <-answered; status != http.StatusOK || e.machine.connections(false) != 2 {
		t.Errorf("a waiting request was answered %d over %d connections", status, e.machine.connections(false))
	}
}
