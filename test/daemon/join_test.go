package daemon

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// enrollCloud is a Cloud whose network/enroll answers with enroll and that
// counts its enrollments; it answers challenges and nothing else.
func enrollCloud(t *testing.T, enroll func(w http.ResponseWriter)) (*httptest.Server, *atomic.Int32) {
	var enrolled atomic.Int32
	route := regexp.MustCompile(`^/api/devices/[A-Za-z0-9_-]+/(challenge|network/enroll)$`)
	cloud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch m := route.FindStringSubmatch(r.URL.Path); {
		case m != nil && m[1] == "challenge":
			json.NewEncoder(w).Encode(map[string]any{"challenge_id": "c", "challenge": "c", "expires_at": time.Now().Unix() + 60})
		case m != nil:
			enrolled.Add(1)
			enroll(w)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(cloud.Close)
	return cloud, &enrolled
}

// status asks the daemon under home why it is not connected. Its reply also
// says that no session uses this computer, as none can before it joins.
func status(t *testing.T, home string) string {
	t.Helper()
	c, err := net.Dial("unix", filepath.Join(daemon.StateDir(home), "daemon.sock"))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	fmt.Fprintln(c, "status")
	var reply struct {
		Connected bool
		Reason    string
		InUse     *bool `json:"in_use"`
	}
	if err := json.NewDecoder(c).Decode(&reply); err != nil || reply.Connected || reply.InUse == nil || *reply.InUse {
		t.Fatalf("status = %+v, %v", reply, err)
	}
	return reply.Reason
}

// logged captures this process's log until t ends.
func logged(t *testing.T) *syncBuffer {
	var b syncBuffer
	log.SetOutput(&b)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	return &b
}

// TestJoinStatus: a daemon that cannot join the private network says why on
// its socket, where Desktop's connection.log and om status read it, and logs
// the detail, instead of reporting joining for ever. Each failure is a step
// a VPN, a proxy or a Cloud without a network can fail.
func TestJoinStatus(t *testing.T) {
	refuse := func(status int, code string) func(w http.ResponseWriter) {
		return func(w http.ResponseWriter) {
			w.WriteHeader(status)
			json.NewEncoder(w).Encode(map[string]string{"error": code, "message": "refused"})
		}
	}
	for _, tc := range []struct{ name, code, want string }{
		{"a Cloud with no private network", "network_unavailable", "network_unavailable"},
		{"a private network at capacity", "network_capacity", "network_capacity"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs := logged(t)
			cloud, enrolled := enrollCloud(t, refuse(http.StatusServiceUnavailable, tc.code))
			home := runDaemon(t, cloud.URL, "dev_join", "exit 1")
			waitFor(t, 15*time.Second, "an enrollment", func() bool { return enrolled.Load() > 0 })
			waitFor(t, 5*time.Second, tc.want, func() bool { return status(t, home) == tc.want })
			if !strings.Contains(logs.String(), "Cloud: "+tc.code+" (503): refused") {
				t.Errorf("the log has no refusal: %s", logs.String())
			}
		})
	}

	t.Run("an answer with no code, as a proxy's error page", func(t *testing.T) {
		cloud, _ := enrollCloud(t, func(w http.ResponseWriter) { http.Error(w, "<html>Bad Gateway</html>", http.StatusBadGateway) })
		home := runDaemon(t, cloud.URL, "dev_join", "exit 1")
		waitFor(t, 15*time.Second, "cloud_unavailable", func() bool { return status(t, home) == "cloud_unavailable" })
	})

	t.Run("a Cloud that cannot be reached", func(t *testing.T) {
		cloud := httptest.NewServer(http.NotFoundHandler())
		origin := cloud.URL
		cloud.Close()
		home := runDaemon(t, origin, "dev_join", "exit 1")
		waitFor(t, 15*time.Second, "cloud_unavailable", func() bool { return status(t, home) == "cloud_unavailable" })
	})

	t.Run("a control the node cannot reach, logged with the node's own error", func(t *testing.T) {
		saved := daemon.JoinTimeout
		daemon.JoinTimeout = 2 * time.Second
		t.Cleanup(func() { daemon.JoinTimeout = saved })
		closed, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		control := "http://" + closed.Addr().String()
		closed.Close()
		logs := logged(t)
		const key = "planted0bootstrap0key0e1b2c3d4"
		cloud, _ := enrollCloud(t, func(w http.ResponseWriter) {
			json.NewEncoder(w).Encode(map[string]any{"state": "pending", "bootstrap_key": key, "expires_at": time.Now().Unix() + 600, "control_url": control})
		})
		home := runDaemon(t, cloud.URL, "dev_join", "exit 1")
		waitFor(t, 20*time.Second, "network_unreachable", func() bool { return status(t, home) == "network_unreachable" })
		line := logs.String()
		if !strings.Contains(line, "the private network's control cannot be reached within 2s: fetch control key") ||
			!strings.Contains(line, "connection refused") {
			t.Errorf("the log does not say why: %s", line)
		}
		// The node's own log is there too, with no secret and no home.
		if !strings.Contains(line, "network: ") || strings.Contains(line, key) || strings.Contains(line, home+"/") {
			t.Errorf("the node's log is missing or unredacted: %s", line)
		}
	})

	t.Run("a node that keeps failing to start does not flood the log", func(t *testing.T) {
		saved := daemon.JoinTimeout
		daemon.JoinTimeout = time.Second
		t.Cleanup(func() { daemon.JoinTimeout = saved })
		closed, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		control := "http://" + closed.Addr().String()
		closed.Close()
		logs := logged(t)
		cloud, enrolled := enrollCloud(t, func(w http.ResponseWriter) {
			json.NewEncoder(w).Encode(map[string]any{"state": "pending", "bootstrap_key": "k", "expires_at": time.Now().Unix() + 600, "control_url": control})
		})
		runDaemon(t, cloud.URL, "dev_join", "exit 1")
		waitFor(t, 60*time.Second, "five starts", func() bool { return enrolled.Load() > 2 }) // each enrollment after the first starts twice: the resumed node, then the new one
		if n := strings.Count(logs.String(), "network: Creating WireGuard device..."); n < 1 || n > 3 {
			t.Errorf("a line of every start logged %d times over %d enrollments", n, enrolled.Load())
		}
	})

	t.Run("a node Cloud bound that cannot reach the control says so", func(t *testing.T) {
		saved := daemon.JoinTimeout
		daemon.JoinTimeout = time.Second
		t.Cleanup(func() { daemon.JoinTimeout = saved })
		closed, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		control := "http://" + closed.Addr().String()
		closed.Close()
		logs := logged(t)
		var calls atomic.Int32
		cloud, _ := enrollCloud(t, func(w http.ResponseWriter) {
			if calls.Add(1) == 1 { // the node registers, then its bind reply is lost
				json.NewEncoder(w).Encode(map[string]any{"state": "pending", "bootstrap_key": "k", "expires_at": time.Now().Unix() + 600, "control_url": control})
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"state": "bound"})
		})
		home := runDaemon(t, cloud.URL, "dev_join", "exit 1")
		waitFor(t, 20*time.Second, "the bound answer", func() bool { return strings.Contains(logs.String(), "Cloud bound this computer's node") })
		if got := status(t, home); got != "network_unreachable" {
			t.Errorf("status %q, want network_unreachable: %s", got, logs.String())
		}
	})

	t.Run("a failure that repeats is logged once", func(t *testing.T) {
		logs := logged(t)
		cloud, enrolled := enrollCloud(t, refuse(http.StatusServiceUnavailable, "network_unavailable"))
		runDaemon(t, cloud.URL, "dev_join", "exit 1")
		waitFor(t, 20*time.Second, "a second enrollment", func() bool { return enrolled.Load() > 2 })
		if n := strings.Count(logs.String(), "network_unavailable"); n != 1 {
			t.Errorf("logged %d times: %s", n, logs.String())
		}
	})
}

// TestDaemonLog: a daemon whose standard error is discarded, as launchd
// leaves a Mac's agent, logs to daemon.log in its state directory, bounded; one
// whose supervisor keeps its log logs there alone.
func TestDaemonLog(t *testing.T) {
	if home := os.Getenv("OM_DAEMONLOG_TEST_HOME"); home != "" {
		daemon.HardwareID = func(string) (string, error) { return "hardware-A", nil }
		daemon.LogDiscarded(daemon.StateDir(home))
		if os.Getenv("OM_DAEMONLOG_TEST_FILL") != "" {
			daemon.MaxDaemonLog = 100
			os.MkdirAll(daemon.StateDir(home), 0o700)
			if os.Getenv("OM_DAEMONLOG_TEST_FILL") == "blocked" { // no earlier file can be kept
				os.Mkdir(filepath.Join(daemon.StateDir(home), "daemon.log.1"), 0o700)
			}
			for i := range 10 {
				log.Printf("line %d", i)
			}
			os.Exit(0)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		runNative(ctx, daemon.Core{Home: home})
		os.Exit(0)
	}
	cloud, _ := enrollCloud(t, func(w http.ResponseWriter) {
		w.WriteHeader(http.StatusServiceUnavailable)
		json.NewEncoder(w).Encode(map[string]string{"error": "network_unavailable", "message": "The private network is not available."})
	})
	start := func(t *testing.T, stderr *syncBuffer, env ...string) (home string, done chan struct{}) {
		home, err := os.MkdirTemp("/tmp", "oml-") // the socket path has a 104-byte limit on macOS
		if err != nil {
			t.Fatal(err)
		}
		home, _ = filepath.EvalSymlinks(home)
		t.Cleanup(func() { os.RemoveAll(home) })
		signIn(t, home, cloud.URL, "dev_log", make([]byte, 32))
		ownEntry(t)
		child := exec.Command(os.Args[0], "-test.run=^TestDaemonLog$")
		child.Env = append(append(os.Environ(), "OM_DAEMONLOG_TEST_HOME="+home), env...)
		if stderr != nil {
			child.Stderr = stderr
		} // else /dev/null
		if err := child.Start(); err != nil {
			t.Fatal(err)
		}
		done = make(chan struct{})
		go func() { child.Wait(); close(done) }()
		t.Cleanup(func() { child.Process.Kill(); <-done })
		return home, done
	}
	file := func(home, name string) string {
		b, _ := os.ReadFile(filepath.Join(daemon.StateDir(home), name))
		return string(b)
	}
	const why = "native: joining the private network: Cloud: network_unavailable (503): The private network is not available."

	t.Run("a discarded standard error", func(t *testing.T) {
		home, _ := start(t, nil)
		waitFor(t, 15*time.Second, "the log", func() bool { return strings.Contains(file(home, "daemon.log"), why) })
		if fi, err := os.Stat(filepath.Join(daemon.StateDir(home), "daemon.log")); err != nil || fi.Mode().Perm() != 0o600 {
			t.Errorf("daemon.log: %v, %v", fi.Mode(), err)
		}
	})

	t.Run("a kept standard error", func(t *testing.T) {
		var stderr syncBuffer
		home, _ := start(t, &stderr)
		waitFor(t, 15*time.Second, "the log", func() bool { return strings.Contains(stderr.String(), why) })
		if _, err := os.Stat(filepath.Join(daemon.StateDir(home), "daemon.log")); !os.IsNotExist(err) {
			t.Errorf("daemon.log beside a kept log: %v", err)
		}
	})

	t.Run("bounded, with one earlier file", func(t *testing.T) {
		home, done := start(t, nil, "OM_DAEMONLOG_TEST_FILL=1")
		<-done
		current, earlier := file(home, "daemon.log"), file(home, "daemon.log.1")
		if len(current) > 100 || len(earlier) > 100 || !strings.HasSuffix(current, "line 9\n") || earlier == "" {
			t.Errorf("daemon.log %q, daemon.log.1 %q", current, earlier)
		}
		lines := 0
		for _, s := range []string{earlier, current} {
			for sc := bufio.NewScanner(strings.NewReader(s)); sc.Scan(); {
				lines++
			}
		}
		if lines == 0 || lines > 10 {
			t.Errorf("%d lines", lines)
		}
	})

	t.Run("bounded when no earlier file can be kept", func(t *testing.T) {
		home, done := start(t, nil, "OM_DAEMONLOG_TEST_FILL=blocked")
		<-done
		if current := file(home, "daemon.log"); len(current) > 100 || !strings.HasSuffix(current, "line 9\n") {
			t.Errorf("daemon.log %q", current)
		}
	})
}

// TestRedactNetwork: secrets planted in a node's log lines never reach om's log.
func TestRedactNetwork(t *testing.T) {
	const home, key = "/Users/alice", "0123456789abcdef0123456789abcdef0123456789abcdef"
	for _, tc := range []struct{ line, want string }{
		{"register with auth key " + key + " failed", "register with auth key [key] failed"},
		{"invalid key hskey-auth-AbC123_x-Yz9", "invalid key hskey-[key]"},
		{"key tskey-auth-kAbc-SECRET0", "key tskey-[key]"},
		{`Get "https://control.example/machine/register?token=SECRET&v=1": EOF`, `Get "https://control.example/machine/register?[query]": EOF`},
		{"Authorization: Bearer SECRETTOKEN", "Authorization: [hidden]"},
		{"state /Users/alice/.local/state/openmachine-om/x", "state ~/.local/state/openmachine-om/x"},
		{"user /Users/alicex/file", "user /Users/alicex/file"},
	} {
		if got := daemon.RedactNetwork(tc.line, home, key); got != tc.want {
			t.Errorf("RedactNetwork(%q) = %q, want %q", tc.line, got, tc.want)
		}
	}
}
