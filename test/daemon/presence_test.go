package daemon

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

func granted() daemon.Lease {
	return daemon.Lease{Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(`{"mode":"full_access"}`)}}
}

// presenceRenewal reads the next renewal, checks it has exactly the fields type, seq, online,
// core_version, policies and services, with policies null as before Core reports them, and returns its
// sequence number, online and core_version.
func (p *presence) presenceRenewal() (int64, bool, any) {
	p.t.Helper()
	m := p.read()
	online, ok := m["online"].(bool)
	policies, present := m["policies"]
	if _, listed := m["services"]; m["type"] != "renew" || len(m) != 6 || !ok || !present || policies != nil || !listed {
		p.t.Fatalf("frame %v, want a renewal with online, core_version and null policies", m)
	}
	version, present := m["core_version"]
	if !present {
		p.t.Fatalf("renewal %v without core_version", m)
	}
	return int64(m["seq"].(float64)), online, version
}

func TestPresence(t *testing.T) {
	t.Run("renewals report whether this computer serves Code, Core's version and the modes it enforces, at once when one changes", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		if _, online, version := p.presenceRenewal(); online || version != nil {
			t.Fatalf("first renewal online %v, core_version %v; want false and null", online, version)
		}
		if a.Recheck() != nil {
			t.Fatal("Recheck reported this computer offline")
		}
		a.Listening() // online only once the SSH host listens
		if _, online, version := p.presenceRenewal(); !online || version != nil {
			t.Fatalf("renewal online %v, core_version %v; want true and null", online, version)
		}
		a.Core("0.2.29", nil)
		if _, online, version := p.presenceRenewal(); !online || version != "0.2.29" {
			t.Fatalf("renewal online %v, core_version %v; want true and 0.2.29", online, version)
		}
		a.Core("0.2.29", nil) // unchanged: no renewal
		a.Recheck()
		e.offline.Store(true)
		if a.Recheck() == nil {
			t.Fatal("Recheck reported an offline computer online")
		}
		seq, online, version := p.presenceRenewal()
		if online || version != "0.2.29" || seq != 4 {
			t.Fatalf("renewal %d online %v, core_version %v; want 4, false and 0.2.29", seq, online, version)
		}
		for _, bad := range []string{"", "0.2.29 beta", strings.Repeat("1", 33), "0.2/29"} {
			a.Core(bad, nil)
			if _, _, version := p.presenceRenewal(); version != nil {
				t.Fatalf("core_version %q reported as %v, want null", bad, version)
			}
			a.Core("0.2.29", nil)
			if _, _, version := p.presenceRenewal(); version != "0.2.29" {
				t.Fatalf("renewal core_version %v, want 0.2.29", version)
			}
		}
		// Core's modes are reported as it states them, and as unknown unless all three are.
		modes := map[string]string{"read_only": "platform_unsupported", "workspace": "platform_unsupported", "full_access": "available"}
		a.Core("0.2.29", modes)
		if m := p.read(); m["type"] != "renew" || fmt.Sprint(m["policies"]) != fmt.Sprint(modes) {
			t.Fatalf("frame %v, want a renewal with policies %v", m, modes)
		}
		a.Core("0.2.29", modes) // unchanged: no renewal
		a.Core("0.2.29", map[string]string{"full_access": "available"})
		if m := p.read(); m["type"] != "renew" || m["policies"] != nil {
			t.Fatalf("frame %v, want a renewal with null policies for two modes missing", m)
		}
	})

	t.Run("status names the first reason this computer is not connected", func(t *testing.T) {
		e := &enforcer{events: make(chan string, 16), release: make(chan struct{})}
		_, key, _ := ed25519.GenerateKey(nil)
		idle := daemon.NewAuthority("http://127.0.0.1:1", "dev", key, e, daemon.OpenLog(t.TempDir(), "dev", key), http.DefaultClient) // never runs: no Cloud session
		e.offline.Store(true)
		want := func(a *daemon.Authority, reason string) {
			t.Helper()
			if got := a.Status(); got != reason {
				t.Fatalf("Status = %q, want %q", got, reason)
			}
		}
		want(idle, "offline")
		e.offline.Store(false)
		want(idle, "joining")
		idle.Listening()
		want(idle, "cloud_unavailable")

		c, e, a := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted())
		e.want(t, "apply r1")
		p.applied(t, "r1")
		want(a, "joining")
		a.Listening()
		p.renewal()
		want(a, "")
		p.changed()
		p.lease(p.renewal(), "denied", 60000, daemon.Lease{})
		e.want(t, "apply denied")
		p.applied(t, "denied")
		want(a, "no_authority")
		e.offline.Store(true)
		want(a, "offline")
		e.offline.Store(false)
		c.newer.Store(true)
		p.conn.CloseNow()
		waitFor(t, 5*time.Second, "the session to end", func() bool { return a.Status() == "cloud_unavailable" })
		// Cloud answers the next connection 426: this om is too old.
		waitFor(t, 15*time.Second, "Cloud's refusal of this Code protocol", func() bool { return a.Status() == "update_required" })
		select { // which asks a headless installation for an update
		case <-a.Refused():
		default:
			t.Fatal("the refusal asked for no update")
		}
		// The reason goes once Cloud takes a connection again.
		c.newer.Store(false)
		c.presence()
		waitFor(t, 15*time.Second, "the next session", func() bool { return a.Status() == "no_authority" })
	})
}

// core returns a Core whose interpreter is a shell script with body; the
// script's HOME is its directory.
func core(t *testing.T, body string) daemon.Core {
	dir := t.TempDir()
	path := filepath.Join(dir, "python")
	if err := os.WriteFile(path, []byte("#!/bin/sh\n"+body), 0o700); err != nil {
		t.Fatal(err)
	}
	return daemon.Core{Python: path, Args: []string{"--config", "C", "--state-dir", "S"}, Home: dir}
}

// runDaemon signs device in at origin under a new HOME and runs its daemon there until t ends, with Core's
// interpreter the shell script body in that HOME and, in lifetimes, the lifetimes.json an earlier daemon of
// the device left. It returns the HOME once the daemon's socket listens.
func runDaemon(t *testing.T, origin, device, body string, lifetimes ...string) string {
	t.Helper()
	// A short HOME: the daemon's socket path has a 104-byte limit on macOS.
	home, err := os.MkdirTemp("/tmp", "omd-")
	if err != nil {
		t.Fatal(err)
	}
	home, _ = filepath.EvalSymlinks(home)
	t.Cleanup(func() { os.RemoveAll(home) })
	signIn(t, home, origin, device, make([]byte, ed25519.SeedSize))
	onHardware(t, "hardware-A", nil)
	script := filepath.Join(home, "python")
	os.WriteFile(script, []byte("#!/bin/sh\n"+body), 0o700)
	for _, file := range lifetimes {
		dir := filepath.Join(daemon.StateDir(home), "devices", device)
		os.MkdirAll(dir, 0o700)
		os.WriteFile(filepath.Join(dir, "lifetimes.json"), []byte(file), 0o600)
	}
	entry := ownEntry(t)
	ctx, cancel := context.WithCancel(context.Background())
	var ended error
	done := make(chan struct{})
	go func() { ended = runNative(ctx, daemon.Core{Python: script, Home: home}); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
	waitFor(t, 10*time.Second, "the daemon's socket", func() bool {
		select {
		case <-done:
			t.Fatalf("the daemon ended: %v", ended)
		default:
		}
		_, err := os.Stat(filepath.Join(daemon.StateDir(home), "daemon.sock"))
		return err == nil
	})
	// Its entry listens, so the daemon has read OM_LOCAL_PORT before another of t's sets it anew.
	waitFor(t, 10*time.Second, "the daemon's entry", func() bool {
		c, err := net.Dial("tcp", "127.0.0.1:"+entry)
		if err == nil {
			c.Close()
		}
		return err == nil
	})
	return home
}

// TestRootOnLinux runs the daemon as root, as which Open Machine is installed on a Linux server.
func TestRootOnLinux(t *testing.T) {
	if runtime.GOOS != "linux" || os.Getuid() != 0 {
		t.Skip("NOT RUN: needs uid 0 on Linux")
	}
	cloud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "down", http.StatusServiceUnavailable) }))
	defer cloud.Close()
	runDaemon(t, cloud.URL, "dev_root", "exit 1") // which fails unless the daemon serves its socket
}

func TestCoreFolders(t *testing.T) {
	request := []byte(`{"folders":["/Users/me/code"]}`)
	t.Run("one Core process per check, with the request on stdin", func(t *testing.T) {
		c := core(t, `read -r line; printf '{"argv":"%s","stdin":%s,"home":"%s"}\n' "$*" "$line" "$HOME"`)
		out, err := c.Folders(request)
		var got struct {
			Argv, Home string
			Stdin      json.RawMessage
		}
		if err != nil || json.Unmarshal(out, &got) != nil {
			t.Fatalf("Folders = %s, %v", out, err)
		}
		if got.Argv != "-I -m openmachine_access --config C --state-dir S folders" || string(got.Stdin) != string(request) || got.Home != c.Home {
			t.Fatalf("Core ran with %+v", got)
		}
	})

	t.Run("Core's refusal is its answer", func(t *testing.T) {
		out, err := core(t, `echo '{"error":{"code":"folder_missing","index":0}}'`).Folders(request)
		if err != nil || string(out) != `{"error":{"code":"folder_missing","index":0}}` {
			t.Fatalf("Folders = %s, %v", out, err)
		}
	})

	t.Run("an answer up to 64 KiB is returned, and a larger one fails", func(t *testing.T) {
		fill := func(n int) string {
			return fmt.Sprintf(`printf '{"a":"'; head -c %d /dev/zero | tr '\0' x; printf '"}'`, n-8)
		}
		if out, err := core(t, fill(64<<10)).Folders(request); err != nil || len(out) != 64<<10 {
			t.Fatalf("64 KiB answer: %d bytes, %v", len(out), err)
		}
		if out, err := core(t, fill(64<<10+1)).Folders(request); err == nil {
			t.Fatalf("an answer over 64 KiB returned %d bytes", len(out))
		}
	})

	for name, body := range map[string]string{
		"a failed launcher":          `echo '{"folders":[]}'; exit 3`,
		"no JSON object":             `echo '[1]'`,
		"invalid JSON":               `echo '{"folders":'`,
		"no output":                  `exit 0`,
		"a launcher that cannot run": `exec /nonexistent`,
	} {
		t.Run(name+" fails", func(t *testing.T) {
			if out, err := core(t, body).Folders(request); err == nil {
				t.Fatalf("Folders = %s, want an error", out)
			}
		})
	}
}

// TestFolderSocket runs a daemon whose Core echoes a folder check's request
// as its answer, and fails a request holding "fail", while $HOME/hold exists
// after recording it in $HOME/requests. Its Cloud refuses everything: the
// check needs neither a lease nor a connection.
func TestFolderSocket(t *testing.T) {
	cloud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Error(w, "down", http.StatusServiceUnavailable) }))
	defer cloud.Close()
	home := runDaemon(t, cloud.URL, "dev_folders", `case "$*" in *" folders") ;; *) exit 1 ;; esac
body=$(cat)
echo x >> "$HOME/requests"
while [ -e "$HOME/hold" ]; do sleep 0.05; done
case "$body" in *fail*) exit 3 ;; esac
printf '%s' "$body"
`)
	sock := filepath.Join(daemon.StateDir(home), "daemon.sock")
	requests := func() int { b, _ := os.ReadFile(filepath.Join(home, "requests")); return bytes.Count(b, []byte("x")) }
	// check sends raw in one write and ends its side, as a client does, and returns the whole answer.
	check := func(raw string) chan string {
		answer := make(chan string, 1)
		go func() {
			c, err := net.Dial("unix", sock)
			if err != nil {
				answer <- err.Error()
				return
			}
			defer c.Close()
			io.WriteString(c, raw)
			c.(*net.UnixConn).CloseWrite()
			b, _ := io.ReadAll(c)
			answer <- string(b)
		}()
		return answer
	}
	answer := func(t *testing.T, raw, want string) {
		t.Helper()
		select {
		case got := <-check(raw):
			if got != want {
				t.Fatalf("answer to %.40q = %.80q, want %.80q", raw, got, want)
			}
		case <-time.After(10 * time.Second):
			t.Fatalf("no answer to %.40q", raw)
		}
	}
	const unavailable = `{"error":{"code":"folders_unavailable"}}` + "\n"

	t.Run("the body after the first line reaches Core and its answer, refusal included, comes back", func(t *testing.T) {
		answer(t, "folders\n"+`{"folders":["/Users/me/code"]}`, `{"folders":["/Users/me/code"]}`+"\n")
		answer(t, "folders\n"+`{"error":{"code":"folder_access_denied","index":1}}`, `{"error":{"code":"folder_access_denied","index":1}}`+"\n")
	})

	t.Run("a body up to 64 KiB is checked, and a larger one or a failed Core is unavailable", func(t *testing.T) {
		pad := func(n int) string { return `{"a":"` + strings.Repeat("x", n-8) + `"}` }
		answer(t, "folders\n"+pad(64<<10), pad(64<<10)+"\n")
		before := requests()
		answer(t, "folders\n"+pad(64<<10+1), unavailable)
		if requests() != before {
			t.Fatal("Core ran for a body over 64 KiB")
		}
		answer(t, "folders\n"+`{"fail":true}`, unavailable)
		answer(t, "folders\n[1]", unavailable)
		answer(t, "folders extra\n{}", "")
	})

	t.Run("checks run one at a time, and a second one waits", func(t *testing.T) {
		os.WriteFile(filepath.Join(home, "hold"), nil, 0o600)
		before := requests()
		first := check("folders\n" + `{"n":1}`)
		waitFor(t, 10*time.Second, "the first check to reach Core", func() bool { return requests() == before+1 })
		second := check("folders\n" + `{"n":2}`)
		time.Sleep(500 * time.Millisecond)
		if requests() != before+1 {
			t.Fatal("a second check started while the first ran")
		}
		os.Remove(filepath.Join(home, "hold"))
		if a, b := <-first, <-second; a != `{"n":1}`+"\n" || b != `{"n":2}`+"\n" {
			t.Fatalf("answers %q, %q", a, b)
		}
	})

	t.Run("a check, and one that waited for it, outlive the 30-second deadline of dial and status", func(t *testing.T) {
		if testing.Short() {
			t.Skip("NOT RUN: takes 32 s")
		}
		os.WriteFile(filepath.Join(home, "hold"), nil, 0o600)
		before := requests()
		slow := check("folders\n" + `{"n":3}`)
		waitFor(t, 10*time.Second, "the slow check to reach Core", func() bool { return requests() == before+1 })
		waiting := check("folders\n" + `{"n":4}`)
		time.Sleep(32 * time.Second)
		os.Remove(filepath.Join(home, "hold"))
		if a, b := <-slow, <-waiting; a != `{"n":3}`+"\n" || b != `{"n":4}`+"\n" {
			t.Fatalf("answers after 32 s = %q, %q", a, b)
		}
	})
}

// TestLeaseFolders leases a daemon Workspace folders its Core's check refuses, accepts, spells otherwise or
// cannot check. Only a Linux build runs the check; Core records each one in
// $HOME/requests.
func TestLeaseFolders(t *testing.T) {
	c, srv := codeCloud(t)
	home := runDaemon(t, srv.URL, "dev_leased", `case "$*" in *" folders") ;; *) exit 1 ;; esac
body=$(cat)
echo x >> "$HOME/requests"
case "$body" in
*refused*) echo '{"error":{"code":"folder_missing","index":1}}' ;;
*link*) echo '{"folders":["/srv/a","/srv/real"]}' ;;
*beyond*) echo '{"error":{"code":"folder_missing","index":2}}' ;;
*shape*) echo '{"error":{"code":"invalid_request"}}' ;;
*broken*) exit 3 ;;
*) printf '%s' "$body" ;;
esac
`)
	requests := func() int { b, _ := os.ReadFile(filepath.Join(home, "requests")); return bytes.Count(b, []byte("x")) }
	p := c.presence()
	n := 0
	// lease issues a new lease under policy and returns its revision.
	lease := func(policy string) string {
		if n++; n > 1 {
			p.changed()
		}
		revision := fmt.Sprint("r", n)
		p.lease(p.renewal(), revision, 60000, daemon.Lease{Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(policy)}})
		return revision
	}
	workspace := func(second string) string {
		return lease(fmt.Sprintf(`{"revision":%d,"mode":"workspace","folders":["/srv/a",%q]}`, n+1, second))
	}
	if runtime.GOOS != "linux" {
		p.applied(t, workspace("/srv/refused"))
		if requests() != 0 {
			t.Fatal("a build for another system than Linux ran the folder check")
		}
		return
	}
	refusal := func(code string, index float64) map[string]any { return map[string]any{"code": code, "index": index} }
	for _, tc := range []struct {
		folder string
		want   any
	}{
		{"/srv/refused", refusal("folder_missing", 1)},
		{"/srv/good", nil},
		{"/srv/link", refusal("folder_changed", 1)},        // admission refuses a path that is not the kernel's
		{"/srv/beyond", refusal("folders_unavailable", 0)}, // an index the lease does not have
		{"/srv/shape", refusal("folders_unavailable", 0)},  // a refusal that names no folder
		{"/srv/broken", refusal("folders_unavailable", 0)}, // Core gave no answer
	} {
		before := requests()
		p.refused(t, workspace(tc.folder), tc.want)
		if requests() != before+1 {
			t.Fatalf("%s: %d folder checks, want one", tc.folder, requests()-before)
		}
	}
	// Only Workspace has folders to check.
	before := requests()
	p.applied(t, lease(fmt.Sprintf(`{"revision":%d,"mode":"full_access","folders":[]}`, n+1)))
	if requests() != before {
		t.Fatal("a Full access lease ran the folder check")
	}
}

// TestCoreRead runs a daemon whose Core cannot build the sandbox until $HOME/fixed exists: its renewals
// then report the modes available, with no restart.
func TestCoreRead(t *testing.T) {
	saved := daemon.CoreRead
	daemon.CoreRead = 100 * time.Millisecond
	t.Cleanup(func() { daemon.CoreRead = saved })
	c, srv := codeCloud(t)
	home := runDaemon(t, srv.URL, "dev_fixed", `case "$*" in *" capabilities") ;; *) exit 1 ;; esac
mode=sandbox_unavailable
[ -e "$HOME/fixed" ] && mode=available
printf '{"protocol":4,"version":"1.0.0","policies":{"full_access":"available","read_only":"%s","workspace":"%s"}}\n' $mode $mode
`)
	p := c.presence()
	reported := func(mode string) {
		t.Helper()
		want := fmt.Sprint(map[string]any{"full_access": "available", "read_only": mode, "workspace": mode})
		for deadline := time.Now().Add(10 * time.Second); ; {
			m := p.read()
			if m["type"] == "renew" && fmt.Sprint(m["policies"]) == want {
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("no renewal reported %s; the last was %v", mode, m)
			}
		}
	}
	reported("sandbox_unavailable")
	os.WriteFile(filepath.Join(home, "fixed"), nil, 0o600)
	reported("available")
}

// TestCoreProtocol runs a daemon whose Core speaks the access protocol before this one. It runs no session
// of this one, so it is reported with its version and every mode unavailable, also
// once a Core that answered is replaced by such a one.
func TestCoreProtocol(t *testing.T) {
	saved := daemon.CoreRead
	daemon.CoreRead = 100 * time.Millisecond
	t.Cleanup(func() { daemon.CoreRead = saved })
	const core = `case "$*" in *" capabilities") ;; *) exit 1 ;; esac
protocol=PROTOCOL
printf '{"protocol":%s,"version":"1.0.0","policies":{"full_access":"available","read_only":"available","workspace":"available"}}\n' $protocol
`
	reported := func(p *presence, mode string) {
		t.Helper()
		want := map[string]any{"read_only": mode, "workspace": mode, "full_access": mode}
		for deadline := time.Now().Add(10 * time.Second); ; {
			if m := p.read(); m["type"] == "renew" && m["core_version"] == "1.0.0" && reflect.DeepEqual(m["policies"], want) {
				return
			} else if time.Now().After(deadline) {
				t.Fatalf("no renewal reported every mode as %s; the last was %v", mode, m)
			}
		}
	}
	c, srv := codeCloud(t)
	runDaemon(t, srv.URL, "dev_old", strings.Replace(core, "PROTOCOL", "3", 1))
	reported(c.presence(), "core_protocol")
	c, srv = codeCloud(t) // its own, so that the presence is this daemon's
	home := runDaemon(t, srv.URL, "dev_replaced", strings.Replace(core, "PROTOCOL", `4; [ -e "$HOME/old" ] && protocol=3`, 1))
	p := c.presence()
	reported(p, "available")
	os.WriteFile(filepath.Join(home, "old"), nil, 0o600)
	reported(p, "core_protocol")
}

// TestLifetimes leases a restarted daemon that kept the lifetimes of three sources. A lifetime, as a
// connection, holds only under the limit its source was admitted with, so a changed limit starts a new one;
// and a target Cloud adds a field to closes nothing, where the same policy read would.
// The sessions themselves need the network: TestSSH has them.
func TestLifetimes(t *testing.T) {
	c, srv := codeCloud(t)
	lifetime := func(source, limit string) string {
		return fmt.Sprintf(`{"id":"c-%[1]s","binding":{"source":%[1]q,"source_grant_revision":1,%[2]s"target_grant_id":"g","target_grant_revision":1,"policy_revision":1}}`, source, limit)
	}
	home := runDaemon(t, srv.URL, "dev_limits", "exit 1", `{"format":1,"lifetimes":[`+
		lifetime("open", "")+","+lifetime("lifted", `"limit":"read_only",`)+","+lifetime("limited", `"limit":"read_only",`)+`]}`)
	kept := func() string {
		var f struct{ Lifetimes []struct{ ID string } }
		b, _ := os.ReadFile(filepath.Join(daemon.StateDir(home), "devices/dev_limits/lifetimes.json"))
		json.Unmarshal(b, &f)
		var ids []string
		for _, lt := range f.Lifetimes {
			ids = append(ids, lt.ID)
		}
		slices.Sort(ids)
		return strings.Join(ids, " ")
	}
	p := c.presence()
	for i, tc := range []struct{ target, lifted, want, why string }{
		{`"policy":{"revision":1,"mode":"full_access","folders":[]}`, "read_only", "c-lifted c-limited", "a source now limited keeps a lifetime admitted without the limit"},
		{`"policy":{"revision":2,"mode":"full_access","folders":[]},"later":true`, "", "c-limited", "a target with an unknown field closed a lifetime, or a lifted limit kept one"},
		{`"policy":{"revision":2,"mode":"full_access","folders":[]}`, "", "", "a new policy revision kept a lifetime"},
	} {
		if i > 0 {
			p.changed()
		}
		revision := fmt.Sprint("r", i+1)
		m := frame(p.renewal(), revision, 60000, daemon.Lease{Sources: []daemon.Source{{DeviceID: "open", GrantRevision: 1, Limit: "read_only"},
			{DeviceID: "lifted", GrantRevision: 1, Limit: tc.lifted}, {DeviceID: "limited", GrantRevision: 1, Limit: "read_only"}}})
		m["target"] = json.RawMessage(`{"grant_id":"g","grant_revision":1,` + tc.target + `}`)
		p.write(m)
		p.applied(t, revision)
		if got := kept(); got != tc.want {
			t.Fatalf("lifetimes kept under %s: %q, want %q: %s", revision, got, tc.want, tc.why)
		}
	}
}

// TestWatch reads the account view from a daemon's socket while a scripted
// Cloud leases it, changes a device's presence and goes away.
func TestWatch(t *testing.T) {
	c, srv := codeCloud(t)
	home := runDaemon(t, srv.URL, "dev_watch", "exit 1")
	conn, err := net.Dial("unix", filepath.Join(daemon.StateDir(home), "daemon.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	io.WriteString(conn, "watch\n")
	lines := bufio.NewReader(conn)
	want := func(view string) {
		t.Helper()
		conn.SetReadDeadline(time.Now().Add(10 * time.Second))
		if line, err := lines.ReadString('\n'); err != nil || line != view+"\n" {
			t.Fatalf("view %q, %v; want %s", line, err, view)
		}
	}
	const account, policy = `{"login":"alice","principal_id":"acct_one"}`, `{"mode":"full_access"}`
	device := func(id, state, seen, version string) string {
		return fmt.Sprintf(`{"device_id":%q,"name":"Mac","mode":"full_access","alias":%q,"state":%q,"last_seen_at":%s,"core_version":%s}`,
			id, strings.Replace(id, "_", "-", 1), state, seen, version)
	}
	// A team's machine this device's owner reaches, under the name its members type.
	team := func(state string) string {
		return fmt.Sprintf(`{"device_id":"dev_team","name":"GPU 01","mode":"read_only","alias":"gpu-01.acme","state":%q,"last_seen_at":null,`+
			`"core_version":null,"team":{"slug":"acme","name":"Acme"}}`, state)
	}
	view := func(live bool, account, policy string, devices ...string) string {
		return fmt.Sprintf(`{"account":%s,"device_id":"dev_watch","devices":[%s],"github":null,"live":%t,"policy":%s}`, account, strings.Join(devices, ","), live, policy)
	}
	want(view(false, "null", "null")) // before the first lease

	p := c.presence()
	lease := frame(p.renewal(), "r1", 60000, granted())
	lease["account"] = json.RawMessage(account)
	lease["devices"] = json.RawMessage("[" + device("dev_watch", "online", "1", `"0.2.29"`) + "," + device("dev_other", "offline", "null", "null") + "," + team("online") + "]")
	p.write(lease)
	p.applied(t, "r1")
	want(view(true, account, policy, device("dev_watch", "online", "1", `"0.2.29"`), device("dev_other", "offline", "null", "null"), team("online")))

	// A device's presence changes its row; one the view does not list and a confirmed renewal write nothing.
	presence := func(id string) {
		p.write(map[string]any{"type": "presence", "device_id": id, "state": "online", "last_seen_at": 5, "core_version": "0.2.30"})
	}
	presence("dev_other")
	want(view(true, account, policy, device("dev_watch", "online", "1", `"0.2.29"`), device("dev_other", "online", "5", `"0.2.30"`), team("online")))
	presence("dev_unlisted")
	p.changed()
	p.write(map[string]any{"type": "renewed", "seq": p.renewal(), "ttl_ms": 60000})

	// Without Cloud the last values stay and every state is unknown.
	p.conn.CloseNow()
	want(view(false, account, policy, device("dev_watch", "unknown", "1", `"0.2.29"`), device("dev_other", "unknown", "5", `"0.2.30"`), team("unknown")))
}
