// Package daemon is Open Machine's device-side Code integration.
package daemon

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"maps"
	"net/http"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

// Sign returns the om-device-v1 proof Cloud verifies: the device key over the
// origin, method, path, challenge and SHA-256 of the exact body bytes.
func Sign(key ed25519.PrivateKey, origin, method, path, challengeID, challenge string, body []byte) string {
	sum := sha256.Sum256(body)
	msg := strings.Join([]string{"om-device-v1", origin, method, path, challengeID, challenge, hex.EncodeToString(sum[:])}, "\n")
	return base64.RawURLEncoding.EncodeToString(ed25519.Sign(key, []byte(msg)))
}

// Lease is this device's Code authority from Cloud.
type Lease struct {
	Epoch    int64    `json:"epoch"`    // the Code presence it was issued to
	Revision string   `json:"revision"` // identifies Target, Sources and Hosts
	Target   *Target  `json:"target"`   // nil: the device has no Code authority, and all its Code connections close
	Sources  []Source `json:"sources"`  // the devices that may connect to this one
	Hosts    []Host   `json:"hosts"`    // the devices this one may connect to
}

// Target is this device's own authority. Code connections admitted under
// another grant revision or policy close; a change of Rules closes nothing,
// and the next session is checked under the lease current then.
type Target struct {
	GrantID       string          `json:"grant_id"`
	GrantRevision int64           `json:"grant_revision"`
	Policy        json.RawMessage `json:"policy"` // Cloud's device policy, or null when unreadable
	Rules         *Rules          `json:"rules"`  // what the account blocks here; nil from a Cloud without rules
	// Record is null, or {"notice": …} where the account records the commands
	// run here; a Cloud without logs sends none.
	Record json.RawMessage `json:"record,omitempty"`
}

// notice returns what a session is told where the account records commands,
// "" where it records none, or an error for a record om cannot read.
func (t *Target) notice() (string, error) {
	var r *struct {
		Notice string `json:"notice"`
	}
	if len(t.Record) == 0 {
		return "", nil
	}
	if err := strict(t.Record, &r); err != nil || r == nil {
		return "", err
	}
	return r.Notice, nil
}

// Source is a device that may connect to this one. Its Code connections
// admitted under another grant revision or limit close, as do those of a
// source no longer listed.
type Source struct {
	DeviceID      string   `json:"device_id"`
	NodeKey       string   `json:"node_key"`
	GrantRevision int64    `json:"grant_revision"`
	Allow         []string `json:"allow"`           // what it gets besides its sessions: github, their GitHub routing; models, this machine's AI models
	Limit         string   `json:"limit,omitempty"` // read_only: its sessions run Read only, whatever the target's policy; models: it gets the AI models only
	Home          string   `json:"home,omitempty"`  // the key of its owner's sandbox home here, for Core; "" for the machine's own
}

// Host is a device this one may connect to, at its registered SSH host.
type Host struct {
	DeviceID string   `json:"device_id"`
	NodeKey  string   `json:"node_key"`
	Key      string   `json:"key"`     // OpenSSH public key
	User     string   `json:"user"`    // the OS user that runs Open Machine there
	Aliases  []string `json:"aliases"` // the first is canonical
}

// ViewDevice is a device as the account view lists it: one of the account, or
// a team's machine this device's owner reaches. It is display data: nothing is
// admitted or closed by it.
type ViewDevice struct {
	DeviceID    string  `json:"device_id"`
	Name        string  `json:"name"`
	Mode        *string `json:"mode"`
	Alias       *string `json:"alias"` // its display alias, else its canonical one, else nil
	State       string  `json:"state"`
	LastSeenAt  *int64  `json:"last_seen_at"`
	CoreVersion *string `json:"core_version"`
	Team        *struct {
		Slug string `json:"slug"`
		Name string `json:"name"`
	} `json:"team,omitempty"` // of a team's machine
}

// Device is the device side of an Authority.
type Device interface {
	// Apply closes the connections l no longer permits, with their channels
	// and forwards, before it returns; Lease.Target nil closes them all. It
	// ends no process: Code is a connection layer.
	Apply(l Lease)
	// Admission reports whether this computer is online: nil, or why not.
	Admission() error
	// Folders is this machine's own check of l's Workspace folders, once l is
	// applied: nil, or its refusal. It may take as long as Core's check.
	Folders(l Lease) *Refusal
	// Update starts this machine's own update now, as its owner asked in the
	// Console, and reports whether it started.
	Update() bool
	// Move starts this machine's move to another account of the person who
	// asked for it in the Console, with the one-time secret Cloud issued, and
	// reports whether it started.
	Move(secret string) bool
}

// Refusal is a machine's refusal of the Workspace folder at Index of its
// lease, with Core's code.
type Refusal struct {
	Code  string `json:"code"`
	Index int    `json:"index"`
}

var (
	// ErrNoAuthority reports that Cloud grants this device no Code authority.
	ErrNoAuthority = errors.New("native: this device has no Code authority")
	// ErrUnavailable reports that Cloud cannot be reached: existing Code
	// sessions continue, but nothing new is admitted until it can.
	ErrUnavailable = errors.New("native: Cloud is unavailable; existing Code sessions only")
	// errUpdateRequired reports that Cloud refused this om as too old.
	errUpdateRequired = errors.New("native: Cloud requires a newer Open Machine")
)

const (
	maxLease = 60 * time.Second // Cloud may shorten a lease, never lengthen it
	renewal  = 20 * time.Second // also how long a renewal may go unanswered
	retry    = 5 * time.Second
	maxFrame = 1 << 20 // a lease lists every device its view, sources and hosts name
)

var (
	coreVersion = regexp.MustCompile(`^[0-9A-Za-z.+-]{1,32}$`)
	policyState = regexp.MustCompile(`^[a-z_]{1,64}$`)
	updateID    = regexp.MustCompile(`^[0-9a-f]{32}$`)
	moveSecret  = regexp.MustCompile(`^ommove_[A-Za-z0-9_-]{43}$`)
)

// Authority holds this device's Code lease. A new Code
// admission needs a current lease from a live Cloud session. Losing Cloud, or
// a lease running out, only stops new admissions: what was admitted keeps
// running until an applied lease closes it. Each lease runs from when its
// renewal was sent, so a late reply never extends it, and replies apply in the
// order sent. A changed lease admits nothing new until it is applied, and is
// then acknowledged.
//
// Each renewal reports whether this computer can serve Code, Core's version,
// the modes Core can enforce and the machine's listening programs. Leases also
// carry the account view, which is held in memory only.
type Authority struct {
	origin, deviceID string
	key              ed25519.PrivateKey
	device           Device
	log              *Log
	client           *http.Client
	renew            chan struct{} // asks the session to renew now
	waiting          chan struct{} // an admission waits: renew unless a renewal is unanswered
	refused          chan struct{} // Cloud refused a connection attempt as too old
	checking         sync.Mutex    // one applied lease's folders are checked at a time

	mu        sync.Mutex
	live      bool              // a Cloud session is open
	outdated  bool              // Cloud refused the last connection attempt as too old
	lease     *Lease            // admits new work; nil outside a session and while a change applies
	start     time.Time         // when the renewal behind lease was sent
	ttl       time.Duration     // of lease
	applied   string            // epoch and revision last applied
	changed   chan struct{}     // closed when the lease or the session changes
	listening bool              // the SSH host listens on the private network
	joining   string            // until then, why the last step toward it failed; "" while none has
	online    bool              // listening, and Admission passed when last read
	version   *string           // Core's version, nil until Core answers
	policies  map[string]string // each mode as Core reports it: available or why not; nil until Core answers
	services  []Service         // this machine's listening programs; nil until the first pass

	leased  bool            // the open session holds a lease
	account json.RawMessage // the last lease's account, kept after its session
	policy  json.RawMessage // the last lease's target policy
	devices []ViewDevice    // the last lease's devices, with presence frames applied
	viewed  chan struct{}   // closed when the view may have changed
}

// NewAuthority returns the Code authority of the device registered under
// deviceID with key, at the Cloud origin, served by d. It starts log at the
// first lease of a Cloud that takes it, and sends it over every presence.
func NewAuthority(origin, deviceID string, key ed25519.PrivateKey, d Device, log *Log, client *http.Client) *Authority {
	return &Authority{origin: origin, deviceID: deviceID, key: key, device: d, log: log, client: client,
		renew: make(chan struct{}, 1), waiting: make(chan struct{}, 1), refused: make(chan struct{}, 1), changed: make(chan struct{}),
		devices: []ViewDevice{}, viewed: make(chan struct{})}
}

// Listening records that the SSH host listens on the private network.
func (a *Authority) Listening() {
	a.mu.Lock()
	a.listening, a.joining = true, ""
	a.mu.Unlock()
	a.Recheck()
}

// Joining records why the last step toward listening failed, as a status
// reason; "" while the next step runs without one.
func (a *Authority) Joining(reason string) {
	a.mu.Lock()
	a.joining = reason
	a.mu.Unlock()
}

// Recheck reads the device's Admission, renews at once when that changes
// whether this computer can serve Code, and returns it.
func (a *Authority) Recheck() error {
	err := a.device.Admission()
	a.mu.Lock()
	defer a.mu.Unlock()
	if online := a.listening && err == nil; online != a.online {
		a.online = online
		a.ask()
	}
	return err
}

// Core records Core's version and the modes it can enforce, renewing at once
// when either changes. A version that is not 1 to 32 letters, digits, '.', '+'
// or '-', or policies that are not the three modes' states, are reported as
// unknown.
func (a *Authority) Core(v string, policies map[string]string) {
	var version *string
	if coreVersion.MatchString(v) {
		version = &v
	}
	for _, mode := range []string{"read_only", "workspace", "full_access"} {
		if len(policies) != 3 || !policyState.MatchString(policies[mode]) {
			policies = nil
		}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if (version == nil) != (a.version == nil) || version != nil && *version != *a.version || !maps.Equal(policies, a.policies) {
		a.version, a.policies = version, policies
		a.ask()
	}
}

// Services records this machine's listening programs, renewing at once when
// they change.
func (a *Authority) Services(list []Service) {
	if list == nil {
		list = []Service{}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !reflect.DeepEqual(list, a.services) { // never nil, as a.services is before the first pass
		a.services = list
		a.ask()
	}
}

// Status reports whether this computer is connected to Open Machine Code:
// "" when it is, else the first reason it is not:
// offline; joining, or while the network is not joined why the last step
// failed (see joinReason); update_required (Cloud refused this om as too old);
// cloud_unavailable or no_authority.
func (a *Authority) Status() string {
	err := a.device.Admission()
	a.mu.Lock()
	defer a.mu.Unlock()
	switch {
	case err != nil:
		return "offline"
	case !a.listening && a.joining != "":
		return a.joining
	case !a.listening:
		return "joining"
	case a.outdated:
		return "update_required"
	case !a.live:
		return "cloud_unavailable"
	case a.current(time.Now()) == nil:
		return "no_authority"
	}
	return ""
}

// Refused receives after each connection attempt Cloud refused as too old.
func (a *Authority) Refused() <-chan struct{} { return a.refused }

// Current returns the lease that admits new Code sessions now, else nil.
func (a *Authority) Current() *Lease {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.current(time.Now())
}

// current requires a.mu. Monotonic time stops while a Mac sleeps, so the wall
// clock bounds the lease too; either one ends it.
func (a *Authority) current(now time.Time) *Lease {
	if a.lease == nil || a.lease.Target == nil || now.Sub(a.start) >= a.ttl || now.Round(0).Sub(a.start.Round(0)) >= a.ttl {
		return nil
	}
	l := *a.lease
	return &l
}

// Wait admits a new Code session from the lease held here, without contacting
// Cloud, while it lasts. Without one it fails at once if Cloud is unavailable;
// otherwise it asks Cloud for a renewal, as after a wake, and waits up to ctx.
func (a *Authority) Wait(ctx context.Context) (*Lease, error) {
	a.mu.Lock()
	changed := a.changed
	l, err := a.admit()
	a.mu.Unlock()
	if err != ErrNoAuthority {
		return l, err
	}
	select {
	case a.waiting <- struct{}{}:
	default:
	}
	select {
	case <-changed:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.admit()
}

// admit requires a.mu.
func (a *Authority) admit() (*Lease, error) {
	if l := a.current(time.Now()); l != nil {
		return l, nil
	}
	if !a.live {
		return nil, ErrUnavailable
	}
	return nil, ErrNoAuthority
}

// set changes the session or the lease, waking Wait and View's readers; it
// requires a.mu.
func (a *Authority) set(live bool, l *Lease, start time.Time, ttl time.Duration) {
	a.live, a.lease, a.start, a.ttl = live, l, start, ttl
	close(a.changed)
	a.changed = make(chan struct{})
	a.touch()
}

// touch wakes View's readers; it requires a.mu.
func (a *Authority) touch() {
	close(a.viewed)
	a.viewed = make(chan struct{})
}

// View returns the account view as one JSON line and a
// channel closed when it may have changed. Without a session that holds a
// lease the last values stay, and every device's state is unknown.
func (a *Authority) View() ([]byte, <-chan struct{}) {
	a.mu.Lock()
	defer a.mu.Unlock()
	devices := a.devices
	if !a.leased {
		devices = slices.Clone(devices)
		for i := range devices {
			devices[i].State = "unknown"
		}
	}
	b, _ := json.Marshal(map[string]any{"live": a.leased, "device_id": a.deviceID, "account": a.account, "policy": a.policy, "devices": devices})
	return append(b, '\n'), a.viewed
}

// Devices returns the machines of the last lease's view.
func (a *Authority) Devices() []ViewDevice {
	a.mu.Lock()
	defer a.mu.Unlock()
	return slices.Clone(a.devices)
}

func (a *Authority) ask() {
	select {
	case a.renew <- struct{}{}:
	default:
	}
}

// Run holds the lease until ctx ends, reconnecting to Cloud as needed.
func (a *Authority) Run(ctx context.Context) {
	for ctx.Err() == nil {
		if err := a.session(ctx); ctx.Err() == nil {
			log.Printf("native: Code session ended: %v", err)
		}
		sleep(ctx, retry)
	}
}

func sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

// message is one frame from Cloud: a change notice, a lease with the account
// view, the confirmation of a renewal under an unchanged lease, a device's
// presence, a request to update now or to move to another account, or the
// confirmation of log records.
type message struct {
	Type       string            `json:"type"`
	ID         string            `json:"id"`      // of a request to update or move
	Key        string            `json:"key"`     // a move's one-time secret
	Queue      string            `json:"queue"`   // of confirmed log records
	Through    int64             `json:"through"` // the last place confirmed
	Seq        int64             `json:"seq"`
	TTLms      int64             `json:"ttl_ms"`
	Epoch      int64             `json:"epoch"`
	Revision   string            `json:"revision"`
	Target     json.RawMessage   `json:"target"`
	Sources    []json.RawMessage `json:"sources"`
	Hosts      []json.RawMessage `json:"hosts"`
	Account    json.RawMessage   `json:"account"`
	Devices    []ViewDevice      `json:"devices"`
	ViewDevice                   // of a presence frame
}

// lease reads m's lease, taking nothing it does not understand as permission:
// a source or host with an unknown field, or a source with an unknown limit, is
// dropped, and a target with an unknown field, or rules it cannot read, is one
// whose policy Cloud could not read, which admits nothing new and closes
// nothing, as is one whose record it cannot read. Unknown names in a source's
// allow stay unused.
func (m message) lease() (Lease, error) {
	l := Lease{Epoch: m.Epoch, Revision: m.Revision}
	if len(m.Target) > 0 {
		unreadable := strict(m.Target, &l.Target) != nil
		if l.Target != nil && !unreadable {
			_, err := l.Target.notice()
			unreadable = err != nil || !l.Target.Rules.readable()
		}
		if unreadable {
			if err := json.Unmarshal(m.Target, &l.Target); err != nil {
				return l, err
			}
			l.Target.Policy = nil
		}
	}
	for _, raw := range m.Sources {
		var s Source
		if strict(raw, &s) == nil && (s.Limit == "" || s.Limit == "read_only" || s.Limit == "models") {
			l.Sources = append(l.Sources, s)
		}
	}
	for _, raw := range m.Hosts {
		var h Host
		if strict(raw, &h) == nil {
			l.Hosts = append(l.Hosts, h)
		}
	}
	return l, nil
}

// strict decodes raw into v, refusing a field v does not have.
func strict(raw []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// session runs one Code presence: it renews every 20 seconds, at once when
// Cloud announces a change or Wait asks, and applies replies as they come. A
// renewal unanswered for 20 seconds ends the session, so a silently dead
// connection is replaced. Its lease ends with it. It returns only after its
// reader has stopped.
func (a *Authority) session(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	conn, err := a.dial(ctx)
	a.mu.Lock()
	a.outdated = errors.Is(err, errUpdateRequired)
	a.mu.Unlock()
	if a.outdated {
		select {
		case a.refused <- struct{}{}:
		default:
		}
	}
	if err != nil {
		cancel()
		return err
	}
	conn.SetReadLimit(maxFrame)
	a.mu.Lock()
	a.set(true, nil, time.Time{}, 0)
	a.mu.Unlock()
	var (
		mu   sync.Mutex
		sent = map[int64]time.Time{} // renewals awaiting a reply
		seq  int64
		read = make(chan error, 1)
		done = make(chan struct{})
	)
	defer func() {
		cancel()
		conn.CloseNow()
		<-done
		a.mu.Lock()
		a.leased = false
		a.set(false, nil, time.Time{}, 0)
		a.mu.Unlock()
	}()
	go func() {
		defer close(done)
		read <- a.read(ctx, conn, &mu, sent)
	}()
	go a.upload(ctx, conn)
	periodic := time.NewTimer(0)
	defer periodic.Stop()
	var last, due time.Time // when a renewal was last sent; when periodic fires
	for {
		mu.Lock()
		left := renewal // until the oldest renewal goes unanswered; a later one never extends it
		for _, at := range sent {
			left = min(left, renewal-time.Since(at))
		}
		mu.Unlock()
		if left <= 0 {
			return errors.New("native: Code renewal unanswered")
		}
		select {
		case err := <-read:
			return err
		case <-time.After(left):
			continue
		case <-a.renew:
		case <-periodic.C:
		case <-a.waiting:
			mu.Lock()
			answered := len(sent) == 0
			mu.Unlock()
			if !answered {
				continue // its answer wakes every waiter
			}
			if next := last.Add(time.Second); time.Now().Before(next) { // admissions renew at most once a second
				if next.Before(due) {
					due = next
					periodic.Reset(time.Until(due))
				}
				continue
			}
		}
		mu.Lock()
		seq++
		sent[seq] = time.Now()
		mu.Unlock()
		a.mu.Lock()
		renew := map[string]any{"type": "renew", "seq": seq, "online": a.online, "core_version": a.version, "policies": a.policies, "services": a.services}
		a.mu.Unlock()
		if err := wsjson.Write(ctx, conn, renew); err != nil {
			return err
		}
		periodic.Reset(renewal)
		last = time.Now()
		due = last.Add(renewal)
	}
}

// upload sends the log over one presence: a frame at a
// time, from the first place Cloud has not confirmed, and again when Cloud
// leaves it unanswered for 30 seconds. A frame travels as the queue holds it:
// escaping its records again would grow it past its bound.
func (a *Authority) upload(ctx context.Context, conn *websocket.Conn) {
	var from int64   // where the frame awaiting its answer starts
	var at time.Time // when it was sent
	for {
		f, changed := a.log.Frame()
		if f != nil && (f.From != from || time.Since(at) >= logResend) {
			var frame bytes.Buffer
			enc := json.NewEncoder(&frame)
			enc.SetEscapeHTML(false)
			if enc.Encode(f) != nil || conn.Write(ctx, websocket.MessageText, frame.Bytes()) != nil {
				return
			}
			from, at = f.From, time.Now()
		}
		select {
		case <-ctx.Done():
			return
		case <-changed:
		case <-time.After(logResend):
		}
	}
}

// read applies Cloud's frames until the connection fails. sent holds the
// renewals awaiting a reply, under mu.
func (a *Authority) read(ctx context.Context, conn *websocket.Conn, mu *sync.Mutex, sent map[int64]time.Time) error {
	for {
		var m message
		if err := wsjson.Read(ctx, conn, &m); err != nil {
			return err
		}
		a.mu.Lock()
		leased := a.leased
		// A presence of a device the view lists; the lease that adds another already holds its state.
		if i := slices.IndexFunc(a.devices, func(d ViewDevice) bool { return d.DeviceID == m.DeviceID }); m.Type == "presence" && i >= 0 {
			a.devices[i].State, a.devices[i].LastSeenAt, a.devices[i].CoreVersion = m.State, m.LastSeenAt, m.CoreVersion
			a.touch()
		}
		a.mu.Unlock()
		switch m.Type {
		case "changed":
			a.ask()
			continue
		case "presence":
			continue
		case "logged":
			a.log.Logged(m.Queue, m.Through)
			continue
		case "update":
			// Acted on at once or never: only while this computer is online under a
			// current lease, and confirmed only once the update started.
			if updateID.MatchString(m.ID) && a.device.Admission() == nil && a.Current() != nil && a.device.Update() {
				wsjson.Write(ctx, conn, map[string]string{"type": "updating", "id": m.ID})
			}
			continue
		case "move":
			// As an update: at once or never, and confirmed only once the move started.
			if updateID.MatchString(m.ID) && moveSecret.MatchString(m.Key) && a.device.Admission() == nil && a.Current() != nil && a.device.Move(m.Key) {
				wsjson.Write(ctx, conn, map[string]string{"type": "moving", "id": m.ID})
			}
			continue
		}
		// A lease lists the view's devices; a renewal is confirmed only under a lease of this session.
		if m.TTLms < 1 || !(m.Type == "lease" && m.Epoch >= 1 && m.Revision != "" && m.Devices != nil || m.Type == "renewed" && leased) {
			return fmt.Errorf("native: unexpected Code frame %q", m.Type)
		}
		mu.Lock()
		start, ok := sent[m.Seq]
		for s := range sent {
			if s <= m.Seq {
				delete(sent, s) // an older reply arriving later never applies
			}
		}
		mu.Unlock()
		if !ok {
			continue
		}
		ttl := min(time.Duration(m.TTLms)*time.Millisecond, maxLease)
		if m.Type == "renewed" { // the lease held runs on from this renewal: nothing to apply or acknowledge
			a.mu.Lock()
			a.set(true, a.lease, start, ttl)
			a.mu.Unlock()
			continue
		}
		l, err := m.lease()
		if err != nil {
			return err
		}
		if a.apply(l, m, start, ttl) { // acknowledged only after it closed what it must
			go a.acknowledge(ctx, conn, l)
		}
	}
}

// acknowledge tells Cloud that l was applied, with the device's check of its
// folders. That check may outlast a renewal, so it runs beside the reader: one
// at a time, and a lease that another replaced meanwhile is neither checked nor
// acknowledged. A failed write shows in the reader.
func (a *Authority) acknowledge(ctx context.Context, conn *websocket.Conn, l Lease) {
	applied := func() bool {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.applied == fmt.Sprint(l.Epoch, "/", l.Revision)
	}
	a.checking.Lock()
	defer a.checking.Unlock()
	if !applied() {
		return
	}
	if folders := a.device.Folders(l); applied() {
		wsjson.Write(ctx, conn, map[string]any{"type": "applied", "revision": l.Revision, "folders": folders})
	}
}

// apply has the device close what l, m's lease, no longer permits, unless this
// epoch and revision were already applied, and then admits under it from
// start and holds m's account view. It reports whether the lease was applied
// now, and so is to be acknowledged.
func (a *Authority) apply(l Lease, m message, start time.Time, ttl time.Duration) bool {
	key := fmt.Sprint(l.Epoch, "/", l.Revision)
	a.mu.Lock()
	changed := a.applied != key
	if changed {
		a.lease = nil  // nothing new is admitted while a change applies: Wait keeps waiting
		a.applied = "" // and the lease it replaces is neither checked nor acknowledged
	}
	a.mu.Unlock()
	if changed {
		if l.Target != nil && l.Target.Record != nil {
			a.log.Start() // before its connections are closed, so their ends are recorded
		}
		a.device.Apply(l)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.applied = key
	a.leased, a.account, a.policy, a.devices = true, m.Account, nil, m.Devices
	if l.Target != nil {
		a.policy = l.Target.Policy
	}
	a.set(true, &l, start, ttl)
	return changed
}

// dial opens a Code presence with a fresh signed proof, within one renewal
// interval. Cloud answers an om of another Code protocol 426.
func (a *Authority) dial(ctx context.Context) (*websocket.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, renewal)
	defer cancel()
	path := "/api/devices/" + a.deviceID + "/code"
	h, err := proof(ctx, a.client, a.origin, a.deviceID, a.key, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	h.Set("X-OM-Protocol", "4") // Cloud refuses any other Code protocol
	conn, res, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(a.origin, "http")+path, &websocket.DialOptions{HTTPClient: a.client, HTTPHeader: h})
	if err != nil && res != nil && res.StatusCode == http.StatusUpgradeRequired {
		err = errUpdateRequired
	}
	return conn, err
}

// proof returns the headers of a fresh om-device-v1 proof by the device's key
// for method, path and the exact body bytes.
func proof(ctx context.Context, client *http.Client, origin, deviceID string, key ed25519.PrivateKey, method, path string, body []byte) (http.Header, error) {
	req, err := http.NewRequestWithContext(ctx, "POST", origin+"/api/devices/"+deviceID+"/challenge", strings.NewReader("{}"))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	var ch struct {
		ID             string `json:"challenge_id"`
		Challenge      string `json:"challenge"`
		Error, Message string // a refusal, such as the per-address limit
	}
	err = json.NewDecoder(io.LimitReader(res.Body, 4096)).Decode(&ch)
	res.Body.Close()
	if res.StatusCode != http.StatusOK && ch.Error != "" {
		return nil, &cloudError{res.StatusCode, ch.Error, ch.Message}
	}
	if res.StatusCode != http.StatusOK || err != nil {
		return nil, fmt.Errorf("native: challenge: %s: %v", res.Status, err)
	}
	return http.Header{"X-Om-Challenge-Id": {ch.ID}, "X-Om-Challenge": {ch.Challenge},
		"X-Om-Signature": {Sign(key, origin, method, path, ch.ID, ch.Challenge, body)}}, nil
}
