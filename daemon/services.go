package daemon

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"html"
	"io"
	"log"
	"maps"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

// Service is one program listening on this machine, as renewals report it: a
// model server with its models, which may be none.
type Service struct {
	Port   int            `json:"port"`
	Name   string         `json:"name"`
	Web    bool           `json:"web"`
	Models []Model        `json:"models,omitzero"`
	Addr   netip.AddrPort `json:"-"` // where it listens: what a request for one of its models dials
}

// Model is an AI model a model server lists, and whether it can chat.
type Model struct {
	ID   string `json:"id"`
	Chat bool   `json:"chat"`
}

// Listener is a TCP socket the OS lists in LISTEN state, with the process
// holding it: PID 0 and no name when that is unreadable.
type Listener struct {
	Addr netip.AddrPort
	PID  int
	Name string
	Exe  string // its executable, read on macOS only
}

const (
	discoverEvery = 10 * time.Second
	maxServices   = 64
	maxProbes     = 8 // a pass; the rest wait for the next
	maxNameRunes  = 40
	maxIDRunes    = 128
	maxModels     = 200      // a machine
	maxModelBytes = 32 << 10 // of their JSON, so a renewal fits its frame whatever a program answers
)

// local is the addresses that localhost reaches a socket bound to.
var local = []netip.Addr{netip.MustParseAddr("127.0.0.1"), netip.MustParseAddr("::1"), netip.IPv4Unspecified(),
	netip.IPv6Unspecified(), netip.MustParseAddr("::ffff:127.0.0.1")}

// Listed returns the listeners a pass lists, one per port by its lowest PID,
// the lowest 64 ports: those bound where localhost reaches them, other than
// om's own, Apple's programs, ports below 1024 but 80 and 443, and the OS's
// ephemeral range from first to last, where editors' and kernels' random
// ports live.
func Listed(ls []Listener, first, last int) []Listener {
	byPort := map[uint16]Listener{}
	for _, l := range ls {
		p := l.Addr.Port()
		if !slices.Contains(local, l.Addr.Addr()) || l.PID == os.Getpid() || apple(l.Exe) ||
			p < 1024 && p != 80 && p != 443 || int(p) >= first && int(p) <= last {
			continue
		}
		// A known process ranks before an unknown one, PID 0.
		if held, ok := byPort[p]; !ok || held.PID == 0 || l.PID != 0 && l.PID < held.PID {
			byPort[p] = l
		}
	}
	listed := slices.SortedFunc(maps.Values(byPort), func(a, b Listener) int { return int(a.Addr.Port()) - int(b.Addr.Port()) })
	return listed[:min(len(listed), maxServices)]
}

// apple reports whether exe is one of Apple's programs, which macOS keeps
// under these directories.
func apple(exe string) bool {
	for _, dir := range []string{"/System/", "/usr/", "/bin/", "/sbin/", "/Library/Apple/"} {
		if strings.HasPrefix(exe, dir) && !strings.HasPrefix(exe, "/usr/local/") {
			return true
		}
	}
	return false
}

// ProcNet reads the sockets in LISTEN state from Linux's /proc/net/tcp or
// tcp6, by inode.
func ProcNet(b []byte) map[string]netip.AddrPort {
	sockets := map[string]netip.AddrPort{}
	for _, line := range strings.Split(string(b), "\n")[1:] {
		f := strings.Fields(line)
		if len(f) < 10 || f[3] != "0A" {
			continue
		}
		if addr, ok := procAddr(f[1]); ok {
			sockets[f[9]] = addr
		}
	}
	return sockets
}

// procAddr reads an address of /proc/net/tcp or tcp6: 32-bit words in the
// kernel's byte order, then the port.
func procAddr(s string) (netip.AddrPort, bool) {
	host, port, _ := strings.Cut(s, ":")
	raw, err := hex.DecodeString(host)
	p, perr := strconv.ParseUint(port, 16, 16)
	if err != nil || perr != nil || len(raw) != 4 && len(raw) != 16 {
		return netip.AddrPort{}, false
	}
	for i := 0; i < len(raw); i += 4 {
		binary.NativeEndian.PutUint32(raw[i:], binary.BigEndian.Uint32(raw[i:]))
	}
	addr, _ := netip.AddrFromSlice(raw)
	return netip.AddrPortFrom(addr, uint16(p)), true
}

// Lsof reads `lsof -F pcn`'s output: a p line starts each process, c names
// it and n is each socket's address, * for any.
func Lsof(b []byte) []Listener {
	var ls []Listener
	var pid int
	var name string
	for _, line := range strings.Split(string(b), "\n") {
		if line == "" {
			continue
		}
		switch v := line[1:]; line[0] {
		case 'p':
			pid, _ = strconv.Atoi(v)
			name = ""
		case 'c':
			name = v
		case 'n':
			i := strings.LastIndexByte(v, ':')
			p, err := strconv.ParseUint(v[i+1:], 10, 16)
			host := strings.Trim(v[:max(i, 0)], "[]")
			addr, aerr := netip.ParseAddr(host)
			if host == "*" {
				addr, aerr = netip.IPv4Unspecified(), nil
			}
			if i > 0 && err == nil && aerr == nil {
				ls = append(ls, Listener{Addr: netip.AddrPortFrom(addr, uint16(p)), PID: pid, Name: name})
			}
		}
	}
	return ls
}

// Discovery remembers each port and process it probed, so each is probed
// once, or up to 3 times in a row of passes while it answers late or with a
// 5xx, and reads each model server's list again every pass, as it asks one
// that is not ready yet.
type Discovery struct {
	known map[[2]int]probed
}

// probed is a probe's service, with every model its server lists, the probes
// left after it, 0 once settled, whether its models are still to come, and
// whether each of Ollama's models, by digest, can chat.
type probed struct {
	Service
	left  int
	open  bool // no answer yet says whether it is a model server
	kinds map[string]bool
}

// ask reads the models of p at l while it is a model server, keeping its
// last list when that fails, or has not said yet whether it is one.
func (p *probed) ask(l Listener) {
	if !p.open && p.Models == nil {
		return
	}
	models, again := readModels(l, p.open, p.kinds)
	if p.open = p.open && again; models != nil {
		p.Models, p.open = models, false
	}
}

// Pass returns the services of ls, in its order, probing at most 8 new or
// unsettled ports and processes at once; the others wait for the next pass.
// Beside them it asks each model server it knows, and each program whose
// models are still to come, again. Of the models it returns the first 200
// whose JSON fits 32 KiB, each id once.
func (d *Discovery) Pass(ls []Listener) []Service {
	known := map[[2]int]probed{}
	var probing, reading []Listener
	for _, l := range ls {
		key := [2]int{int(l.Addr.Port()), l.PID}
		if p, ok := d.known[key]; ok && (p.left == 0 || len(probing) == maxProbes) {
			if known[key] = p; p.Models != nil || p.open {
				reading = append(reading, l)
			}
		} else if len(probing) < maxProbes {
			probing = append(probing, l)
		}
	}
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, l := range probing {
		wg.Go(func() {
			key := [2]int{int(l.Addr.Port()), l.PID}
			p, ok := d.known[key]
			if !ok {
				p = probed{left: 3, open: true, kinds: map[string]bool{}}
			}
			web, title, again := Probe(l.Addr)
			if p.left--; !again {
				p.left = 0
			}
			name := Clean(title)
			if name == "" {
				name = Clean(l.Name)
			}
			p.ask(l)
			p.Service = Service{key[0], name, web, p.Models, l.Addr}
			mu.Lock()
			known[key] = p
			mu.Unlock()
		})
	}
	for _, l := range reading {
		wg.Go(func() {
			key := [2]int{int(l.Addr.Port()), l.PID}
			p := d.known[key]
			p.ask(l)
			mu.Lock()
			known[key] = p
			mu.Unlock()
		})
	}
	wg.Wait()
	d.known = known
	list := []Service{}
	listed, size, full := map[string]bool{}, len("[]"), false
	for _, l := range ls {
		p, ok := known[[2]int{int(l.Addr.Port()), l.PID}]
		if !ok {
			continue
		}
		s := p.Service
		if s.Models != nil {
			s.Models = []Model{}
		}
		for _, m := range p.Models {
			if full || listed[m.ID] {
				continue
			}
			b, _ := json.Marshal(m)
			if full = len(listed) == maxModels || size+len(b) > maxModelBytes; !full {
				listed[m.ID], size = true, size+len(b)+1
				s.Models = append(s.Models, m)
			}
		}
		list = append(list, s)
	}
	return list
}

// readModels reads the models of the model server at l: one answers GET
// /v1/models, or Ollama, by its process's name, GET
// /api/tags, with 200 and a JSON list whose items each have a string id. It
// asks without a Host line, and once more with one after a 403, which
// llama.cpp gives without it once its tools are on. It returns nil for any
// other answer, and whether that one is still to come: late, a 5xx, or shut. An
// id is kept without control and format characters, unless it is then empty
// or longer than 128 code points. A model can chat unless its server says otherwise;
// Ollama says so for a local model at POST /api/show, asked once per digest
// and kept in kinds, and not at all once an answer is late.
func readModels(l Listener, first bool, kinds map[string]bool) (models []Model, again bool) {
	asking := true
	to, port := loopbacks(l.Addr), int(l.Addr.Port())
	ollama := l.Name == "ollama"
	path := "/v1/models"
	if ollama {
		path = "/api/tags"
	}
	status, body, _ := get(to, port, path, "", first, true, "")
	if status == http.StatusForbidden {
		status, body, _ = get(to, port, path, "localhost:"+strconv.Itoa(port), false, true, "")
	}
	if status != http.StatusOK {
		return nil, status == late || status == shut || status/100 == 5
	}
	type item struct {
		ID, Name     any    // a model's id; Ollama's
		Digest       string // Ollama's
		RemoteHost   string `json:"remote_host"` // of an Ollama cloud model
		Task         any    // Speaches'
		ModelType    any    `json:"model_type"` // Xinference's
		Architecture struct {
			OutputModalities []string `json:"output_modalities"` // llama.cpp's
		}
	}
	var list struct {
		Object       string
		Data, Models []item
	}
	// A field of another type than its server's is no kind: only the id decides.
	if err := json.Unmarshal(body, &list); err != nil && !errors.As(err, new(*json.UnmarshalTypeError)) {
		return nil, false
	}
	items := list.Models
	if !ollama {
		if items = list.Data; list.Object != "list" {
			return nil, false
		}
	}
	if items == nil {
		return nil, false
	}
	models = []Model{}
	for _, m := range items {
		name := m.ID
		if ollama {
			name = m.Name
		}
		raw, ok := name.(string)
		if !ok {
			return nil, false
		}
		id := strings.Map(func(r rune) rune {
			if unicode.In(r, unicode.Cc, unicode.Cf) {
				return -1
			}
			return r
		}, raw)
		if n := utf8.RuneCountInString(id); n < 1 || n > maxIDRunes {
			continue
		}
		chat := !(m.Task != nil && m.Task != "text-generation" || m.ModelType != nil && m.ModelType != "LLM" ||
			m.Architecture.OutputModalities != nil && !slices.Contains(m.Architecture.OutputModalities, "text"))
		if ollama && m.RemoteHost == "" {
			can, known := kinds[m.Digest]
			if !known && asking {
				ask, _ := json.Marshal(map[string]string{"model": raw})
				status, body, _ := get(to, port, "/api/show", "", false, true, string(ask))
				var shown struct{ Capabilities []string }
				if asking = status != late; asking { // an answer that names no capability stands too
					json.Unmarshal(body, &shown)
					can = len(shown.Capabilities) == 0 || slices.Contains(shown.Capabilities, "completion")
					kinds[m.Digest], known = can, true
				}
			}
			chat = !known || can
		}
		models = append(models, Model{id, chat})
	}
	return models, false
}

var titleTag = regexp.MustCompile(`(?is)<title[^>]*>(.*?)</title`)

// Probe tells a web page from another program listening at addr. A program
// that speaks first is not one. Otherwise it sends
// GET / without a Host line, which Redis closes on, and once more with one
// after a 403, which Vite 5 and 6 and webpack-dev-server give without it. 3xx,
// or HTML with 2xx, 401 or 403, is a page; a page's title comes from a 2xx
// answer, after at most 2 redirects to this port's address. again reports
// an answer late or with a 5xx, also a redirect's, as from a program
// still starting. It dials
// the loopback addresses that reach addr, never a name, which a resolver
// could send elsewhere: both for any address, as a dual-stack socket may be
// reachable on either.
func Probe(addr netip.AddrPort) (web bool, title string, again bool) {
	to, port := loopbacks(addr), int(addr.Port())
	target, host := &url.URL{Path: "/"}, ""
	status, page, location := get(to, port, "/", host, true, false, "")
	if status == http.StatusForbidden {
		host = "localhost:" + strconv.Itoa(port)
		status, page, location = get(to, port, "/", host, false, false, "")
	}
	switch {
	case status == late || status/100 == 5:
		return false, "", true
	case status/100 != 3 && (page == nil || status/100 != 2 && status != http.StatusUnauthorized && status != http.StatusForbidden):
		return false, "", false // a program is named by its process
	}
	for redirects := 0; status/100 == 3; redirects++ {
		next, err := target.Parse(location)
		if redirects == 2 || err != nil || location == "" || !here(next, port) { // a redirect without a Location is not followed
			return true, "", false
		}
		target = next
		status, page, location = get(to, port, target.RequestURI(), host, false, false, "")
	}
	if status == late || status/100 == 5 {
		return true, "", true // a redirect's page still building
	}
	if status/100 != 2 {
		return true, "", false // as behind a sign-in: named by its process
	}
	return true, string(page), false
}

// loopbacks returns the loopback addresses that reach a socket bound to addr.
func loopbacks(addr netip.AddrPort) []netip.Addr {
	v4, v6 := netip.AddrFrom4([4]byte{127, 0, 0, 1}), netip.IPv6Loopback()
	switch ip := addr.Addr().Unmap(); ip {
	case netip.IPv4Unspecified():
		return []netip.Addr{v4, v6}
	case netip.IPv6Unspecified():
		return []netip.Addr{v6, v4}
	default:
		return []netip.Addr{ip}
	}
}

// dial connects to port at the first of to that answers within the time: the
// addresses loopbacks gave for a program, and so that program, where a name
// may lead to another one at the same port.
func dial(to []netip.Addr, port int, within time.Duration) (conn net.Conn, err error) {
	err = errors.New("no address")
	for deadline := time.Now().Add(within); err != nil && len(to) > 0 && time.Now().Before(deadline); to = to[1:] {
		conn, err = net.DialTimeout("tcp", netip.AddrPortFrom(to[0], uint16(port)).String(), time.Until(deadline))
	}
	return conn, err
}

// here reports whether u is at port's address: a path, or http at
// localhost, 127.0.0.1 or [::1] with that port, or none for port 80.
func here(u *url.URL, port int) bool {
	if u.Scheme == "" && u.Host == "" {
		return true
	}
	host := strings.ToLower(u.Host)
	for _, h := range []string{"localhost", "127.0.0.1", "[::1]"} {
		if u.Scheme == "http" && u.User == nil && (host == h+":"+strconv.Itoa(port) || port == 80 && host == h) {
			return true
		}
	}
	return false
}

// get's statuses for no answer within its 2 s, and for a port that closed
// before it was asked: Docker's, until the server in its container listens.
const late, shut = -1, -2

// get requests path from port at the first address of to that connects
// within a second, with host as its Host line unless empty: its status, late,
// shut or 0 for none, the title of an HTML page (nil for another answer; empty
// without a title) and its Location. The first request of a probe waits for
// a program that speaks first, which is no page: status 0. With asJSON it asks
// for JSON, posting post as one unless empty, and returns the body, up to 1
// MiB, in place of a title.
func get(to []netip.Addr, port int, path, host string, first, asJSON bool, post string) (status int, page []byte, location string) {
	conn, err := dial(to, port, time.Second)
	if err != nil {
		return 0, nil, ""
	}
	defer conn.Close()
	if first {
		conn.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
		if n, err := conn.Read(make([]byte, 1)); n > 0 {
			return 0, nil, ""
		} else if !errors.Is(err, os.ErrDeadlineExceeded) {
			return shut, nil, ""
		}
	}
	conn.SetDeadline(time.Now().Add(2 * time.Second))
	if host != "" {
		host = "Host: " + host + "\r\n"
	}
	method, accept, limit := "GET", "text/html", int64(64<<10)
	if asJSON {
		accept, limit = "application/json", 1<<20
	}
	if post != "" {
		method, host = "POST", host+"Content-Type: application/json\r\nContent-Length: "+strconv.Itoa(len(post))+"\r\n"
	}
	if _, err := io.WriteString(conn, method+" "+path+" HTTP/1.0\r\n"+host+"Accept: "+accept+"\r\nUser-Agent: OpenMachine\r\n\r\n"+post); err != nil {
		return 0, nil, ""
	}
	r := bufio.NewReader(io.LimitReader(conn, limit))
	res, err := http.ReadResponse(r, nil)
	if errors.Is(err, os.ErrDeadlineExceeded) {
		return late, nil, ""
	} else if err != nil {
		return 0, nil, ""
	}
	defer res.Body.Close()
	media, _, _ := strings.Cut(res.Header.Get("Content-Type"), ";")
	if !asJSON && !strings.EqualFold(strings.TrimSpace(media), "text/html") {
		return res.StatusCode, nil, res.Header.Get("Location")
	}
	body, err := io.ReadAll(res.Body) // what arrived within the limits
	if asJSON && errors.Is(err, os.ErrDeadlineExceeded) {
		return late, nil, "" // half a list is none yet
	} else if asJSON {
		return res.StatusCode, body, ""
	}
	page = []byte{}
	if m := titleTag.FindSubmatch(body); m != nil {
		page = []byte(html.UnescapeString(string(m[1])))
	}
	return res.StatusCode, page, res.Header.Get("Location")
}

// Clean makes a name from a machine's program safe to show: white space
// collapsed, control and format characters removed, the ends trimmed and at
// most 40 code points kept.
func Clean(s string) string {
	var b strings.Builder
	space := false
	for _, r := range s {
		switch {
		case unicode.IsSpace(r):
			space = true
		case unicode.In(r, unicode.Cc, unicode.Cf):
		default:
			if space && b.Len() > 0 {
				b.WriteByte(' ')
			}
			space = false
			b.WriteRune(r)
		}
	}
	runes := []rune(b.String())
	return strings.TrimSpace(string(runes[:min(len(runes), maxNameRunes)]))
}

// discover reports this machine's services to Cloud, and holds them for its
// models' requests: a pass at once and then every 10 seconds. A pass whose list the OS does not give is skipped, and
// logged once until it fails otherwise.
func (d *daemon) discover(ctx context.Context) {
	var found Discovery
	var failed string
	for {
		ls, first, last, err := listening(ctx)
		if err != nil && err.Error() != failed {
			log.Printf("native: listing listening programs: %v", err)
		} else if err == nil {
			list := found.Pass(Listed(ls, first, last))
			d.services.Store(&list)
			d.authority.Services(list)
		}
		if failed = ""; err != nil {
			failed = err.Error()
		}
		if sleep(ctx, discoverEvery); ctx.Err() != nil {
			return
		}
	}
}
