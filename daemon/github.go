package daemon

import (
	"bufio"
	"bytes"
	"cmp"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"maps"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	// Cloud's declared limits, checked here first.
	maxPush         = 90 << 20
	maxFetchRequest = 16 << 20
	maxGitResult    = 1 << 20
	// operationTTL bounds an idle Git command credential: each request extends it as it starts and
	// ends, so a long command keeps its account, and a finished command's slot frees up after it.
	operationTTL  = 15 * time.Minute
	ticketMargin  = time.Minute
	maxOperations = 4096
	// maxSessionOperations keeps one session from taking every command's slot on this computer.
	maxSessionOperations = maxOperations / 4
	// ticketReuse lets commands in the same scope share a ticket for a minute: each mint takes a
	// device challenge, which Cloud limits per address. A default change applies within it.
	ticketReuse = time.Minute
	maxTickets  = 1024
	// callDeadline bounds one om github call, its ticket included, under the CLI's 90 seconds.
	callDeadline = 75 * time.Second
	// A request that waits for its person's approval is asked about at most once at a time, 20
	// seconds after a failed call began, and the machine's calls stay within 20 a minute.
	waitRetry = 20 * time.Second
	maxWaits  = 20
)

// notRouted is why a session whose source's allow has no github gets nothing from the broker.
const notRouted = "GitHub does not go through Open Machine in this session"

// requestExpired is Cloud's text when nobody approved a request in time, which om shows too.
const requestExpired = "Nobody approved within 5 minutes. Nothing reached GitHub. Run the command again."

// approvalLines is what a person waiting in a terminal is shown.
func approvalLines(number string) string {
	return "Open Machine needs your approval.\nChoose " + number + " in Desktop or the Console.\nWaiting up to 5 minutes…\n"
}

// waitingLine is what a command run again without a terminal, as by an agent that passed the number
// on, shows as it waits.
func waitingLine(number string) string {
	return "Waiting for the user to choose " + number + " in Desktop or the Console…\n"
}

// StillEvery is how often a wait says it still waits, so that an agent's silence timeout never ends
// it and an agent whose command was stopped learns why; tests shorten it.
var StillEvery = time.Minute

// stillLine says a wait still lasts, with until's minutes left.
func stillLine(number string, until time.Time) string {
	return fmt.Sprintf("Still waiting for %s, %d min left. If this command stops, run it again.\n", number,
		(time.Until(until)+time.Minute-1)/time.Minute)
}

// ownerRepo is OWNER/REPO, optionally with .git, capturing both names.
const ownerRepo = `([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?`

var (
	// githubRemote is a github.com remote over HTTPS or SSH: the forms the session routes here.
	githubRemote = regexp.MustCompile(`^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)` + ownerRepo + `/?$`)
	brokerPath   = regexp.MustCompile(`^/github\.com/` + ownerRepo + `/(info/refs|git-upload-pack|git-receive-pack)$`)
	credentialAt = regexp.MustCompile(`^github\.com/` + ownerRepo + `/?$`)
	requestID    = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

// GitSession is what one admitted SSH session may do with GitHub: the binding and policy it was
// admitted under, and the om it runs. Live reports whether it may start new GitHub work, and GitHub
// whether its source's allow has github now; Done closes when its connection ends, which stops its
// requests in flight.
type GitSession struct {
	Source              string
	SourceGrantRevision int64
	TargetGrant         string
	TargetGrantRevision int64
	PolicyRevision      int64
	Mode                string // read_only, workspace or full_access
	Visit               string // the session's connection in the log, which Cloud's records of its Git name
	Client              string // the om the session runs as its credential helper
	Live                func() error
	GitHub              func() bool
	Done                <-chan struct{}
}

type gitSession struct {
	GitSession
	closed   chan struct{}
	route    string // its routing file
	proof    string // what a Full Access session also sends, read from the file only unsandboxed work can read
	proofErr error  // why that file could not be written
}

// errProofRequired asks a Full Access session for its proof.
var errProofRequired = errors.New("a Full Access session's GitHub access needs its proof")

func (g *gitSession) live() error {
	select {
	case <-g.closed:
		return errors.New("this SSH session has ended, and its GitHub access with it")
	default:
	}
	if !g.GitHub() {
		return errors.New(notRouted)
	}
	return g.Live()
}

// context is parent, also ended when the session's connection ends: revocation, Disconnect or
// link loss stops its GitHub requests in flight.
func (g *gitSession) context(parent context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(parent)
	go func() {
		select {
		case <-g.Done:
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, cancel
}

// operation is one Git command's or om github call's access to one repository.
type operation struct {
	session     *gitSession
	repository  string // owner/name, lowercase
	actions     []string
	refused     string // why the actions exclude push
	interactive bool   // a person reads the command's output in a terminal, and can wait for an approval
	expires     time.Time

	mu      sync.Mutex
	ticket  string
	until   time.Time
	account *accountPin // pinned by the first ticket: a default change never redirects the command
	team    bool        // its tickets are for a team's machine, whose person may need to approve a push
}

type accountPin struct {
	ID       string `json:"id"`
	GitHubID int64  `json:"github_id"`
	Revision int64  `json:"revision"`
}

// ticketKey is a ticket's scope: the binding, repository and actions it was minted for.
type ticketKey struct{ binding, repository, actions string }

type sharedTicket struct {
	ticket        string
	minted, until time.Time
	account       accountPin
	team          bool
}

// Broker is the target's Code GitHub broker: a loopback endpoint for the Git
// commands and om github calls of admitted SSH sessions, never the daemon's management socket.
// A session holds a capability in its environment; om git-credential exchanges it for one
// command's credential, for one repository and the actions the session's policy allows there.
// Cloud adds the account's token: neither the broker nor the session ever holds one.
//
// A restricted sandbox can read other processes' environments (kern.procargs2), which Seatbelt
// cannot deny. So a Full Access session's capability, the only one that writes, also needs a
// proof the broker keeps in a file named after it under proofs, in the daemon's state, which Core
// denies to restricted work. om reads it there, outside any sandbox, from the directory its
// own routing names, never from a reply: whatever answers at the broker's port gets nothing else.
//
// Any local process, a restricted sandbox too, may listen on the port a stopped broker freed, where
// detached jobs' routing still points. So the broker serves TLS with a key made in memory each time
// it starts, never stored, and every client pins it: Git through its routing, om by the same
// routing. Whatever else answers there, a later broker too, fails the handshake before a request is
// sent, and Git fails closed.
//
// A session's routing is a file Git includes at every command, named in its environment, so that it
// follows the source's allow while the session lasts: the file exists only while that allow has github.
// Restricted work may read the routing folder, never write it; a file holds no secret.
type Broker struct {
	URL              string // https://127.0.0.1:PORT
	Pin              string // sha256//BASE64 of its key, Git's http.pinnedPubkey form
	origin, deviceID string
	key              ed25519.PrivateKey
	client           *http.Client
	srv              *http.Server
	proofs, routes   string

	routing  sync.Mutex // one routing file is written or removed at a time
	mu       sync.Mutex
	sessions map[string]*gitSession // by capability
	ops      map[string]*operation  // by credential
	tickets  map[ticketKey]*sharedTicket
	waits    map[string]*waiting  // by request
	asked    map[string]time.Time // when each request Cloud refused this broker's calls for expires, kept a minute more
	calls    []time.Time          // Cloud wait calls begun in the last minute, oldest first
	queued   []*waiting           // requests held for one of them
	freed    chan struct{}        // closed when one was taken
}

// NewBroker starts the broker of the device registered under deviceID with key, at the Cloud origin,
// keeping Full Access sessions' proofs and pushes that wait for an approval in the owner-only
// directory proofs, emptied first, and sessions' routing in the directory routes, whose files are
// removed. routes itself stays: a restricted lifetime started earlier holds it.
func NewBroker(origin, deviceID string, key ed25519.PrivateKey, client *http.Client, proofs, routes string) (*Broker, error) {
	cert, pin, err := brokerCertificate()
	if err != nil {
		return nil, err
	}
	if err := os.RemoveAll(proofs); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(proofs, 0o700); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(routes, 0o700); err != nil {
		return nil, err
	}
	left, err := os.ReadDir(routes)
	for _, e := range left {
		if err == nil {
			err = os.Remove(filepath.Join(routes, e.Name()))
		}
	}
	if err != nil {
		return nil, err
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	b := &Broker{URL: "https://" + ln.Addr().String(), Pin: pin, origin: origin, deviceID: deviceID, key: key, client: client, proofs: proofs,
		routes: routes, sessions: map[string]*gitSession{}, ops: map[string]*operation{}, tickets: map[ticketKey]*sharedTicket{},
		waits: map[string]*waiting{}, asked: map[string]time.Time{}, freed: make(chan struct{})}
	b.srv = &http.Server{Handler: b, ReadHeaderTimeout: 10 * time.Second, MaxHeaderBytes: 64 << 10, ErrorLog: log.New(io.Discard, "", 0),
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12},
		// HTTP/1.1 only: a refusal may answer before git has sent its whole request, then drain the rest.
		TLSNextProto: map[string]func(*http.Server, *tls.Conn, http.Handler){}}
	go b.srv.ServeTLS(ln, "", "")
	return b, nil
}

// brokerCertificate returns a self-signed certificate for a new P-256 key and the key's pin. Only
// the key is checked: clients pin it.
func brokerCertificate() (tls.Certificate, string, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	now := time.Now()
	template := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Open Machine Code GitHub broker"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.AddDate(10, 0, 0), IPAddresses: []net.IP{net.IPv4(127, 0, 0, 1)},
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	spki, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sum := sha256.Sum256(spki)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, "sha256//" + base64.StdEncoding.EncodeToString(sum[:]), nil
}

// PinnedClient returns a client that completes a TLS handshake only with a server holding the key
// pin names, and checks nothing else of its certificate, as Git does with the routing's pinnedPubkey
// and sslVerify false. It never sends a request in plain text.
func PinnedClient(pin string, timeout time.Duration) *http.Client {
	want, _ := base64.StdEncoding.DecodeString(strings.TrimPrefix(pin, "sha256//"))
	check := func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) > 0 && strings.HasPrefix(pin, "sha256//") && len(want) == sha256.Size {
			if sum := sha256.Sum256(cs.PeerCertificates[0].RawSubjectPublicKeyInfo); subtle.ConstantTimeCompare(sum[:], want) == 1 {
				return nil
			}
		}
		return errors.New("this is not the session's Open Machine Code service: its key is not the one the session's routing pins")
	}
	config := &tls.Config{InsecureSkipVerify: true, VerifyConnection: check, MinVersion: tls.VersionTLS12}
	return &http.Client{Timeout: timeout, Transport: &http.Transport{
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return (&tls.Dialer{Config: config}).DialContext(ctx, network, addr)
		},
		DialContext: func(context.Context, string, string) (net.Conn, error) { // plain routing, from an earlier version
			return nil, errors.New("the Open Machine Code service is reached only over TLS; start a new SSH session")
		},
	}}
}

// Close stops the broker.
func (b *Broker) Close() error { return b.srv.Close() }

// Open registers an admitted session, writing its routing if its source's allow has github, and
// returns its capability, and a function that ends the session's GitHub access, called when the
// session ends.
func (b *Broker) Open(s GitSession) (string, func()) {
	id := make([]byte, 16)
	rand.Read(id)
	g, capability := &gitSession{GitSession: s, closed: make(chan struct{}), route: filepath.Join(b.routes, fmt.Sprintf("%x", id))}, secret()
	file := filepath.Join(b.proofs, capability)
	if s.Mode == "full_access" {
		g.proof = secret()
		f, err := os.OpenFile(file, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
		if err == nil {
			_, err = f.WriteString(g.proof)
			if cerr := f.Close(); err == nil {
				err = cerr
			}
		}
		g.proofErr = err
	}
	b.mu.Lock()
	b.sessions[capability] = g
	b.mu.Unlock()
	b.route(g)
	var once sync.Once
	return capability, func() {
		once.Do(func() {
			if g.proof != "" {
				os.Remove(file)
			}
			b.mu.Lock()
			delete(b.sessions, capability)
			for credential, op := range b.ops {
				if op.session == g {
					delete(b.ops, credential)
				}
			}
			b.mu.Unlock()
			close(g.closed)
			b.route(g)
		})
	}
}

// Reroute writes or removes each session's routing as its source's allow now has github or not.
func (b *Broker) Reroute() {
	b.mu.Lock()
	sessions := slices.Collect(maps.Values(b.sessions))
	b.mu.Unlock()
	for _, g := range sessions {
		b.route(g)
	}
}

// route writes g's routing file while g is open and its source's allow has github, else removes it.
// The file is replaced whole, so Git reads either all of it or none.
func (b *Broker) route(g *gitSession) {
	b.routing.Lock()
	defer b.routing.Unlock()
	select {
	case <-g.closed:
		os.Remove(g.route)
		return
	default:
	}
	if !g.GitHub() {
		os.Remove(g.route)
		return
	}
	f, err := os.CreateTemp(b.routes, ".new-*")
	if err == nil {
		_, err = f.Write(gitConfig(b.pairs(g.Client)))
		if cerr := f.Close(); err == nil {
			err = cerr
		}
		if err == nil {
			err = os.Chmod(f.Name(), 0o644)
		}
		if err == nil {
			err = os.Rename(f.Name(), g.route)
		}
		if err != nil {
			os.Remove(f.Name())
		}
	}
	if err != nil {
		log.Printf("native: a session's GitHub routing: %v", err)
	}
}

// gitConfig is pairs in Git's configuration file syntax, each in its own section, with the
// subsection and the value quoted.
func gitConfig(pairs [][2]string) []byte {
	escape := strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`)
	var b bytes.Buffer
	for _, p := range pairs {
		i, j := strings.IndexByte(p[0], '.'), strings.LastIndexByte(p[0], '.')
		fmt.Fprintf(&b, "[%s \"%s\"]\n\t%s = \"%s\"\n", p[0][:i], escape.Replace(p[0][i+1:j]), p[0][j+1:], escape.Replace(p[1]))
	}
	return b.Bytes()
}

// Environment is the environment of the session whose capability it is: the broker, the capability
// and the absolute path of the session's routing, which Git includes in every command.
func (b *Broker) Environment(capability string) map[string]string {
	b.mu.Lock()
	route := b.sessions[capability].route
	b.mu.Unlock()
	return map[string]string{"OM_BROKER": b.URL, "OM_BROKER_CAPABILITY": capability, "GIT_CONFIG_COUNT": "1",
		"GIT_CONFIG_KEY_0": "include.path", "GIT_CONFIG_VALUE_0": route}
}

// pairs is a session's Git routing: process-scoped configuration, never a global or repository
// edit, that sends github.com remotes over HTTPS or SSH to this broker, with client's git-credential
// as their only credential helper and, for om alone (Git ignores the key), where a Full Access
// session finds its proof.
func (b *Broker) pairs(client string) [][2]string {
	return [][2]string{
		{"url." + b.URL + "/github.com/.insteadOf", "https://github.com/"},
		{"url." + b.URL + "/github.com/.insteadOf", "git@github.com:"},
		{"url." + b.URL + "/github.com/.insteadOf", "ssh://git@github.com/"},
		// No certificate authority vouches for the broker: its pinned key does, and a server without it
		// fails the handshake. The pin is a hash, which a session reading this file cannot use.
		{"http." + b.URL + ".sslVerify", "false"},
		{"http." + b.URL + ".pinnedPubkey", b.Pin},
		{"http." + b.URL + ".proxy", ""},        // straight to it: a proxy from the environment reaches its own loopback
		{"credential." + b.URL + ".helper", ""}, // no other helper sees, or stores, these credentials
		{"credential." + b.URL + ".helper", "!" + quote(client) + " git-credential"},
		{"credential." + b.URL + ".useHttpPath", "true"},
		// Pushes within the limit go with their length, so an oversized one fails before it is sent.
		{"http." + b.URL + ".postBuffer", strconv.Itoa(maxPush)},
		{"credential." + b.URL + "." + ProofsKey, b.proofs},
	}
}

// ProofsKey names, under credential.<broker>, the directory of Full Access proofs.
const ProofsKey = "openmachineProofs"

func secret() string {
	b := make([]byte, 32)
	rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func (b *Broker) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Only clients that name this endpoint: a rebound browser page reaches nothing.
	if r.Host != strings.TrimPrefix(b.URL, "https://") {
		refuse(w, http.StatusMisdirectedRequest, "use "+b.URL)
		return
	}
	switch {
	case r.URL.Path == "/v1/credential" && r.Method == http.MethodPost:
		b.credential(w, r)
	case r.URL.Path == "/v1/github" && r.Method == http.MethodPost:
		b.github(w, r)
	case r.URL.Path == "/v1/wait" && r.Method == http.MethodPost:
		b.wait(w, r)
	case strings.HasPrefix(r.URL.Path, "/github.com/"):
		b.git(w, r)
	default:
		refuse(w, http.StatusNotFound, "Open Machine routes only github.com repositories")
	}
}

// refuse answers with a reason; git shows a text/plain one on stderr as remote: lines.
func refuse(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "text/plain")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	fmt.Fprintf(w, "om: %s\n", message)
}

// fail answers a /v1 request in the shape of Cloud's refusals.
func fail(w http.ResponseWriter, status int, code, message string) {
	answer(w, &cloudError{Status: status, Code: code, Message: message})
}

// answer passes on e, a refusal of no effect, with an approval's fields.
func answer(w http.ResponseWriter, e *cloudError) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(e.Status)
	json.NewEncoder(w).Encode(struct {
		*cloudError
		Effect string `json:"effect"`
	}{e, "known_no_effect"})
}

func (b *Broker) session(r *http.Request) (*gitSession, error) {
	capability, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	b.mu.Lock()
	g := b.sessions[capability]
	b.mu.Unlock()
	if !ok || g == nil {
		return nil, errors.New("this SSH session's GitHub capability is not valid here")
	}
	if g.proof != "" {
		if g.proofErr != nil {
			return nil, fmt.Errorf("Open Machine could not keep this session's GitHub proof: %w", g.proofErr)
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-OM-Broker-Proof")), []byte(g.proof)) != 1 {
			return nil, errProofRequired
		}
	}
	return g, g.live()
}

// unavailable refuses a /v1 request whose session cannot use GitHub, or asks for its proof.
func unavailable(w http.ResponseWriter, err error) {
	if errors.Is(err, errProofRequired) {
		fail(w, http.StatusUnauthorized, "code_proof_required", err.Error())
		return
	}
	fail(w, http.StatusForbidden, "code_session_unavailable", err.Error())
}

// allowed returns the actions s may take and, when they exclude push, why. Only Full Access pushes
// and opens pull requests. In a Workspace folder no repository can be verified, outside the
// sandbox, as one restricted work could not have configured, so it fetches and reads like Read Only.
func allowed(s *gitSession) ([]string, string) {
	switch s.Mode {
	case "full_access":
		return []string{"read", "push", "pull_request"}, ""
	case "workspace":
		return []string{"read"}, "Workspace fetches and reads through Open Machine but does not push or open pull requests; that needs Full access"
	}
	return []string{"read"}, "Read Only access fetches through Open Machine but does not push or open pull requests"
}

func validName(name string) bool { return name != "." && name != ".." }

// credential gives one Git command a credential for the repository its remote names: git calls
// om git-credential, which sends the capability and the remote's path.
func (b *Broker) credential(w http.ResponseWriter, r *http.Request) {
	g, err := b.session(r)
	if err != nil {
		unavailable(w, err)
		return
	}
	var in struct {
		Path        string `json:"path"`
		Interactive bool   `json:"interactive"`
	}
	if json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&in) != nil {
		fail(w, http.StatusBadRequest, "invalid_fields", "send the remote's path")
		return
	}
	m := credentialAt.FindStringSubmatch(in.Path)
	if m == nil || !validName(m[2]) {
		fail(w, http.StatusNotFound, "github_repository_unsupported", "Open Machine routes only github.com/OWNER/REPOSITORY remotes")
		return
	}
	op := &operation{session: g, repository: strings.ToLower(m[1] + "/" + m[2]), interactive: in.Interactive, expires: time.Now().Add(operationTTL)}
	op.actions, op.refused = allowed(g)
	ctx, cancel := g.context(r.Context())
	defer cancel()
	if _, err := b.ticket(ctx, op); err != nil {
		cloudFailure(w, err)
		return
	}
	credential := secret()
	b.mu.Lock()
	mine := 0
	for c, o := range b.ops {
		if time.Now().After(o.expires) {
			delete(b.ops, c)
		} else if o.session == g {
			mine++
		}
	}
	if len(b.ops) >= maxOperations || mine >= maxSessionOperations {
		b.mu.Unlock()
		fail(w, http.StatusTooManyRequests, "code_github_busy", "too many recent Git commands; try again in a few minutes")
		return
	}
	b.ops[credential] = op
	b.mu.Unlock()
	json.NewEncoder(w).Encode(map[string]string{"username": "om", "password": credential})
}

// cloudFailure answers with Cloud's refusal, or that Cloud could not be reached.
func cloudFailure(w http.ResponseWriter, err error) {
	var ce *cloudError
	if errors.As(err, &ce) && ce.Status >= 400 && ce.Status < 500 {
		answer(w, ce)
		return
	}
	fail(w, http.StatusBadGateway, "cloud_unavailable", "Open Machine could not reach Cloud; nothing was changed")
}

// ticket returns a current Cloud ticket for op, minted under a fresh device proof, or one minted
// for the same scope within the last minute. Its account then stays pinned: every later ticket of
// op must keep it. A ticket's lifetime is counted on this computer's clock from before its request.
func (b *Broker) ticket(ctx context.Context, op *operation) (string, error) {
	op.mu.Lock()
	defer op.mu.Unlock()
	if op.ticket != "" && time.Until(op.until) > ticketMargin {
		return op.ticket, nil
	}
	s := op.session
	key := ticketKey{fmt.Sprint(s.Source, "/", s.SourceGrantRevision, "/", s.TargetGrant, "/", s.TargetGrantRevision, "/", s.PolicyRevision),
		op.repository, strings.Join(op.actions, ",")}
	fresh := op.account == nil // a new command resolves the current default; a pinned one keeps its account
	if fresh {
		b.mu.Lock()
		t := b.tickets[key]
		b.mu.Unlock()
		if t != nil && time.Since(t.minted) < ticketReuse && time.Until(t.until) > ticketMargin {
			op.ticket, op.until, op.account, op.team = t.ticket, t.until, &t.account, t.team
			return op.ticket, nil
		}
	}
	var r struct {
		Ticket  string     `json:"ticket"`
		TTL     int64      `json:"ttl_ms"`
		Account accountPin `json:"account"`
		Team    bool       `json:"team"`
	}
	minted := time.Now()
	err := signedPost(ctx, b.client, b.origin, b.deviceID, b.key, "code/github/ticket", map[string]any{"v": 1,
		"repository": op.repository, "actions": op.actions, "account": op.account,
		"binding": map[string]any{"source": s.Source, "source_grant_revision": s.SourceGrantRevision, "target_grant_id": s.TargetGrant,
			"target_grant_revision": s.TargetGrantRevision, "policy_revision": s.PolicyRevision}}, &r)
	if err != nil {
		var ce *cloudError
		if errors.As(err, &ce) {
			b.asks(ce)
		}
		return "", err
	}
	if r.Ticket == "" || r.TTL < 1 || (op.account != nil && r.Account != *op.account) {
		return "", errors.New("native: Cloud answered with another GitHub account")
	}
	op.ticket, op.until, op.account, op.team = r.Ticket, minted.Add(time.Duration(r.TTL)*time.Millisecond), &r.Account, r.Team
	b.mu.Lock()
	for k, t := range b.tickets {
		if time.Since(t.minted) >= ticketReuse {
			delete(b.tickets, k)
		}
	}
	if fresh && len(b.tickets) < maxTickets {
		b.tickets[key] = &sharedTicket{op.ticket, minted, op.until, r.Account, r.Team}
	}
	b.mu.Unlock()
	return op.ticket, nil
}

// operation returns the operation of a request's credential, extending its expiry.
func (b *Broker) operation(r *http.Request) *operation {
	_, credential, ok := r.BasicAuth()
	b.mu.Lock()
	defer b.mu.Unlock()
	if op := b.ops[credential]; ok && op != nil && time.Now().Before(op.expires) {
		op.expires = time.Now().Add(operationTTL)
		return op
	}
	return nil
}

// git forwards one smart-HTTP request of a Git command to Cloud under its operation's ticket.
func (b *Broker) git(w http.ResponseWriter, r *http.Request) {
	m := brokerPath.FindStringSubmatch(r.URL.Path)
	if m == nil || !validName(m[2]) {
		refuse(w, http.StatusNotFound, "Open Machine routes only github.com/OWNER/REPOSITORY remotes")
		return
	}
	endpoint, service := m[3], m[3]
	if endpoint == "info/refs" {
		service = r.URL.Query().Get("service")
	}
	if (endpoint == "info/refs") != (r.Method == http.MethodGet) || service != "git-upload-pack" && service != "git-receive-pack" ||
		(endpoint == "info/refs") != (r.URL.RawQuery == "service="+service) {
		refuse(w, http.StatusBadRequest, "unsupported Git request")
		return
	}
	op := b.operation(r)
	if op != nil {
		defer b.operation(r) // a long request, such as a large clone, keeps its credential too
	}
	if op == nil {
		w.Header().Set("WWW-Authenticate", `Basic realm="Open Machine"`)
		refuse(w, http.StatusUnauthorized, "this Git command has no Open Machine credential")
		return
	}
	push := service == "git-receive-pack"
	var head []string // the push's capabilities, so a refusal can be shown as git shows remote errors
	probe := false    // git's empty push, which only probes authentication before a large one
	body := io.Reader(nil)
	sent := &counter{}
	refusal := func(status int, message string) {
		if r.Method == http.MethodPost {
			// Read what git is still sending, within bounds, so it reads the reason, not a broken pipe.
			http.NewResponseController(w).SetReadDeadline(time.Now().Add(30 * time.Second))
			sent.drain()
		}
		b.refusePush(w, head, status, message)
	}
	if r.Method == http.MethodPost {
		limit := int64(maxFetchRequest)
		if push {
			limit = maxPush
		}
		br := bufio.NewReaderSize(r.Body, 64<<10)
		if push {
			head, probe = capabilities(br)
		}
		sent.r, sent.max = br, limit
		if r.ContentLength > limit {
			refusal(http.StatusRequestEntityTooLarge, fmt.Sprintf("the request exceeds Open Machine's %d MiB limit; nothing was sent", limit>>20))
			return
		}
		body = sent
	}
	if !strings.EqualFold(m[1]+"/"+m[2], op.repository) {
		refusal(http.StatusForbidden, "this Git command's credential is for another repository")
		return
	}
	if err := op.session.live(); err != nil {
		refusal(http.StatusForbidden, err.Error())
		return
	}
	need := "read"
	if push {
		need = "push"
	}
	if !slices.Contains(op.actions, need) {
		refusal(http.StatusForbidden, op.refused)
		return
	}
	ctx, cancel := op.session.context(r.Context())
	defer cancel()
	unticketed := func(err error) {
		var ce *cloudError
		message := "Open Machine could not reach Cloud; nothing was changed"
		if errors.As(err, &ce) && ce.Message != "" {
			message = ce.Message
		}
		refusal(http.StatusBadGateway, message)
	}
	ticket, err := b.ticket(ctx, op)
	if err != nil {
		unticketed(err)
		return
	}
	op.mu.Lock()
	team := op.team
	op.mu.Unlock()
	length := r.ContentLength
	var held *os.File // a team's push, held whole so it can be sent again once its person approves it
	if push && team && r.Method == http.MethodPost {
		if held, err = os.CreateTemp(b.proofs, "push-*"); err != nil {
			refusal(http.StatusInternalServerError, "Open Machine could not hold this push; nothing was sent")
			return
		}
		defer os.Remove(held.Name())
		defer held.Close()
		if length, err = io.Copy(held, sent); err != nil {
			if over, _, _ := sent.state(); over {
				refusal(http.StatusRequestEntityTooLarge, fmt.Sprintf("the request exceeds Open Machine's %d MiB limit; nothing was sent", maxPush>>20))
			} else {
				refusal(http.StatusInternalServerError, "Open Machine could not hold this push; nothing was sent")
			}
			return
		}
	}
	answered := false // a team's push that waits for its person has had its answer and the lines that say so
	var resent *counted
	for {
		if held != nil { // read anew each time: the transport may still be reading an earlier send
			resent = &counted{r: io.NewSectionReader(held, 0, length)}
			body = resent
		}
		target := b.origin + "/api/devices/" + b.deviceID + "/code/github/git/" + m[1] + "/" + m[2] + "/" + endpoint
		if endpoint == "info/refs" {
			target += "?service=" + service
		}
		req, err := http.NewRequestWithContext(ctx, r.Method, target, body)
		if err != nil {
			refuse(w, http.StatusBadRequest, "unsupported Git request")
			return
		}
		if body != nil {
			req.ContentLength = length
		}
		for _, h := range []string{"Content-Type", "Accept", "Content-Encoding", "Git-Protocol", "User-Agent"} {
			if v := r.Header.Get(h); v != "" {
				req.Header.Set(h, v)
			}
		}
		req.Header.Set("Authorization", "Bearer "+ticket)
		req.Header.Set("X-OM-Visit", op.session.Visit)
		res, err := b.client.Do(req)
		if err != nil {
			switch over, read, _ := sent.state(); {
			case resent != nil && resent.n.Load() == 0: // a held push Cloud never read changed nothing
				refusal(http.StatusBadGateway, "Open Machine could not reach Cloud; nothing was changed")
			case push && over:
				refusal(http.StatusBadGateway, fmt.Sprintf("this push exceeded Open Machine's %d MiB limit and was cut, so %s", maxPush>>20, unknownOutcome))
			case push && read > 0: // GitHub acts once a whole pack, or deletions alone, arrived, before the body ends
				refusal(http.StatusBadGateway, unknownPush)
			case over: // a fetch changes nothing
				refusal(http.StatusRequestEntityTooLarge, fmt.Sprintf("the request exceeds Open Machine's %d MiB limit; nothing was changed", sent.max>>20))
			default:
				refusal(http.StatusBadGateway, "Open Machine could not reach Cloud; nothing was changed")
			}
			return
		}
		defer res.Body.Close()
		if res.StatusCode == http.StatusOK {
			if endpoint == "git-receive-pack" { // the push's result, whole or outcome unknown; its advertisement streams
				reply, err := io.ReadAll(io.LimitReader(res.Body, maxGitResult+1))
				if err != nil || len(reply) > maxGitResult || !probe && !completePkts(reply) {
					refusal(http.StatusBadGateway, unknownPush)
					return
				}
				if _, _, done := sent.state(); !done { // Cloud refused the refs before reading the pack git still sends
					http.NewResponseController(w).SetReadDeadline(time.Now().Add(30 * time.Second))
					sent.drain()
				}
				w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
				w.Header().Set("Cache-Control", "no-store")
				w.Write(reply)
				return
			}
			w.Header().Set("Content-Type", res.Header.Get("Content-Type"))
			w.Header().Set("Cache-Control", "no-store")
			copyFlushing(w, res.Body)
			return
		}
		var e struct {
			cloudError
			Effect string `json:"effect"`
		}
		json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&e)
		if e.Message == "" {
			e.Message = fmt.Sprintf("Cloud answered %d", res.StatusCode)
		}
		b.asks(&e.cloudError)
		if held != nil && e.Code == "github_approval_required" && slices.Contains(head, "side-band-64k") {
			if message := b.approval(ctx, w, op, &e.cloudError, &answered); message != "" {
				refusal(http.StatusForbidden, message)
				return
			}
			if ticket, err = b.ticket(ctx, op); err != nil {
				unticketed(err)
				return
			}
			continue
		}
		status := res.StatusCode
		if status == http.StatusUnauthorized {
			status = http.StatusBadGateway // a refused ticket is not git's credential to change
		}
		// Once Cloud may have a push's commands, only its explicit no-effect refusal is a known
		// outcome; its own unknown outcome says what it read back from GitHub.
		if _, read, _ := sent.state(); push && read > 0 && (resent == nil || resent.n.Load() > 0) &&
			e.Effect != "known_no_effect" && e.Code != "github_outcome_unknown" {
			e.Message = unknownPush
		}
		refusal(status, e.Message)
		return
	}
}

// counted is a held push as one send reads it.
type counted struct {
	r io.Reader
	n atomic.Int64
}

func (c *counted) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n.Add(int64(n))
	return n, err
}

// approval answers a side-band push of a team operation that Cloud refused until its person
// approves it, e. In a terminal it says so in band 2, waits, and returns "" once the push may be
// sent again; otherwise it passes Cloud's message on in band 2. Else it returns the refusal to show.
func (b *Broker) approval(ctx context.Context, w http.ResponseWriter, op *operation, e *cloudError, answered *bool) string {
	if !*answered {
		w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
		w.Header().Set("Cache-Control", "no-store")
		*answered = true
	}
	say := func(text string) {
		for line := range strings.Lines(text) {
			payload := append([]byte{2}, line...)
			fmt.Fprintf(w, "%04x%s", len(payload)+4, payload)
		}
		http.NewResponseController(w).Flush()
	}
	switch {
	case op.interactive:
		say(approvalLines(e.Number))
	case e.Again:
		say(waitingLine(e.Number))
	default:
		say(e.Message + "\n")
		return "approval needed"
	}
	return b.approved(ctx, e, say)
}

// approved waits until Cloud answers the request e names, sharing each wait with every command
// waiting on it, saying each StillEvery that it still waits, and returns "" when its person approved
// it, else why it ends: Cloud's refusal, the session ending, or the request expiring.
func (b *Broker) approved(ctx context.Context, e *cloudError, say func(string)) string {
	deadline := time.Now().Add(time.Duration(e.ExpiresIn) * time.Second)
	for said := time.Now(); time.Now().Before(deadline); {
		if time.Since(said) >= StillEvery {
			say(stillLine(e.Number, deadline))
			said = time.Now()
		}
		until := said.Add(StillEvery) // the call in flight goes on, and the next await shares it
		if deadline.Before(until) {
			until = deadline
		}
		state, err := b.await(ctx, e.Request, until)
		var ce *cloudError
		switch {
		case state == "approved":
			return ""
		case ctx.Err() != nil:
			return "this SSH session has ended, and its GitHub access with it"
		case errors.As(err, &ce) && final(ce):
			return cmp.Or(ce.Message, fmt.Sprintf("Cloud answered %d", ce.Status))
		}
	}
	return requestExpired
}

// asks keeps the request a refusal e names as one this broker may wait on, until it expires, and
// marks e Again when it already was one.
func (b *Broker) asks(e *cloudError) {
	if e.Code != "github_approval_required" || !requestID.MatchString(e.Request) {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	maps.DeleteFunc(b.asked, func(_ string, until time.Time) bool { return time.Since(until) > time.Minute })
	until, ok := b.asked[e.Request]
	e.Again = ok && time.Now().Before(until)
	b.asked[e.Request] = time.Now().Add(time.Duration(e.ExpiresIn) * time.Second)
}

// final reports whether Cloud's refusal ends a wait: any but a failure (429 and 5xx), which is asked again.
func final(e *cloudError) bool {
	return e.Status >= 400 && e.Status < 500 && e.Status != http.StatusTooManyRequests
}

// waiting is one request whose commands wait for its person's approval.
type waiting struct {
	since  time.Time // when its first command began to wait: its place in line
	failed time.Time // when its last failed Cloud call began
	ended  time.Time // when its last Cloud call ended
	call   *waitCall // the Cloud call in flight, or nil
}

// waitCall is one Cloud wait call and, once done is closed, its answer.
type waitCall struct {
	done  chan struct{}
	state string
	err   error
}

// wait answers om's /v1/wait: Cloud's next answer to the session's request, {state} while it waits or
// once approved, or its refusal.
func (b *Broker) wait(w http.ResponseWriter, r *http.Request) {
	g, err := b.session(r)
	if err != nil {
		unavailable(w, err)
		return
	}
	var in struct {
		Request string `json:"request"`
	}
	if json.NewDecoder(io.LimitReader(r.Body, 4<<10)).Decode(&in) != nil || !requestID.MatchString(in.Request) {
		fail(w, http.StatusBadRequest, "invalid_fields", "send the request")
		return
	}
	b.mu.Lock()
	until, ok := b.asked[in.Request]
	b.mu.Unlock()
	switch {
	case !ok:
		fail(w, http.StatusNotFound, "github_request_unavailable", "This request is not waiting for this device.")
		return
	case time.Now().After(until):
		fail(w, http.StatusForbidden, "github_request_expired", requestExpired)
		return
	}
	ctx, cancel := g.context(r.Context())
	defer cancel()
	state, err := b.await(ctx, in.Request, time.Now().Add(callDeadline))
	if err != nil {
		cloudFailure(w, err)
		return
	}
	json.NewEncoder(w).Encode(map[string]string{"state": state})
}

// await returns Cloud's answer to the wait call for request in flight, or to the next one, which it
// starts, or ctx's error once ctx ends or deadline passes first.
func (b *Broker) await(ctx context.Context, request string, deadline time.Time) (string, error) {
	ctx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	b.mu.Lock()
	q := b.waits[request]
	if q == nil {
		for id, o := range b.waits {
			if o.call == nil && time.Since(o.ended) > time.Minute {
				delete(b.waits, id)
			}
		}
		q = &waiting{since: time.Now()}
		b.waits[request] = q
	}
	c := q.call
	if c == nil {
		c = &waitCall{done: make(chan struct{})}
		q.call = c
		go b.callWait(request, q, c)
	}
	b.mu.Unlock()
	select {
	case <-c.done:
		return c.state, c.err
	case <-ctx.Done():
		return "", ctx.Err()
	}
}

// callWait makes c, request's Cloud wait call: 20 seconds after a failed one began at the earliest,
// and within the machine's 20 a minute.
func (b *Broker) callWait(request string, q *waiting, c *waitCall) {
	b.mu.Lock()
	failed := q.failed
	b.mu.Unlock()
	if !failed.IsZero() {
		time.Sleep(time.Until(failed.Add(waitRetry)))
	}
	b.slot(q)
	began := time.Now()
	var r struct {
		State string `json:"state"`
	}
	err := signedPost(context.Background(), b.client, b.origin, b.deviceID, b.key, "code/github/wait", map[string]any{"v": 1, "request": request}, &r)
	if err == nil && r.State != "waiting" && r.State != "approved" {
		err = fmt.Errorf("native: Cloud answered a request's wait with %q", r.State)
	}
	var ce *cloudError
	b.mu.Lock()
	q.failed, q.ended, q.call = time.Time{}, time.Now(), nil
	if err != nil && !(errors.As(err, &ce) && final(ce)) {
		q.failed = began
	}
	b.mu.Unlock()
	c.state, c.err = r.State, err
	close(c.done)
}

// slot holds q's next Cloud wait call until the machine has made fewer than 20 in the last minute
// and no request that began to wait before q's is held for one.
func (b *Broker) slot(q *waiting) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.queued = append(b.queued, q)
	for {
		now := time.Now()
		b.calls = slices.DeleteFunc(b.calls, func(t time.Time) bool { return now.Sub(t) >= time.Minute })
		first := !slices.ContainsFunc(b.queued, func(o *waiting) bool { return o.since.Before(q.since) })
		if first && len(b.calls) < maxWaits {
			b.calls = append(b.calls, now)
			b.queued = slices.DeleteFunc(b.queued, func(o *waiting) bool { return o == q })
			close(b.freed)
			b.freed = make(chan struct{})
			return
		}
		freed, next := b.freed, time.Minute
		if len(b.calls) >= maxWaits {
			next = time.Minute - now.Sub(b.calls[0])
		}
		b.mu.Unlock()
		select {
		case <-freed:
		case <-time.After(next):
		}
		b.mu.Lock()
	}
}

const (
	unknownOutcome = "its outcome is unknown, and it was not retried; check with git ls-remote before pushing again"
	unknownPush    = "the reply to this push was lost, so " + unknownOutcome
)

// refusePush shows a reason as git shows a remote error: in band 3 for a push that asked for
// side-band, which git prints and fails with, and as text otherwise.
func (b *Broker) refusePush(w http.ResponseWriter, capabilities []string, status int, message string) {
	if !slices.Contains(capabilities, "side-band-64k") {
		refuse(w, status, message)
		return
	}
	w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
	w.Header().Set("Cache-Control", "no-store")
	payload := append([]byte{3}, "om: "+message+"\n"...)
	fmt.Fprintf(w, "%04x%s0000", len(payload)+4, payload)
}

// capabilities peeks a push's capabilities on its first command, past any shallow lines, and
// whether it has no command at all, as git's probe has.
func capabilities(r *bufio.Reader) ([]string, bool) {
	for at := 0; ; {
		head, err := r.Peek(at + 4)
		if err != nil {
			return nil, false
		}
		n, err := strconv.ParseUint(string(head[at:at+4]), 16, 16)
		if err != nil || n < 4 {
			return nil, err == nil && n == 0 && at == 0
		}
		line, err := r.Peek(at + int(n))
		if err != nil {
			return nil, false
		}
		payload := line[at+4:]
		if bytes.HasPrefix(payload, []byte("shallow ")) {
			at += int(n)
			continue
		}
		if i := bytes.IndexByte(payload, 0); i >= 0 {
			return strings.Fields(string(payload[i+1:])), false
		}
		return nil, false
	}
}

// completePkts reports whether b is whole pkt-lines ending with a flush, as every complete
// receive-pack reply is.
func completePkts(b []byte) bool {
	last := -1
	for len(b) > 0 {
		if len(b) < 4 {
			return false
		}
		n, err := strconv.ParseUint(string(b[:4]), 16, 16)
		if err != nil || n != 0 && n < 4 || int(n) > len(b) {
			return false
		}
		last = int(n)
		b = b[max(int(n), 4):]
	}
	return last == 0
}

// counter passes a request body to Cloud, failing it past max; done means it ended within max.
// Its reads and drain are serialized: the client may still read after it answered.
type counter struct {
	mu                  sync.Mutex
	r                   io.Reader
	n, max              int64
	over, done, stopped bool
}

func (c *counter) Read(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.stopped {
		return 0, errors.New("native: request body stopped")
	}
	n, err := c.r.Read(p)
	if c.n += int64(n); c.n > c.max {
		c.over = true
		return 0, errors.New("native: request over its limit")
	}
	if err == io.EOF {
		c.done = true
	}
	return n, err
}

// state reports whether the body went over its limit, how much of it was read and whether all was.
func (c *counter) state() (over bool, read int64, done bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.over, c.n, c.done
}

// drain stops passing the body on and discards up to 64 MiB of what is left.
func (c *counter) drain() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stopped = true
	if c.r != nil && !c.done {
		io.CopyN(io.Discard, c.r, 64<<20)
	}
}

// copyFlushing streams src to w, flushing each read so git sees progress as it comes.
func copyFlushing(w http.ResponseWriter, src io.Reader) {
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 32<<10)
	for {
		n, err := src.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if err != nil {
			return
		}
	}
}

// github runs one fixed om github call: the session's capability, the repository the CLI
// resolved, and the operation Cloud validates and performs.
func (b *Broker) github(w http.ResponseWriter, r *http.Request) {
	g, err := b.session(r)
	if err != nil {
		unavailable(w, err)
		return
	}
	var in struct {
		Repository string          `json:"repository"`
		Call       json.RawMessage `json:"call"`
	}
	var call struct {
		Operation string `json:"operation"`
	}
	if json.NewDecoder(io.LimitReader(r.Body, 256<<10)).Decode(&in) != nil || json.Unmarshal(in.Call, &call) != nil {
		fail(w, http.StatusBadRequest, "invalid_fields", "send the repository and the call")
		return
	}
	m := credentialAt.FindStringSubmatch("github.com/" + in.Repository)
	need := "read"
	if call.Operation == "pull_create" {
		need = "pull_request"
	}
	if m == nil || !validName(m[2]) || !slices.Contains([]string{"pull_create", "pull_view", "pull_list", "checks"}, call.Operation) {
		fail(w, http.StatusBadRequest, "invalid_fields", "unsupported repository or operation")
		return
	}
	op := &operation{session: g, repository: strings.ToLower(m[1] + "/" + m[2]), expires: time.Now().Add(operationTTL)}
	actions, refused := allowed(g)
	if !slices.Contains(actions, need) {
		fail(w, http.StatusForbidden, "code_github_not_permitted", refused)
		return
	}
	op.actions = []string{need}
	// One deadline for the ticket and the call, under om github's own, so the CLI always hears
	// whether a pull request may exist.
	ctx, cancel := context.WithTimeout(r.Context(), callDeadline)
	defer cancel()
	ctx, stop := g.context(ctx)
	defer stop()
	ticket, err := b.ticket(ctx, op)
	if err != nil {
		cloudFailure(w, err)
		return
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.origin+"/api/devices/"+b.deviceID+"/code/github/call", bytes.NewReader(in.Call))
	if err != nil {
		fail(w, http.StatusBadRequest, "invalid_fields", "unsupported call")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+ticket)
	req.Header.Set("X-OM-Visit", g.Visit)
	effect := "known_no_effect" // a lost reply leaves a creation unknown, a read without effect
	if need == "pull_request" {
		effect = "effect_unknown"
	}
	res, err := b.client.Do(req)
	var reply []byte
	if err == nil {
		defer res.Body.Close()
		reply, err = io.ReadAll(io.LimitReader(res.Body, 4<<20))
	}
	var parsed struct {
		Error  string          `json:"error"`
		Result json.RawMessage `json:"result"`
	}
	if err != nil || json.Unmarshal(reply, &parsed) != nil || (res.StatusCode == http.StatusOK) == (parsed.Error != "") {
		// A lost or unreadable reply: a pull request may have been created, a read had no effect.
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		json.NewEncoder(w).Encode(map[string]string{"error": "cloud_unavailable", "message": "Open Machine lost Cloud's reply", "effect": effect})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(res.StatusCode)
	w.Write(reply)
}
