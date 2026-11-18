package daemon

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/OpenMachineLabs/openmachine/connector"
	"golang.org/x/crypto/ssh"
)

const (
	admitWait        = 5 * time.Second // how long a new admission waits for current authority
	handshakeTimeout = 20 * time.Second
	maxConns         = 64 // SSH connections per daemon
	maxSourceConns   = 16 // of them from one source
	maxChannels      = 32 // open sessions and forwards per connection
	drainTimeout     = time.Second
	// A peer that answers no keepalive for three intervals is gone, as after
	// power or path loss; Cloud plays no part, so an outage never trips it.
	keepalive = 15 * time.Second
	// The channel of one request to this machine's model servers.
	modelsChannel = "models@openmachine.app"
	maxModelBody  = 32 << 20
	onlyModels    = "om: only AI models on this machine" // to a source limited to them
)

// inference is the paths a model server is asked at.
var inference = []string{"/v1/chat/completions", "/v1/completions", "/v1/responses", "/v1/messages", "/v1/messages/count_tokens",
	"/v1/embeddings", "/v1/audio/speech"}

var (
	termName = regexp.MustCompile(`^[A-Za-z0-9._+-]{1,64}$`)
	envName  = regexp.MustCompile(`^(LANG|LC_[A-Z]{1,32})$`)
	envValue = regexp.MustCompile(`^[A-Za-z0-9._@-]{0,128}$`)
	// signals are those a client may send, the ones Core's launcher forwards to the command.
	signals = map[string]syscall.Signal{"ABRT": syscall.SIGABRT, "ALRM": syscall.SIGALRM, "HUP": syscall.SIGHUP,
		"INT": syscall.SIGINT, "QUIT": syscall.SIGQUIT, "TERM": syscall.SIGTERM, "USR1": syscall.SIGUSR1, "USR2": syscall.SIGUSR2}
	// exits names the signals RFC 4254 reports by name.
	exits = map[syscall.Signal]string{syscall.SIGABRT: "ABRT", syscall.SIGALRM: "ALRM", syscall.SIGFPE: "FPE",
		syscall.SIGHUP: "HUP", syscall.SIGILL: "ILL", syscall.SIGINT: "INT", syscall.SIGKILL: "KILL", syscall.SIGPIPE: "PIPE",
		syscall.SIGQUIT: "QUIT", syscall.SIGSEGV: "SEGV", syscall.SIGTERM: "TERM", syscall.SIGUSR1: "USR1", syscall.SIGUSR2: "USR2"}
)

// admission is the authority one new SSH transport, channel or stream is admitted under.
type admission struct {
	lease   *Lease
	source  Source
	binding binding
	policy  *policy // of its sessions
}

// refused is an error with the reason the log gives its refusal: user,
// unavailable, changed or busy. Any other error's is error.
type refused struct {
	error
	reason string
}

func reasonOf(err error) string {
	if r := (refused{}); errors.As(err, &r) {
		return r.reason
	}
	return "error"
}

// admit checks a new admission from the peer holding nodeKey, locally:
// Desktop's intent and the Connector's state, then a current lease from a live
// Cloud session that lists the peer.
func (d *daemon) admit(nodeKey string) (*admission, error) {
	l, err := d.lease("the target")
	if err != nil {
		return nil, refused{err, "unavailable"}
	}
	for _, s := range l.Sources {
		if s.NodeKey == nodeKey {
			b, p, err := bindingOf(l, s)
			if err != nil {
				return nil, refused{err, "unavailable"}
			}
			return &admission{l, s, b, p}, nil
		}
	}
	return nil, refused{errors.New("the connecting computer may not reach the target"), "changed"}
}

// conn is one admitted SSH transport from a verified network peer.
type conn struct {
	d       *daemon
	net     *connector.Conn
	ssh     *ssh.ServerConn
	address string // SSH_CONNECTION for its sessions
	binding binding
	visit   *Visit // its connection in the log; nil when it is not recorded
	slots   chan struct{}
	done    chan struct{} // closed when the connection ends
}

// buffered is a connection whose first bytes were read ahead.
type buffered struct {
	net.Conn
	r *bufio.Reader
}

func (b buffered) Read(p []byte) (int, error) { return b.r.Read(p) }

// serve runs OM SSH on the private network's Code port; there is no OS port.
func (d *daemon) serve(ln net.Listener) {
	var mu sync.Mutex
	open, total := map[string]int{}, 0 // connections per source node key, and in all
	for {
		accepted, err := ln.Accept()
		if errors.Is(err, net.ErrClosed) {
			return
		} else if err != nil {
			time.Sleep(100 * time.Millisecond) // such as too many open files: keep serving
			continue
		}
		nc := accepted.(*connector.Conn)
		key := nc.Peer().NodeKey
		mu.Lock()
		admit := total < maxConns && open[key] < maxSourceConns
		if admit {
			open[key]++
			total++
		}
		mu.Unlock()
		if !admit {
			nc.Close()
			continue
		}
		go func() {
			defer func() {
				mu.Lock()
				if total--; open[key] == 1 {
					delete(open, key)
				} else {
					open[key]--
				}
				mu.Unlock()
			}()
			d.handle(nc)
		}()
	}
}

// sshConnection is what sshd puts in SSH_CONNECTION:
// the client's address and port and the server's, here on the private network,
// whose addresses are literal and have no zone.
func sshConnection(c net.Conn) (string, error) {
	var ends [2]netip.AddrPort
	for i, a := range []net.Addr{c.RemoteAddr(), c.LocalAddr()} {
		end, err := netip.ParseAddrPort(a.String())
		if err != nil {
			return "", err
		}
		ends[i] = end
	}
	return fmt.Sprintf("%s %d %s %d", ends[0].Addr().Unmap().WithZone(""), ends[0].Port(),
		ends[1].Addr().Unmap().WithZone(""), ends[1].Port()), nil
}

// handle runs one connection. SSH "none" authentication is accepted only for
// the daemon's own OS user, after the transport's admission. The log names
// the transport's source as the lease applied last does, current or not, and
// its app as the line before SSH does; a transport from no source of that
// lease is not recorded.
func (d *daemon) handle(nc *connector.Conn) {
	defer nc.Close()
	address, err := sshConnection(nc)
	if err != nil {
		return
	}
	nc.SetDeadline(time.Now().Add(handshakeTimeout))
	stream := buffered{nc, bufio.NewReaderSize(nc, 64)}
	app, source := ReadApp(stream.r), ""
	d.mu.Lock()
	if d.applied != nil {
		if i := slices.IndexFunc(d.applied.Sources, func(s Source) bool { return s.NodeKey == nc.Peer().NodeKey }); i >= 0 {
			source = d.applied.Sources[i].DeviceID
		}
	}
	d.mu.Unlock()
	var adm *admission
	c := &conn{d: d, net: nc, address: address, slots: make(chan struct{}, maxChannels), done: make(chan struct{})}
	cfg := &ssh.ServerConfig{
		NoClientAuth:  true,
		ServerVersion: "SSH-2.0-OpenMachine",
		// Each attempt is checked on its own, since a client may change user between attempts.
		NoClientAuthCallback: func(m ssh.ConnMetadata) (*ssh.Permissions, error) {
			var err error = refused{fmt.Errorf("log in as %s; other users are refused", d.user), "user"}
			if m.User() == d.user {
				if adm, err = d.admit(nc.Peer().NodeKey); err == nil {
					c.binding = adm.binding
					return nil, nil
				}
			}
			d.log.Refuse(source, app, reasonOf(err), err.Error())
			return nil, &ssh.BannerError{Err: err, Message: "om: " + err.Error() + "\r\n"}
		},
	}
	cfg.AddHostKey(d.signer)
	sc, chans, reqs, err := ssh.NewServerConn(stream, cfg)
	if err != nil {
		return
	}
	nc.SetDeadline(time.Time{})
	c.ssh = sc
	err = d.register(c, adm, app)
	defer c.visit.Leave()
	if err != nil {
		if c.visit == nil {
			d.log.Refuse(adm.source.DeviceID, app, reasonOf(err), err.Error())
		}
		c.visit.Refuse("transport", reasonOf(err), err.Error())
		sc.Close()
		return
	}
	defer d.unregister(c)
	defer close(c.done)
	go c.keepalive()
	// Remote forwarding, streamlocal and every other global request are refused.
	go ssh.DiscardRequests(reqs)
	for nch := range chans {
		switch nch.ChannelType() {
		case "session":
			go c.session(nch)
		case "direct-tcpip":
			go c.forward(nch)
		case modelsChannel:
			go c.models(nch)
		default:
			c.reject(nch, ssh.UnknownChannelType, errors.New(nch.ChannelType()+" channels are not supported"))
		}
	}
	sc.Close()
}

// keepalive asks the peer every interval and closes c once a request goes
// unanswered for three.
func (c *conn) keepalive() {
	for {
		select {
		case <-c.done:
			return
		case <-time.After(keepalive):
		}
		answered := make(chan error, 1)
		go func() { _, _, err := c.ssh.SendRequest("keepalive@openssh.com", true, nil); answered <- err }()
		select {
		case <-c.done:
			return
		case err := <-answered:
			if err != nil {
				return
			}
		case <-time.After(3 * keepalive):
			c.ssh.Close()
			return
		}
	}
}

// register admits c's transport, which adm admitted from app, and enters it
// in the log under the same lock that a lease ending its connection takes.
func (d *daemon) register(c *conn, adm *admission, app string) error {
	d.mu.Lock()
	d.conns[c] = struct{}{} // admissible also requires c registered
	err := d.admissible(c)
	if err == nil {
		c.visit = d.log.Enter(adm.lease, adm.source, app)
	}
	d.mu.Unlock()
	if err == nil {
		if err = d.id.Admission(); err != nil { // registered first, so going offline is seen here or by watch
			err = refused{err, "unavailable"}
		}
	}
	if err != nil {
		d.unregister(c)
	}
	return err
}

func (d *daemon) unregister(c *conn) {
	d.mu.Lock()
	delete(d.conns, c)
	d.mu.Unlock()
}

// admitChannel admits new work on c, which needs current authority unchanged
// since c was admitted that still holds under the lease applied last: a
// cached transport is never a grant.
func (c *conn) admitChannel() (*admission, error) {
	adm, err := c.d.admit(c.net.Peer().NodeKey)
	if err != nil {
		return nil, err
	}
	if adm.binding != c.binding {
		return nil, refused{errors.New("Code authority changed; reconnect"), "changed"}
	}
	c.d.mu.Lock()
	defer c.d.mu.Unlock()
	if err := c.d.admissible(c); err != nil {
		return nil, err
	}
	return adm, nil
}

// reject refuses a channel for err, and records it.
func (c *conn) reject(nch ssh.NewChannel, code ssh.RejectionReason, err error) {
	c.visit.Refuse("channel", reasonOf(err), err.Error())
	nch.Reject(code, "om: "+err.Error())
}

// open admits a new channel on c, or rejects it with the reason: a free slot
// and admitChannel. It returns the admission and what frees the slot, or nil
// when it rejected.
func (c *conn) open(nch ssh.NewChannel) (*admission, func()) {
	select {
	case c.slots <- struct{}{}:
	default:
		c.reject(nch, ssh.ResourceShortage, refused{errors.New("too many channels on this connection"), "busy"})
		return nil, nil
	}
	adm, err := c.admitChannel()
	if err != nil {
		<-c.slots
		c.reject(nch, ssh.Prohibited, err)
		return nil, nil
	}
	return adm, func() { <-c.slots }
}

// forward is one -L stream to a TCP destination the target's OS network
// reaches, never the private network. Each stream is a new admission.
func (c *conn) forward(nch ssh.NewChannel) {
	var p struct {
		Host       string
		Port       uint32
		OriginHost string
		OriginPort uint32
	}
	if ssh.Unmarshal(nch.ExtraData(), &p) != nil || p.Port == 0 || p.Port > 65535 {
		c.reject(nch, ssh.ConnectionFailed, errors.New("invalid forward"))
		return
	}
	adm, release := c.open(nch)
	if release == nil {
		return
	}
	defer release()
	to := Record{Type: "forward", Host: p.Host, Port: int(p.Port)}
	needs := ""
	if adm.source.Limit == "models" {
		needs = onlyModels
	} else if adm.policy.Mode != "full_access" { // the effective policy: the machine's mode and the source's limit
		needs = needsFull
	}
	if needs != "" {
		to.Type, to.Kind, to.Rule = "block", "forward", "access"
		c.visit.Write(to)
		nch.Reject(ssh.Prohibited, needs)
		return
	}
	if stop := adm.lease.Target.Rules.Check("direct-tcpip", "", c.d.core.Home); stop != nil {
		to.Type, to.Kind, to.Rule = "block", "forward", "lock" // a machine with a lock
		c.visit.Write(to)
		nch.Reject(ssh.Prohibited, stop.Rule)
		return
	}
	tcp, err := (&net.Dialer{Timeout: 10 * time.Second}).Dial("tcp", net.JoinHostPort(p.Host, strconv.Itoa(int(p.Port))))
	if err != nil {
		c.reject(nch, ssh.ConnectionFailed, err)
		return
	}
	ch, reqs, err := nch.Accept()
	if err != nil {
		tcp.Close()
		return
	}
	c.visit.Write(to)
	go ssh.DiscardRequests(reqs)
	finished := make(chan struct{})
	defer close(finished)
	go func() { // a closed connection, as on revocation, ends the stream both ways
		select {
		case <-c.done:
			tcp.Close()
		case <-finished:
		}
	}()
	splice(ch, tcp, 0)
}

// models is one request to this machine's model servers, admitted as a
// forward is: a slot, the binding and the machine's lock, and then only for a
// source Cloud allows them. The channel closes with its answer, so the next
// request is admitted from the lease current then.
func (c *conn) models(nch ssh.NewChannel) {
	adm, release := c.open(nch)
	if release == nil {
		return
	}
	defer release()
	if stop := adm.lease.Target.Rules.Check("direct-tcpip", "", c.d.core.Home); stop != nil {
		c.visit.Refuse("channel", "error", stop.Rule)
		nch.Reject(ssh.Prohibited, stop.Rule)
		return
	}
	if !slices.Contains(adm.source.Allow, "models") {
		c.reject(nch, ssh.Prohibited, errors.New("AI models are off for you on this machine"))
		return
	}
	ch, reqs, err := nch.Accept()
	if err != nil {
		return
	}
	defer ch.Close()
	go ssh.DiscardRequests(reqs)
	ServeModels(ch, c.d.Services(), func(port int) { c.visit.Write(Record{Type: "forward", Host: "localhost", Port: port}) })
}

// ServeModels answers the one HTTP request of a models channel from list, this
// machine's services: GET /v1/models with their models; an inference POST by
// passing it to the server of the model its body names, once forwarded is told
// that server's port, and passing the answer back as it comes. Anything else is {"error":{"message":…}}. Every
// answer says Connection: close.
func ServeModels(ch io.ReadWriter, list []Service, forwarded func(port int)) {
	answer := func(status int, v any) {
		b, _ := json.Marshal(v)
		fmt.Fprintf(ch, "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
			status, http.StatusText(status), len(b), b)
	}
	// The headers have the megabyte net/http gives them.
	r, err := http.ReadRequest(bufio.NewReader(io.LimitReader(ch, maxModelBody+http.DefaultMaxHeaderBytes)))
	if err != nil {
		return
	}
	if r.Method == "GET" && r.URL.Path == "/v1/models" {
		data := []map[string]any{}
		for _, s := range list {
			for _, m := range s.Models {
				data = append(data, map[string]any{"id": m.ID, "object": "model", "owned_by": s.Name, "chat": m.Chat})
			}
		}
		answer(http.StatusOK, map[string]any{"object": "list", "data": data})
		return
	}
	body, model, _, _, status, message := modelRequest(r)
	var at netip.AddrPort // the model's server, where discovery read its list
	for _, loose := range []bool{false, true} {
		for _, s := range list {
			if !at.IsValid() && slices.ContainsFunc(s.Models, func(m Model) bool { return answers(m.ID, model, loose) }) {
				at = s.Addr
			}
		}
	}
	if status == 0 && !at.IsValid() {
		status, message = http.StatusNotFound, "This machine has no model "+strconv.Quote(model)+"."
	}
	port := int(at.Port())
	var server net.Conn
	if status == 0 {
		if server, err = dial(loopbacks(at), port, 10*time.Second); err != nil {
			status, message = http.StatusBadGateway, "The model’s server can’t be reached."
		}
	}
	if status != 0 {
		answer(status, modelError(message))
		return
	}
	defer server.Close()
	forwarded(port)
	go func() { io.Copy(io.Discard, ch); server.Close() }() // the channel's end, as on revocation, ends the request
	// The request as a program on this machine would send it; its body is read already, so nothing waits for a 100.
	r.Host = "localhost:" + strconv.Itoa(port)
	for _, name := range []string{"Origin", "Referer", "Expect", "Connection"} {
		r.Header.Del(name)
	}
	if r.Header.Get("User-Agent") == "" {
		r.Header.Set("User-Agent", "") // none is added
	}
	r.Body, r.ContentLength, r.TransferEncoding, r.Close = io.NopCloser(bytes.NewReader(body)), int64(len(body)), nil, true
	if r.Write(server) != nil {
		return
	}
	res, err := http.ReadResponse(bufio.NewReader(server), r)
	if err != nil {
		answer(http.StatusBadGateway, modelError("The model’s server gave no answer."))
		return
	}
	res.Close = true
	res.Header.Del("Connection")
	res.Write(ch)
}

// modelRequest reads an inference request's JSON body, at most 32 MiB, and
// the model it names: the string under the top-level key model, which is
// there once, at body[start:end]. Otherwise it returns
// the status and message of the refusal: 404 for another method or path, 413,
// or 400.
func modelRequest(r *http.Request) (body []byte, model string, start, end, status int, message string) {
	if r.Method != "POST" || !slices.Contains(inference, r.URL.Path) {
		return nil, "", 0, 0, http.StatusNotFound, "Not found."
	}
	body, _ = io.ReadAll(io.LimitReader(r.Body, maxModelBody+1))
	if len(body) > maxModelBody {
		return nil, "", 0, 0, http.StatusRequestEntityTooLarge, "A request can be at most 32 MiB."
	}
	dec, found := json.NewDecoder(bytes.NewReader(body)), 0
	first, err := dec.Token()
	for err == nil && first == json.Delim('{') && dec.More() {
		var key json.Token
		var value json.RawMessage
		if key, err = dec.Token(); err == nil {
			err = dec.Decode(&value)
		}
		if err == nil && key == "model" {
			if found++; value[0] == '"' && json.Unmarshal(value, &model) == nil {
				end = int(dec.InputOffset())
				start = end - len(value)
			}
		}
	}
	if last, lerr := dec.Token(); err == nil && lerr == nil && last == json.Delim('}') && found == 1 && end > 0 {
		return body, model, start, end, 0, ""
	}
	return nil, "", 0, 0, http.StatusBadRequest, "Send a JSON object with one \"model\", a string."
}

// answers reports whether the model a server lists as id is the one a
// request names: by that id or, loosely, as Ollama answers to a name, without
// :latest and in any case. That holds for any server: om cannot always tell
// which one it is.
func answers(id, name string, loose bool) bool {
	bare := func(s string) string { return strings.TrimSuffix(strings.ToLower(s), ":latest") }
	return id == name || loose && bare(id) == bare(name)
}

// modelError is the body of one of om's own refusals of a model request.
func modelError(message string) any {
	return map[string]any{"error": map[string]string{"message": message}}
}

// session is one session channel: at most one command, through Core.
type session struct {
	id                 string // names it in the log
	c                  *conn
	ch                 ssh.Channel
	input              io.Reader // the command's: ch, after what was held of it
	held               *os.File  // the pipe ch's input is held in, when it is
	stopped            bool      // a rule stopped it, and it starts nothing
	env                map[string]string
	term               string
	master, slave      *os.File // its terminal, when requested
	stdin, out, errout *os.File // its pipes otherwise
	mu                 sync.Mutex
	cmd                *exec.Cmd
	done               bool   // the command exited
	endGitHub          func() // ends the session's GitHub access, when it has any
}

func (c *conn) session(nch ssh.NewChannel) {
	_, release := c.open(nch)
	if release == nil {
		return
	}
	defer release()
	ch, reqs, err := nch.Accept()
	if err != nil {
		return
	}
	s := &session{c: c, ch: ch, input: ch, env: map[string]string{}, id: newID()}
	for r := range reqs {
		s.request(r)
	}
	ch.Close()
	if s.held != nil {
		s.held.Close()
	}
	if s.endGitHub != nil {
		s.endGitHub() // a detached job keeps running, but its GitHub access ends with the session
	}
	if s.master != nil {
		s.master.Close() // hangs up the terminal, as sshd does when its client goes
	}
	if s.cmd == nil && s.slave != nil {
		s.slave.Close()
	}
}

func (s *session) request(r *ssh.Request) {
	ok := false
	switch r.Type {
	case "pty-req":
		var p struct {
			Term                      string
			Cols, Rows, Width, Height uint32
			Modes                     string
		}
		err := ssh.Unmarshal(r.Payload, &p)
		if p.Term == "" {
			p.Term = "dumb" // a client without TERM
		}
		if err == nil && s.cmd == nil && s.master == nil && termName.MatchString(p.Term) {
			if s.master, s.slave, err = openTerminal(p.Modes); err == nil {
				s.term, ok = p.Term, true
				setSize(s.master, p.Cols, p.Rows)
			}
		}
	case "window-change":
		var p struct{ Cols, Rows, Width, Height uint32 }
		if s.master != nil && ssh.Unmarshal(r.Payload, &p) == nil {
			ok = setSize(s.master, p.Cols, p.Rows) == nil
		}
	case "env":
		var p struct{ Name, Value string }
		if s.cmd == nil && ssh.Unmarshal(r.Payload, &p) == nil && envName.MatchString(p.Name) && envValue.MatchString(p.Value) {
			if _, set := s.env[p.Name]; set || len(s.env) < 32 {
				s.env[p.Name], ok = p.Value, true
			}
		}
	case "shell":
		s.start(r, "shell", "")
		return
	case "exec":
		var p struct{ Command string }
		if ssh.Unmarshal(r.Payload, &p) == nil && p.Command != "" && len(p.Command) <= 65536 {
			s.start(r, "exec", p.Command)
			return
		}
	case "subsystem":
		var p struct{ Name string }
		if ssh.Unmarshal(r.Payload, &p) == nil && p.Name == "sftp" {
			s.start(r, "sftp", "")
			return
		}
		fmt.Fprintf(s.ch.Stderr(), "om: subsystem %q is not supported\r\n", p.Name)
	case "signal":
		var p struct{ Signal string }
		if ssh.Unmarshal(r.Payload, &p) == nil && signals[p.Signal] != 0 {
			sig := signals[p.Signal]
			s.mu.Lock()
			if s.cmd != nil && !s.done {
				ok = s.cmd.Process.Signal(sig) == nil // the launcher forwards it to the command
			}
			s.mu.Unlock()
		}
	}
	// x11-req, auth-agent-req@openssh.com and anything else are refused.
	r.Reply(ok, nil)
}

// start launches the session's command once, as a new admission.
func (s *session) start(r *ssh.Request, kind, command string) {
	if s.cmd != nil || s.stopped {
		r.Reply(false, nil)
		return
	}
	// OpenSSH discards stderr when the request itself is rejected, and a client
	// may send a script only after the reply. Report a launch error as its
	// result, including Core's effect classification, and a stop as a rule's.
	r.Reply(true, nil)
	if err := s.launch(kind, command); err != nil {
		message, status := fmt.Sprintf("om: %v\r\n", err), uint32(255)
		if stop := (*Stop)(nil); errors.As(err, &stop) {
			message, status, s.stopped = stop.Message, 126, true
		}
		io.WriteString(s.ch.Stderr(), message)
		s.ch.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{status}))
		s.ch.Close()
		return
	}
	go s.run()
}

// check returns the script it held of the session's input, and what stops
// the session: that its source gets only this machine's AI models, or one of
// the account's rules. A script sent whole is held first and checked as its
// line; input that does not end is a conversation, which only a lock stops.
func (s *session) check(adm *admission, kind, line string) (script []byte, err error) {
	if adm.source.Limit == "models" { // a models-only source reaches the AI models and nothing else
		return nil, &Stop{Rule: "access", Message: onlyModels + "\r\n"}
	}
	d, rules := s.c.d, adm.lease.Target.Rules
	stop := rules.Check(kind, line, d.core.Home)
	if stop == nil && s.master != nil { // a terminal is a conversation, which only a lock stops
		stop = rules.Check("shell", "", d.core.Home)
	}
	if stop == nil && rules != nil && (kind == "shell" && s.master == nil || kind == "exec" && SendsScript(line)) {
		if script, err = s.hold(); err != nil {
			return nil, err
		}
		if script != nil {
			stop = rules.Check("exec", string(script), d.core.Home)
		} else {
			stop = rules.Check("shell", "", d.core.Home)
		}
	}
	if stop != nil {
		err = stop
	}
	return script, err
}

// hold returns the channel's input when it ends within 300 ms and 65,536
// bytes, else nil: a conversation. What it read stays the command's input. It
// holds once: a request after a failed launch finds the input held, and
// checked, already.
func (s *session) hold() ([]byte, error) {
	if s.held != nil {
		return nil, nil
	}
	r, w, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	go func() { io.Copy(w, s.ch); w.Close() }()
	r.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	script, err := io.ReadAll(io.LimitReader(r, 65537))
	r.SetReadDeadline(time.Time{})
	s.held, s.input = r, io.MultiReader(bytes.NewReader(script), r)
	if err != nil || len(script) > 65536 {
		return nil, nil
	}
	return script, nil
}

// launch starts the session's command through Core and records it: a
// refusal, a block, or its start, on disk before Core runs it, and then its
// end if it did not start. Where the account records
// commands, the session is told so first, and the records hold its text.
func (s *session) launch(kind, command string) (err error) {
	d, v := s.c.d, s.c.visit
	adm, err := s.c.admitChannel()
	if err != nil {
		v.Refuse("channel", reasonOf(err), err.Error())
		return err
	}
	notice, _ := adm.lease.Target.notice()
	io.WriteString(s.ch.Stderr(), v.Notice(kind, notice))
	script, err := s.check(adm, kind, command)
	r := Record{Type: "start", Session: s.id, Kind: kind}
	if notice != "" {
		r.Command, r.Script = command, string(script)
	}
	if stop := (*Stop)(nil); errors.As(err, &stop) {
		if r.Type, r.Rule = "block", stop.Rule; stop.Lock {
			r.Rule = "lock"
		}
		v.Write(r)
		return err
	} else if err != nil {
		v.Refuse("channel", reasonOf(err), err.Error())
		return err
	}
	if s.master != nil {
		r.Terminal = 1
	}
	if err := v.Write(r); err != nil {
		return fmt.Errorf("can't record this command: %v", err)
	}
	defer func() {
		if err != nil {
			end := Record{Type: "end", Session: s.id, Error: "launch"}
			if denied := (*coreDenied)(nil); errors.As(err, &denied) {
				if denied.Effect != "known_no_effect" {
					return // it may be running, so it has no end
				}
				end.Error = "core"
			}
			v.Write(end)
		}
	}()
	req := coreRequest{Protocol: 4, Connection: s.c.address, User: d.user, Policy: adm.policy.Mode, Folders: adm.policy.Folders,
		Home: adm.source.Home, Kind: kind, Environment: s.env}
	if kind == "exec" {
		req.Command = &command
	}
	lt, err := d.lifetimeFor(s.c)
	if err != nil {
		return err
	}
	req.Lifetime = lt.ID
	var gh *githubRouting // SFTP runs no git, and only a source Cloud allows it gets the routing
	if kind != "sftp" && slices.Contains(adm.source.Allow, "github") {
		gh = d.githubRouting()
	}
	// Full Access runs the daemon's own om; a restricted sandbox may run only Core's copy,
	// which a Core without restricted policies does not name, and then nothing is routed.
	run := ""
	if gh != nil {
		run = gh.client
		if adm.policy.Mode != "full_access" {
			run = gh.restricted
		}
	}
	if run != "" {
		b, c := adm.binding, s.c
		capability, end := gh.broker.Open(GitSession{Source: b.Source, SourceGrantRevision: b.SourceGrantRevision,
			TargetGrant: b.TargetGrant, TargetGrantRevision: b.TargetGrantRevision, PolicyRevision: b.PolicyRevision,
			Mode: adm.policy.Mode, Visit: v.ID(), Done: c.done, Live: func() error {
				d.mu.Lock()
				err := d.admissible(c)
				d.mu.Unlock()
				if err == nil && d.id.Admission() != nil {
					err = disconnected("the target")
				}
				return err
			}})
		if s.endGitHub != nil {
			s.endGitHub() // an earlier request of this channel that did not start its command
		}
		s.endGitHub = end
		req.GitHub = &coreGitHub{Environment: gh.broker.Environment(capability, run), Client: gh.client}
	}
	var stdio [3]*os.File
	if s.master != nil {
		req.Terminal = &coreTerminal{s.term}
		stdio = [3]*os.File{s.slave, s.slave, s.slave}
	} else {
		var in, out, errout [2]*os.File
		for _, p := range []*[2]*os.File{&in, &out, &errout} {
			r, w, err := os.Pipe()
			if err != nil {
				for _, f := range [...]*os.File{in[0], in[1], out[0], out[1]} {
					if f != nil {
						f.Close()
					}
				}
				return err
			}
			*p = [2]*os.File{r, w}
		}
		stdio = [3]*os.File{in[0], out[1], errout[1]}
		s.stdin, s.out, s.errout = in[1], out[0], errout[0]
	}
	cmd, err := d.core.session(req, stdio)
	for _, f := range slices.Compact(stdio[:]) {
		f.Close() // the command holds its own ends
	}
	if err != nil {
		var denied *coreDenied
		if errors.As(err, &denied) && (denied.Code == "lifetime_terminated" || denied.Code == "lifetime_changed") {
			d.retire(lt)
		}
		for _, f := range []*os.File{s.master, s.stdin, s.out, s.errout} {
			if f != nil {
				f.Close()
			}
		}
		return err
	}
	s.mu.Lock()
	s.cmd = cmd
	s.mu.Unlock()
	return nil
}

// run copies the command's IO until it exits and its output drains, then
// reports its exit like sshd. A closed channel only closes the command's IO,
// with standard SSH EOF and hangup semantics; nothing is replayed.
func (s *session) run() {
	var output sync.WaitGroup
	if s.master != nil {
		go io.Copy(s.master, s.input)
		output.Add(1)
		go func() { io.Copy(s.ch, s.master); output.Done() }()
	} else {
		go func() { io.Copy(s.stdin, s.input); s.stdin.Close() }()
		output.Add(2)
		go func() { io.Copy(s.ch, s.out); s.out.Close(); output.Done() }()
		go func() { io.Copy(s.ch.Stderr(), s.errout); s.errout.Close(); output.Done() }()
	}
	s.cmd.Wait()
	s.mu.Lock()
	s.done = true
	s.mu.Unlock()
	drained := make(chan struct{})
	go func() { output.Wait(); close(drained) }()
	if s.master != nil {
		// Detached work may hold the terminal open; like sshd, stop at the command's exit.
		select {
		case <-drained:
		case <-time.After(drainTimeout):
		}
		s.master.Close()
	} else {
		<-drained
	}
	ws := s.cmd.ProcessState.Sys().(syscall.WaitStatus)
	end := Record{Type: "end", Session: s.id}
	s.ch.CloseWrite()
	if name, named := exits[ws.Signal()]; ws.Signaled() && named {
		end.Signal = name
		s.ch.SendRequest("exit-signal", false, ssh.Marshal(struct {
			Signal     string
			CoreDumped bool
			Error      string
			Lang       string
		}{end.Signal, ws.CoreDump(), "", ""}))
	} else {
		status := ws.ExitStatus()
		if ws.Signaled() {
			status = 128 + int(ws.Signal()) // as a shell reports a signal without a standard name
		}
		end.Exit = &status
		s.ch.SendRequest("exit-status", false, ssh.Marshal(struct{ Status uint32 }{uint32(status)}))
	}
	s.c.visit.Write(end)
	s.ch.Close()
}

// splice copies both ways until each side ends, passing on half-closes; with
// grace, it closes both that long after a's side ends.
func splice(a, b io.ReadWriteCloser, grace time.Duration) {
	var wg sync.WaitGroup
	wg.Add(2)
	copyTo := func(dst, src io.ReadWriteCloser) {
		defer wg.Done()
		io.Copy(dst, src)
		if !closeWrite(dst) {
			dst.Close()
		}
	}
	go copyTo(a, b)
	go func() {
		copyTo(b, a)
		if grace > 0 {
			time.AfterFunc(grace, func() { a.Close(); b.Close() })
		}
	}()
	wg.Wait()
	a.Close()
	b.Close()
}

func closeWrite(c any) bool {
	if nc, ok := c.(*connector.Conn); ok {
		c = nc.Conn
	}
	h, ok := c.(interface{ CloseWrite() error })
	return ok && h.CloseWrite() == nil
}
