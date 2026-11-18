package daemon

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// scriptedCloud answers the broker's challenge, ticket and Git requests as a test scripts them.
type scriptedCloud struct {
	mu      sync.Mutex
	tickets []map[string]any // ticket request bodies
	account string           // the account each ticket reports
	ttl     time.Duration    // of each ticket
	git     func(w http.ResponseWriter, r *http.Request)
	paths   []string
}

func (c *scriptedCloud) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	c.mu.Lock()
	c.paths = append(c.paths, r.Method+" "+r.URL.Path)
	c.mu.Unlock()
	switch {
	case strings.HasSuffix(r.URL.Path, "/challenge"):
		json.NewEncoder(w).Encode(map[string]string{"challenge_id": strings.Repeat("c", 43), "challenge": strings.Repeat("d", 43)})
	case strings.HasSuffix(r.URL.Path, "/code/github/ticket"):
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		c.mu.Lock()
		c.tickets = append(c.tickets, body)
		n, account, ttl := len(c.tickets), c.account, c.ttl
		c.mu.Unlock()
		json.NewEncoder(w).Encode(map[string]any{"v": 1, "ticket": fmt.Sprint("ticket-", n), "ttl_ms": ttl.Milliseconds(),
			"account": map[string]any{"id": account, "github_id": 7, "revision": 1, "login": "dev"}})
	default:
		c.mu.Lock()
		git := c.git
		c.mu.Unlock()
		git(w, r)
	}
}

// reply scripts how Cloud answers the next Git requests.
func (c *scriptedCloud) reply(git func(w http.ResponseWriter, r *http.Request)) {
	c.mu.Lock()
	c.git = git
	c.mu.Unlock()
}

func newBroker(t *testing.T, cloud *scriptedCloud, mode string) (*daemon.Broker, string, func()) {
	b, capability, end, _ := openBroker(t, cloud, daemon.GitSession{Mode: mode})
	return b, capability, end
}

// openBroker starts a broker against cloud and opens session s, completed with fixed revisions,
// returning what closes its connection too.
func openBroker(t *testing.T, cloud *scriptedCloud, s daemon.GitSession) (*daemon.Broker, string, func(), func()) {
	srv := httptest.NewServer(cloud)
	t.Cleanup(srv.Close)
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	b, err := daemon.NewBroker(srv.URL, "dev_target", key, srv.Client(), filepath.Join(t.TempDir(), "proofs"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { b.Close() })
	done := make(chan struct{})
	s.Source, s.SourceGrantRevision, s.TargetGrant, s.TargetGrantRevision, s.PolicyRevision = "dev_source", 1, "grant", 1, 2
	s.Live, s.Done = func() error { return nil }, done
	capability, end := b.Open(s)
	return b, capability, end, sync.OnceFunc(func() { close(done) })
}

// call sends one request to b, as a client pinning its key.
func call(t *testing.T, b *daemon.Broker, method, url string, header http.Header, body []byte) (int, http.Header, string) {
	t.Helper()
	req, _ := http.NewRequest(method, url, bytes.NewReader(body))
	for k, v := range header {
		req.Header[k] = v
	}
	if body == nil {
		req.Body = nil
	}
	res, err := daemon.PinnedClient(b.Pin, 0).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	out, _ := io.ReadAll(res.Body)
	return res.StatusCode, res.Header, string(out)
}

// credential is what om git-credential gets for the session's capability.
func credential(t *testing.T, b *daemon.Broker, capability, path string) (int, string) {
	t.Helper()
	body, _ := json.Marshal(map[string]string{"path": path})
	status, out := v1(t, b, capability, "/v1/credential", body)
	var r struct{ Password string }
	json.Unmarshal([]byte(out), &r)
	return status, r.Password
}

// v1 calls the broker as om does, sending a Full Access session's proof when it is asked for.
func v1(t *testing.T, b *daemon.Broker, capability, path string, body []byte) (int, string) {
	t.Helper()
	h := http.Header{"Authorization": {"Bearer " + capability}, "Content-Type": {"application/json"}}
	status, _, out := call(t, b, "POST", b.URL+path, h, body)
	if status == 401 && strings.Contains(out, "code_proof_required") {
		proof, err := os.ReadFile(proofFile(b, capability))
		if err != nil {
			t.Fatal(err)
		}
		h.Set("X-OM-Broker-Proof", string(proof))
		status, _, out = call(t, b, "POST", b.URL+path, h, body)
	}
	return status, out
}

// proofFile is where a session's routing tells om to find its proof.
func proofFile(b *daemon.Broker, capability string) string {
	env := b.Environment(capability, "/opt/om")
	for k, v := range env {
		if v == "credential."+b.URL+"."+daemon.ProofsKey {
			return filepath.Join(env[strings.Replace(k, "KEY", "VALUE", 1)], capability)
		}
	}
	return ""
}

func TestBrokerAsksFullAccessForAProofOnlyUnsandboxedWorkCanRead(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, end := newBroker(t, cloud, "full_access")
	file := proofFile(b, capability)
	body, _ := json.Marshal(map[string]string{"path": "github.com/owner/app"})
	// The capability alone, as a restricted sandbox could read it from the session's environment.
	status, _, out := call(t, b, "POST", b.URL+"/v1/credential", http.Header{"Authorization": {"Bearer " + capability}}, body)
	fi, err := os.Lstat(file)
	if status != 401 || !strings.Contains(out, "code_proof_required") || strings.Contains(out, file) || err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("without the proof = %d %q (%v, %v)", status, out, fi, err)
	}
	session := func(mode string) (string, func()) {
		return b.Open(daemon.GitSession{Source: "dev_source", SourceGrantRevision: 1, TargetGrant: "grant", TargetGrantRevision: 1,
			PolicyRevision: 2, Mode: mode, Live: func() error { return nil }, Done: make(chan struct{})})
	}
	other, endOther := session("full_access")
	defer endOther()
	theirs, _ := os.ReadFile(proofFile(b, other))
	for _, proof := range []string{strings.Repeat("x", 43), string(theirs)} { // a guess, or another session's proof
		if status, _, _ := call(t, b, "POST", b.URL+"/v1/credential", http.Header{"Authorization": {"Bearer " + capability}, "X-OM-Broker-Proof": {proof}}, body); status != 401 {
			t.Fatalf("another proof = %d", status)
		}
	}
	if status, password := credential(t, b, capability, "github.com/owner/app"); status != 200 || password == "" {
		t.Fatalf("with the proof = %d", status)
	}
	cloud.mu.Lock()
	asked := len(cloud.tickets)
	cloud.mu.Unlock()
	if asked != 1 {
		t.Fatalf("Cloud was asked %d times", asked)
	}
	end()
	if _, err := os.Lstat(file); !os.IsNotExist(err) {
		t.Fatalf("the proof outlived its session: %v", err)
	}
	// A restricted session's capability fetches and reads only, and needs no proof.
	restricted, _ := session("read_only")
	if status, _, out := call(t, b, "POST", b.URL+"/v1/credential", http.Header{"Authorization": {"Bearer " + restricted}}, body); status != 200 {
		t.Fatalf("a Read Only capability = %d %q", status, out)
	}
	// A proof that cannot be kept refuses the session's GitHub access rather than skip the proof.
	os.Chmod(filepath.Dir(file), 0o500)
	defer os.Chmod(filepath.Dir(file), 0o700)
	unkept, _ := session("full_access")
	if status, out := v1(t, b, unkept, "/v1/credential", body); status != 403 || !strings.Contains(out, "could not keep") {
		t.Fatalf("an unkept proof = %d %q", status, out)
	}
}

func TestARestartedBrokerHasANewKeyAndNoEarlierProofs(t *testing.T) {
	dir := t.TempDir()
	proofs := filepath.Join(dir, "proofs")
	os.MkdirAll(proofs, 0o700)
	os.WriteFile(filepath.Join(proofs, strings.Repeat("c", 43)), []byte(strings.Repeat("p", 43)), 0o600)
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	start := func() *daemon.Broker {
		t.Helper()
		b, err := daemon.NewBroker("http://127.0.0.1:1", "dev_target", key, http.DefaultClient, proofs)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { b.Close() })
		return b
	}
	reaches := func(pin string, b *daemon.Broker) bool {
		res, err := daemon.PinnedClient(pin, 5*time.Second).Get(b.URL + "/")
		if err == nil {
			res.Body.Close()
		}
		return err == nil
	}
	first := start()
	if entries, _ := os.ReadDir(proofs); len(entries) != 0 {
		t.Fatalf("an earlier run's proofs remain: %v", entries)
	}
	if fi, err := os.Stat(proofs); err != nil || fi.Mode().Perm() != 0o700 {
		t.Fatalf("proofs directory = %v %v", fi, err)
	}
	if !strings.HasPrefix(first.URL, "https://127.0.0.1:") || !strings.HasPrefix(first.Pin, "sha256//") || !reaches(first.Pin, first) {
		t.Fatalf("broker %s pinned %s", first.URL, first.Pin)
	}
	first.Close()
	// Routing that pins the first key reaches no later broker; the key was never stored.
	second := start()
	if second.Pin == first.Pin || reaches(first.Pin, second) || !reaches(second.Pin, second) {
		t.Fatalf("a restarted broker pins %s, was %s", second.Pin, first.Pin)
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 1 {
		t.Fatalf("the broker stored more than its proofs: %v", entries)
	}
}

// fakeBroker serves h over TLS as a session's broker, with the session's routing pinning its key,
// then the routing entries extra.
func fakeBroker(t *testing.T, h http.Handler, extra ...[2]string) *httptest.Server {
	srv := httptest.NewUnstartedServer(h)
	srv.Config.ErrorLog = log.New(io.Discard, "", 0) // clients that refuse its key
	srv.StartTLS()
	t.Cleanup(srv.Close)
	sum := sha256.Sum256(srv.Certificate().RawSubjectPublicKeyInfo)
	entries := append([][2]string{{"http." + srv.URL + ".pinnedPubkey", "sha256//" + base64.StdEncoding.EncodeToString(sum[:])}}, extra...)
	t.Setenv("OM_BROKER", srv.URL)
	t.Setenv("GIT_CONFIG_COUNT", fmt.Sprint(len(entries)))
	for i, e := range entries {
		t.Setenv(fmt.Sprintf("GIT_CONFIG_KEY_%d", i), e[0])
		t.Setenv(fmt.Sprintf("GIT_CONFIG_VALUE_%d", i), e[1])
	}
	return srv
}

func TestOmTalksOnlyToTheBrokerItsRoutingPins(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, _ := newBroker(t, cloud, "full_access")
	var asked atomic.Int32
	impostor := fakeBroker(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked.Add(1)
		io.WriteString(w, `{"username":"om","password":"forged","result":[]}`)
	}))
	t.Setenv("OM_BROKER_CAPABILITY", capability)
	get := func() (string, string) {
		var out, errout bytes.Buffer
		daemon.GitCredential([]string{"get"}, strings.NewReader("protocol=https\nhost="+strings.TrimPrefix(os.Getenv("OM_BROKER"), "https://")+
			"\npath=github.com/owner/app\n\n"), &out, &errout)
		return out.String(), errout.String()
	}
	if out, _ := get(); out != "username=om\npassword=forged\n" || asked.Load() != 1 {
		t.Fatalf("the pinned server = %q, asked %d", out, asked.Load())
	}
	// A job's routing after its broker restarted: the address now answers with another key.
	t.Setenv("GIT_CONFIG_VALUE_0", b.Pin)
	out, errout := get()
	if out != "quit=1\n" || !strings.Contains(errout, "not the one the session's routing pins") {
		t.Fatalf("another key = %q %q", out, errout)
	}
	var pr, prErr bytes.Buffer
	if code := daemon.GitHub([]string{"pr", "list", "--repo", "owner/app"}, &pr, &prErr); code == 0 || !strings.Contains(prErr.String(), "pins") {
		t.Fatalf("om github with another key = %d %q", code, prErr.String())
	}
	// An earlier version's plain routing, and routing without a pin, reach nothing either.
	t.Setenv("GIT_CONFIG_COUNT", "0")
	if out, _ := get(); out != "quit=1\n" {
		t.Fatalf("no pin = %q", out)
	}
	plain := strings.Replace(impostor.URL, "https://", "http://", 1)
	t.Setenv("OM_BROKER", plain)
	var cred, credErr bytes.Buffer
	daemon.GitCredential([]string{"get"}, strings.NewReader("protocol=http\nhost="+strings.TrimPrefix(plain, "http://")+"\npath=github.com/owner/app\n\n"), &cred, &credErr)
	if cred.String() != "quit=1\n" || !strings.Contains(credErr.String(), "only over TLS") {
		t.Fatalf("plain routing = %q %q", cred.String(), credErr.String())
	}
	if asked.Load() != 1 {
		t.Fatalf("the server without the pinned key was asked %d times, its capability sent", asked.Load()-1)
	}
}

func basic(password string) http.Header {
	req, _ := http.NewRequest("GET", "http://x", nil)
	req.SetBasicAuth("om", password)
	return req.Header
}

func TestBrokerRemintsTicketsForThePinnedAccountOnly(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 30 * time.Second}
	cloud.git = func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer ticket-2" && r.Header.Get("Authorization") != "Bearer ticket-3" {
			t.Errorf("Git request under %q", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
		io.WriteString(w, "001e# service=git-upload-pack\n0000")
	}
	b, capability, _ := newBroker(t, cloud, "full_access")
	status, password := credential(t, b, capability, "github.com/Owner/App.git")
	if status != 200 || password == "" {
		t.Fatalf("credential = %d %q", status, password)
	}
	refs := b.URL + "/github.com/Owner/App.git/info/refs?service=git-upload-pack"
	// The first ticket expires within a minute, so each request mints another for the same account.
	for range 2 {
		if status, header, out := call(t, b, "GET", refs, basic(password), nil); status != 200 || header.Get("Content-Type") != "application/x-git-upload-pack-advertisement" || !strings.Contains(out, "# service") {
			t.Fatalf("info/refs = %d %v %q", status, header, out)
		}
	}
	cloud.mu.Lock()
	tickets := cloud.tickets
	cloud.mu.Unlock()
	if len(tickets) != 3 || tickets[0]["account"] != nil || tickets[1]["account"].(map[string]any)["id"] != cloud.account ||
		tickets[0]["repository"] != "owner/app" || fmt.Sprint(tickets[0]["actions"]) != "[read push pull_request]" {
		t.Fatalf("ticket requests = %v", tickets)
	}
	// Cloud answering with another account is refused: a command never changes actor.
	cloud.mu.Lock()
	cloud.account = strings.Repeat("b", 32)
	cloud.mu.Unlock()
	if status, _, out := call(t, b, "GET", refs, basic(password), nil); status != 502 || !strings.Contains(out, "om: ") {
		t.Fatalf("another account = %d %q", status, out)
	}
}

func TestBrokerRefusesBeforeCloud(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute,
		git: func(w http.ResponseWriter, r *http.Request) {
			t.Errorf("unexpected Cloud request %s %s", r.Method, r.URL)
		}}
	b, capability, end := newBroker(t, cloud, "read_only")
	_, password := credential(t, b, capability, "github.com/owner/app")
	refs := b.URL + "/github.com/owner/app/info/refs?service="
	for _, c := range []struct {
		name, url string
		header    http.Header
		status    int
		text      string
	}{
		{"a rebound browser page", refs + "git-upload-pack", http.Header{"Host": {"evil.example"}}, 421, "use https://127.0.0.1"},
		{"no credential", refs + "git-upload-pack", nil, 401, "no Open Machine credential"},
		{"a guessed credential", refs + "git-upload-pack", basic("guess"), 401, "no Open Machine credential"},
		{"another repository", b.URL + "/github.com/owner/other/info/refs?service=git-upload-pack", basic(password), 403, "another repository"},
		{"Read Only push", refs + "git-receive-pack", basic(password), 403, "Read Only access fetches"},
		{"an unsupported service", refs + "git-upload-archive", basic(password), 400, "unsupported Git request"},
		{"another host", b.URL + "/gitlab.com/owner/app/info/refs?service=git-upload-pack", basic(password), 404, "only github.com"},
	} {
		req, _ := http.NewRequest("GET", c.url, nil)
		for k, v := range c.header {
			req.Header[k] = v
		}
		if host := c.header.Get("Host"); host != "" {
			req.Host = host
		}
		res, err := daemon.PinnedClient(b.Pin, 0).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		out, _ := io.ReadAll(res.Body)
		res.Body.Close()
		if res.StatusCode != c.status || res.Header.Get("Content-Type") != "text/plain" || !strings.Contains(string(out), c.text) {
			t.Errorf("%s = %d %q %q", c.name, res.StatusCode, res.Header.Get("Content-Type"), out)
		}
		if c.status == 401 && res.Header.Get("WWW-Authenticate") != `Basic realm="Open Machine"` {
			t.Errorf("%s: no Basic challenge", c.name)
		}
	}
	end()
	if status, _ := credential(t, b, capability, "github.com/owner/app"); status != 403 {
		t.Fatalf("credential after the session = %d", status)
	}
	if status, _, out := call(t, b, "GET", refs+"git-upload-pack", basic(password), nil); status != 401 || !strings.Contains(out, "no Open Machine credential") {
		t.Fatalf("a credential after its session = %d %q", status, out)
	}
}

func TestBrokerReportsCloudOutcomes(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, _ := newBroker(t, cloud, "full_access")
	_, password := credential(t, b, capability, "github.com/owner/app")
	push := func(body string) (int, string) {
		h := basic(password)
		h.Set("Content-Type", "application/x-git-receive-pack-request")
		status, _, out := call(t, b, "POST", b.URL+"/github.com/owner/app.git/git-receive-pack", h, []byte(body))
		return status, out
	}
	command := fmt.Sprintf("%s %s refs/heads/feature\x00report-status side-band-64k\n", strings.Repeat("a", 40), strings.Repeat("b", 40))
	request := fmt.Sprintf("%04x%s0000PACK", len(command)+4, command)
	for _, c := range []struct {
		name  string
		reply func(w http.ResponseWriter, r *http.Request)
		want  string
	}{
		{"a refusal Cloud explains", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(403)
			io.WriteString(w, `{"error":"code_authority_changed","message":"Code authority changed; reconnect.","effect":"known_no_effect"}`)
		}, "\x03om: Code authority changed; reconnect.\n"},
		{"a refused ticket", func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(401)
			io.WriteString(w, `{"error":"code_ticket_expired","message":"The Code ticket expired.","effect":"known_no_effect"}`)
		}, "\x03om: The Code ticket expired.\n"},
		{"a partial reply", func(w http.ResponseWriter, r *http.Request) {
			io.Copy(io.Discard, r.Body)
			w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
			io.WriteString(w, "0011\x01000eunpa")
		}, "outcome is unknown"},
		{"a lost reply", func(w http.ResponseWriter, r *http.Request) {
			io.Copy(io.Discard, r.Body)
			conn, _, _ := w.(http.Hijacker).Hijack()
			conn.Close()
		}, "outcome is unknown"},
	} {
		cloud.reply(c.reply)
		status, out := push(request)
		if status != 200 || !strings.Contains(out, c.want) || !strings.HasSuffix(out, "0000") {
			t.Errorf("%s = %d %q", c.name, status, out)
		}
	}
	// Cloud may pass a push's commands on before it has the whole body: deletions alone need no pack.
	cloud.reply(func(w http.ResponseWriter, r *http.Request) {
		io.CopyN(io.Discard, r.Body, 4)
		conn, _, _ := w.(http.Hijacker).Hijack()
		conn.Close()
	})
	if status, out := push(request + strings.Repeat("P", 8<<20)); status != 200 || !strings.Contains(out, "outcome is unknown") {
		t.Errorf("a reply lost before Cloud had the whole push = %d %q", status, out)
	}
	// git's empty probe before a large push passes Cloud's empty answer through.
	cloud.reply(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
	})
	if status, out := push("0000"); status != 200 || out != "" {
		t.Errorf("probe = %d %q", status, out)
	}
	// Without side-band, the reason is text.
	plain := strings.Replace(command, " side-band-64k", "", 1)
	cloud.reply(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(403)
		io.WriteString(w, `{"error":"code_github_not_permitted","message":"Not permitted.","effect":"known_no_effect"}`)
	})
	if status, out := push(fmt.Sprintf("%04x%s0000", len(plain)+4, plain)); status != 403 || out != "om: Not permitted.\n" {
		t.Errorf("plain refusal = %d %q", status, out)
	}
}

func TestGitCredentialAnswersOnlyItsBroker(t *testing.T) {
	var asked []string
	srv := fakeBroker(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var in map[string]string
		json.NewDecoder(r.Body).Decode(&in)
		asked = append(asked, r.Header.Get("Authorization")+" "+in["path"])
		if in["path"] == "github.com/owner/refused" {
			w.WriteHeader(403)
			io.WriteString(w, `{"error":"code_session_unavailable","message":"this SSH session has ended"}`)
			return
		}
		io.WriteString(w, `{"username":"om","password":"op-credential"}`)
	}))
	t.Setenv("OM_BROKER_CAPABILITY", "session-capability")
	host := strings.TrimPrefix(srv.URL, "https://")
	run := func(action, input string) (int, string, string) {
		var out, errout bytes.Buffer
		code := daemon.GitCredential([]string{action}, strings.NewReader(input), &out, &errout)
		return code, out.String(), errout.String()
	}
	if code, out, _ := run("get", "protocol=https\nhost="+host+"\npath=github.com/owner/app.git\n\n"); code != 0 || out != "username=om\npassword=op-credential\n" {
		t.Fatalf("get = %d %q", code, out)
	}
	if code, out, errout := run("get", "protocol=https\nhost="+host+"\npath=github.com/owner/refused\n\n"); code != 0 || out != "quit=1\n" ||
		!strings.Contains(errout, "this SSH session has ended") {
		t.Fatalf("refused get = %d %q %q", code, out, errout)
	}
	for _, c := range [][2]string{{"get", "protocol=https\nhost=github.com\npath=owner/app\n\n"}, {"get", "protocol=http\nhost=" + host + "\npath=owner/app\n\n"},
		{"store", "protocol=https\nhost=" + host + "\npassword=x\n\n"}, {"erase", "protocol=https\nhost=" + host + "\n\n"}} {
		if code, out, _ := run(c[0], c[1]); code != 0 || out != "" {
			t.Errorf("%s %q = %d %q", c[0], c[1], code, out)
		}
	}
	if len(asked) != 2 || asked[0] != "Bearer session-capability github.com/owner/app.git" {
		t.Fatalf("broker asked %v", asked)
	}
}

func TestALostPullRequestCreationIsUnknownAndOmSaysItMayExist(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	lose := func(w http.ResponseWriter, r *http.Request) {
		conn, _, _ := w.(http.Hijacker).Hijack()
		conn.Close()
	}
	cloud.reply(lose)
	b, capability, _ := newBroker(t, cloud, "full_access")
	for operation, effect := range map[string]string{"pull_create": "effect_unknown", "pull_list": "known_no_effect"} {
		call := map[string]any{"v": 1, "operation": operation, "state": "open", "head": nil, "base": nil}
		if operation == "pull_create" {
			call = map[string]any{"v": 1, "operation": operation, "title": "x", "body": "", "head": "feature", "base": nil, "draft": false}
		}
		body, _ := json.Marshal(map[string]any{"repository": "owner/app", "call": call})
		if status, out := v1(t, b, capability, "/v1/github", body); status != 502 || !strings.Contains(out, `"effect":"`+effect+`"`) {
			t.Errorf("%s with its reply lost = %d %q", operation, status, out)
		}
	}
	// When the broker's own answer is lost, om github pr create says the pull request may exist.
	fakeBroker(t, http.HandlerFunc(lose))
	t.Setenv("OM_BROKER_CAPABILITY", strings.Repeat("c", 43))
	var out, errout bytes.Buffer
	code := daemon.GitHub([]string{"pr", "create", "--repo", "owner/app", "--title", "x", "--head", "feature"}, &out, &errout)
	if code == 0 || !strings.Contains(errout.String(), "the pull request may exist") {
		t.Fatalf("pr create with its answer lost = %d %q", code, errout.String())
	}
}

func TestOneSessionCannotTakeEveryCommandSlot(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, _ := newBroker(t, cloud, "read_only")
	for i := range 1024 {
		if status, _ := credential(t, b, capability, "github.com/owner/app"); status != 200 {
			t.Fatalf("command %d = %d", i, status)
		}
	}
	if status, _ := credential(t, b, capability, "github.com/owner/app"); status != 429 {
		t.Fatalf("one command too many = %d", status)
	}
	other, _ := b.Open(daemon.GitSession{Source: "dev_source", SourceGrantRevision: 1, TargetGrant: "grant", TargetGrantRevision: 1,
		PolicyRevision: 2, Mode: "read_only", Live: func() error { return nil }, Done: make(chan struct{})})
	if status, _ := credential(t, b, other, "github.com/owner/app"); status != 200 {
		t.Fatalf("another session = %d", status)
	}
}

func TestGitCredentialSendsOnlyItsOwnProofFromItsRouting(t *testing.T) {
	capability, proof := strings.Repeat("c", 43), strings.Repeat("p", 43)
	var sent []string
	dir := t.TempDir()
	srv := fakeBroker(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := r.Header.Get("X-OM-Broker-Proof")
		sent = append(sent, got)
		if got != proof {
			w.WriteHeader(401)
			io.WriteString(w, `{"error":"code_proof_required","message":"proof","proof_file":"/etc/hosts"}`)
			return
		}
		io.WriteString(w, `{"username":"om","password":"op-credential"}`)
	}))
	t.Setenv("OM_BROKER_CAPABILITY", capability)
	t.Setenv("GIT_CONFIG_COUNT", "2")
	t.Setenv("GIT_CONFIG_KEY_1", "credential."+srv.URL+"."+daemon.ProofsKey)
	t.Setenv("GIT_CONFIG_VALUE_1", dir)
	get := func() string {
		var out, errout bytes.Buffer
		daemon.GitCredential([]string{"get"}, strings.NewReader("protocol=https\nhost="+strings.TrimPrefix(srv.URL, "https://")+"\npath=github.com/o/r\n\n"), &out, &errout)
		return out.String()
	}
	// Whatever answers at the broker's port cannot choose the file: a link in the routing's
	// directory, as to another session's proof, is not followed, and a missing proof is not sent.
	elsewhere := filepath.Join(t.TempDir(), "other")
	os.WriteFile(elsewhere, []byte(proof), 0o600)
	os.Symlink(elsewhere, filepath.Join(dir, capability))
	if out := get(); out != "quit=1\n" || len(sent) != 1 {
		t.Fatalf("through a link = %q, sent %q", out, sent)
	}
	os.Remove(filepath.Join(dir, capability))
	os.WriteFile(filepath.Join(dir, capability), []byte(proof+"\n"), 0o600)
	if out := get(); out != "username=om\npassword=op-credential\n" || len(sent) != 3 || sent[2] != proof {
		t.Fatalf("own proof = %q, sent %q", out, sent)
	}
	t.Setenv("GIT_CONFIG_COUNT", "1") // the pin alone
	if out := get(); out != "quit=1\n" || len(sent) != 4 {
		t.Fatalf("without a proofs directory = %q, sent %q", out, sent)
	}
}

func TestWorkspaceAndReadOnlyFetchButNeverWrite(t *testing.T) {
	for mode, refusal := range map[string]string{
		"workspace": "Workspace fetches and reads through Open Machine but does not push or open pull requests; that needs Full access",
		"read_only": "Read Only access fetches through Open Machine but does not push or open pull requests",
	} {
		cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
		b, capability, _ := newBroker(t, cloud, mode)
		if status, _ := credential(t, b, capability, "github.com/org/app.git"); status != 200 {
			t.Fatalf("%s: credential = %d", mode, status)
		}
		body, _ := json.Marshal(map[string]any{"repository": "org/app", "call": map[string]any{"v": 1, "operation": "pull_create",
			"title": "x", "body": "", "head": "feature", "base": nil, "draft": false}})
		// No proof: a restricted session's om runs in the sandbox, where it could not read one.
		status, _, out := call(t, b, "POST", b.URL+"/v1/github", http.Header{"Authorization": {"Bearer " + capability}, "Content-Type": {"application/json"}}, body)
		cloud.mu.Lock()
		tickets := cloud.tickets
		cloud.mu.Unlock()
		_, workspace := tickets[0]["workspace"]
		if len(tickets) != 1 || fmt.Sprint(tickets[0]["actions"]) != "[read]" || workspace ||
			status != 403 || !strings.Contains(out, refusal) {
			t.Errorf("%s: tickets %v, pull_create = %d %q", mode, tickets, status, out)
		}
	}
}

func TestBrokerSharesOnlyTicketsForTheCurrentDefault(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 30 * time.Second}
	cloud.reply(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/x-git-upload-pack-advertisement")
		io.WriteString(w, "001e# service=git-upload-pack\n0000")
	})
	b, capability, _ := newBroker(t, cloud, "full_access")
	_, password := credential(t, b, capability, "github.com/owner/app")
	// The command's short ticket is minted again for its pinned account, this time for long...
	cloud.mu.Lock()
	cloud.ttl = 15 * time.Minute
	cloud.mu.Unlock()
	if status, _, _ := call(t, b, "GET", b.URL+"/github.com/owner/app/info/refs?service=git-upload-pack", basic(password), nil); status != 200 {
		t.Fatalf("info/refs = %d", status)
	}
	// ...but a new command in that scope resolves the current default rather than inherit the pin.
	credential(t, b, capability, "github.com/owner/app")
	cloud.mu.Lock()
	defer cloud.mu.Unlock()
	if len(cloud.tickets) != 3 || cloud.tickets[1]["account"] == nil || cloud.tickets[2]["account"] != nil {
		t.Fatalf("ticket requests = %v", cloud.tickets)
	}
}

func TestBrokerReportsAnUnreadableFailureAfterAWholePushAsUnknown(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, _ := newBroker(t, cloud, "full_access")
	_, password := credential(t, b, capability, "github.com/owner/app")
	command := fmt.Sprintf("%s %s refs/heads/feature\x00report-status side-band-64k\n", strings.Repeat("a", 40), strings.Repeat("b", 40))
	request := fmt.Sprintf("%04x%s0000PACK", len(command)+4, command)
	for _, reply := range []func(w http.ResponseWriter, r *http.Request){
		func(w http.ResponseWriter, r *http.Request) { // an edge error page after the whole push
			io.Copy(io.Discard, r.Body)
			w.WriteHeader(502)
			io.WriteString(w, "<html>Bad gateway</html>")
		},
		func(w http.ResponseWriter, r *http.Request) {
			io.Copy(io.Discard, r.Body)
			w.WriteHeader(503)
			io.WriteString(w, `{"error":"temporarily_unavailable","message":"x","effect":"effect_unknown"}`)
		},
	} {
		cloud.reply(reply)
		h := basic(password)
		h.Set("Content-Type", "application/x-git-receive-pack-request")
		if status, _, out := call(t, b, "POST", b.URL+"/github.com/owner/app.git/git-receive-pack", h, []byte(request)); status != 200 || !strings.Contains(out, "outcome is unknown") {
			t.Errorf("reply = %d %q", status, out)
		}
	}
}

func TestBrokerStopsRequestsWhenTheConnectionEnds(t *testing.T) {
	held := make(chan struct{})
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	cloud.git = func(w http.ResponseWriter, r *http.Request) {
		close(held)
		<-r.Context().Done() // Cloud holds the request until the broker gives it up
	}
	b, capability, _, closeConnection := openBroker(t, cloud, daemon.GitSession{Mode: "full_access"})
	_, password := credential(t, b, capability, "github.com/owner/app")
	answered := make(chan string, 1)
	go func() {
		_, _, out := call(t, b, "GET", b.URL+"/github.com/owner/app/info/refs?service=git-upload-pack", basic(password), nil)
		answered <- out
	}()
	<-held
	closeConnection()
	select {
	case out := <-answered:
		if !strings.Contains(out, "could not reach Cloud") {
			t.Fatalf("after the connection ended = %q", out)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a request in flight outlived its connection")
	}
}

func TestBrokerCutsAChunkedPushAtTheLimit(t *testing.T) {
	var received int64
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	cloud.git = func(w http.ResponseWriter, r *http.Request) {
		n, _ := io.Copy(io.Discard, r.Body)
		cloud.mu.Lock()
		received = n
		cloud.mu.Unlock()
	}
	b, capability, _ := newBroker(t, cloud, "full_access")
	_, password := credential(t, b, capability, "github.com/owner/app")
	// GitHub acts once a whole pack, or deletions alone, arrived: a push cut at the limit may still apply.
	for _, next := range []string{strings.Repeat("b", 40), strings.Repeat("0", 40)} {
		command := fmt.Sprintf("%s %s refs/heads/feature\x00report-status side-band-64k\n", strings.Repeat("a", 40), next)
		head := fmt.Sprintf("%04x%s0000PACK", len(command)+4, command)
		body := io.MultiReader(strings.NewReader(head), io.LimitReader(zeros{}, 91<<20)) // no length: sent chunked
		req, _ := http.NewRequest("POST", b.URL+"/github.com/owner/app.git/git-receive-pack", body)
		req.SetBasicAuth("om", password)
		req.Header.Set("Content-Type", "application/x-git-receive-pack-request")
		res, err := daemon.PinnedClient(b.Pin, 0).Do(req)
		if err != nil {
			t.Fatal(err)
		}
		out, _ := io.ReadAll(res.Body)
		res.Body.Close()
		if res.StatusCode != 200 || !strings.Contains(string(out), "exceeded Open Machine's 90 MiB limit and was cut, so its outcome is unknown") {
			t.Fatalf("a push to %s over the limit = %d %q", next[:4], res.StatusCode, out)
		}
		cloud.mu.Lock()
		got := received
		cloud.mu.Unlock()
		if got > 90<<20 {
			t.Fatalf("Cloud received %d bytes", got)
		}
	}
}

type zeros struct{}

func (zeros) Read(p []byte) (int, error) {
	clear(p)
	return len(p), nil
}

func TestBrokerSharesATicketForAMinuteWithinOneScope(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	b, capability, _ := newBroker(t, cloud, "full_access")
	for _, path := range []string{"github.com/owner/app", "github.com/owner/app.git", "github.com/owner/other"} {
		if status, _ := credential(t, b, capability, path); status != 200 {
			t.Fatalf("credential for %s = %d", path, status)
		}
	}
	cloud.mu.Lock()
	defer cloud.mu.Unlock()
	if len(cloud.tickets) != 2 || cloud.tickets[1]["repository"] != "owner/other" {
		t.Fatalf("ticket requests = %v", cloud.tickets)
	}
}
