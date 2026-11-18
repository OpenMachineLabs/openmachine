package daemon

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"log"
	"maps"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"
)

// Opener is the daemon side of a Local: this computer's lease, its view of
// the account's machines and the OM SSH streams out of it.
type Opener interface {
	// Lease returns the lease new work starts under, once this computer has
	// joined the private network, or why it is not ready.
	Lease() (*Lease, error)
	// Devices returns the machines of the last lease's view.
	Devices() []ViewDevice
	// Dial opens an OM SSH stream to the machine alias names, for app, that
	// going offline closes. ErrUnreachable reports a machine that is offline.
	Dial(alias, app string) (net.Conn, error)
	// Services returns this computer's listening programs, with their models.
	Services() []Service
	// Key returns the 32-byte key the cookies of confirmed pages are made with.
	Key() ([]byte, error)
	// Pages returns the pages given a port on this computer, the one opened
	// last first, as SavePages last kept them.
	Pages() ([]Page, error)
	SavePages([]Page) error
}

// Page is a machine's web page with its port on this computer.
type Page struct {
	Page string `json:"page"` // alias:port
	Port int    `json:"port"`
}

const (
	pagePorts   = 299 // the ports after the entry's that pages get, as many as the state file holds
	pageStreams = 30  // of one page's connection at once; the target allows 32
	pageIdle    = 5 * time.Minute
	needsFull   = "om: ports need Full access on this machine"
	reconnect   = "om: Code authority changed; reconnect"
)

// Local is this computer's entry to the web pages on the machines it reaches.
// http://localhost:<entry>/<alias>/<port> gives the page a port of its own on
// this computer and sends the browser there:
// http://127.0.0.1:<that port>/ is localhost:<port> on that machine, through
// one OM SSH connection for each page, once the person has confirmed the page
// in this browser. Of the names that reach this computer, only localhost and
// its addresses are ones no browser sends to a system proxy, so pages differ
// by port; 127.0.0.1 keeps their cookies apart from this computer's own
// programs at localhost. At the entry's port it is also the one address of
// the AI models this computer reaches, through one OM SSH connection for each
// machine.
type Local struct {
	o           Opener
	device      string         // this computer's
	entry       string         // the entry's port
	first, last int            // the ports pages get
	path        *regexp.Regexp // an entry's path: a page's alias and port
	transport   *http.Transport
	models      *http.Transport // of the models' requests, each on a stream of its own

	mu      sync.Mutex
	pages   map[string]*pageConn // by alias:port, or alias:models
	table   []Page               // nil until read; the one opened last first
	serving context.Context      // set while Serve runs
	server  *http.Server
	bound   map[int][]net.Listener       // the pages' listeners, by port
	set     map[string]map[string]string // the cookies each page set for its script, by page, name and value, while om runs

	listing sync.Mutex // one list of models is read at a time
	list    []reached  // the models read last, in alias order; with listed, under mu
	listed  time.Time
}

// reached is a model the address reaches: on the machine alias names, or at
// addr, where its server listens on this computer.
type reached struct {
	Model
	server string // its server's name
	alias  string // the machine's display alias, "" for this computer without one
	addr   netip.AddrPort
}

// pageConn is the OM SSH connection of one page, or of a machine's models.
type pageConn struct {
	host    Host          // as the lease had it when this opened
	ready   chan struct{} // closed once client or err is set
	client  *ssh.Client
	err     error
	ended   chan struct{} // closed once client has ended
	slots   chan struct{} // one for each open stream
	streams int           // open or being opened; under Local.mu
	idle    *time.Timer   // closes it 5 minutes after its last stream; under Local.mu
}

// holds reports whether h, the page's host in the lease now, is the one p
// opened to: it has the same node key, key and user.
func (p *pageConn) holds(h *Host) bool {
	return h != nil && p.host.NodeKey == h.NodeKey && p.host.Key == h.Key && p.host.User == h.User
}

// hostOf returns the host alias names in lease, if any.
func hostOf(lease *Lease, alias string) *Host {
	if i := slices.IndexFunc(lease.Hosts, func(h Host) bool { return slices.Contains(h.Aliases, alias) }); i >= 0 {
		return &lease.Hosts[i]
	}
	return nil
}

// NewLocal returns the entry of the device deviceID, which listens on port.
func NewLocal(o Opener, deviceID, port string) *Local {
	n, _ := strconv.Atoi(port)
	l := &Local{o: o, device: deviceID, entry: port, first: n + 1, last: n + pagePorts, pages: map[string]*pageConn{}, bound: map[int][]net.Listener{}, set: map[string]map[string]string{},
		path: regexp.MustCompile(`^/(` + label + `(?:\.` + label + `)?)/([1-9][0-9]{0,4})/?$`)}
	// Each stream is one direct-tcpip channel. The page's alias and port stand
	// in for an address. The transport bounds the dials that wait; stream
	// counts the streams, as the transport counts WebSockets apart and stops
	// counting one once it is open.
	l.transport = &http.Transport{DialContext: l.stream, MaxConnsPerHost: pageStreams, MaxIdleConnsPerHost: pageStreams,
		IdleConnTimeout: time.Minute, DisableCompression: true}
	// A request to <alias>.om is one models channel to that machine; any other is to this computer's own server,
	// at the address it listens on.
	l.models = &http.Transport{DisableKeepAlives: true, DisableCompression: true, DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
		host, _, _ := net.SplitHostPort(addr)
		if alias, ok := strings.CutSuffix(host, ".om"); ok {
			return l.stream(ctx, network, alias+":models")
		}
		at, err := netip.ParseAddrPort(addr)
		if err != nil {
			return nil, err
		}
		return dial(loopbacks(at), int(at.Port()), 10*time.Second)
	}}
	return l
}

// Serve serves l on 127.0.0.1 and [::1], at its port and at each page's,
// until ctx ends. An entry address it cannot bind is tried again every 30
// seconds and logged once; a page whose port is taken gets another one when it
// is opened next.
func (l *Local) Serve(ctx context.Context, port string) {
	srv := &http.Server{Handler: l}
	l.mu.Lock()
	l.serving, l.server = ctx, srv
	if l.read() == nil {
		for _, p := range slices.Clone(l.table) {
			if l.listen(p.Port) != nil {
				l.forget(p.Port)
			}
		}
		l.o.SavePages(l.table)
	}
	l.mu.Unlock()
	for _, addr := range []string{"127.0.0.1:" + port, "[::1]:" + port} {
		go func() {
			for logged := false; ctx.Err() == nil; sleep(ctx, 30*time.Second) {
				ln, err := net.Listen("tcp", addr)
				if err == nil {
					err = srv.Serve(ownPeers{ln})
				}
				if !logged && ctx.Err() == nil {
					log.Printf("native: serving pages on %s: %v", addr, err)
					logged = true
				}
			}
		}()
	}
	<-ctx.Done()
	srv.Close()
}

// read reads the pages' ports once, under mu, keeping each page and port once.
func (l *Local) read() error {
	if l.table != nil {
		return nil
	}
	table, err := l.o.Pages()
	if err != nil {
		return err
	}
	l.table = []Page{}
	for _, p := range table {
		if l.path.MatchString("/"+strings.Replace(p.Page, ":", "/", 1)) && p.Port >= l.first && p.Port <= l.last &&
			!slices.ContainsFunc(l.table, func(q Page) bool { return q.Page == p.Page || q.Port == p.Port }) {
			l.table = append(l.table, p)
		}
	}
	return nil
}

// listen serves the page port on 127.0.0.1, under mu, while Serve runs.
func (l *Local) listen(port int) error {
	if l.serving == nil || l.bound[port] != nil {
		return nil
	}
	ln, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
	if err != nil {
		return err
	}
	l.bound[port] = []net.Listener{ln}
	go l.server.Serve(ownPeers{ln})
	return nil
}

// forget takes the page at port out of the table and stops serving it, under mu.
func (l *Local) forget(port int) {
	l.table = slices.DeleteFunc(l.table, func(p Page) bool {
		if p.Port == port {
			delete(l.set, p.Page)
		}
		return p.Port == port
	})
	for _, ln := range l.bound[port] {
		ln.Close()
	}
	delete(l.bound, port)
}

// portOf returns the port of page on this computer, giving it one first: the
// lowest free one that binds, or else the one of the page opened longest ago.
// The page becomes the one opened last.
func (l *Local) portOf(page string) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.read(); err != nil {
		return 0, err
	}
	if i := slices.IndexFunc(l.table, func(p Page) bool { return p.Page == page }); i >= 0 {
		p := l.table[i]
		l.table = slices.Insert(slices.Delete(l.table, i, i+1), 0, p)
		return p.Port, l.o.SavePages(l.table)
	}
	for tried := map[int]bool{}; ; {
		port := 0
		for n := l.first; n <= l.last && port == 0; n++ {
			if !tried[n] && !slices.ContainsFunc(l.table, func(p Page) bool { return p.Port == n }) {
				port = n
			}
		}
		if port == 0 {
			if len(l.table) == 0 {
				return 0, errors.New("no port is free for a page")
			}
			port = l.table[len(l.table)-1].Port
			l.forget(port)
		}
		if l.listen(port) != nil {
			tried[port] = true
			continue
		}
		l.table = slices.Insert(l.table, 0, Page{page, port})
		return port, l.o.SavePages(l.table)
	}
}

// pageAt returns the page served at port, if any.
func (l *Local) pageAt(port int) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.read() == nil {
		if i := slices.IndexFunc(l.table, func(p Page) bool { return p.Port == port }); i >= 0 {
			return l.table[i].Page
		}
	}
	return ""
}

// enter answers the entry: GET /<alias>/<port> sends the browser to the
// page's port, given here; nothing else is found. Only a page the browser
// loads is given a port, so another website cannot fill the table.
func (l *Local) enter(w http.ResponseWriter, r *http.Request) {
	own(w.Header())
	m := l.path.FindStringSubmatch(strings.ToLower(r.URL.Path))
	if m == nil || r.URL.RawQuery != "" || r.Method != "GET" && r.Method != "HEAD" || len(m[len(m)-1]) == 5 && m[len(m)-1] > "65535" {
		http.Error(w, "Not found.", http.StatusNotFound)
		return
	}
	if dest := r.Header.Get("Sec-Fetch-Dest"); dest != "" && dest != "document" {
		http.Error(w, "Forbidden.", http.StatusForbidden)
		return
	}
	alias, port := m[1], m[len(m)-1]
	if slices.ContainsFunc(l.o.Devices(), func(v ViewDevice) bool { return v.DeviceID == l.device && v.Alias != nil && *v.Alias == alias }) {
		http.Redirect(w, r, "http://localhost:"+port+"/", http.StatusFound)
		return
	}
	n, err := l.portOf(alias + ":" + port)
	if err != nil {
		http.Error(w, "om: "+err.Error(), http.StatusInternalServerError)
		return
	}
	http.Redirect(w, r, "http://127.0.0.1:"+strconv.Itoa(n)+"/", http.StatusFound)
}

// pageCookie names the cookies om keeps for a page, om<its port>_<its mark>_<name>.
var pageCookie = regexp.MustCompile(`^om[0-9]+_`)

// attrs returns the attributes of the Set-Cookie line c, trimmed.
func attrs(c string) []string {
	_, rest, _ := strings.Cut(c, ";")
	var a []string
	for _, v := range strings.Split(rest, ";") {
		a = append(a, strings.TrimSpace(v))
	}
	return a
}

// httpOnly reports whether the Set-Cookie line c is HttpOnly.
func httpOnly(c string) bool {
	return slices.ContainsFunc(attrs(c), func(a string) bool { return strings.EqualFold(a, "HttpOnly") })
}

// removes reports whether the Set-Cookie line c removes its cookie: Max-Age
// 0 or less, or Expires in the past.
func removes(c string, now time.Time) bool {
	return slices.ContainsFunc(attrs(c), func(a string) bool {
		k, v, _ := strings.Cut(a, "=")
		if strings.EqualFold(strings.TrimSpace(k), "Max-Age") {
			n, err := strconv.Atoi(strings.TrimSpace(v))
			return err == nil && n <= 0
		}
		t, err := http.ParseTime(strings.TrimSpace(v))
		return strings.EqualFold(strings.TrimSpace(k), "Expires") && err == nil && t.Before(now)
	})
}

// ownPeers accepts, on Linux, only the connections of om's own OS user, as
// only that user reaches the daemon's socket; any other is closed unanswered.
type ownPeers struct{ net.Listener }

func (l ownPeers) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil || runtime.GOOS != "linux" || ownPeer(c) {
			return c, err
		}
		c.Close()
	}
}

func ownPeer(c net.Conn) bool {
	peer, err := netip.ParseAddrPort(c.RemoteAddr().String())
	local, lerr := netip.ParseAddrPort(c.LocalAddr().String())
	if err != nil || lerr != nil {
		return false
	}
	for _, name := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		b, _ := os.ReadFile(name)
		if uid, ok := PeerUID(b, peer, local); ok {
			return uid == uint32(os.Getuid())
		}
	}
	return false
}

// PeerUID finds, in Linux's /proc/net/tcp or tcp6, the socket connected from
// peer to local, and returns the user that owns it.
func PeerUID(proc []byte, peer, local netip.AddrPort) (uint32, bool) {
	for _, line := range strings.Split(string(proc), "\n") {
		f := strings.Fields(line)
		if len(f) < 8 {
			continue
		}
		from, ok := procAddr(f[1])
		to, tok := procAddr(f[2])
		uid, err := strconv.ParseUint(f[7], 10, 32)
		// A socket of both families holds an IPv4 address as an IPv6 one.
		same := func(a, b netip.AddrPort) bool { return a.Port() == b.Port() && a.Addr().Unmap() == b.Addr().Unmap() }
		if ok && tok && err == nil && same(from, peer) && same(to, local) {
			return uint32(uid), true
		}
	}
	return 0, false
}

// The answers that are om's own, never a machine's.
func own(h http.Header) {
	h.Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; frame-ancestors 'none'")
	h.Set("Cache-Control", "no-store")
	// Not no-referrer, under which Ask would post Origin: null and never confirm.
	h.Set("Referrer-Policy", "same-origin")
}

func (l *Local) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	host := strings.ToLower(r.Host)
	// At the entry's port, /v1/ is the models' address, but for the page of a machine named v1.
	if slices.Contains([]string{"localhost:" + l.entry, "127.0.0.1:" + l.entry, "[::1]:" + l.entry}, host) &&
		strings.HasPrefix(r.URL.Path, "/v1/") && !l.path.MatchString(strings.ToLower(r.URL.Path)) {
		l.serveModels(w, r)
		return
	}
	if host == "localhost:"+l.entry {
		l.enter(w, r)
		return
	}
	if host == "127.0.0.1:"+l.entry && r.URL.Path == "/" && r.Method == "GET" {
		l.try(w, r)
		return
	}
	// A page answers only to its own address, so no name that resolves here reaches it.
	at, _ := strconv.Atoi(strings.TrimPrefix(host, "127.0.0.1:"))
	page := ""
	if host == "127.0.0.1:"+strconv.Itoa(at) && at >= l.first && at <= l.last {
		page = l.pageAt(at)
	}
	if page == "" {
		own(w.Header())
		http.Error(w, "Not found.", http.StatusNotFound)
		return
	}
	alias, port, _ := strings.Cut(page, ":")
	origin, served := "http://"+host, strconv.Itoa(at)
	devices := l.o.Devices()
	key, err := l.o.Key()
	if err != nil {
		own(w.Header())
		http.Error(w, "om: "+err.Error(), http.StatusInternalServerError)
		return
	}
	// The cookie is the page's at this port: a port given to another page asks again.
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(page + "@" + served))
	cookie, confirmed := base64.RawURLEncoding.EncodeToString(mac.Sum(nil)), "om_open_"+served
	if confirming(r) {
		own(w.Header())
		if r.Header.Get("Origin") != origin {
			http.Error(w, "Forbidden.", http.StatusForbidden)
			return
		}
		w.Header().Set("Set-Cookie", confirmed+"="+cookie+"; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
		http.Redirect(w, r, origin+r.URL.RequestURI(), http.StatusSeeOther)
		return
	}
	lease, err := l.o.Lease()
	// The machine as the view names it, with what this person gets there.
	name, row := alias, (*ViewDevice)(nil)
	var h *Host
	if err == nil {
		h = hostOf(lease, alias)
	}
	if i := slices.IndexFunc(devices, func(v ViewDevice) bool {
		return v.Alias != nil && *v.Alias == alias || h != nil && v.DeviceID == h.DeviceID
	}); i >= 0 {
		name, row = devices[i].Name, &devices[i]
	}
	name = html.EscapeString(name)
	notReady := func() {
		show(w, http.StatusServiceUnavailable, "Open Machine isn’t ready", "Check that Open Machine is on and signed in on this computer, then try again.", again)
	}
	if !slices.ContainsFunc(r.Cookies(), func(c *http.Cookie) bool { return c.Name == confirmed && hmac.Equal([]byte(c.Value), []byte(cookie)) }) {
		// The first visit: nothing is sent to the machine before the person agrees.
		switch {
		case r.Method != "GET" && r.Method != "HEAD":
			own(w.Header())
			http.Error(w, "Forbidden.", http.StatusForbidden)
		case err != nil:
			notReady()
		default:
			show(w, http.StatusOK, "Open port "+port+"?", "It runs on "+name+".", `<form method="post"><input type="hidden" name="om_open" value="1"><button class="primary" autofocus>Open port `+port+
				`</button></form><p class="why">We ask once per page, so no other website can open it without you.</p>`)
		}
		return
	}
	if err != nil {
		notReady()
		return
	}
	// Another page the person opened cannot post here or open a WebSocket.
	if from := r.Header.Get("Origin"); from != "" && from != origin && !(from == "null" && r.Header.Get("Sec-Fetch-Site") == "same-origin") {
		own(w.Header())
		http.Error(w, "Forbidden.", http.StatusForbidden)
		return
	}
	// The page keeps its HttpOnly cookies as om<port>_<mark>_<name>, its mark
	// made from the page and its port, so another page given the port later
	// gets none of them.
	mark := hmac.New(sha256.New, key)
	mark.Write([]byte("cookies " + page + "@" + served))
	there, mine := "http://localhost:"+port, "om"+served+"_"+fmt.Sprintf("%x", mark.Sum(nil)[:4])+"_"
	// A connection opened under another node key, key or user of the host
	// closes before this request, not up to a second after the change.
	l.mu.Lock()
	held := l.pages[page]
	l.mu.Unlock()
	if held != nil && !held.holds(h) {
		l.drop(page, held)
	}
	var got *httptrace.GotConnInfo
	r = r.WithContext(httptrace.WithClientTrace(r.Context(), &httptrace.ClientTrace{GotConn: func(i httptrace.GotConnInfo) { got = &i }}))
	(&httputil.ReverseProxy{
		Transport: l.transport,
		// The request as the machine's own browser would send it.
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme, pr.Out.URL.Host, pr.Out.URL.RawQuery = "http", page, pr.In.URL.RawQuery
			pr.Out.Host = "localhost:" + port
			// The browser keeps one set of cookies for 127.0.0.1, whatever the
			// port: the page gets its own, and a cookie for its script only with
			// the value it set, so no other page's or program's.
			l.mu.Lock()
			set := maps.Clone(l.set[page])
			l.mu.Unlock()
			var cookies []string
			for _, c := range strings.Split(strings.Join(pr.In.Header.Values("Cookie"), ";"), ";") {
				c = strings.TrimSpace(c)
				name, _, _ := strings.Cut(c, "=")
				switch {
				case strings.HasPrefix(c, mine):
					cookies = append(cookies, strings.TrimPrefix(c, mine))
				default:
					if value, ok := set[name]; ok && strings.Contains(c, "=") && value == strings.TrimPrefix(c, name+"=") {
						cookies = append(cookies, c)
					}
				}
			}
			if pr.Out.Header.Del("Cookie"); len(cookies) > 0 {
				pr.Out.Header.Set("Cookie", strings.Join(cookies, "; "))
			}
			if pr.In.Header.Get("Origin") == origin {
				pr.Out.Header.Set("Origin", there)
			}
			if path, ok := strings.CutPrefix(pr.In.Header.Get("Referer"), origin+"/"); ok {
				pr.Out.Header.Set("Referer", there+"/"+path)
			}
			// A link from another site, such as the Console's, to the page's front
			// door arrives as if typed; any other cross-site request stays one, for
			// the page's own guard.
			if pr.In.Method == "GET" && pr.In.URL.RequestURI() == "/" && pr.In.Header.Get("Sec-Fetch-Site") == "cross-site" && pr.In.Header.Get("Sec-Fetch-Dest") == "document" {
				pr.Out.Header.Set("Sec-Fetch-Site", "none")
			}
		},
		ModifyResponse: func(res *http.Response) error {
			// An HttpOnly cookie, which no script of a page reads, is kept under
			// the page's mark; any other is the page's with the value it set; a
			// removal removes both; a page cannot set one of om's or another page's.
			lines := res.Header.Values("Set-Cookie")
			res.Header.Del("Set-Cookie")
			for _, c := range lines {
				c = strings.TrimSpace(c)
				name, _, _ := strings.Cut(c, "=")
				if name = strings.TrimSpace(name); name == "" || !strings.Contains(c, "=") || strings.HasPrefix(name, "om_open") || pageCookie.MatchString(name) {
					continue
				}
				value, _, _ := strings.Cut(strings.TrimPrefix(c[strings.Index(c, "=")+1:], " "), ";")
				l.mu.Lock()
				set := l.set[page]
				if set == nil {
					set = map[string]string{}
					l.set[page] = set
				}
				switch {
				case removes(c, time.Now()):
					delete(set, name)
					res.Header.Add("Set-Cookie", mine+c)
					res.Header.Add("Set-Cookie", c)
				case httpOnly(c):
					res.Header.Add("Set-Cookie", mine+c)
				default:
					if _, ok := set[name]; ok || len(set) < 64 {
						set[name] = strings.TrimSpace(value)
					}
					res.Header.Add("Set-Cookie", c)
				}
				l.mu.Unlock()
			}
			n, _ := strconv.Atoi(port)
			// A redirect to this port's address stays on the page.
			if u, err := url.Parse(res.Header.Get("Location")); err == nil && u.Host != "" && here(u, n) {
				u.Host = host
				res.Header.Set("Location", u.String())
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			var refused *ssh.OpenChannelError
			errors.As(err, &refused)
			level := "Read only"
			if row != nil && row.Mode != nil && *row.Mode == "workspace" {
				level = "Workspace"
			}
			switch {
			case refused != nil && refused.Reason == ssh.ConnectionFailed:
				show(w, http.StatusBadGateway, "Nothing on port "+port, "Nothing on "+name+" is using this port. Check the number, or start the program first.", again)
			case refused != nil && refused.Reason == ssh.Prohibited && refused.Message == needsFull && row != nil && row.Team != nil:
				show(w, http.StatusForbidden, "You can’t open this", "You have "+level+" on "+name+". Ask an admin of "+html.EscapeString(row.Team.Name)+" for Full access.", "")
			case refused != nil && refused.Reason == ssh.Prohibited && refused.Message == needsFull:
				show(w, http.StatusForbidden, "You can’t open this", name+" is set to "+level+". Change it to Full access to open this.", "")
			case refused != nil && refused.Reason == ssh.Prohibited && !strings.HasPrefix(refused.Message, "om: "): // a lock's
				show(w, http.StatusForbidden, "You can’t open this", "A rule on "+name+" blocks it.", "")
			case errors.Is(err, ErrUnreachable):
				show(w, http.StatusBadGateway, "Can’t reach "+name, "It’s offline. Try again when it’s back.", again)
			case refused == nil && got != nil && !got.Reused: // a new stream's first answer is not HTTP
				show(w, http.StatusBadGateway, "This isn’t a web page", "Port "+port+" on "+name+" doesn’t answer as a web page. Use it as a program instead.", "")
			default:
				show(w, http.StatusBadGateway, "Can’t reach "+name, "This computer can’t connect to it.", "")
			}
		},
	}).ServeHTTP(w, r)
}

// confirming reports whether r is Ask's POST, byte for byte; the body of any
// other request stays whole.
func confirming(r *http.Request) bool {
	if r.Method != "POST" || r.Header.Get("Content-Type") != "application/x-www-form-urlencoded" || r.ContentLength != 9 {
		return false
	}
	b := make([]byte, 9)
	n, _ := io.ReadFull(r.Body, b)
	r.Body = struct {
		io.Reader
		io.Closer
	}{io.MultiReader(bytes.NewReader(b[:n]), r.Body), r.Body}
	return string(b[:n]) == "om_open=1"
}

// stream opens one stream of the page addr names, alias:port, to localhost
// at that port on the machine, over the page's connection, once fewer than
// 30 are open; alias:models is a models channel, over the connection of that
// machine's models. It is asked for once more, on a new connection, when the
// machine refuses it because its authority changed or when the connection
// ended meanwhile.
func (l *Local) stream(ctx context.Context, _, addr string) (net.Conn, error) {
	alias, port, _ := net.SplitHostPort(addr)
	for first := true; ; first = false {
		lease, err := l.o.Lease()
		if err != nil {
			return nil, err
		}
		h := hostOf(lease, alias)
		if h == nil {
			return nil, fmt.Errorf("%s is not a machine this computer can use", alias)
		}
		l.mu.Lock()
		p := l.pages[addr]
		if p == nil {
			streams := pageStreams
			if port == "models" {
				streams-- // with the list beside them (listing), as many channels as a page's
			}
			p = &pageConn{host: *h, ready: make(chan struct{}), ended: make(chan struct{}), slots: make(chan struct{}, streams)}
			l.pages[addr] = p
			go l.open(addr, alias, p)
		}
		if p.streams++; p.idle != nil {
			p.idle.Stop()
		}
		l.mu.Unlock()
		<-p.ready
		release := func() {
			l.mu.Lock()
			if p.streams--; p.streams == 0 && p.client != nil {
				p.idle = time.AfterFunc(pageIdle, func() {
					l.mu.Lock()
					quiet := p.streams == 0
					if quiet && l.pages[addr] == p {
						delete(l.pages, addr)
					}
					l.mu.Unlock()
					if quiet {
						p.client.Close()
					}
				})
			}
			l.mu.Unlock()
		}
		list := ctx.Value(listing{}) != nil
		give := func() {
			if !list {
				<-p.slots
			}
		}
		if err = p.err; err == nil && (list || l.take(p)) {
			var c net.Conn
			if port != "models" {
				c, err = p.client.Dial("tcp", "localhost:"+port)
			} else if ch, reqs, cerr := p.client.OpenChannel(modelsChannel, nil); cerr == nil {
				go ssh.DiscardRequests(reqs)
				c = channel{ch}
			} else {
				err = cerr
			}
			if err == nil {
				return &pageStream{Conn: c, closed: func() { give(); release() }}, nil
			}
			give()
		} else if err == nil {
			err = errors.New("the page's connection ended")
		}
		release()
		if refused := (*ssh.OpenChannelError)(nil); !first || p.err != nil || errors.As(err, &refused) && refused.Message != reconnect {
			return nil, err
		}
		l.drop(addr, p)
	}
}

// take waits for one of p's 30 streams, and reports whether it got one
// before p ended. While none is free, the streams the transport keeps open
// for a next request close to make room: it keeps those of WebSockets and of
// other requests apart, so neither would use the other's.
func (l *Local) take(p *pageConn) bool {
	for {
		select {
		case p.slots <- struct{}{}:
			return true
		default:
			l.transport.CloseIdleConnections()
		}
		select {
		case p.slots <- struct{}{}:
			return true
		case <-p.ended:
			return false
		case <-time.After(100 * time.Millisecond):
		}
	}
}

// open connects p, the connection of the page addr names, and holds it until
// it ends, as when the machine's access changes, or until its host changes in
// the lease, read each second: the next request opens a new one.
func (l *Local) open(addr, alias string, p *pageConn) {
	app := "Browser"
	if strings.HasSuffix(addr, ":models") {
		app = "Models"
	}
	p.client, p.err = l.connect(alias, app, p.host)
	close(p.ready)
	if p.err == nil {
		go func() { p.client.Wait(); close(p.ended) }()
		for held := true; held; {
			select {
			case <-p.ended:
				held = false
			case <-time.After(time.Second):
				// With no lease, as offline, the daemon closes what must end.
				if lease, err := l.o.Lease(); err == nil {
					held = p.holds(hostOf(lease, alias))
				}
			}
		}
	}
	l.drop(addr, p)
}

// connect runs an SSH client on a new OM SSH stream to h, for app, as its
// user and accepting only its key; OM SSH asks for no other authentication.
func (l *Local) connect(alias, app string, h Host) (*ssh.Client, error) {
	key, _, _, _, err := ssh.ParseAuthorizedKey([]byte(h.Key))
	if err != nil {
		return nil, err
	}
	nc, err := l.o.Dial(alias, app)
	if err != nil {
		return nil, err
	}
	nc.SetDeadline(time.Now().Add(handshakeTimeout))
	c, chans, reqs, err := ssh.NewClientConn(nc, alias, &ssh.ClientConfig{User: h.User, HostKeyCallback: ssh.FixedHostKey(key)})
	if err != nil {
		nc.Close()
		return nil, err
	}
	nc.SetDeadline(time.Time{})
	return ssh.NewClient(c, chans, reqs), nil
}

// drop forgets p, unless another connection took its place, and closes it.
func (l *Local) drop(addr string, p *pageConn) {
	l.mu.Lock()
	if l.pages[addr] == p {
		delete(l.pages, addr)
	}
	l.mu.Unlock()
	if <-p.ready; p.client != nil {
		p.client.Close()
	}
}

// pageStream is one stream of a page's connection.
type pageStream struct {
	net.Conn
	once   sync.Once
	closed func()
}

// Close ends the stream before it is counted out, so the machine hears of
// its end before the next one opens.
func (s *pageStream) Close() error {
	err := s.Conn.Close()
	s.once.Do(s.closed)
	return err
}

// listing marks, in its context, the request for a machine's list of models:
// it waits for none of the connection's 29 streams, so requests that hold
// them all hide no model. One list is read at a time, so a machine's models
// take at most 30 of the 32 channels it allows, as a page does.
type listing struct{}

// channel is an SSH channel as the connection of one HTTP request: it has no
// addresses, and the request's own context bounds it.
type channel struct{ ssh.Channel }

func (channel) LocalAddr() net.Addr              { return &net.TCPAddr{} }
func (channel) RemoteAddr() net.Addr             { return &net.TCPAddr{} }
func (channel) SetDeadline(time.Time) error      { return nil }
func (channel) SetReadDeadline(time.Time) error  { return nil }
func (channel) SetWriteDeadline(time.Time) error { return nil }

// serveModels answers the models' address, /v1/ at the entry's port: GET
// /v1/models lists the models this computer reaches, and an inference POST
// goes to the machine of the model its body names, under that machine's own
// id for it. /v1/v1/ is /v1/. A website's request is refused: only no Origin,
// the address's own and one whose scheme is not http or https are answered,
// and no server sees it.
func (l *Local) serveModels(w http.ResponseWriter, r *http.Request) {
	h := w.Header()
	h.Set("Content-Type", "application/json")
	refuse := func(status int, message string) {
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(modelError(message))
	}
	if origin := r.Header.Get("Origin"); origin != "" {
		scheme, _, ok := strings.Cut(strings.ToLower(origin), "://")
		if origin != "http://"+r.Host && (!ok || scheme == "http" || scheme == "https") {
			refuse(http.StatusForbidden, "Websites can’t use the models here.")
			return
		}
		h.Set("Access-Control-Allow-Origin", origin)
		h.Set("Vary", "Origin")
		if r.Method == "OPTIONS" {
			h.Set("Access-Control-Allow-Methods", r.Header.Get("Access-Control-Request-Method"))
			h.Set("Access-Control-Allow-Headers", r.Header.Get("Access-Control-Request-Headers"))
			h.Del("Content-Type")
			w.WriteHeader(http.StatusNoContent)
			return
		}
	}
	if path, ok := strings.CutPrefix(r.URL.Path, "/v1/v1/"); ok {
		r.URL.Path = "/v1/" + path
	}
	if r.Method == "GET" && r.URL.Path == "/v1/models" {
		data, list := []map[string]any{}, l.reach(false)
		for i, name := range named(list) {
			data = append(data, map[string]any{"id": name, "object": "model", "owned_by": list[i].server, "chat": list[i].Chat})
		}
		json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
		return
	}
	body, model, start, end, status, message := modelRequest(r)
	if status != 0 {
		refuse(status, message)
		return
	}
	m := find(l.reach(false), model)
	if m == nil {
		m = find(l.reach(true), model)
	}
	if m == nil {
		refuse(http.StatusNotFound, "No machine this computer reaches has the model "+strconv.Quote(model)+".")
		return
	}
	id, _ := json.Marshal(m.ID)
	body = slices.Concat(body[:start], id, body[end:])
	r.Body, r.ContentLength, r.TransferEncoding = io.NopCloser(bytes.NewReader(body)), int64(len(body)), nil
	to, host := m.alias+".om", m.alias+".om"
	if m.addr.IsValid() {
		to, host = m.addr.String(), "localhost:"+strconv.Itoa(int(m.addr.Port()))
	}
	(&httputil.ReverseProxy{
		Transport: l.models,
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.Out.URL.Scheme, pr.Out.URL.Host, pr.Out.Host = "http", to, host
			pr.Out.Header.Del("Origin")
			pr.Out.Header.Del("Referer")
		},
		// The answer to an app's origin is om's own, whatever the server says to none.
		ModifyResponse: func(res *http.Response) error {
			res.Header.Del("Access-Control-Allow-Origin")
			h.Del("Content-Type")
			return nil
		},
		ErrorHandler: func(_ http.ResponseWriter, _ *http.Request, err error) {
			message := "This computer can’t reach the model’s machine."
			if refused := (*ssh.OpenChannelError)(nil); errors.As(err, &refused) {
				message = strings.TrimPrefix(refused.Message, "om: ") // the machine's reason
			}
			refuse(http.StatusBadGateway, message)
		},
	}).ServeHTTP(w, r)
}

// named returns the name the address gives each model of list, which is in
// alias order: its id, and id@alias on a machine after the first that has it.
func named(list []reached) []string {
	names, seen := make([]string, len(list)), map[string]bool{}
	for i, m := range list {
		if names[i] = m.ID; seen[m.ID] {
			names[i] += "@" + m.alias
		}
		seen[m.ID] = true
	}
	return names
}

// find returns the model of list that name names: the first, in alias order,
// that answers to it, or the one on the machine after its @.
func find(list []reached, name string) *reached {
	for _, loose := range []bool{false, true} {
		for i, m := range list {
			if on, ok := strings.CutSuffix(name, "@"+m.alias); answers(m.ID, name, loose) || ok && answers(m.ID, on, loose) {
				return &list[i]
			}
		}
	}
	return nil
}

// reach returns the models the address reaches, in alias order: this
// computer's own, and those of each host of its lease, asked at once over a
// models channel and left out when it gives no list within 3 seconds. The
// list is read again once it is 15 seconds old, or when fresh is asked for:
// once for the requests that ask meanwhile, and no other request waits for it.
func (l *Local) reach(fresh bool) []reached {
	asked := time.Now()
	kept := func() []reached {
		l.mu.Lock()
		defer l.mu.Unlock()
		if time.Since(l.listed) < 15*time.Second && (!fresh || l.listed.After(asked)) {
			return l.list
		}
		return nil
	}
	if list := kept(); list != nil {
		return list
	}
	l.listing.Lock()
	defer l.listing.Unlock()
	if list := kept(); list != nil {
		return list
	}
	own := ""
	for _, v := range l.o.Devices() {
		if v.DeviceID == l.device && v.Alias != nil {
			own = *v.Alias
		}
	}
	list := []reached{}
	for _, s := range l.o.Services() {
		for _, m := range s.Models {
			list = append(list, reached{m, s.Name, own, s.Addr})
		}
	}
	var hosts []Host
	if lease, err := l.o.Lease(); err == nil {
		hosts = lease.Hosts
	}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, host := range hosts {
		if len(host.Aliases) == 0 {
			continue
		}
		alias := host.Aliases[len(host.Aliases)-1] // its display alias, as Machines shows it
		wg.Go(func() {
			req, _ := http.NewRequestWithContext(context.WithValue(context.Background(), listing{}, true), "GET", "http://"+alias+".om/v1/models", nil)
			// Its answer is the machine's own: a redirect would lead out of the channel.
			res, err := (&http.Client{Transport: l.models, Timeout: 3 * time.Second,
				CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}).Do(req)
			if err != nil {
				return
			}
			defer res.Body.Close()
			var answer struct {
				Data []struct {
					Model
					OwnedBy string `json:"owned_by"`
				}
			}
			if res.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&answer) != nil {
				return
			}
			mu.Lock()
			defer mu.Unlock()
			for _, m := range answer.Data {
				list = append(list, reached{m.Model, m.OwnedBy, alias, netip.AddrPort{}})
			}
		})
	}
	wg.Wait()
	slices.SortStableFunc(list, func(a, b reached) int { return strings.Compare(a.alias, b.alias) })
	l.mu.Lock()
	l.list, l.listed = list, time.Now()
	l.mu.Unlock()
	return list
}

// again reloads the page.
const again = `<a class="button" href="" autofocus>Try again</a>`

// show writes one of om's own pages: the brand, then a title, a sentence and
// what follows them, each escaped by its caller. It follows the system's theme
// and loads nothing.
func show(w http.ResponseWriter, status int, title, text, action string) {
	own(w.Header())
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	fmt.Fprintf(w, `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>%s · Open Machine</title><style>`+
		theme+`*{box-sizing:border-box}body{margin:0;padding:0 16px;display:flex;flex-direction:column;align-items:center;background:var(--bg);color:var(--ink);font:400 14px/20px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:-.01em;-webkit-font-smoothing:antialiased;overflow-wrap:anywhere}`+
		`header{display:flex;align-items:center;gap:10px;padding-top:72px;font-size:18px;line-height:24px;font-weight:600}header img{width:28px;height:28px;border-radius:6px;background:#090909}`+
		`main{width:100%%;max-width:400px;padding:40px 0 48px}h1{margin:0 0 8px;font-size:24px;line-height:30px;font-weight:600;letter-spacing:-.02em}p{margin:0;color:var(--ink2)}`+
		`button,.button{display:flex;align-items:center;justify-content:center;width:100%%;height:44px;margin-top:24px;border:0;border-radius:6px;background:var(--fill);color:var(--ink);font:inherit;font-size:15px;font-weight:500;text-decoration:none;cursor:pointer}`+
		`button:hover,.button:hover{background:var(--pill)}.primary,.primary:hover{background:var(--pri);color:var(--onpri)}.primary:hover{opacity:.9}:focus-visible{outline:2px solid var(--link);outline-offset:2px}`+
		`.why{margin-top:16px;color:var(--muted);font-size:12px;line-height:16px;text-align:center}@media(max-width:960px){header{padding-top:40px}main{padding-top:32px}}`+
		`</style></head><body><header><img src="%s" alt=""><span>Open Machine</span></header><main><h1>%s</h1><p>%s</p>%s</main></body></html>`, title, mark, title, text, action)
}

// theme is the colors of om's own pages, by the system's theme.
const theme = `:root{--bg:#fff;--ink:#1f1e1e;--ink2:#3b3938;--muted:#6b6867;--fill:#efedeb;--pill:#e2dfdd;--line:#e2dfdd;--pri:#1f1e1e;--onpri:#fff;--link:#2b59c3}` +
	`@media(prefers-color-scheme:dark){:root{--bg:#1f1e1e;--ink:#f7f5f4;--ink2:#dad6d5;--muted:#afacab;--fill:#2e2d2d;--pill:#444342;--line:#383737;--pri:#f7f5f4;--onpri:#1f1e1e;--link:#93b4f5}}`

// try serves om's chat page, at the root of 127.0.0.1 and the entry's port:
// the chat models the address reaches, the one ?model= names chosen, also when
// this computer does not list it, so that its answer says why. Each is drawn
// with its machine's name, so nothing moves once the page is open. Its script
// alone runs, and reaches only this address.
func (l *Local) try(w http.ResponseWriter, r *http.Request) {
	machines := map[string]string{} // a machine's name by its alias; this computer's also by ""
	for _, v := range l.o.Devices() {
		if v.Alias != nil {
			machines[*v.Alias] = v.Name
		}
		if v.DeviceID == l.device {
			machines[""] = v.Name
		}
	}
	list, asked, options, listed := l.reach(false), r.URL.Query().Get("model"), "", false
	for i, name := range named(list) {
		if !list[i].Chat {
			continue
		}
		chosen := ""
		if name == asked {
			chosen, listed = " selected", true
		}
		machine := machines[list[i].alias]
		if machine == "" {
			machine = list[i].alias
		}
		name = html.EscapeString(name)
		options += fmt.Sprintf(`<option value="%s" data-machine="%s"%s>%s</option>`, name, html.EscapeString(machine), chosen, name)
	}
	if asked = html.EscapeString(asked); asked != "" && !listed {
		options = fmt.Sprintf(`<option value="%s" selected>%s</option>`, asked, asked) + options
	}
	own(w.Header())
	w.Header().Set("Content-Security-Policy", tryPolicy)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	io.WriteString(w, tryHead+options+tryBody)
}

// The Try page, around its models: om's page look under the Console's brand
// header, and the script that streams an answer. It stores nothing.
const (
	tryHead = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>Try a model · Open Machine</title><style>` + theme +
		`*{box-sizing:border-box}html,body{height:100%}body{margin:0;display:flex;flex-direction:column;background:var(--bg);color:var(--ink);font:400 14px/20px Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:-.01em;-webkit-font-smoothing:antialiased;overflow-wrap:anywhere}` +
		`header{flex:none;display:flex;align-items:center;gap:12px;height:56px;padding:0 16px;border-bottom:1px solid var(--line)}.brand{display:flex;align-items:center;gap:10px;font-weight:600;white-space:nowrap}.brand img{flex:none;display:block;width:22px;height:22px;border-radius:6px;background:#090909}` +
		`.pick{position:relative;margin-left:auto;min-width:0}select{appearance:none;max-width:100%;height:32px;padding:0 32px 0 12px;border:1px solid var(--pill);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;text-overflow:ellipsis}` +
		`.pick::after{content:"";position:absolute;right:12px;top:12px;width:6px;height:6px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;transform:rotate(45deg);pointer-events:none}` +
		`main{flex:1;overflow-y:auto;padding:24px 16px}#log{max-width:720px;margin:0 auto;display:flex;flex-direction:column;gap:16px}` +
		`#hello{margin:12vh auto 0;max-width:400px;text-align:center}h1{margin:0 0 8px;font-size:24px;line-height:30px;font-weight:600;letter-spacing:-.02em}#hello p{margin:0;color:var(--ink2)}` +
		`.me{align-self:flex-end;max-width:80%;padding:8px 12px;border-radius:12px;background:var(--fill)}.it{max-width:100%;white-space:pre-wrap}.wait{color:var(--muted)}` +
		`.think summary{display:inline-flex;align-items:center;gap:8px;color:var(--muted);cursor:pointer;list-style:none;user-select:none}.think summary::-webkit-details-marker{display:none}` +
		`.think summary::after{content:"";width:5px;height:5px;border:solid currentColor;border-width:0 1.5px 1.5px 0;transform:translateY(-1px) rotate(-45deg);transition:transform .12s}.think[open] summary::after{transform:translateY(-2px) rotate(45deg)}` +
		`.think div{margin-top:8px;padding-left:12px;border-left:2px solid var(--line);color:var(--muted);white-space:pre-wrap}.cut{margin-top:-8px;color:var(--muted);font-size:12px;line-height:16px}` +
		`@media(prefers-reduced-motion:reduce){.think summary::after{transition:none}}` +
		`form{padding:12px 16px 16px;border-top:1px solid var(--line)}.box{max-width:720px;margin:0 auto;display:flex;gap:8px}` +
		`textarea{flex:1;min-height:44px;max-height:160px;padding:11px 12px;border:1px solid var(--pill);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit;resize:none}` +
		`button{flex:none;height:44px;padding:0 16px;border:0;border-radius:6px;background:var(--pri);color:var(--onpri);font:inherit;font-weight:500;cursor:pointer}button:disabled{opacity:.5;cursor:default}` +
		`.why{max-width:720px;margin:8px auto 0;color:var(--muted);font-size:12px;line-height:16px;text-align:center}:focus-visible{outline:2px solid var(--link);outline-offset:2px}` +
		`@media(max-width:520px){.brand span{display:none}}` +
		`</style></head><body><header><span class="brand"><img src="` + mark + `" width="22" height="22" alt=""><span>Open Machine</span></span><span class="pick"><select id="model" aria-label="Model">`
	tryBody = `</select></span></header><main><div id="log"><div id="hello"><h1>Chat with a model</h1><p id="where"></p></div></div></main>` +
		`<form id="form"><div class="box"><textarea id="ask" rows="1" placeholder="Ask anything" aria-label="Message"></textarea><button id="send">Send</button></div>` +
		`<p class="why">Runs on your machine. Nothing is kept after you close this page.</p></form><script>` + tryScript + `</script></body></html>`
	tryScript = `const pick = document.getElementById('model'), log = document.getElementById('log'), hello = document.getElementById('hello'), ask = document.getElementById('ask'), send = document.getElementById('send');
let said = [];
function where() {
  const o = pick.selectedOptions[0];
  document.getElementById('where').textContent = o && o.dataset.machine ? 'Ask ' + o.value + ' on ' + o.dataset.machine + '.' : '';
  send.disabled = !o;
}
pick.addEventListener('change', () => { history.replaceState(null, '', '?model=' + encodeURIComponent(pick.value)); log.querySelectorAll('.me,.it,.think,.cut').forEach(n => n.remove()); hello.hidden = false; said = []; where(); });
where();
// A server's error is shown as its text.
const told = (error, body) => (typeof error === 'string' ? error : error?.message) || body;
const say = (cls, text) => { const n = document.createElement('div'); n.className = cls; n.textContent = text; log.append(n); n.scrollIntoView({ block: 'end' }); return n; };
document.getElementById('form').addEventListener('submit', async event => {
  event.preventDefault();
  const text = ask.value.trim();
  if (!text || send.disabled) return;
  hello.hidden = true; ask.value = ''; send.disabled = pick.disabled = true;
  say('me', text);
  said.push({ role: 'user', content: text });
  const it = say('it wait', '…');
  // How long since the thinking began: 12s, 1m 12s.
  const took = () => { const s = Math.floor((Date.now() - start) / 1000); return (s < 60 ? '' : Math.floor(s / 60) + 'm ') + s % 60 + 's'; };
  let answer = '', think, start, tick, cut;
  // The model's thinking, while it streams before the answer: one line that counts the time and opens to show it.
  const thought = () => { clearInterval(tick); tick = 0; think.open = false; think.firstChild.textContent = 'Thought for ' + took(); };
  try {
    const res = await fetch('/v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: pick.value, messages: said, stream: true }) });
    if (!res.ok) {
      const body = await res.text();
      let error;
      try { error = JSON.parse(body).error; } catch {}
      throw new Error(told(error, body));
    }
    const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
    for (let rest = '';;) {
      const { value, done } = await reader.read();
      if (done) break;
      const lines = (rest + value).split('\n');
      rest = lines.pop();
      for (const line of lines) {
        const at = /^(data|error):/.exec(line);
        if (!at) continue;
        const text = line.slice(at[0].length);
        let piece;
        try { piece = JSON.parse(text); } catch { continue; }
        // One sent after the answer began, as the stream's data or its own line.
        if (at[1] === 'error' || piece.error) throw new Error(told(piece.error ?? piece, text));
        const choice = piece.choices?.[0], more = choice?.delta?.reasoning_content ?? choice?.delta?.reasoning;
        cut = choice?.finish_reason === 'length' || cut;
        if (more && !answer) {
          if (!think) {
            think = document.createElement('details'); think.className = 'think';
            think.append(document.createElement('summary'), document.createElement('div'));
            it.before(think); it.hidden = true; start = Date.now();
            const count = () => { think.firstChild.textContent = 'Thinking · ' + took(); };
            count(); tick = setInterval(count, 1000);
          }
          think.lastChild.textContent += more;
          if (think.open) think.scrollIntoView({ block: 'end' });
        }
        // Blank lines before the answer, as some servers send after thinking, are not shown.
        answer = (answer + (choice?.delta?.content ?? '')).replace(/^\s*\n/, '');
        if (!answer.trim()) continue;
        if (tick) thought();
        it.hidden = false; it.className = 'it'; it.textContent = answer; it.scrollIntoView({ block: 'end' });
      }
    }
    // A turn without an answer is not kept, as a failed one.
    answer ? said.push({ role: 'assistant', content: answer }) : said.pop();
  } catch (error) {
    said.pop(); cut = false;
    answer += (answer && '\n\n') + error.message;
  }
  if (tick) thought();
  it.className = 'it'; it.textContent = answer; it.hidden = !answer;
  if (cut) say('cut', answer ? 'The answer was cut off here: it reached the model’s length limit.' : 'The model reached its length limit before it could answer.');
  send.disabled = pick.disabled = false; ask.focus();
});
ask.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); document.getElementById('form').requestSubmit(); } });
`
)

// tryPolicy lets the Try page run its own script alone, which reaches only this address.
var tryPolicy = func() string {
	sum := sha256.Sum256([]byte(tryScript))
	return "default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src 'sha256-" + base64.StdEncoding.EncodeToString(sum[:]) + "'; connect-src 'self'; frame-ancestors 'none'"
}()

// mark is the brand mark of the Console's shell.
const mark = "data:image/svg+xml;base64,PHN2ZyBwcmVzZXJ2ZUFzcGVjdFJhdGlvPSJub25lIiBvdmVyZmxvdz0idmlzaWJsZSIgc3R5bGU9ImRpc3BsYXk6IGJsb2NrOyIgd2lkdGg9IjI5IiBoZWlnaHQ9IjI5IiB2aWV3Qm94PSIwIDAgMjkgMjkiIGZpbGw9Im5vbmUiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+CjxnIGlkPSJPZmZpY2lhbCBPTSAmIzE5NDsmIzE4MzsgdmVjdG9yIHRyYWNlZCBmcm9tIHN1cHBsaWVkIGxvZ28iPgo8cGF0aCBpZD0iVmVjdG9yIiBkPSJNNS44NTA4OCAxMy4yNTEyTDUuNjY1ODcgMTQuMjkxOUw1LjY4OSAxNS4wNTVMNS44NzQgMTUuOTMzOEw2LjI5MDI3IDE2Ljg4Mkw2Ljc1Mjc5IDE3LjU1MjZMNy4zNzcxOSAxOC4xNzdMOC4wMjQ3MiAxOC42MTY0TDguOTQ5NzYgMTkuMDA5Nkw5Ljc4MjMgMTkuMTcxNUgxMC42MzhMMTEuMzA4NiAxOS4wNTU4TDEyLjE2NDMgMTguNzU1MkwxMy40MzYyIDE3LjkyMjZMMTQuMTc2MiAxNy4xNTk1TDE1LjEwMTMgMTUuOTMzOEwxNS45MTA3IDE2LjUzNTFMMTYuNDg4OCAxNi44MTI2TDE3LjE1OTUgMTYuOTA1MUwxNy44NTMzIDE2Ljc2NjNMMTguMzM4OSAxNi40ODg4TDE4Ljg3MDggMTUuOTgwMUwyMC4xNDI3IDE0LjM2MTJMMjAuNjA1MyAxMy45NjgxSDIwLjc5MDNMMjAuOTUyMiAxNC4xOTk0VjE4LjEwNzdMMjEuMDY3OCAxOC41MjM5TDIxLjI3NTkgMTguODAxNEwyMS42NDU5IDE5LjAzMjdMMjIuMjkzNSAxOS4xMDIxTDIyLjkxNzkgMTguODAxNEwyMy4xNDkxIDE4LjM4NTJMMjMuMTI2IDEyLjg4MTJMMjIuNzMyOSAxMi4wMjU1TDIyLjI0NzIgMTEuNjA5M0wyMS44MDc4IDExLjQyNDJMMjAuOTI5IDExLjM3OEwyMC4wNzM0IDExLjY3ODZMMTkuNjM0IDEyLjAwMjRMMTcuNDgzMyAxNC4xNTMxTDE3LjA5MDEgMTQuMzYxMkwxMy44MjkzIDExLjEwMDVMMTIuOTk2OCAxMC40Mjk4TDEyLjQ2NDkgMTAuMTUyM0wxMS42Nzg2IDkuODk3OTNMMTEuMDMxMSA5LjgwNTQyTDEwLjAzNjcgOS44Mjg1NUw4LjgxMSAxMC4xMjkyTDcuOTc4NDcgMTAuNTQ1NUw3LjQyMzQ0IDEwLjkzODZMNi41NDQ2NiAxMS44NjM2TDYuMTk3NzcgMTIuNDE4N0w1Ljg1MDg4IDEzLjI1MTJaIiBmaWxsPSJ1cmwoI3BhaW50MF9saW5lYXJfMF8xMSkiLz4KPHBhdGggaWQ9IlZlY3Rvcl8yIiBkPSJNMTAuMDM2NyAxMi4zNDkzSDEwLjkxNTVMMTEuNjA5MyAxMi41NTc0TDEyLjM3MjQgMTMuMDQzMUwxMi45NTA2IDEzLjY0NDNMMTIuOTI3NCAxNC41Njk0TDEyLjUxMTIgMTUuNTYzOEwxMS44MTc0IDE2LjI1NzZMMTEuMDA4IDE2LjYyNzZMMTAuMTI5MiAxNi42NzM4TDkuMzY2MDMgMTYuNDE5NUw4LjY3MjI1IDE1LjgxODJMOC4zMDIyMyAxNS4wMzE5TDguMjU1OTggMTQuMzM4MUw4LjU1NjYyIDEzLjQ1OTNMOS4xMTE2NCAxMi44MTE4TDkuNjg5NzkgMTIuNDQxOEwxMC4wMzY3IDEyLjM0OTNaIiBmaWxsPSIjMDUwNTA1Ii8+CjwvZz4KPGRlZnM+CjxsaW5lYXJHcmFkaWVudCBpZD0icGFpbnQwX2xpbmVhcl8wXzExIiB4MT0iNS42NjU4NyIgeTE9IjkuODA1NDIiIHgyPSIxMi4yOTc3IiB5Mj0iMjMuNTYwNCIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPgo8c3RvcCBzdG9wLWNvbG9yPSJ3aGl0ZSIvPgo8c3RvcCBvZmZzZXQ9IjAuNTUiIHN0b3AtY29sb3I9IiNGQUZBRkEiLz4KPHN0b3Agb2Zmc2V0PSIxIiBzdG9wLWNvbG9yPSIjQkNCRUMyIi8+CjwvbGluZWFyR3JhZGllbnQ+CjwvZGVmcz4KPC9zdmc+Cg=="
