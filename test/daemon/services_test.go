package daemon

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// A machine lists its own listening programs from the OS's list, never by scanning.

func TestProcNet(t *testing.T) {
	// Little-endian words, as on amd64 and arm64.
	tcp := `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1 0000000000000000 100 0 0 10 0
   1: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 102 1 0000000000000000 100 0 0 10 0
   2: 0100007F:1F90 0100007F:D431 01 00000000:00000000 00:00000000 00000000  1000        0 103 1 0000000000000000 20 4 30 10 -1
   3: 0500A8C0:0BB8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 104 1 0000000000000000 100 0 0 10 0
`
	tcp6 := `  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:18EB 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 201 1 0000000000000000 100 0 0 10 0
   1: 00000000000000000000000000000000:22B8 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 202 1 0000000000000000 100 0 0 10 0
   2: 0000000000000000FFFF00000100007F:2382 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 203 1 0000000000000000 100 0 0 10 0
`
	want := map[string]string{"101": "127.0.0.1:8080", "102": "0.0.0.0:80", "104": "192.168.0.5:3000"}
	got := map[string]string{}
	for inode, addr := range daemon.ProcNet([]byte(tcp)) {
		got[inode] = addr.String()
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("tcp = %v, want %v (LISTEN only)", got, want)
	}
	want, got = map[string]string{"201": "[::1]:6379", "202": "[::]:8888", "203": "[::ffff:127.0.0.1]:9090"}, map[string]string{}
	for inode, addr := range daemon.ProcNet([]byte(tcp6)) {
		got[inode] = addr.String()
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("tcp6 = %v, want %v", got, want)
	}
	if len(daemon.ProcNet(nil)) != 0 {
		t.Error("an empty file lists sockets")
	}
}

func TestLsof(t *testing.T) {
	out := "p642\ncControlCenter\nf9\nn*:7000\nf10\nn*:7000\np5088\ncjupyter-lab\nf15\nn127.0.0.1:8888\nf16\nn[::1]:8888\np7001\ncredis server\nf6\nn[::ffff:127.0.0.1]:6379\nf7\nn10.0.0.2:5432\n"
	addr := netip.MustParseAddrPort
	want := []daemon.Listener{{Addr: addr("0.0.0.0:7000"), PID: 642, Name: "ControlCenter"}, {Addr: addr("0.0.0.0:7000"), PID: 642, Name: "ControlCenter"},
		{Addr: addr("127.0.0.1:8888"), PID: 5088, Name: "jupyter-lab"}, {Addr: addr("[::1]:8888"), PID: 5088, Name: "jupyter-lab"},
		{Addr: addr("[::ffff:127.0.0.1]:6379"), PID: 7001, Name: "redis server"}, {Addr: addr("10.0.0.2:5432"), PID: 7001, Name: "redis server"}}
	if got := daemon.Lsof([]byte(out)); !reflect.DeepEqual(got, want) {
		t.Errorf("Lsof = %+v\nwant %+v", got, want)
	}
}

func TestListed(t *testing.T) {
	at := func(addr string, port, pid int, exe string) daemon.Listener {
		return daemon.Listener{Addr: netip.AddrPortFrom(netip.MustParseAddr(addr), uint16(port)), PID: pid, Name: fmt.Sprint("p", pid), Exe: exe}
	}
	ls := []daemon.Listener{
		at("127.0.0.1", 8888, 30, ""), at("::1", 8888, 20, ""), at("0.0.0.0", 8888, 0, ""), // the lowest known PID names it
		at("::", 3000, 0, ""), // unreadable: listed with no process
		at("::ffff:127.0.0.1", 6379, 40, "/usr/local/bin/redis-server"), // /usr/local is not Apple's
		at("0.0.0.0", 80, 50, ""), at("::", 443, 51, ""), at("127.0.0.1", 1023, 52, ""), at("127.0.0.1", 22, 53, ""), // below 1024 only 80 and 443
		at("192.168.1.5", 5000, 60, ""), at("127.0.0.53", 5353, 61, ""), // localhost does not reach them
		at("127.0.0.1", 9000, os.Getpid(), ""),                         // om itself
		at("127.0.0.1", 32768, 70, ""), at("127.0.0.1", 60999, 71, ""), // the ephemeral range
		at("127.0.0.1", 32767, 72, ""), at("127.0.0.1", 61000, 73, ""),
	}
	for _, exe := range []string{"/System/Library/CoreServices/ControlCenter.app/Contents/MacOS/ControlCenter", "/usr/libexec/rapportd",
		"/bin/x", "/sbin/x", "/Library/Apple/System/x"} {
		ls = append(ls, at("0.0.0.0", 7000, 80, exe)) // Apple's
	}
	var got []string
	for _, l := range daemon.Listed(ls, 32768, 60999) {
		got = append(got, fmt.Sprint(l.Addr.Port(), "/", l.PID))
	}
	want := []string{"80/50", "443/51", "3000/0", "6379/40", "8888/20", "32767/72", "61000/73"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Listed = %v, want %v", got, want)
	}
	// The lowest 64 ports are kept. Their PIDs are above any real one, so
	// none is this process's, which Listed drops as om's.
	ls = nil
	for port := 2000; port < 2100; port++ {
		ls = append(ls, at("127.0.0.1", 4099-port, 1<<22+port, "")) // from 2099 down
	}
	listed := daemon.Listed(ls, 50000, 60000)
	if len(listed) != 64 || listed[0].Addr.Port() != 2000 || listed[63].Addr.Port() != 2063 {
		t.Errorf("Listed kept %d ports from %v to %v, want 64 from 2000 to 2063", len(listed), listed[0].Addr, listed[len(listed)-1].Addr)
	}
}

func TestClean(t *testing.T) {
	for in, want := range map[string]string{
		"  Jupyter\n\tLab  ":           "Jupyter Lab",
		"a​b\u0000c‮d\x7f":             "abcd",
		" Comfy　UI ":                   "Comfy UI",
		strings.Repeat("é", 50):        strings.Repeat("é", 40),
		strings.Repeat("😀", 41):        strings.Repeat("😀", 40), // code points, not bytes or UTF-16 units
		"\xffbad":                      "�bad",
		strings.Repeat("a", 39) + " b": strings.Repeat("a", 39),
	} {
		if got := daemon.Clean(in); got != want {
			t.Errorf("Clean(%q) = %q, want %q", in, got, want)
		}
	}
}

// serve answers each connection to a new local port with answer, counting them.
func serve(t *testing.T, answer func(net.Conn)) (port int, conns *atomic.Int32) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	conns = &atomic.Int32{}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			conns.Add(1)
			go func() { defer c.Close(); answer(c) }()
		}
	}()
	return ln.Addr().(*net.TCPAddr).Port, conns
}

func TestProbe(t *testing.T) {
	page := func(title string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Host != "" { // HTTP/1.0 needs none, and Redis closes on one
				t.Errorf("the probe sent Host %q", r.Host)
			}
			w.Header().Set("Content-Type", "Text/HTML; charset=utf-8")
			fmt.Fprintf(w, "<!doctype html><head><TITLE lang=en>%s</title></head><title>second</title>", title)
		})
	}
	http1 := func(h http.Handler) int {
		srv := httptest.NewServer(h)
		t.Cleanup(srv.Close)
		return srv.Listener.Addr().(*net.TCPAddr).Port
	}
	titled := http1(page("  Jupyter &amp; Lab &#x1F600; "))
	mux := http.NewServeMux()
	var self int // the port it serves
	mux.Handle("/lab", page("JupyterLab"))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Query().Get("to") {
		case "":
			http.Redirect(w, r, "/?to=absolute", http.StatusFound)
		case "absolute":
			w.Header().Set("Location", "HTTP://LOCALHOST:"+strconv.Itoa(self)+"/lab")
			w.WriteHeader(http.StatusFound)
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	redirected := srv.Listener.Addr().(*net.TCPAddr).Port
	self = redirected
	plain := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("OK")) }))
	elsewhere := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://example.com/", http.StatusMovedPermanently)
	}))
	httpsOnly, _ := serve(t, func(c net.Conn) { // as nginx answers plain HTTP on a port that serves only HTTPS
		bufio.NewReader(c).ReadString('\n')
		io.WriteString(c, "HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\n\r\n<title>400 The plain HTTP request was sent to HTTPS port</title>")
	})
	heard := make(chan int64, 1) // what the program that speaks first was sent
	speaks, _ := serve(t, func(c net.Conn) {
		io.WriteString(c, "+OK redis\r\n")
		n, _ := io.Copy(io.Discard, c)
		heard <- n
	})
	silent, _ := serve(t, func(c net.Conn) { io.Copy(io.Discard, c) })
	answer := func(status int, kind, body string) int {
		return http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", kind)
			w.WriteHeader(status)
			io.WriteString(w, body)
		}))
	}
	signIn := answer(http.StatusUnauthorized, "text/html", "<title>401 Unauthorized</title>")
	jenkins := answer(http.StatusForbidden, "text/html", "<title>Sign in [Jenkins]</title>") // also with a Host line
	api := answer(http.StatusNotFound, "text/html; charset=utf-8", "<title>Error</title><pre>Cannot GET /</pre>")
	proxy := answer(http.StatusServiceUnavailable, "text/html", "<title>Failed to parse request header</title>")
	var hosts atomic.Value // the Host lines the page that needs one was sent
	hosts.Store("")
	vite := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { // as Vite 5.4.12 to 6 and webpack-dev-server answer
		hosts.Store(hosts.Load().(string) + "[" + r.Host + "]")
		if r.Host == "" {
			http.Error(w, "Blocked request.", http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		io.WriteString(w, "<title>Vite App</title>")
	}))
	base := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { // as Vite with a base path
		switch {
		case r.Host == "":
			http.Error(w, "Blocked request.", http.StatusForbidden)
		case r.URL.Path == "/":
			http.Redirect(w, r, "/app/", http.StatusFound)
		default:
			w.Header().Set("Content-Type", "text/html")
			io.WriteString(w, "<title>Vite Base</title>")
		}
	}))
	starting := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, "/app", http.StatusFound)
		} else {
			http.Error(w, "starting", http.StatusServiceUnavailable)
		}
	}))
	broken := http1(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, "/gone", http.StatusFound)
		} else {
			w.Header().Set("Content-Type", "text/html")
			w.WriteHeader(http.StatusNotFound)
			io.WriteString(w, "<title>404</title>")
		}
	}))
	tls := httptest.NewTLSServer(page("secret"))
	t.Cleanup(tls.Close)
	for name, c := range map[string]struct {
		port  int
		web   bool
		title string
		again bool
	}{
		"a titled page":                        {titled, true, "  Jupyter & Lab 😀 ", false},
		"two redirects to this port's page":    {redirected, true, "JupyterLab", false},
		"a redirect to a page answering 503":   {starting, true, "", true},
		"a redirect to an error page":          {broken, true, "", false},
		"text/plain":                           {plain, false, "", false},
		"a redirect elsewhere, not followed":   {elsewhere, true, "", false},
		"a program that speaks first":          {speaks, false, "", false},
		"a program that never answers":         {silent, false, "", true},
		"TLS":                                  {tls.Listener.Addr().(*net.TCPAddr).Port, false, "", false},
		"HTTPS only, answering plain HTTP 400": {httpsOnly, false, "", false},
		"behind a sign-in, 401":                {signIn, true, "", false},
		"behind a sign-in, 403 also with Host": {jenkins, true, "", false},
		"an API's 404":                         {api, false, "", false},
		"a proxy's 503":                        {proxy, false, "", true},
		"a page that needs a Host line":        {vite, true, "Vite App", false},
		"a Host line kept through a redirect":  {base, true, "Vite Base", false},
	} {
		if web, title, again := daemon.Probe(netip.AddrPortFrom(netip.MustParseAddr("0.0.0.0"), uint16(c.port))); web != c.web || title != c.title || again != c.again {
			t.Errorf("%s: Probe = %v %q %v, want %v %q %v", name, web, title, again, c.web, c.title, c.again)
		}
	}
	if got, want := hosts.Load(), "[][localhost:"+strconv.Itoa(vite)+"]"; got != want {
		t.Errorf("the page that needs a Host line was sent %s, want none and then one", got)
	}
	select {
	case n := <-heard:
		if n != 0 {
			t.Errorf("a program that speaks first was sent %d bytes, want no request", n)
		}
	case <-time.After(5 * time.Second):
		t.Error("the program that speaks first was never closed")
	}
	// Each address localhost reaches is dialed at its loopback: also an IPv6 one for a page served on IPv4 only,
	// as with IPv6 off.
	for _, addr := range []string{"[::]", "[::ffff:127.0.0.1]", "127.0.0.1"} {
		if web, title, _ := daemon.Probe(netip.MustParseAddrPort(addr + ":" + strconv.Itoa(redirected))); !web || title != "JupyterLab" {
			t.Errorf("Probe at %s = %v %q, want the page", addr, web, title)
		}
	}
}

func TestProbeFollowsRedirectsOnlyToThisPort(t *testing.T) {
	var location atomic.Value
	port, requests := serve(t, func(c net.Conn) {
		line, _ := bufio.NewReader(c).ReadString('\n')
		if strings.HasPrefix(line, "GET / ") {
			io.WriteString(c, "HTTP/1.0 302 Found\r\nLocation: "+location.Load().(string)+"\r\n\r\n")
		} else { // the page a followed redirect reaches
			io.WriteString(c, "HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<title>there</title>")
		}
	})
	self, other := strconv.Itoa(port), strconv.Itoa(port%65535+1)
	for _, c := range []struct{ location, title string }{
		{"/there", "there"},
		{"http://LOCALHOST:" + self + "/there", "there"},
		{"http://[::1]:" + self + "/there", "there"},
		// Another port's address is not this one's, however written.
		{"http://localhost:" + other + "/there", ""},
		{"http://127.0.0.1:" + other + "/there", ""},
		{"https://localhost:" + self + "/there", ""},
		{"//localhost:" + other + "/there", ""},
		{"http://user@localhost:" + self + "/there", ""},
		{"http://example.com:" + self + "/there", ""},
	} {
		location.Store(c.location)
		if web, title, _ := daemon.Probe(netip.MustParseAddrPort("127.0.0.1:" + self)); !web || title != c.title {
			t.Errorf("a redirect to %s: Probe = %v %q, want a web page titled %q", c.location, web, title, c.title)
		}
	}
	// A redirect's page still building is a page named by its process, to be probed again.
	var built atomic.Bool
	building := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, "/en", http.StatusTemporaryRedirect)
		} else if built.Swap(true) {
			w.Header().Set("Content-Type", "text/html")
			io.WriteString(w, "<title>Next App</title>")
		} else {
			<-r.Context().Done() // the first build outlasts the probe
		}
	}))
	t.Cleanup(building.Close)
	for _, want := range []struct {
		title string
		again bool
	}{{"", true}, {"Next App", false}} {
		if web, title, again := daemon.Probe(netip.MustParseAddrPort(building.Listener.Addr().String())); !web || title != want.title || again != want.again {
			t.Errorf("a redirect to a page building: Probe = %v %q %v, want a web page %q, again %v", web, title, again, want.title, want.again)
		}
	}
	// A redirect without a Location is a page named by its process, asked once.
	location.Store("")
	before := requests.Load()
	if web, title, _ := daemon.Probe(netip.MustParseAddrPort("127.0.0.1:" + self)); !web || title != "" || requests.Load() != before+1 {
		t.Errorf("a redirect without a Location: Probe = %v %q after %d requests, want a web page named by its process after one",
			web, title, requests.Load()-before)
	}
}

// Each port and process is probed once, at most 8 in a pass; the rest wait for the next.
func TestDiscoveryProbesEachPortAndProcessOnce(t *testing.T) {
	port, conns := serve(t, func(c net.Conn) {
		bufio.NewReader(c).ReadString('\n')
		io.WriteString(c, "HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<title>\x00Comfy​UI</title>")
	})
	listener := func(port, pid int, name string) daemon.Listener {
		return daemon.Listener{Addr: netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port)), PID: pid, Name: name}
	}
	var closed []daemon.Listener // ports nothing listens on: a program that never answers
	for i := range 10 {
		ln, _ := net.Listen("tcp", "127.0.0.1:0")
		closed = append(closed, listener(ln.Addr().(*net.TCPAddr).Port, 100+i, fmt.Sprint("prog\n", i)))
		ln.Close()
	}
	var d daemon.Discovery
	page := listener(port, 7, "python3")
	if got := d.Pass([]daemon.Listener{page}); !reflect.DeepEqual(got, []daemon.Service{{Port: port, Name: "ComfyUI", Web: true, Addr: page.Addr}}) {
		t.Fatalf("Pass = %+v, want the page named by its cleaned title", got)
	}
	d.Pass([]daemon.Listener{page})
	if n := conns.Load(); n != 2 { // GET / and GET /v1/models
		t.Fatalf("the page was asked %d times, want once for its page and once for its models", n)
	}
	page.PID = 8 // another process on the same port is probed again
	d.Pass([]daemon.Listener{page})
	if n := conns.Load(); n != 4 {
		t.Fatalf("a new process on the port was asked %d times in all, want 4", n)
	}
	if got := d.Pass(closed); len(got) != 8 || !reflect.DeepEqual(got[0], daemon.Service{Port: int(closed[0].Addr.Port()), Name: "prog 0", Addr: closed[0].Addr}) {
		t.Fatalf("the first pass listed %+v, want the first 8 as programs named by their process", got)
	}
	began := time.Now()
	if got := d.Pass(closed); len(got) != 10 || time.Since(began) > 5*time.Second {
		t.Fatalf("the second pass listed %d, want all 10", len(got))
	}
	if got := d.Pass(nil); len(got) != 0 {
		t.Fatalf("an empty pass listed %+v", got)
	}
}

// A program that answers late is listed by its process and probed again in each of the next 2 passes, as a dev
// server that builds its first page; the last probe stands. Its models are asked for every pass until an answer
// says whether it is a model server, as Ollama listens before it serves.
func TestDiscoveryProbesALateAnswerAgain(t *testing.T) {
	hold := func(c net.Conn) { io.Copy(io.Discard, c) } // no answer until the probe gives up
	var up, serving atomic.Bool
	starting, startingConns := serve(t, func(c net.Conn) {
		if r := bufio.NewReader(c); !up.Load() {
			hold(c)
		} else {
			r.ReadString('\n')
			io.WriteString(c, "HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n<title>Next App</title>")
		}
	})
	silent, silentConns := serve(t, func(c net.Conn) {
		if r := bufio.NewReader(c); !serving.Load() {
			hold(c)
		} else {
			r.ReadString('\n')
			io.WriteString(c, "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n{\"models\":[{\"name\":\"qwen3:latest\"}]}")
		}
	})
	listener := func(port, pid int, name string) daemon.Listener {
		return daemon.Listener{Addr: netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port)), PID: pid, Name: name}
	}
	ls := []daemon.Listener{listener(starting, 7, "node"), listener(silent, 8, "ollama")}
	var d daemon.Discovery
	model := []daemon.Model{{ID: "qwen3:latest", Chat: true}}
	for pass, want := range [][]daemon.Service{
		{{Port: starting, Name: "node", Addr: ls[0].Addr}, {Port: silent, Name: "ollama", Addr: ls[1].Addr}},
		{{Port: starting, Name: "node", Addr: ls[0].Addr}, {Port: silent, Name: "ollama", Addr: ls[1].Addr}},
		{{Port: starting, Name: "Next App", Web: true, Addr: ls[0].Addr}, {Port: silent, Name: "ollama", Addr: ls[1].Addr}},
		{{Port: starting, Name: "Next App", Web: true, Addr: ls[0].Addr}, {Port: silent, Name: "ollama", Addr: ls[1].Addr}},
		{{Port: starting, Name: "Next App", Web: true, Addr: ls[0].Addr}, {Port: silent, Name: "ollama", Models: model, Addr: ls[1].Addr}},
	} {
		up.Store(pass >= 2)
		serving.Store(pass >= 4)
		if got := d.Pass(ls); !reflect.DeepEqual(got, want) {
			t.Fatalf("pass %d listed %+v, want %+v", pass+1, got, want)
		}
	}
	// Each of a page's 3 probes, and its models every pass until its page, which is no list, answers: 2, 2, 2.
	// The silent one: 2, 2, 2, then its models alone, 1, and 2 that answer: its list and that model's kind.
	if s, n := startingConns.Load(), silentConns.Load(); s != 6 || n != 9 {
		t.Fatalf("asked %d and %d times, want 6 and 9", s, n)
	}
}

// A machine lists the models of its model servers.

// modelServer answers each path with the status and body its routes give now, and keeps what it was asked.
type modelServer struct {
	mu     sync.Mutex
	routes map[string]string // "GET /v1/models" to "<status> <body>"
	asked  []string
}

func (m *modelServer) set(route, answer string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.routes[route] = answer
}

func (m *modelServer) requests() []string {
	m.mu.Lock()
	defer m.mu.Unlock()
	return slices.Clone(m.asked)
}

// newModelServer starts a model server, and returns it and its listener as a process with name.
func newModelServer(t *testing.T, pid int, name string, routes map[string]string) (*modelServer, daemon.Listener) {
	m := &modelServer{routes: routes}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		m.mu.Lock()
		route := r.Method + " " + r.URL.RequestURI()
		answer, ok := m.routes[route+" "+string(body)]
		if r.Host == "" && !ok {
			answer, ok = m.routes[route+" without Host"]
		}
		if !ok {
			answer, ok = m.routes[route]
		}
		m.asked = append(m.asked, strings.TrimSpace(route+" "+string(body))+" "+r.Header.Get("Accept"))
		m.mu.Unlock()
		if !ok {
			answer = "404 {\"detail\":\"Not Found\"}"
		}
		status, text, _ := strings.Cut(answer, " ")
		code, _ := strconv.Atoi(status)
		if strings.HasPrefix(text, "<") {
			w.Header().Set("Content-Type", "text/html")
		}
		w.WriteHeader(code)
		if rest, slow := strings.CutPrefix(text, "slow "); slow { // the headers at once, the body after discovery's 2 seconds
			w.(http.Flusher).Flush()
			time.Sleep(2500 * time.Millisecond)
			text = rest
		}
		io.WriteString(w, text)
	}))
	t.Cleanup(server.Close)
	return m, daemon.Listener{Addr: netip.MustParseAddrPort(server.Listener.Addr().String()), PID: pid, Name: name}
}

func TestDiscoveryReadsEachServersModels(t *testing.T) {
	long := strings.Repeat("é", 128)
	for _, c := range []struct {
		name, process string
		routes        map[string]string
		want          []daemon.Model // nil: not a model server
	}{
		{"vLLM", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[{"id":"Qwen/Qwen3-8B","object":"model","owned_by":"vllm","max_model_len":32768}]}`},
			[]daemon.Model{{ID: "Qwen/Qwen3-8B", Chat: true}}},
		{"no models loaded", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[]}`}, []daemon.Model{}},
		{"llama.cpp", "llama-server", map[string]string{"GET /v1/models": `200 {"object":"list","data":[
			{"id":"qwen3.gguf","architecture":{"input_modalities":["text"],"output_modalities":["text"]}},
			{"id":"bge.gguf","architecture":{"output_modalities":["embedding"]}}]}`},
			[]daemon.Model{{ID: "qwen3.gguf", Chat: true}, {ID: "bge.gguf"}}},
		{"Speaches", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[
			{"id":"whisper","task":"automatic-speech-recognition"},{"id":"smol","task":"text-generation"}]}`},
			[]daemon.Model{{ID: "whisper"}, {ID: "smol", Chat: true}}},
		{"Xinference", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[
			{"id":"qwen","model_type":"LLM"},{"id":"bge","model_type":"embedding"},{"id":"odd","model_type":7,"task":["x"]}]}`},
			[]daemon.Model{{ID: "qwen", Chat: true}, {ID: "bge"}, {ID: "odd"}}},
		{"Ollama", "ollama", map[string]string{
			"GET /v1/models": `200 {"object":"list","data":[{"id":"never-read"}]}`,
			"GET /api/tags": `200 {"models":[{"name":"qwen3:latest","digest":"a1"},{"name":"nomic-embed-text:latest","digest":"b2"},
				{"name":"gpt-oss:120b-cloud","digest":"c3","remote_host":"https://ollama.com:443"},{"name":"old:1","digest":"d4"},{"name":"gone:1","digest":"e5"}]}`,
			`POST /api/show {"model":"qwen3:latest"}`:            `200 {"capabilities":["completion","tools"]}`,
			`POST /api/show {"model":"nomic-embed-text:latest"}`: `200 {"capabilities":["embedding"]}`,
			`POST /api/show {"model":"old:1"}`:                   `200 {"details":{}}`,
			`POST /api/show {"model":"gone:1"}`:                  `404 {"error":"model 'gone:1' not found"}`},
			[]daemon.Model{{ID: "qwen3:latest", Chat: true}, {ID: "nomic-embed-text:latest"}, {ID: "gpt-oss:120b-cloud", Chat: true},
				{ID: "old:1", Chat: true}, {ID: "gone:1", Chat: true}}},
		{"ids", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[{"id":"a\u0000b​ c\n"},{"id":"` + long + `"},
			{"id":"` + long + `x"},{"id":"​"},{"id":""}]}`},
			[]daemon.Model{{ID: "ab c", Chat: true}, {ID: long, Chat: true}}},
		{"a web page", "node", map[string]string{"GET /v1/models": "200 <title>App</title>"}, nil},
		{"ComfyUI", "python3", map[string]string{"GET /v1/models": `200 ["checkpoints","loras"]`}, nil},
		{"an API key", "python3", map[string]string{"GET /v1/models": `401 {"error":"Unauthorized"}`}, nil},
		{"forbidden", "python3", map[string]string{"GET /v1/models": `403 {"object":"list","data":[{"id":"m"}]}`}, nil},
		{"llama.cpp with tools", "llama-server", map[string]string{ // it refuses a request without a Host line
			"GET /v1/models without Host": `403 {"error":{"message":"Cross-origin request not allowed (see --cors-origins)","code":403}}`,
			"GET /v1/models":              `200 {"object":"list","data":[{"id":"qwen3.gguf"}]}`},
			[]daemon.Model{{ID: "qwen3.gguf", Chat: true}}},
		{"no object", "python3", map[string]string{"GET /v1/models": `200 {"data":[{"id":"m"}]}`}, nil},
		{"no list", "python3", map[string]string{"GET /v1/models": `200 {"object":"list"}`}, nil},
		{"an id that is no string", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":[{"id":"m"},{"id":5}]}`}, nil},
		{"an item that is no object", "python3", map[string]string{"GET /v1/models": `200 {"object":"list","data":["m"]}`}, nil},
		{"Ollama without tags", "ollama", map[string]string{"GET /v1/models": `200 {"object":"list","data":[{"id":"m"}]}`}, nil},
	} {
		t.Run(c.name, func(t *testing.T) {
			server, l := newModelServer(t, 7, c.process, c.routes)
			var d daemon.Discovery
			got := d.Pass([]daemon.Listener{l})
			if len(got) != 1 || !reflect.DeepEqual(got[0].Models, c.want) || got[0].Name != c.process {
				t.Fatalf("Pass = %+v, want %s with the models %+v", got, c.process, c.want)
			}
			d.Pass([]daemon.Listener{l})
			asked := server.requests()
			for _, r := range asked {
				if !strings.HasPrefix(r, "GET / ") && !strings.HasSuffix(r, " application/json") {
					t.Errorf("discovery asked %q without Accept: application/json", r)
				}
				if what := strings.Fields(r)[1]; !slices.Contains([]string{"/", "/v1/models", "/api/tags", "/api/show"}, what) {
					t.Errorf("discovery asked %q", r)
				}
			}
			// After the first pass: nothing for a program that is no model server, its list for one, and no kind
			// twice, whatever the answer was. A 403 is asked once more with a Host line, each time.
			again := 1
			if c.want == nil {
				again = 0
			}
			more := map[string]int{"Ollama": 4, "forbidden": 1, "llama.cpp with tools": 1 + again}[c.name]
			if len(asked) != 2+more+again {
				t.Errorf("discovery asked %d times in two passes, want %d: %q", len(asked), 2+more+again, asked)
			}
		})
	}
}

// A model server's list is read again every pass, and kept when an answer is late, a 5xx or another shape, until
// another process holds the port.
func TestDiscoveryReadsAModelServersListEveryPass(t *testing.T) {
	list := func(ids ...string) string {
		return `200 {"object":"list","data":[{"id":"` + strings.Join(ids, `"},{"id":"`) + `"}]}`
	}
	server, l := newModelServer(t, 7, "python3", map[string]string{"GET /v1/models": list("a")})
	var d daemon.Discovery
	for step, c := range []struct {
		answer string
		want   []daemon.Model
	}{
		{list("a"), []daemon.Model{{ID: "a", Chat: true}}},
		{list("a", "b"), []daemon.Model{{ID: "a", Chat: true}, {ID: "b", Chat: true}}},
		{`503 {"error":"loading"}`, []daemon.Model{{ID: "a", Chat: true}, {ID: "b", Chat: true}}},
		{`200 <title>Busy</title>`, []daemon.Model{{ID: "a", Chat: true}, {ID: "b", Chat: true}}},
		{`200 {"object":"list","data":[]}`, []daemon.Model{}},
		{list("b"), []daemon.Model{{ID: "b", Chat: true}}},
	} {
		server.set("GET /v1/models", c.answer)
		if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || !reflect.DeepEqual(got[0].Models, c.want) {
			t.Fatalf("step %d: Pass = %+v, want the models %+v", step+1, got, c.want)
		}
	}
	server.set("GET /v1/models", "200 <title>App</title>")
	l.PID = 8
	if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || got[0].Models != nil {
		t.Fatalf("another process on the port: Pass = %+v, want no model server", got)
	}
}

// A list whose body is late is asked for again, as a late answer is.
func TestDiscoveryAsksAgainForALateList(t *testing.T) {
	const list = `{"object":"list","data":[{"id":"a"}]}`
	server, l := newModelServer(t, 7, "python3", map[string]string{"GET /v1/models": "200 slow " + list})
	var d daemon.Discovery
	if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || got[0].Models != nil {
		t.Fatalf("Pass = %+v, want no models yet", got)
	}
	server.set("GET /v1/models", "200 "+list)
	if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || !reflect.DeepEqual(got[0].Models, []daemon.Model{{ID: "a", Chat: true}}) {
		t.Fatalf("the next Pass = %+v, want the model a", got)
	}
}

// A model server that is not ready is asked every pass until it is, however long that takes: llama.cpp answers 503
// while it loads a model, an older one also at /, and a port Docker publishes closes each connection until the
// server in the container listens.
func TestDiscoveryAsksUntilAModelServerIsReady(t *testing.T) {
	const list = `{"object":"list","data":[{"id":"qwen3.gguf"}]}`
	loading := `503 {"error":{"message":"Loading model","type":"unavailable_error","code":503}}`
	want := []daemon.Model{{ID: "qwen3.gguf", Chat: true}}
	t.Run("llama.cpp loading a model", func(t *testing.T) {
		server, l := newModelServer(t, 7, "llama-server", map[string]string{"GET /": "200 <title>llama.cpp</title>", "GET /v1/models": loading})
		var d daemon.Discovery
		for pass := range 5 {
			if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || got[0].Models != nil || !got[0].Web {
				t.Fatalf("pass %d = %+v, want its page and no models yet", pass+1, got)
			}
		}
		server.set("GET /v1/models", "200 "+list)
		if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || !reflect.DeepEqual(got[0].Models, want) {
			t.Fatalf("once loaded: Pass = %+v, want the models %+v", got, want)
		}
		if asked := server.requests(); len(asked) != 7 { // its page once, its models each pass
			t.Fatalf("asked %d times, want 7: %q", len(asked), asked)
		}
	})
	t.Run("a list beside a page that answers 503", func(t *testing.T) {
		_, l := newModelServer(t, 7, "llama-server", map[string]string{"GET /": loading, "GET /v1/models": "200 " + list})
		var d daemon.Discovery
		if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || !reflect.DeepEqual(got[0].Models, want) {
			t.Fatalf("Pass = %+v, want the models %+v", got, want)
		}
	})
	t.Run("a port Docker publishes", func(t *testing.T) {
		var up atomic.Bool
		port, conns := serve(t, func(c net.Conn) {
			if up.Load() { // else closed at once, as docker-proxy does with nothing behind it
				bufio.NewReader(c).ReadString('\n')
				io.WriteString(c, "HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n"+list)
			}
		})
		l := daemon.Listener{Addr: netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(port)), Name: "docker-proxy"}
		var d daemon.Discovery
		for pass := range 5 {
			if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || got[0].Models != nil {
				t.Fatalf("pass %d = %+v, want no models yet", pass+1, got)
			}
		}
		up.Store(true)
		if got := d.Pass([]daemon.Listener{l}); len(got) != 1 || !reflect.DeepEqual(got[0].Models, want) {
			t.Fatalf("once it listens: Pass = %+v, want the models %+v", got, want)
		}
		if n := conns.Load(); n != 7 { // its page once, its models each pass
			t.Fatalf("asked %d times, want 7", n)
		}
	})
}

// A machine lists at most 200 models whose JSON fits 32 KiB, in port order, and an id once: on its lowest port.
func TestDiscoveryLimitsAMachinesModels(t *testing.T) {
	list := func(n int, id func(int) string) string {
		var b strings.Builder
		for i := range n {
			fmt.Fprintf(&b, `%s{"id":%q}`, map[bool]string{true: ",", false: ""}[i > 0], id(i))
		}
		return `200 {"object":"list","data":[` + b.String() + `]}`
	}
	_, first := newModelServer(t, 7, "a", map[string]string{"GET /v1/models": list(150, func(i int) string { return fmt.Sprint("m", i) })})
	_, second := newModelServer(t, 8, "b", map[string]string{"GET /v1/models": list(150, func(i int) string { return fmt.Sprint("m", i+100) })})
	var d daemon.Discovery
	got := d.Pass([]daemon.Listener{first, second})
	if len(got) != 2 || len(got[0].Models) != 150 || len(got[1].Models) != 50 || got[1].Models[0].ID != "m150" || got[1].Models[49].ID != "m199" {
		t.Fatalf("Pass listed %d and %d models, want 150 and the next 50 the first does not list", len(got[0].Models), len(got[1].Models))
	}
	_, wide := newModelServer(t, 9, "c", map[string]string{"GET /v1/models": list(190, func(i int) string { return strings.Repeat("é", 124) + fmt.Sprintf("%04d", i) })})
	_, after := newModelServer(t, 10, "d", map[string]string{"GET /v1/models": list(1, func(int) string { return "small" })})
	got = (&daemon.Discovery{}).Pass([]daemon.Listener{wide, after})
	b, _ := json.Marshal(got[0].Models)
	if n := len(got[0].Models); n < 100 || n > 130 || len(b) > 32<<10 || len(b) < 32<<10-300 || len(got[1].Models) != 0 || got[1].Models == nil {
		t.Fatalf("Pass listed %d models in %d bytes and then %+v, want as many as fit 32 KiB and none after", n, len(b), got[1].Models)
	}
}
