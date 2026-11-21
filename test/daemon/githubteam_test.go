package daemon

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// TestOmProcess is om as Git runs it, when a test's routing names this test binary's client script.
func TestOmProcess(t *testing.T) {
	if os.Getenv("OM_TEST_PROCESS") != "1" {
		return
	}
	if every, err := time.ParseDuration(os.Getenv("OM_TEST_STILL_EVERY")); err == nil {
		daemon.StillEvery = every
	}
	args := os.Args[slices.Index(os.Args, "--")+1:]
	os.Exit(daemon.GitCredential(args[1:], os.Stdin, os.Stdout, os.Stderr))
}

// omClient writes the client a session's routing runs: this test binary as om, in a directory whose
// name Git's configuration must quote and escape.
func omClient(t *testing.T) string {
	dir := filepath.Join(t.TempDir(), `o"m\x`)
	os.Mkdir(dir, 0o755)
	path := filepath.Join(dir, "om")
	script := fmt.Sprintf("#!/bin/sh\nOM_TEST_PROCESS=1 exec '%s' -test.run='^TestOmProcess$' -- \"$@\"\n", os.Args[0])
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path
}

const (
	request     = "0123456789abcdef0123456789abcdef"
	readMessage = "Open Machine needs the user’s approval to use owner/app.\nTell the user: choose 42 in Desktop or the Console.\n" +
		"The number works for 5 minutes. Then run this command again at once: it waits until they have."
	pushMessage = "Open Machine needs the user’s approval to push to main.\nTell the user: choose 57 in Desktop or the Console.\n" +
		"The number works for 5 minutes. Then run this command again at once: it waits until they have."
)

// approvalRequired writes Cloud's refusal of a use that waits for its person, numbered number.
func approvalRequired(w http.ResponseWriter, message, number string, expiresIn int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	json.NewEncoder(w).Encode(map[string]any{"error": "github_approval_required", "message": message, "request": request,
		"number": number, "expires_in": expiresIn, "effect": "known_no_effect"})
}

// teamCloud is a Cloud whose first asks tickets refuse until the person approves, and whose waits answer
// as wait scripts them, the nth call from 1.
type teamCloud struct {
	*scriptedCloud
	asks   atomic.Int32 // tickets still to refuse
	expiry int          // the seconds a refusal gives
	wait   func(n int, w http.ResponseWriter, r *http.Request)
	waits  atomic.Int32
	began  []time.Time // when each wait call began
}

func (c *teamCloud) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case strings.HasSuffix(r.URL.Path, "/code/github/wait"):
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		c.mu.Lock()
		c.began = append(c.began, time.Now())
		c.mu.Unlock()
		if body["request"] != request || body["v"] != float64(1) {
			http.Error(w, "unexpected", http.StatusBadRequest)
			return
		}
		c.wait(int(c.waits.Add(1)), w, r)
	case strings.HasSuffix(r.URL.Path, "/code/github/ticket") && c.asks.Add(-1) >= 0:
		approvalRequired(w, readMessage, "42", c.expiry)
	default:
		c.scriptedCloud.ServeHTTP(w, r)
	}
}

// answers scripts waits: each call answers the next state, the last one repeating.
func answers(states ...string) func(int, http.ResponseWriter, *http.Request) {
	return func(n int, w http.ResponseWriter, r *http.Request) {
		state := states[min(n, len(states))-1]
		if strings.HasPrefix(state, "{") {
			w.WriteHeader(http.StatusForbidden)
			io.WriteString(w, state)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"state": state})
	}
}

// sessionEnv is a session's environment, as SSH gives it, with no Git configuration but its routing.
func sessionEnv(t *testing.T, b *daemon.Broker, capability string) []string {
	env := []string{"HOME=" + t.TempDir(), "PATH=" + os.Getenv("PATH"), "GIT_CONFIG_NOSYSTEM=1", "GIT_TERMINAL_PROMPT=0", "LANG=C"}
	for k, v := range b.Environment(capability) {
		env = append(env, k+"="+v)
	}
	return env
}

// git runs git in dir with env, stderr to stderr, and returns its stdout and stderr when stderr is nil.
func git(t *testing.T, env []string, dir string, stderr *os.File, args ...string) (string, string, error) {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Env, cmd.Dir = env, dir
	var out, errout bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errout
	if stderr != nil {
		cmd.Stderr = stderr
	}
	err := cmd.Run()
	return out.String(), errout.String(), err
}

// screen reads what a terminal shows until its last writer closes it, as plain lines.
func screen(master, slave *os.File) func() string {
	read := make(chan string, 1)
	go func() {
		b, _ := io.ReadAll(master)
		read <- plain(string(b))
	}()
	return func() string {
		slave.Close()
		select {
		case s := <-read:
			return s
		case <-time.After(10 * time.Second):
			return "the terminal did not close"
		}
	}
}

// plain is what a terminal shows as lines, without the spaces and controls git ends remote lines with.
func plain(s string) string {
	return regexp.MustCompile(` +\n`).ReplaceAllString(strings.NewReplacer("\r", "", "\x1b[K", "").Replace(s), "\n")
}

// TestRoutingFileFollowsTheSourcesAllow: a session's routing is one file of today's pairs, at an absolute
// path its environment includes, there exactly while its source's allow has github.
func TestRoutingFileFollowsTheSourcesAllow(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	var allow atomic.Bool
	allow.Store(true)
	client := omClient(t)
	b, capability, end, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only", Client: client, GitHub: allow.Load})
	env := b.Environment(capability)
	file := env["GIT_CONFIG_VALUE_0"]
	if len(env) != 5 || env["OM_BROKER"] != b.URL || env["OM_BROKER_CAPABILITY"] != capability || env["GIT_CONFIG_COUNT"] != "1" ||
		env["GIT_CONFIG_KEY_0"] != "include.path" || !filepath.IsAbs(file) || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(filepath.Base(file)) {
		t.Fatalf("environment %v", env)
	}
	folder := filepath.Dir(file)
	listed := func() []string {
		t.Helper()
		out, err := exec.Command("git", "config", "--file", file, "--list").Output()
		if err != nil {
			t.Fatalf("git config --list: %v", err)
		}
		return strings.Split(strings.TrimSuffix(string(out), "\n"), "\n")
	}
	proofs := routedValue(b, capability, "credential."+b.URL+"."+daemon.ProofsKey)
	want := []string{
		"url." + b.URL + "/github.com/.insteadof=https://github.com/",
		"url." + b.URL + "/github.com/.insteadof=git@github.com:",
		"url." + b.URL + "/github.com/.insteadof=ssh://git@github.com/",
		"http." + b.URL + ".sslverify=false",
		"http." + b.URL + ".pinnedpubkey=" + b.Pin,
		"http." + b.URL + ".proxy=",
		"credential." + b.URL + ".helper=",
		"credential." + b.URL + ".helper=!'" + client + "' git-credential",
		"credential." + b.URL + ".usehttppath=true",
		"http." + b.URL + ".postbuffer=" + fmt.Sprint(90<<20),
		"credential." + b.URL + ".openmachineproofs=" + proofs,
	}
	if got := listed(); !slices.Equal(got, want) || filepath.Base(proofs) != "proofs" {
		t.Fatalf("routing\n%s\nwant\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	if fi, err := os.Stat(file); err != nil || fi.Mode().Perm() != 0o644 {
		t.Fatalf("routing file %v %v", fi, err)
	}
	// Its source's allow loses github: the file goes, and the broker refuses without asking Cloud.
	allow.Store(false)
	b.Reroute()
	if _, err := os.Stat(file); !os.IsNotExist(err) {
		t.Fatalf("without github the file remains: %v", err)
	}
	body, _ := json.Marshal(map[string]string{"path": "github.com/owner/app"})
	if status, out := v1(t, b, capability, "/v1/credential", body); status != 403 ||
		!strings.Contains(out, "GitHub does not go through Open Machine in this session") {
		t.Fatalf("without github = %d %q", status, out)
	}
	cloud.mu.Lock()
	asked := cloud.paths
	cloud.mu.Unlock()
	if asked != nil {
		t.Fatalf("Cloud was asked %v", asked)
	}
	for _, on := range []bool{true, false, true} { // in quick succession, the file follows the last state
		allow.Store(on)
		b.Reroute()
	}
	if got := listed(); !slices.Equal(got, want) {
		t.Fatalf("routing again = %v", got)
	}
	if status, _ := credential(t, b, capability, "github.com/owner/app"); status != 200 {
		t.Fatalf("with github again = %d", status)
	}
	end()
	if entries, _ := os.ReadDir(folder); len(entries) != 0 {
		t.Fatalf("after the session the routing folder holds %v", entries)
	}
}

// TestGitReadsTheRoutingWhenItStarts: each Git process reads the file as it is: missing, it uses the
// machine's own configuration; unreadable or named relatively, it fails.
func TestGitReadsTheRoutingWhenItStarts(t *testing.T) {
	cloud := &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}
	var allow atomic.Bool
	allow.Store(true)
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only", GitHub: allow.Load})
	env := sessionEnv(t, b, capability)
	repo := t.TempDir()
	if _, errout, err := git(t, env, repo, nil, "init", "-q"); err != nil {
		t.Fatal(errout)
	}
	pin := "http." + b.URL + ".pinnedPubkey"
	if out, errout, err := git(t, env, repo, nil, "config", "--get", pin); err != nil || out != b.Pin+"\n" {
		t.Fatalf("routed: %q %q %v", out, errout, err)
	}
	allow.Store(false)
	b.Reroute()
	if out, _, err := git(t, env, repo, nil, "config", "--get", pin); out != "" || err == nil {
		t.Fatalf("without the file: %q %v", out, err)
	}
	if _, errout, err := git(t, env, repo, nil, "status", "--short"); err != nil {
		t.Fatalf("without the file git status fails: %s", errout)
	}
	relative := append(slices.Clone(env), "GIT_CONFIG_VALUE_0="+filepath.Base(b.Environment(capability)["GIT_CONFIG_VALUE_0"]))
	if _, errout, err := git(t, relative, repo, nil, "status"); err == nil || !strings.Contains(errout, "relative config includes") {
		t.Fatalf("a relative include: %q %v", errout, err)
	}
	if os.Getuid() == 0 {
		t.Log("NOT RUN as root, who reads any file: an unreadable routing file")
		return
	}
	allow.Store(true)
	b.Reroute()
	os.Chmod(b.Environment(capability)["GIT_CONFIG_VALUE_0"], 0)
	if _, errout, err := git(t, env, repo, nil, "status"); err == nil || !strings.Contains(errout, "Permission denied") {
		t.Fatalf("an unreadable file: %q %v", errout, err)
	}
}

// A ref advertisement of one branch, as GitHub answers a fetch.
func advertise(w http.ResponseWriter, r *http.Request) {
	io.Copy(io.Discard, r.Body)
	service := r.URL.Query().Get("service")
	w.Header().Set("Content-Type", "application/x-"+service+"-advertisement")
	ref := strings.Repeat("1", 40)
	caps := "side-band-64k ofs-delta"
	if service == "git-receive-pack" {
		ref, caps = strings.Repeat("0", 40), "report-status side-band-64k delete-refs ofs-delta"
	}
	name := "refs/heads/main"
	if service == "git-receive-pack" {
		name = "capabilities^{}"
	}
	fmt.Fprint(w, pkt("# service="+service+"\n")+"0000"+pkt(ref+" "+name+"\x00"+caps+"\n")+"0000")
}

func pkt(s string) string { return fmt.Sprintf("%04x%s", len(s)+4, s) }

// TestApprovalInATerminal: Git gives om its standard error, so a person at a terminal is shown the number
// and om waits, through the broker, for the approval; then the same command goes on.
func TestApprovalInATerminal(t *testing.T) {
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute, git: advertise},
		expiry: 300, wait: answers("waiting", "approved")}
	cloud.asks.Store(1)
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "full_access", Client: omClient(t)})
	master, slave := terminalPair(t)
	shown := screen(master, slave)
	out, _, err := git(t, sessionEnv(t, b, capability), t.TempDir(), slave, "ls-remote", "https://github.com/owner/app")
	text := shown()
	if err != nil || out != strings.Repeat("1", 40)+"\trefs/heads/main\n" {
		t.Fatalf("ls-remote = %q %v; terminal %q", out, err, text)
	}
	if !strings.Contains(text, "Open Machine needs your approval.\nChoose 42 in Desktop or the Console.\nWaiting up to 5 minutes…\n") ||
		strings.Count(text, "Open Machine") != 1 || cloud.waits.Load() != 2 || len(cloud.tickets) != 1 {
		t.Fatalf("terminal %q, %d waits, %d tickets", text, cloud.waits.Load(), len(cloud.tickets))
	}
}

// TestApprovalForAnAgent: without a terminal, as for an AI agent, om shows Cloud's own words and fails at once.
func TestApprovalForAnAgent(t *testing.T) {
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute, git: advertise},
		expiry: 300, wait: func(n int, w http.ResponseWriter, r *http.Request) {
			if n < 3 {
				time.Sleep(300 * time.Millisecond)
				answers("waiting")(n, w, r)
				return
			}
			answers("approved")(n, w, r)
		}}
	cloud.asks.Store(1)
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "full_access", Client: omClient(t)})
	env := append(sessionEnv(t, b, capability), "OM_TEST_STILL_EVERY=250ms")
	out, errout, err := git(t, env, t.TempDir(), nil, "ls-remote", "https://github.com/owner/app")
	if err == nil || out != "" || !strings.HasPrefix(errout, readMessage+"\n") || cloud.waits.Load() != 0 || cloud.asks.Load() != 0 {
		t.Fatalf("ls-remote = %q %v %q; %d waits", out, err, errout, cloud.waits.Load())
	}
	// Run again at once, as the message says: it waits, saying so, and goes on once the person approves,
	// with nobody telling the agent.
	cloud.asks.Store(1)
	out, errout, err = git(t, env, t.TempDir(), nil, "ls-remote", "https://github.com/owner/app")
	want := "Waiting for the user to choose 42 in Desktop or the Console…\nStill waiting for 42, 5 min left. If this command stops, run it again.\n"
	if err != nil || out == "" || !strings.HasPrefix(errout, want) || strings.Contains(errout, "Tell the user") || cloud.waits.Load() != 3 {
		t.Fatalf("run again = %q %v %q; %d waits", out, err, errout, cloud.waits.Load())
	}
}

// TestRefusedOrExpiredApproval: in a terminal, a refusal says om's reason after the number, as does a
// request nobody approved in time.
func TestRefusedOrExpiredApproval(t *testing.T) {
	for _, c := range []struct {
		name, want string
		expiry     int
		wait       func(int, http.ResponseWriter, *http.Request)
	}{
		{"denied", "om: The request was denied. Nothing reached GitHub.\n", 300,
			answers(`{"error":"github_request_denied","message":"The request was denied. Nothing reached GitHub.","effect":"known_no_effect"}`)},
		{"expired", "om: Nobody approved within 5 minutes. Nothing reached GitHub. Run the command again.\n", 1, answers("waiting")},
		{"unavailable", "om: This request is not waiting for this device.\n", 300, func(n int, w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			io.WriteString(w, `{"error":"github_request_unavailable","message":"This request is not waiting for this device.","effect":"known_no_effect"}`)
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}, expiry: c.expiry, wait: c.wait}
			cloud.asks.Store(1)
			b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only"})
			for k, v := range b.Environment(capability) {
				t.Setenv(k, v)
			}
			master, slave := terminalPair(t)
			shown := screen(master, slave)
			var out bytes.Buffer
			daemon.GitCredential([]string{"get"}, strings.NewReader("protocol=https\nhost="+strings.TrimPrefix(b.URL, "https://")+
				"\npath=github.com/owner/app\n\n"), &out, slave)
			if text := shown(); out.String() != "quit=1\n" || !strings.HasSuffix(text, "Waiting up to 5 minutes…\n"+c.want) {
				t.Fatalf("%q, terminal %q", out.String(), text)
			}
		})
	}
}

// TestOmGithubWaits: om github waits for the approval in a terminal and then makes its call
// once more; for an agent it shows Cloud's words and fails.
func TestOmGithubWaits(t *testing.T) {
	var calls atomic.Int32
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute, git: func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		io.WriteString(w, `{"result":[]}`)
	}}, expiry: 300, wait: answers("approved")}
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only"})
	for k, v := range b.Environment(capability) {
		t.Setenv(k, v)
	}
	cloud.asks.Store(1)
	var out, errout bytes.Buffer
	if code := daemon.GitHub([]string{"pr", "list", "--repo", "owner/app"}, &out, &errout); code != 1 || errout.String() != readMessage+"\n" {
		t.Fatalf("for an agent = %d %q", code, errout.String())
	}
	cloud.asks.Store(1)
	errout.Reset()
	if code := daemon.GitHub([]string{"pr", "list", "--repo", "owner/app"}, &out, &errout); code != 0 ||
		errout.String() != "Waiting for the user to choose 42 in Desktop or the Console…\n" || calls.Load() != 1 {
		t.Fatalf("for an agent, run again = %d %q", code, errout.String())
	}
	cloud.asks.Store(1)
	master, slave := terminalPair(t)
	shown := screen(master, slave)
	code := daemon.GitHub([]string{"pr", "list", "--repo", "owner/app2"}, &out, slave) // not the ticket just minted
	if text := shown(); code != 0 || text != "Open Machine needs your approval.\nChoose 42 in Desktop or the Console.\nWaiting up to 5 minutes…\n" ||
		calls.Load() != 2 || cloud.waits.Load() != 2 {
		t.Fatalf("in a terminal = %d %q, %d calls", code, text, calls.Load())
	}
}

// TestOmGithubPullLookupAsks: pr view's lookup of the branch's pull request is asked about like any
// call, so an agent is told the number at once and a rerun waits.
func TestOmGithubPullLookupAsks(t *testing.T) {
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute, git: func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"result":[{"number":7}]}`)
	}}, expiry: 300, wait: answers("approved")}
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only"})
	for k, v := range b.Environment(capability) {
		t.Setenv(k, v)
	}
	dir := t.TempDir()
	if out, err := exec.Command("git", "-C", dir, "init", "-q", "-b", "topic").CombinedOutput(); err != nil {
		t.Fatal(string(out))
	}
	t.Chdir(dir)
	cloud.asks.Store(1)
	var out, errout bytes.Buffer
	if code := daemon.GitHub([]string{"pr", "view", "--repo", "owner/app"}, &out, &errout); code != 1 || errout.String() != readMessage+"\n" {
		t.Fatalf("for an agent = %d %q", code, errout.String())
	}
	cloud.asks.Store(1)
	errout.Reset()
	if code := daemon.GitHub([]string{"pr", "view", "--repo", "owner/app"}, &out, &errout); code != 0 ||
		errout.String() != "Waiting for the user to choose 42 in Desktop or the Console…\n" || cloud.waits.Load() != 1 {
		t.Fatalf("run again = %d %q", code, errout.String())
	}
}

// pushResult is GitHub's report of a pushed main, in side-band.
var pushResult = pkt("\x01"+pkt("unpack ok\n")+pkt("ok refs/heads/main\n")+"0000") + "0000"

// pushCloud is a team operation's Cloud that refuses pushes until the person approves, as approve
// scripts it (the nth push from 1), and the bodies of the pushes it got.
func pushCloud(approve func(n int) bool) (*teamCloud, func() [][]byte) {
	var bodies [][]byte
	var mu sync.Mutex
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute, team: true}, expiry: 300}
	cloud.git = func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			advertise(w, r)
			return
		}
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, body)
		n := len(bodies)
		mu.Unlock()
		if !approve(n) {
			approvalRequired(w, pushMessage, fmt.Sprint(56+n), 300)
			return
		}
		w.Header().Set("Content-Type", "application/x-git-receive-pack-result")
		io.WriteString(w, pushResult)
	}
	return cloud, func() [][]byte { mu.Lock(); defer mu.Unlock(); return slices.Clone(bodies) }
}

// repository makes a repository of one commit on main whose origin is the GitHub repository owner/app.
func repository(t *testing.T, env []string) string {
	dir := t.TempDir()
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q",
		"--allow-empty", "-m", "x"}, {"remote", "add", "origin", "https://github.com/owner/app"}} {
		if _, errout, err := git(t, env, dir, nil, args...); err != nil {
			t.Fatal(errout)
		}
	}
	return dir
}

// noHeldPush fails t if a push is still held in the broker's state.
func noHeldPush(t *testing.T, b *daemon.Broker, capability string) {
	t.Helper()
	if held, _ := filepath.Glob(filepath.Join(filepath.Dir(proofFile(b, capability)), "push-*")); len(held) != 0 {
		t.Fatalf("held pushes remain: %v", held)
	}
}

// TestTeamPushWaitsInATerminal: a team operation's push is held whole; in a terminal git shows the number as
// remote lines, the broker waits, sends the same push again and git shows GitHub's result. A second refusal,
// as when an identical push used the approval first, shows a new number. The held push is then gone.
func TestTeamPushWaitsInATerminal(t *testing.T) {
	cloud, pushes := pushCloud(func(n int) bool { return n == 3 })
	cloud.wait = answers("approved")
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "full_access", Client: omClient(t)})
	env := sessionEnv(t, b, capability)
	dir := repository(t, env)
	master, slave := terminalPair(t)
	shown := screen(master, slave)
	_, _, err := git(t, env, dir, slave, "push", "origin", "main")
	text := shown()
	lines := func(number string) string {
		return "remote: Open Machine needs your approval.\nremote: Choose " + number + " in Desktop or the Console.\nremote: Waiting up to 5 minutes…\n"
	}
	if err != nil || !strings.Contains(text, lines("57")+lines("58")) || !strings.Contains(text, "main -> main") || strings.Contains(text, "Tell the user") {
		t.Fatalf("push = %v; terminal %q", err, text)
	}
	if got := pushes(); len(got) != 3 || !bytes.Equal(got[0], got[2]) || !bytes.Equal(got[1], got[2]) || cloud.waits.Load() != 2 {
		t.Fatalf("Cloud got %d pushes, %d waits", len(pushes()), cloud.waits.Load())
	}
	noHeldPush(t, b, capability)
}

// TestTeamPushForAnAgent: without a terminal the push fails at once with Cloud's words as remote lines.
func TestTeamPushForAnAgent(t *testing.T) {
	saved := daemon.StillEvery
	daemon.StillEvery = 200 * time.Millisecond
	t.Cleanup(func() { daemon.StillEvery = saved })
	cloud, pushes := pushCloud(func(n int) bool { return n == 3 })
	cloud.wait = func(n int, w http.ResponseWriter, r *http.Request) {
		if n == 1 {
			time.Sleep(300 * time.Millisecond)
			answers("waiting")(n, w, r)
			return
		}
		answers("approved")(n, w, r)
	}
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "full_access", Client: omClient(t)})
	env := sessionEnv(t, b, capability)
	dir := repository(t, env)
	_, errout, err := git(t, env, dir, nil, "push", "origin", "main")
	errout = plain(errout)
	if err == nil || len(pushes()) != 1 || cloud.waits.Load() != 0 ||
		!strings.Contains(errout, "remote: "+strings.ReplaceAll(pushMessage, "\n", "\nremote: ")+"\n") ||
		!strings.Contains(errout, "remote: om: approval needed\n") {
		t.Fatalf("push = %v %q; %d pushes, %d waits", err, errout, len(pushes()), cloud.waits.Load())
	}
	// Run again at once, it waits, saying so in remote lines, and the push goes through once approved.
	_, errout, err = git(t, env, dir, nil, "push", "origin", "main")
	errout = plain(errout)
	if err != nil || len(pushes()) != 3 || cloud.waits.Load() != 2 || strings.Contains(errout, "Tell the user") ||
		!strings.Contains(errout, "remote: Waiting for the user to choose 58 in Desktop or the Console…\n"+
			"remote: Still waiting for 58, 5 min left. If this command stops, run it again.\n") || !strings.Contains(errout, "main -> main") {
		t.Fatalf("push again = %v %q; %d pushes, %d waits", err, errout, len(pushes()), cloud.waits.Load())
	}
	noHeldPush(t, b, capability)
}

// TestTeamPushRefusals: without side-band the refusal is text at once; after the lines, a refusal of the
// wait is in band 3, and a wait ends with the session.
func TestTeamPushRefusals(t *testing.T) {
	cloud, _ := pushCloud(func(int) bool { return false })
	held, done := make(chan struct{}, 1), make(chan struct{})
	cloud.wait = func(n int, w http.ResponseWriter, r *http.Request) {
		if n == 1 {
			answers(`{"error":"github_request_denied","message":"The request was denied. Nothing reached GitHub.","effect":"known_no_effect"}`)(n, w, r)
			return
		}
		held <- struct{}{}
		<-done
	}
	b, capability, _, closeConnection := openBroker(t, cloud, daemon.GitSession{Mode: "full_access"})
	t.Cleanup(func() { close(done) })
	body, _ := json.Marshal(map[string]any{"path": "github.com/owner/app", "interactive": true})
	var reply struct{ Password string }
	_, out := v1(t, b, capability, "/v1/credential", body)
	json.Unmarshal([]byte(out), &reply)
	push := func(caps string) (int, string) {
		command := fmt.Sprintf("%s %s refs/heads/main\x00report-status%s\n", strings.Repeat("0", 40), strings.Repeat("b", 40), caps)
		h := basic(reply.Password)
		h.Set("Content-Type", "application/x-git-receive-pack-request")
		status, _, out := call(t, b, "POST", b.URL+"/github.com/owner/app.git/git-receive-pack", h, []byte(pkt(command)+"0000PACK"))
		return status, out
	}
	if status, out := push(""); status != 403 || out != "om: "+pushMessage+"\n" {
		t.Fatalf("without side-band = %d %q", status, out)
	}
	if status, out := push(" side-band-64k"); status != 200 || !strings.Contains(out, "\x02Choose 58 in Desktop or the Console.\n") ||
		!strings.HasSuffix(out, "\x03om: The request was denied. Nothing reached GitHub.\n0000") {
		t.Fatalf("a denied wait = %d %q", status, out)
	}
	go func() { <-held; closeConnection() }()
	status, out := push(" side-band-64k")
	if status != 200 || !strings.Contains(out, "\x02Choose 59 in Desktop or the Console.\n") ||
		!strings.HasSuffix(out, "\x03om: this SSH session has ended, and its GitHub access with it\n0000") {
		t.Fatalf("a wait whose session ended = %d %q", status, out)
	}
	noHeldPush(t, b, capability)
}

// TestBrokerWaitsOnceAtATime: commands waiting on one request share each Cloud call, refusals keep their
// fields, and a machine's calls stay within 20 a minute.
func TestBrokerWaitsOnceAtATime(t *testing.T) {
	release := make(chan struct{})
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}, expiry: 300}
	cloud.wait = func(n int, w http.ResponseWriter, r *http.Request) {
		if n == 1 {
			<-release
			answers("approved")(n, w, r)
			return
		}
		answers("waiting")(n, w, r)
	}
	cloud.asks.Store(1)
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only"})
	body, _ := json.Marshal(map[string]any{"path": "github.com/owner/app"})
	if status, out := v1(t, b, capability, "/v1/credential", body); status != 403 ||
		!strings.Contains(out, `"request":"`+request+`","number":"42","expires_in":300`) {
		t.Fatalf("refusal = %d %s", status, out)
	}
	wait, _ := json.Marshal(map[string]string{"request": request})
	if status, _ := v1(t, b, capability, "/v1/wait", []byte(`{"request":"../x"}`)); status != 400 {
		t.Fatalf("another request's name = %d", status)
	}
	got := make(chan string, 2)
	for range 2 {
		go func() { _, out := v1(t, b, capability, "/v1/wait", wait); got <- out }()
	}
	time.Sleep(300 * time.Millisecond)
	close(release)
	for range 2 {
		if out := <-got; out != `{"state":"approved"}`+"\n" || cloud.waits.Load() != 1 {
			t.Fatalf("a shared wait = %q after %d calls", out, cloud.waits.Load())
		}
	}
	for range 19 {
		v1(t, b, capability, "/v1/wait", wait)
	}
	go func() { // its answer outlives the test
		req, _ := http.NewRequest("POST", b.URL+"/v1/wait", bytes.NewReader(wait))
		req.Header.Set("Authorization", "Bearer "+capability)
		if res, err := daemon.PinnedClient(b.Pin, 0).Do(req); err == nil {
			res.Body.Close()
		}
	}()
	time.Sleep(time.Second)
	if n := cloud.waits.Load(); n != 20 {
		t.Fatalf("%d wait calls in a minute", n)
	}
}

// TestBrokerWaitsAfterAFailure: a call after a failed one starts 20 seconds after the failed one began.
func TestBrokerWaitsAfterAFailure(t *testing.T) {
	t.Parallel()
	cloud := &teamCloud{scriptedCloud: &scriptedCloud{account: strings.Repeat("a", 32), ttl: 15 * time.Minute}, expiry: 300}
	cloud.wait = func(n int, w http.ResponseWriter, r *http.Request) {
		if n == 1 {
			http.Error(w, "down", http.StatusServiceUnavailable)
			return
		}
		answers("approved")(n, w, r)
	}
	cloud.asks.Store(1)
	b, capability, _, _ := openBroker(t, cloud, daemon.GitSession{Mode: "read_only"})
	wait, _ := json.Marshal(map[string]string{"request": request})
	if status, out := v1(t, b, capability, "/v1/wait", wait); status != 404 || !strings.Contains(out, `"error":"github_request_unavailable"`) {
		t.Fatalf("a request Cloud named to no call here = %d %s", status, out)
	}
	body, _ := json.Marshal(map[string]any{"path": "github.com/owner/app"})
	v1(t, b, capability, "/v1/credential", body)
	if status, _ := v1(t, b, capability, "/v1/wait", wait); status != 502 {
		t.Fatalf("a failed call = %d", status)
	}
	if status, out := v1(t, b, capability, "/v1/wait", wait); status != 200 || !strings.Contains(out, "approved") {
		t.Fatalf("the next call = %d %q", status, out)
	}
	cloud.mu.Lock()
	defer cloud.mu.Unlock()
	if gap := cloud.began[1].Sub(cloud.began[0]); gap < 20*time.Second {
		t.Fatalf("the next call began %v after the failed one", gap)
	}
}

// pace shortens the daemon's GitHub pace for t.
func pace(t *testing.T, every, grace, requests time.Duration) {
	saved := []time.Duration{daemon.HoldEvery, daemon.HoldGrace, daemon.RequestsEvery}
	daemon.HoldEvery, daemon.HoldGrace, daemon.RequestsEvery = every, grace, requests
	t.Cleanup(func() { daemon.HoldEvery, daemon.HoldGrace, daemon.RequestsEvery = saved[0], saved[1], saved[2] })
}

// TestHolds: a dial to a team's machine opens the person's session alongside it and keeps it while a dial
// lasts; commands in a row share it; held: false posts nothing until a change; a failure waits its turn.
func TestHolds(t *testing.T) {
	pace(t, 1500*time.Millisecond, time.Second, daemon.RequestsEvery)
	posts := make(chan string, 32)
	var refuse, fail atomic.Bool
	release := make(chan struct{}, 1)
	h := daemon.NewHolds(func(target string, open bool) (bool, error) {
		posts <- fmt.Sprint(target, " ", open)
		if target == "slow" {
			<-release
		}
		if fail.Load() {
			return false, fmt.Errorf("down")
		}
		return !refuse.Load(), nil
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go h.Run(ctx)
	next := func(want string, within time.Duration) {
		t.Helper()
		select {
		case got := <-posts:
			if got != want {
				t.Fatalf("post %q, want %q", got, want)
			}
		case <-time.After(within):
			t.Fatalf("no post %q", want)
		}
	}
	none := func(d time.Duration) {
		t.Helper()
		select {
		case got := <-posts:
			t.Fatalf("unexpected post %q", got)
		case <-time.After(d):
		}
	}
	// The dial waits for its post; a second dial meanwhile posts nothing.
	ready, end1 := h.Dial("slow")
	next("slow true", time.Second)
	select {
	case <-ready:
		t.Fatal("the dial answered before its post")
	case <-time.After(200 * time.Millisecond):
	}
	_, end2 := h.Dial("slow")
	release <- struct{}{}
	<-ready
	none(500 * time.Millisecond)
	// Posted again while a dial lasts; then dials in a row share the session, closed after the grace.
	next("slow true", 2500*time.Millisecond)
	release <- struct{}{}
	end1()
	end2()
	_, end3 := h.Dial("slow")
	end3()
	next("slow false", 2500*time.Millisecond)
	// A dial during the closing post opens the session again once it ends, and waits for that.
	reopened, endSlow := h.Dial("slow")
	release <- struct{}{}
	next("slow true", time.Second)
	select {
	case <-reopened:
		t.Fatal("the dial answered before its session opened again")
	case <-time.After(200 * time.Millisecond):
	}
	release <- struct{}{}
	<-reopened
	h.Changed() // a held session is not posted again on a change
	none(500 * time.Millisecond)
	endSlow()
	next("slow false", 2500*time.Millisecond)
	release <- struct{}{}
	none(1500 * time.Millisecond)
	// held: false: nothing more, whatever dials, until a change, which posts for the dials that last.
	refuse.Store(true)
	_, end4 := h.Dial("m")
	next("m true", time.Second)
	time.Sleep(100 * time.Millisecond)
	_, end5 := h.Dial("m")
	none(2500 * time.Millisecond)
	refuse.Store(false)
	h.Changed()
	next("m true", time.Second)
	end4()
	end5()
	next("m false", 2500*time.Millisecond)
	// A failed post is posted again after its pace, not by the next dial, and its session, which may be open, is closed.
	fail.Store(true)
	_, end6 := h.Dial("f")
	next("f true", time.Second)
	time.Sleep(100 * time.Millisecond)
	_, end7 := h.Dial("f")
	none(time.Second)
	next("f true", 2*time.Second)
	fail.Store(false)
	end6()
	end7()
	next("f false", 2500*time.Millisecond)
}

// githubCloud is codeCloud with a person's GitHub routes, recording each body by its action.
func githubCloud(t *testing.T, decide func(w http.ResponseWriter)) (*cloud, string, chan string) {
	c, srv := codeCloud(t)
	bodies := make(chan string, 64)
	var now atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if r.Method != "POST" || !strings.HasSuffix(path, "/challenge") && !strings.Contains(path, "/github/") {
			srv.Config.Handler.ServeHTTP(w, r)
			return
		}
		body, _ := io.ReadAll(r.Body)
		switch {
		case strings.HasSuffix(path, "/challenge"):
			w.Write([]byte(`{"challenge_id":"id","challenge":"c"}`))
			return
		case strings.HasSuffix(path, "/github/requests"):
			at := now.Add(100)
			fmt.Fprintf(w, `{"now":%d,"requests":[{"id":"r1","machine":"GPU 01","repository":"owner/app","action":{"kind":"read"},`+
				`"choices":["12","42","77"],"expires_in":300}],"notices":[{"id":"n%d","machine":"GPU 01","repository":"owner/app",`+
				`"kind":"pushed","detail":"main","at":%d}]}`, at, at, at)
		case strings.HasSuffix(path, "/github/decide"):
			decide(w)
		case strings.HasSuffix(path, "/code/github/session"):
			io.WriteString(w, `{"held":true}`)
		}
		bodies <- path[strings.LastIndex(path, "/dev_watch/")+len("/dev_watch/"):] + " " + string(body)
	}))
	t.Cleanup(api.Close)
	return c, api.URL, bodies
}

// TestDesktopRequests: while a watch client is connected to a person's computer, the daemon reads the
// person's GitHub requests: when the view's account arrives, on changes (one read for those within
// RequestsEvery) and at once after a decision, which the socket's github line makes.
func TestDesktopRequests(t *testing.T) {
	pace(t, daemon.HoldEvery, daemon.HoldGrace, 2*time.Second)
	const result = `{"result":"approved"}`
	var wrong atomic.Bool
	c, origin, bodies := githubCloud(t, func(w http.ResponseWriter) {
		if wrong.Load() {
			w.WriteHeader(http.StatusForbidden)
			io.WriteString(w, `{"error":"github_person_required","message":"Only a device of a person’s own account answers GitHub requests.","effect":"known_no_effect"}`)
			return
		}
		io.WriteString(w, result)
	})
	home := runDaemon(t, origin, "dev_watch", "exit 1")
	socket := filepath.Join(daemon.StateDir(home), "daemon.sock")
	conn, err := net.Dial("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	io.WriteString(conn, "watch\n")
	lines := bufio.NewReader(conn)
	view := func() map[string]json.RawMessage {
		t.Helper()
		conn.SetReadDeadline(time.Now().Add(10 * time.Second))
		line, err := lines.ReadString('\n')
		var v map[string]json.RawMessage
		if err != nil || json.Unmarshal([]byte(line), &v) != nil {
			t.Fatalf("view %q %v", line, err)
		}
		return v
	}
	next := func(want string, within time.Duration) time.Time {
		t.Helper()
		select {
		case got := <-bodies:
			if got != want {
				t.Fatalf("Cloud got %q, want %q", got, want)
			}
		case <-time.After(within):
			t.Fatalf("Cloud got no %q", want)
		}
		return time.Now()
	}
	none := func(d time.Duration) {
		t.Helper()
		select {
		case got := <-bodies:
			t.Fatalf("Cloud got %q", got)
		case <-time.After(d):
		}
	}
	if v := view(); string(v["github"]) != "null" {
		t.Fatalf("before any read, github %s", v["github"])
	}
	none(500 * time.Millisecond) // no account yet, so no person
	p := c.presence()
	lease := frame(p.renewal(), "r1", 60000, granted())
	lease["account"] = json.RawMessage(`{"login":"alice","principal_id":"acct_one","provider":"github"}`)
	p.write(lease)
	p.applied(t, "r1")
	first := next(`github/requests {"after":0,"v":1}`, 5*time.Second)
	var github struct {
		Requests []map[string]any
		Notices  []any
	}
	for github.Requests == nil {
		json.Unmarshal(view()["github"], &github)
	}
	at, _ := github.Requests[0]["expires_at"].(float64)
	if _, left := github.Requests[0]["expires_in"]; left || len(github.Notices) != 0 || github.Requests[0]["id"] != "r1" ||
		int64(at) < time.Now().Unix()+298 || int64(at) > time.Now().Unix()+300 {
		t.Fatalf("the first read's view %+v", github)
	}
	// Changes within RequestsEvery of the last read make one read RequestsEvery after it, from Cloud's last now.
	p.changed()
	p.changed()
	read := next(`github/requests {"after":100,"v":1}`, 5*time.Second)
	if read.Sub(first) < 1900*time.Millisecond {
		t.Fatalf("a change read %v after the last read", read.Sub(first))
	}
	// A decision is posted as typed and answered with Cloud's result; the read after it is not delayed.
	decide := func(line string) string {
		t.Helper()
		c, err := net.Dial("unix", socket)
		if err != nil {
			t.Fatal(err)
		}
		defer c.Close()
		io.WriteString(c, "github "+line+"\n")
		c.SetReadDeadline(time.Now().Add(10 * time.Second))
		answer, _ := bufio.NewReader(c).ReadString('\n')
		return answer
	}
	if answer := decide(`{"request":"r1","decision":"choose","choice":"42"}`); answer != result+"\n" {
		t.Fatalf("decision = %q", answer)
	}
	next(`github/decide {"choice":"42","decision":"choose","request":"r1","v":1}`, time.Second)
	if after := next(`github/requests {"after":200,"v":1}`, time.Second); after.Sub(read) >= 1900*time.Millisecond {
		t.Fatalf("the read after a decision waited %v", after.Sub(read))
	}
	wrong.Store(true)
	if answer := decide(`{"request":"r1","decision":"deny"}`); answer != `{"error":{"code":"github_person_required","message":"Only a device of a person’s own account answers GitHub requests."}}`+"\n" {
		t.Fatalf("refused decision = %q", answer)
	}
	if answer := decide(`{"request":`); answer != `{"error":{"code":"invalid_fields","message":"send the request, the decision and the choice"}}`+"\n" {
		t.Fatalf("a malformed line = %q", answer)
	}
	next(`github/decide {"decision":"deny","request":"r1","v":1}`, time.Second)
	next(`github/requests {"after":300,"v":1}`, time.Second)
	none(2500 * time.Millisecond)
	// Later reads pass the notices on.
	for len(github.Notices) == 0 {
		json.Unmarshal(view()["github"], &github)
	}
	// With no watch client, nothing is read, not even a read scheduled while one was connected.
	p.changed()
	next(`github/requests {"after":400,"v":1}`, time.Second)
	p.changed()
	time.Sleep(200 * time.Millisecond)
	conn.Close()
	none(2500 * time.Millisecond)
	p.changed()
	none(2500 * time.Millisecond)
}

// TestDialHoldsATeamSession: a socket dial to a team's machine the view lists posts the person's session
// alongside it, whether or not the dial connects, and closes it after the grace; another machine posts nothing.
func TestDialHoldsATeamSession(t *testing.T) {
	pace(t, time.Minute, time.Second, daemon.RequestsEvery)
	c, origin, bodies := githubCloud(t, nil)
	home := runDaemon(t, origin, "dev_watch", "exit 1")
	p := c.presence()
	lease := granted()
	lease.Hosts = []daemon.Host{{DeviceID: "dev_gpu", NodeKey: "nodekey:gpu", Key: "ssh-ed25519 AAAA", User: "root", Aliases: []string{"gpu-01"}},
		{DeviceID: "dev_team", NodeKey: "nodekey:team", Key: "ssh-ed25519 AAAA", User: "root", Aliases: []string{"gpu-01.acme"}}}
	f := frame(p.renewal(), "r1", 60000, lease)
	f["devices"] = json.RawMessage(`[{"device_id":"dev_team","name":"GPU 01","mode":"read_only","alias":"gpu-01.acme","state":"online",` +
		`"last_seen_at":null,"core_version":null,"team":{"slug":"acme","name":"Acme"}}]`)
	p.write(f)
	p.applied(t, "r1")
	dial := func(host string) string {
		conn, err := net.Dial("unix", filepath.Join(daemon.StateDir(home), "daemon.sock"))
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(20 * time.Second))
		io.WriteString(conn, "dial "+host+"\n")
		line, _ := bufio.NewReader(conn).ReadString('\n')
		return line
	}
	const joining = "error the Code service is still joining the private network\n"
	if got := dial("gpu-01.om"); got != joining {
		t.Fatalf("dial = %q", got)
	}
	select {
	case got := <-bodies:
		t.Fatalf("a machine of the account posted %q", got)
	case <-time.After(500 * time.Millisecond):
	}
	if got := dial("gpu-01.acme.om"); got != joining {
		t.Fatalf("dial = %q", got)
	}
	for _, want := range []string{`code/github/session {"open":true,"target":"dev_team","v":1}`, `code/github/session {"open":false,"target":"dev_team","v":1}`} {
		select {
		case got := <-bodies:
			if got != want {
				t.Fatalf("Cloud got %q, want %q", got, want)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("Cloud got no %q", want)
		}
	}
}

// TestPersonAccount: only a person's account, not a team's, has GitHub requests to read.
func TestPersonAccount(t *testing.T) {
	c, e, a := start(t)
	p := c.presence()
	for i, account := range []string{`{"login":"acme","provider":"team"}`, `{"login":"alice","provider":"github"}`, `null`} {
		if i > 0 {
			p.changed()
		}
		revision := fmt.Sprint("r", i)
		f := frame(p.renewal(), revision, 60000, granted())
		f["account"] = json.RawMessage(account)
		p.write(f)
		e.want(t, "apply "+revision)
		p.applied(t, revision)
		if a.Person() != (i == 1) {
			t.Errorf("%s: person %t", account, a.Person())
		}
	}
}
