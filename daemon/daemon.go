package daemon

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	crand "crypto/rand"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/OpenMachineLabs/openmachine/connector"
	"golang.org/x/crypto/ssh"
)

// StateDir is the daemon's private state under home. Core denies it, and the
// Connector's state beside it, to restricted sessions.
func StateDir(home string) string { return filepath.Join(home, ".local/state/openmachine-om") }

const (
	codePort   = 22
	socketName = "daemon.sock"
	// proxyGrace bounds a ProxyCommand's stream once its side has ended, as when
	// ssh is killed: SSH needs no half-close, and a vanished target answers none.
	proxyGrace = 5 * time.Second
)

// daemon is this device's OM SSH: a target serving SSH on the private network
// under its Code lease, and a source dialing the devices its lease lists for
// local ProxyCommands.
type daemon struct {
	id        *Identity
	state     *state // shared by every device of this computer
	device    *state // this device's own
	core      Core
	user      string
	signer    ssh.Signer
	log       *Log
	authority *Authority
	node      atomic.Pointer[connector.Node]
	netlog    *netLog                         // the network node's, over every start
	allowed   atomic.Pointer[map[string]bool] // node keys of the sources and hosts applied last
	services  atomic.Pointer[[]Service]       // this machine's listening programs; nil until the first pass
	configMu  sync.Mutex
	coreRead  chan struct{} // closed once Core's capabilities were first asked
	githubMu  sync.Mutex
	github    *githubRouting // the broker, or nil until Core answers and without its support
	foldersMu sync.Mutex     // folder checks run one at a time
	keyMu     sync.Mutex     // open_key is made once

	mu        sync.Mutex
	applied   *Lease
	conns     map[*conn]struct{}     // admitted SSH connections into this computer
	streams   map[io.Closer]struct{} // SSH streams out of it, for local clients
	lifetimes map[binding]*lifetime  // one per binding; persisted
}

// Run runs the daemon of the user whose HOME is core.Home until ctx ends. It
// waits for the device identity, which sign-in creates, then joins the network
// once for that device and reuses its node and host key from then on. Its
// only OS TCP ports are the local entry to machines' web pages and each opened
// page's own, on 127.0.0.1 and [::1]. Neither a missing identity nor state that
// needs recovery, which a restart cannot fix, ends it: a supervisor never
// restarts it in a loop. It returns ErrDeviceChanged when a sign-in moves this
// computer to another device, also during recovery, so its supervisor starts
// it again for that device.
func Run(ctx context.Context, core Core) error {
	if !filepath.IsAbs(core.Home) {
		return errors.New("native: the daemon needs its user's absolute HOME")
	}
	// The state is taken before the wait for a sign-in: its lock says a daemon
	// runs, and a second one never waits beside it.
	st, err := openState(StateDir(core.Home))
	if errors.Is(err, errRecovery) {
		log.Print(err)
		<-ctx.Done()
		return nil
	} else if err != nil {
		return err
	}
	dir := filepath.Join(core.Home, ".local/state/openmachine-connector")
	id, err := ReadIdentity(dir)
	if err != nil {
		log.Printf("%v; waiting", err)
	}
	for err != nil {
		sleep(ctx, retry)
		if ctx.Err() != nil {
			return nil
		}
		id, err = ReadIdentity(dir)
	}
	ctx, exit := context.WithCancelCause(ctx)
	defer exit(nil)
	go func() {
		for ctx.Err() == nil {
			if id.changed() {
				exit(ErrDeviceChanged)
			}
			sleep(ctx, time.Second)
		}
	}()
	err = run(ctx, core, id, st)
	if errors.Is(err, errRecovery) {
		log.Print(err)
		<-ctx.Done()
	}
	if errors.Is(context.Cause(ctx), ErrDeviceChanged) {
		return ErrDeviceChanged
	}
	if errors.Is(err, errRecovery) {
		return nil
	}
	return err
}

// ErrDeviceChanged ends a daemon whose computer a sign-in moved to another device.
var ErrDeviceChanged = errors.New("native: this computer moved to another device")

// ErrUnreachable is in dial's error for a machine that is offline.
var ErrUnreachable = errors.New("offline or unreachable")

func run(ctx context.Context, core Core, id *Identity, st *state) error {
	ctx, cancel := context.WithCancel(ctx) // ends what run starts when it returns
	defer cancel()
	u, err := user.Current()
	if err != nil {
		return err
	}
	// Open Machine runs as the user who installed it, root included on Linux.
	if u.Uid == "0" && runtime.GOOS != "linux" {
		return errors.New("native: OM SSH runs only as a non-root user")
	}
	own, err := st.device(id.DeviceID, id.stored)
	if err != nil {
		return err
	}
	var e enrollment
	if err := own.readJSON("enrollment.json", maxState, &e); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("%w: the enrollment record is unreadable: %v", errRecovery, err)
	}
	if e.DeviceID != "" && (e.DeviceID != id.DeviceID || e.Origin != id.Origin) {
		return fmt.Errorf("%w: this Code state belongs to another registration", errRecovery)
	}
	e.DeviceID, e.Origin = id.DeviceID, id.Origin
	signer, err := own.hostKey(e.Host)
	if err != nil {
		return err
	}
	d := &daemon{id: id, state: st, device: own, core: core, user: u.Username, signer: signer, log: OpenLog(own.path("log"), id.DeviceID, id.Key),
		netlog: newNetLog(core.Home), coreRead: make(chan struct{}), conns: map[*conn]struct{}{}, streams: map[io.Closer]struct{}{}, lifetimes: map[binding]*lifetime{}}
	d.allowed.Store(&map[string]bool{})
	if err := d.loadLifetimes(); err != nil {
		return err
	}
	// Everything below may use the authority; it applies leases, closing
	// revoked connections, even before the network is up.
	d.authority = NewAuthority(id.Origin, id.DeviceID, id.Key, d, d.log, CloudClient)
	sock, err := d.listenProxy()
	if err != nil {
		return err
	}
	defer sock.Close()
	go d.serveProxy(sock)
	port := "11400"
	if p := os.Getenv("OM_LOCAL_PORT"); p != "" { // set only by tests
		port = p
	}
	go NewLocal(d, id.DeviceID, port).Serve(ctx, port)
	go d.watch(ctx)
	go d.authority.Run(ctx)
	go d.readCore(ctx)
	go d.discover(ctx)
	go AutoUpdate(ctx, st.Dir, d.authority.Refused(), d.connected, func() { StartUpdate() }, rand.N(24*time.Hour), 24*time.Hour, 10*time.Minute)
	var node *connector.Node
	var last string // the failure logged last, so one that repeats is logged once
	failed := func(step string, err error) {
		d.authority.Joining(joinReason(err))
		if line := fmt.Sprintf("native: %s: %v", step, err); line != last {
			log.Print(line)
			last = line
		}
	}
	for {
		if node, err = d.join(ctx, &e); err == nil {
			break
		}
		if ctx.Err() != nil {
			return nil
		}
		if errors.Is(err, errRecovery) {
			return err
		}
		failed("joining the private network", err)
		sleep(ctx, retry)
	}
	defer node.Close()
	d.node.Store(node)
	d.authority.Joining("")
	for wait := retry; !e.Host; wait = min(2*wait, 5*time.Minute) {
		err := d.registerHost(ctx, &e)
		if ctx.Err() != nil {
			return nil
		}
		if errors.Is(err, errRecovery) {
			return err
		}
		if err != nil {
			failed("registering the SSH host", err)
			sleep(ctx, wait)
		}
	}
	ln, err := node.Listen(codePort)
	if err != nil {
		return err
	}
	go d.serve(ln)
	d.authority.Listening()
	<-ctx.Done()
	ln.Close()
	return nil
}

var errRecovery = errors.New("native: recovery is required")

// githubRouting is this daemon's GitHub broker and the om its sessions run: its own file
// under Full Access, and Core's copy of it in restricted sessions.
type githubRouting struct {
	broker             *Broker
	client, restricted string
}

// CoreRead is how often a Core that answered is asked again; tests shorten it.
var CoreRead = 10 * time.Minute

// readCore asks Core's access launcher what it supports: at once, each minute
// until it answers and then every 10 minutes, so a machine whose sandbox its
// administrator fixed needs no restart. Renewals report its version and the
// modes it can enforce: none for a Core of another access protocol. An answer
// that takes Git routing starts the broker.
func (d *daemon) readCore(ctx context.Context) {
	for first := true; ; first = false {
		caps, err := d.core.capabilities()
		wait := CoreRead
		if err != nil {
			log.Printf("native: Core's capabilities: %v", err)
			wait = time.Minute
			if errors.Is(err, errCoreProtocol) { // it runs no session of this protocol, whatever answered before
				d.authority.Core(caps.Version, map[string]string{"read_only": "core_protocol", "workspace": "core_protocol", "full_access": "core_protocol"})
			}
		} else {
			d.authority.Core(caps.Version, caps.Policies)
			d.route(caps)
		}
		if first {
			// Sessions waited for the routing the first answer starts, not for a Core that gives none.
			close(d.coreRead)
		}
		if sleep(ctx, wait); ctx.Err() != nil {
			return
		}
	}
}

// route starts the GitHub broker once, for a Core that takes Git routing, and
// then follows the om Core names for restricted sessions.
func (d *daemon) route(caps coreCapabilities) {
	d.githubMu.Lock()
	defer d.githubMu.Unlock()
	if gh := d.github; gh != nil {
		d.github = &githubRouting{gh.broker, gh.client, caps.Client}
		return
	}
	client, err := os.Executable()
	if err == nil {
		client, err = filepath.EvalSymlinks(client)
	}
	if caps.GitHub != "https" || err != nil {
		if err != nil {
			log.Printf("native: GitHub broker: %v", err)
		}
		return
	}
	b, err := NewBroker(d.id.Origin, d.id.DeviceID, d.id.Key, CloudClient, d.state.path("github-proofs"))
	if err != nil {
		log.Printf("native: GitHub broker: %v", err)
		return
	}
	d.github = &githubRouting{b, client, caps.Client}
}

// githubRouting returns the broker for shell and exec sessions once Core said it takes their Git
// routing; it is nil while Core gives no answer or has no such support. A session started while
// Core is first asked waits for its answer.
func (d *daemon) githubRouting() *githubRouting {
	<-d.coreRead
	d.githubMu.Lock()
	defer d.githubMu.Unlock()
	return d.github
}

// Admission reports whether this computer is online.
func (d *daemon) Admission() error { return d.id.Admission() }

// Lease is the lease a page opens under: the current one, once this computer
// has joined the private network.
func (d *daemon) Lease() (*Lease, error) {
	l, err := d.lease("this computer")
	if err == nil && d.node.Load() == nil {
		err = errors.New("the Code service is still joining the private network")
	}
	return l, err
}

// Devices returns the machines of the last lease's view.
func (d *daemon) Devices() []ViewDevice { return d.authority.Devices() }

// Services returns this machine's listening programs, as discovery listed
// them last.
func (d *daemon) Services() []Service {
	if list := d.services.Load(); list != nil {
		return *list
	}
	return nil
}

// Dial opens an OM SSH stream to a machine for its pages or its models, as
// serveProxy does for ssh: named for app and held in d.streams, so that going
// offline closes it.
func (d *daemon) Dial(alias, app string) (net.Conn, error) {
	target, err := d.dial(alias + ".om")
	if err != nil {
		return nil, err
	}
	d.mu.Lock()
	d.streams[target] = struct{}{}
	d.mu.Unlock()
	held := heldStream{target, d}
	// Registered first, so going offline during the dial is seen here or by watch.
	if d.id.Admission() != nil {
		held.Close()
		return nil, disconnected("this computer")
	}
	if _, err := io.WriteString(target, "OM-APP "+app+"\r\n"); err != nil {
		held.Close()
		return nil, err
	}
	return held, nil
}

// heldStream is a stream in d.streams until it closes.
type heldStream struct {
	*connector.Conn
	d *daemon
}

func (h heldStream) Close() error {
	h.d.mu.Lock()
	delete(h.d.streams, h.Conn)
	h.d.mu.Unlock()
	return h.Conn.Close()
}

// Key returns the key confirmed pages' cookies are made with: 32 bytes in the
// state file open_key, made on first use. Deleting the file asks again for
// every page.
func (d *daemon) Key() ([]byte, error) {
	d.keyMu.Lock()
	defer d.keyMu.Unlock()
	key, err := d.state.read("open_key", 32)
	if errors.Is(err, os.ErrNotExist) {
		key = make([]byte, 32)
		crand.Read(key)
		err = d.state.write("open_key", key)
	}
	return key, err
}

// Pages returns the pages given a port on this computer, from the state file
// pages; none before the first.
func (d *daemon) Pages() ([]Page, error) {
	var pages []Page
	b, err := d.state.read("pages", maxState)
	if errors.Is(err, os.ErrNotExist) {
		return []Page{}, nil
	}
	if err == nil {
		err = json.Unmarshal(b, &pages)
	}
	return pages, err
}

// SavePages keeps the pages' ports for Pages.
func (d *daemon) SavePages(pages []Page) error {
	b, err := json.Marshal(pages)
	if err == nil {
		err = d.state.write("pages", b)
	}
	return err
}

// Update starts om update for the machine's owner, as typed on the machine:
// whatever auto_update says and whoever is connected.
// Only a headless installation has one, so a Mac starts nothing.
func (d *daemon) Update() bool {
	_, err := readPrivate(d.state.Dir, "headless.json", maxState)
	return err == nil && StartUpdate()
}

// Move starts om move for the person who moved this machine in the Console.
// As an update, only a headless installation has one.
func (d *daemon) Move(secret string) bool {
	_, err := readPrivate(d.state.Dir, "headless.json", maxState)
	return err == nil && StartMove(secret)
}

// JoinTimeout bounds each start of the network node; tests shorten it.
var JoinTimeout = time.Minute

// errUnreachable is a node that did not reach the private network's control in time.
var errUnreachable = errors.New("native: the private network's control cannot be reached")

// joinReason is the status reason of a failed step toward listening on the
// private network, in the form Desktop and om status take: Cloud's own
// refusal code, as network_unavailable or network_capacity; cloud_unavailable
// when Cloud cannot be reached or answers with no code, as a proxy does;
// network_unreachable when the node cannot reach the control; joining for a
// wait Cloud asked for; else join_failed. The log has the detail.
func joinReason(err error) string {
	var ce *cloudError
	switch {
	case errors.As(err, &ce) && (ce.Code == "network_enrollment_wait" || ce.Code == "network_enrollment_changed"):
		return "joining" // the next attempt proceeds
	case errors.As(err, &ce) && policyState.MatchString(ce.Code):
		return ce.Code
	case errors.As(err, &ce):
		return "cloud_unavailable"
	case errors.Is(err, ErrOffline):
		return "offline"
	case errors.Is(err, errUnreachable):
		return "network_unreachable"
	case errors.As(err, new(net.Error)), errors.As(err, new(*tls.CertificateVerificationError)), errors.Is(err, context.DeadlineExceeded):
		return "cloud_unavailable" // only Cloud's requests remain here
	}
	return "join_failed"
}

// start starts the node of cfg within JoinTimeout, logging to nl. A node
// that times out fails with errUnreachable and the last error it logged, such
// as a control it cannot dial: tsnet itself reports only the timeout.
func start(ctx context.Context, cfg connector.Config, nl *netLog) (*connector.Node, error) {
	var mu sync.Mutex
	var last string
	started := false // the node logs on after Start; nothing more is kept
	nl.mu.Lock()
	nl.key = cfg.AuthKey
	nl.mu.Unlock()
	cfg.Logf = func(format string, args ...any) {
		nl.logf(format, args...)
		mu.Lock()
		defer mu.Unlock()
		if started {
			return
		}
		if line, ok := strings.CutPrefix(fmt.Sprintf(format, args...), "Received error: "); ok {
			last = nl.redact(line)
		}
	}
	ctx, cancel := context.WithTimeout(ctx, JoinTimeout)
	defer cancel()
	node, err := connector.Start(ctx, cfg)
	mu.Lock()
	defer mu.Unlock()
	started = true
	if err != nil && ctx.Err() != nil {
		if last != "" {
			return nil, fmt.Errorf("%w within %v: %s", errUnreachable, JoinTimeout, last)
		}
		return nil, fmt.Errorf("%w within %v: %v", errUnreachable, JoinTimeout, err)
	}
	return node, err
}

// join starts this device's network node. A node it enrolled is resumed; an
// unbound device enrolls through its one original intent, starts and binds,
// and is recorded as bound. A bound device is never enrolled again.
func (d *daemon) join(ctx context.Context, e *enrollment) (*connector.Node, error) {
	dir := d.device.path("network")
	cfg := connector.Config{Dir: dir, ControlURL: e.ControlURL, Hostname: "om", // the control server's node name; nothing reads it
		Allow: func(p connector.Peer) bool { return (*d.allowed.Load())[p.NodeKey] }}
	var resumed error // why the node held here did not start
	if e.ControlURL != "" {
		node, err := start(ctx, cfg, d.netlog)
		if err == nil {
			return d.bind(ctx, e, node)
		}
		if e.NodeKey != "" {
			return nil, err // bound: retry the same node, never replace it
		}
		resumed = err
	}
	if e.NodeKey != "" {
		return nil, fmt.Errorf("%w: the bound network identity is missing", errRecovery)
	}
	if err := d.id.Admission(); err != nil {
		return nil, err // enroll only while online
	}
	var r struct {
		State        string `json:"state"`
		BootstrapKey string `json:"bootstrap_key"`
		ControlURL   string `json:"control_url"`
	}
	if err := d.post(ctx, "network/enroll", struct{}{}, &r); err != nil {
		if ce := (*cloudError)(nil); errors.As(err, &ce) && ce.Code == "network_enrollment_wait" {
			sleep(ctx, time.Minute)
		}
		return nil, err
	}
	if r.State == "bound" {
		if e.ControlURL != "" { // the bind reply was lost: the node held here is the bound one
			return nil, fmt.Errorf("native: Cloud bound this computer's node, which did not start: %w", resumed)
		}
		return nil, fmt.Errorf("%w: Cloud holds a binding this state lost", errRecovery)
	}
	if err := os.RemoveAll(dir); err != nil { // never bound: nothing to keep
		return nil, err
	}
	e.ControlURL = r.ControlURL
	if err := d.device.writeJSON("enrollment.json", e); err != nil {
		return nil, err
	}
	cfg.ControlURL, cfg.AuthKey = r.ControlURL, r.BootstrapKey
	node, err := start(ctx, cfg, d.netlog)
	if err != nil {
		return nil, err
	}
	return d.bind(ctx, e, node)
}

func (d *daemon) bind(ctx context.Context, e *enrollment, node *connector.Node) (*connector.Node, error) {
	st, err := node.Status(ctx)
	if err == nil && e.NodeKey != "" && st.Self.NodeKey != e.NodeKey {
		err = fmt.Errorf("%w: the network identity changed", errRecovery)
	}
	if err == nil && e.NodeKey == "" {
		var r struct{ State string }
		if err = d.post(ctx, "network/bind", map[string]string{"node_key": st.Self.NodeKey}, &r); err == nil {
			e.NodeKey = st.Self.NodeKey
			err = d.device.writeJSON("enrollment.json", e)
		} else if ce := (*cloudError)(nil); errors.As(err, &ce) && ce.Code == "network_rebind_required" {
			err = fmt.Errorf("%w: %v", errRecovery, err)
		}
	}
	if err != nil {
		node.Close()
		return nil, err
	}
	return node, nil
}

// registerHost registers the SSH host key and OS user once.
func (d *daemon) registerHost(ctx context.Context, e *enrollment) error {
	key := strings.TrimSpace(string(ssh.MarshalAuthorizedKey(d.signer.PublicKey())))
	var r struct{ State string }
	err := d.post(ctx, "code/host", map[string]string{"host_key": key, "os_user": d.user}, &r)
	if ce := (*cloudError)(nil); errors.As(err, &ce) && slices.Contains([]string{"code_host_changed", "invalid_host_key", "invalid_os_user", "invalid_fields"}, ce.Code) {
		return fmt.Errorf("%w: %s", errRecovery, ce.Code) // retrying never helps
	}
	if err != nil {
		return err
	}
	e.Host = true
	return d.device.writeJSON("enrollment.json", e)
}

// watch takes this computer offline within a second of Desktop's Disconnect,
// a sign-out or any other Connector stop: it closes every SSH connection into
// and out of it, with their channels and forwards, and keeps lifetimes, so
// detached jobs run on and later sessions find them. New admissions check
// Admission themselves, so they are refused at once.
func (d *daemon) watch(ctx context.Context) {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		d.log.Expire(time.Now())
		if d.authority.Recheck() != nil {
			d.mu.Lock()
			d.log.Close("offline", nil)
			var closing []io.Closer
			for c := range d.conns {
				closing = append(closing, c.ssh)
			}
			for s := range d.streams {
				closing = append(closing, s)
			}
			clear(d.conns)
			clear(d.streams)
			d.mu.Unlock()
			for _, c := range closing {
				c.Close()
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// connected reports whether an SSH connection into or out of this computer is open.
func (d *daemon) connected() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.conns)+len(d.streams) > 0
}

// lease returns the current lease for new Code work while this computer is
// online; a refusal names the side that refuses.
func (d *daemon) lease(where string) (*Lease, error) {
	err := d.id.Admission()
	var l *Lease
	if err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), admitWait)
		defer cancel()
		if l, err = d.authority.Wait(ctx); err == nil {
			err = d.id.Admission() // this computer may have gone offline meanwhile
		}
	}
	switch {
	case err == nil:
		return l, nil
	case errors.Is(err, ErrOffline):
		return nil, disconnected(where)
	case errors.Is(err, ErrUnavailable):
		return nil, fmt.Errorf("%s cannot reach Cloud, so it refuses new SSH; existing sessions continue", where)
	case errors.Is(err, ErrNoAuthority):
		return nil, fmt.Errorf("%s has no Code authority", where)
	}
	return nil, fmt.Errorf("the Code authority of %s is not current yet; try again", where)
}

func disconnected(where string) error {
	return fmt.Errorf("%s is disconnected from Open Machine", where)
}

// listenProxy opens the daemon's owner-only socket for local ProxyCommands.
func (d *daemon) listenProxy() (*net.UnixListener, error) {
	path := d.state.path(socketName)
	if fi, err := os.Lstat(path); err == nil && fi.Mode()&os.ModeSocket != 0 {
		os.Remove(path) // a previous daemon's; this one holds the state lock
	}
	ln, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err == nil {
		err = os.Chmod(path, 0o600)
	}
	return ln, err
}

// serveProxy answers "dial HOST" with "ok" and the SSH stream, or "error
// REASON", "status" with one JSON line, "folders" followed by a JSON object up
// to EOF with Core's folder check, and "watch" with the account view as one
// JSON line now and one after every change, until the client closes, for
// processes of this user only.
func (d *daemon) serveProxy(ln *net.UnixListener) {
	for {
		uc, err := ln.AcceptUnix()
		if errors.Is(err, net.ErrClosed) {
			return
		} else if err != nil {
			time.Sleep(100 * time.Millisecond) // such as too many open files: keep serving
			continue
		}
		go func() {
			defer uc.Close()
			if uid, err := peerUID(uc); err != nil || uid != uint32(os.Getuid()) {
				return
			}
			uc.SetDeadline(time.Now().Add(30 * time.Second))
			r := bufio.NewReaderSize(uc, 512)
			b, err := r.ReadSlice('\n') // at most 512 bytes
			line := strings.TrimSuffix(string(b), "\n")
			if err == nil && line == "folders" {
				uc.Write(d.folders(uc, r))
				return
			}
			if err == nil && line == "status" {
				reply := map[string]any{"connected": true, "in_use": d.connected()}
				if reason := d.authority.Status(); reason != "" {
					reply["connected"], reply["reason"] = false, reason
				}
				json.NewEncoder(uc).Encode(reply)
				return
			}
			if err == nil && line == "watch" {
				uc.SetDeadline(time.Time{})
				gone := make(chan struct{})
				go func() { io.Copy(io.Discard, r); close(gone) }()
				var last []byte
				for {
					view, changed := d.authority.View()
					if !bytes.Equal(view, last) {
						if _, err := uc.Write(view); err != nil {
							return
						}
						last = view
					}
					select {
					case <-changed:
					case <-gone:
						return
					}
				}
			}
			host, ok := strings.CutPrefix(line, "dial ")
			if err != nil || !ok {
				return
			}
			target, err := d.dial(host)
			if err != nil {
				fmt.Fprintf(uc, "error %s\n", err)
				return
			}
			d.mu.Lock()
			d.streams[target] = struct{}{} // so going offline closes it
			d.mu.Unlock()
			defer func() {
				d.mu.Lock()
				delete(d.streams, target)
				d.mu.Unlock()
			}()
			// Registered first, so going offline during the dial is seen here or by watch.
			if d.id.Admission() != nil {
				target.Close()
				fmt.Fprintf(uc, "error %s\n", disconnected("this computer"))
				return
			}
			uc.SetDeadline(time.Time{})
			if _, err := io.WriteString(uc, "ok\n"); err != nil {
				target.Close()
				return
			}
			splice(uc, target, proxyGrace)
		}()
	}
}

// folders runs Core's folder check on the JSON object r holds up to EOF,
// after any other check has ended. From then on uc has Core's time and one
// second to answer, so a check that waited still gets its answer. It needs no
// lease: it is local and reads only metadata. Any failure answers
// folders_unavailable.
func (d *daemon) folders(uc *net.UnixConn, r io.Reader) []byte {
	request, err := io.ReadAll(io.LimitReader(r, maxFolders+1))
	if err == nil && len(request) <= maxFolders {
		d.foldersMu.Lock()
		uc.SetDeadline(time.Now().Add(foldersTimeout + time.Second))
		var out json.RawMessage
		out, err = d.core.Folders(request)
		d.foldersMu.Unlock()
		if err == nil {
			return append(out, '\n')
		}
	}
	return []byte(`{"error":{"code":"folders_unavailable"}}` + "\n")
}

// Folders is a Linux machine's own check of l's Workspace folders: nil, or the
// first refusal. That is Core's, or folder_changed for a folder whose path is
// not the kernel's, which admission refuses, or folders_unavailable when Core
// gave no answer. A Mac runs no check here: its
// folders are checked in the app before they are saved.
func (d *daemon) Folders(l Lease) *Refusal {
	if runtime.GOOS != "linux" || l.Target == nil {
		return nil
	}
	p, err := parsePolicy(l.Target.Policy)
	if err != nil || p.Mode != "workspace" {
		return nil
	}
	request, _ := json.Marshal(map[string]any{"folders": p.Folders})
	d.foldersMu.Lock()
	out, err := d.core.Folders(request)
	d.foldersMu.Unlock()
	var reply struct {
		Folders []string
		Error   *struct {
			Code  string
			Index *int
		}
	}
	if err == nil {
		err = json.Unmarshal(out, &reply)
	}
	switch e := reply.Error; {
	case err == nil && e != nil && policyState.MatchString(e.Code) && e.Index != nil && *e.Index >= 0 && *e.Index < len(p.Folders):
		return &Refusal{e.Code, *e.Index}
	case err == nil && e == nil && len(reply.Folders) == len(p.Folders):
		for i, folder := range p.Folders {
			if reply.Folders[i] != folder {
				return &Refusal{"folder_changed", i}
			}
		}
		return nil
	}
	return &Refusal{"folders_unavailable", 0}
}

// dial connects to a device this one may reach, by an alias its lease's hosts
// give it, at its verified node key: an unknown, revoked or offline
// target fails, with no DNS or local fallback, and fails at once while Cloud
// is known to be unavailable.
func (d *daemon) dial(host string) (*connector.Conn, error) {
	name, ok := strings.CutSuffix(strings.ToLower(host), ".om")
	if !ok {
		return nil, fmt.Errorf("%s is not an Open Machine host", host)
	}
	l, err := d.lease("this computer")
	if err != nil {
		return nil, err
	}
	if name == "" { // the configuration's catch-all
		return nil, errors.New("that .om name is not a machine this computer can use")
	}
	i := slices.IndexFunc(l.Hosts, func(h Host) bool { return slices.Contains(h.Aliases, name) })
	if i < 0 {
		return nil, fmt.Errorf("%s is not a machine this computer can use", host)
	}
	node := d.node.Load()
	if node == nil {
		return nil, errors.New("the Code service is still joining the private network")
	}
	target := l.Hosts[i]
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	st, err := node.Status(ctx)
	if err != nil {
		return nil, err
	}
	var addr netip.Addr
	for _, p := range st.Peers {
		if p.NodeKey == target.NodeKey {
			for _, a := range p.Addrs {
				if !addr.IsValid() || a.Is4() {
					addr = a
				}
			}
		}
	}
	if !addr.IsValid() {
		return nil, fmt.Errorf("%s is %w", host, ErrUnreachable)
	}
	c, err := node.Dial(ctx, netip.AddrPortFrom(addr, codePort))
	if err != nil {
		return nil, fmt.Errorf("%s is %w: %v", host, ErrUnreachable, err)
	}
	if c.Peer().NodeKey != target.NodeKey {
		c.Close()
		return nil, fmt.Errorf("%s answered with another network identity", host)
	}
	return c, nil
}

// cloudError is a request Cloud refused.
type cloudError struct {
	Status        int
	Code, Message string
}

func (e *cloudError) Error() string {
	return fmt.Sprintf("Cloud: %s (%d): %s", e.Code, e.Status, e.Message)
}

// post makes one signed om-device-v1 request under a fresh challenge.
func (d *daemon) post(ctx context.Context, action string, body, out any) error {
	return signedPost(ctx, CloudClient, d.id.Origin, d.id.DeviceID, d.id.Key, action, body, out)
}

// signedPost makes one signed om-device-v1 POST of the device's under a fresh
// challenge, within 30 seconds, and decodes its reply or Cloud's refusal.
func signedPost(ctx context.Context, client *http.Client, origin, deviceID string, key ed25519.PrivateKey, action string, body, out any) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	path := "/api/devices/" + deviceID + "/" + action
	h, err := proof(ctx, client, origin, deviceID, key, "POST", path, raw)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", origin+path, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header = h
	req.Header.Set("Content-Type", "application/json")
	res, err := client.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	b, err := io.ReadAll(io.LimitReader(res.Body, maxState))
	if err != nil {
		return err
	}
	if res.StatusCode != http.StatusOK {
		var e struct{ Error, Message string }
		json.Unmarshal(b, &e)
		return &cloudError{res.StatusCode, e.Error, e.Message}
	}
	return json.Unmarshal(b, out)
}
