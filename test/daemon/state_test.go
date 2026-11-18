package daemon

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
	"golang.org/x/crypto/ssh"
)

// signIn writes the Connector state a sign-in leaves under home: the device's
// key file and config.device bound to it and to this machine, hardware-A,
// with stored the IDs of every stored device.
func signIn(t *testing.T, home, origin, device string, seed []byte, stored ...string) {
	dir := filepath.Join(home, ".local/state/openmachine-connector")
	os.MkdirAll(dir, 0o700)
	os.WriteFile(filepath.Join(dir, "key-"+fingerprint(seed)), seed, 0o600)
	devices := []any{}
	for _, id := range append([]string{device}, stored...) {
		devices = append(devices, map[string]any{"device_id": id})
	}
	b, _ := json.Marshal(map[string]any{"server": origin, "enabled": true, "devices": devices, "device": map[string]any{
		"device_id": device, "principal_id": "acct_one", "key_fingerprint": fingerprint(seed), "hardware_id": "hardware-A"}})
	tmp := filepath.Join(dir, "config.json.tmp")
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		t.Fatal(err)
	}
	os.Rename(tmp, filepath.Join(dir, "config.json"))
}

// TestState writes the daemon's private state on a real file system.
func TestState(t *testing.T) {
	if home := os.Getenv("OM_STATE_TEST_HOME"); home != "" {
		// A child, alone under a file size limit of 0: the daemon's first write, its host key, fails.
		daemon.HardwareID = func(string) (string, error) { return "hardware-A", nil }
		var limit syscall.Rlimit
		syscall.Getrlimit(syscall.RLIMIT_FSIZE, &limit)
		syscall.Setrlimit(syscall.RLIMIT_FSIZE, &syscall.Rlimit{Cur: 0, Max: limit.Max})
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := runNative(ctx, daemon.Core{Home: home}); !errors.Is(err, syscall.EFBIG) {
			fmt.Printf("Run with no room to write = %v, want EFBIG\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	t.Run("a failed write is an error and publishes nothing", func(t *testing.T) {
		home, _ := filepath.EvalSymlinks(t.TempDir())
		signIn(t, home, "https://cloud.test", "dev_one", make([]byte, ed25519.SeedSize))
		ownEntry(t)
		child := exec.Command(os.Args[0], "-test.run=^TestState$")
		child.Env = append(os.Environ(), "OM_STATE_TEST_HOME="+home)
		if out, err := child.CombinedOutput(); err != nil {
			t.Errorf("%v: %s", err, out)
		}
		// The first write is the daemon's process ID into its lock: it fails there and goes no further.
		filepath.WalkDir(daemon.StateDir(home), func(path string, e os.DirEntry, err error) error {
			if err == nil && path != daemon.StateDir(home) && e.Name() != "daemon.lock" {
				t.Errorf("a failed write published %s", path)
			}
			return err
		})
		if pid, _ := os.ReadFile(filepath.Join(daemon.StateDir(home), "daemon.lock")); len(pid) != 0 {
			t.Errorf("daemon.lock holds %q", pid)
		}
	})
}

// TestDeviceChange: each device keeps its own Code state, and a sign-in
// that moves this computer to another device ends the daemon, also while it
// waits in recovery. Each daemon is a child process, as its supervisor
// starts one.
func TestDeviceChange(t *testing.T) {
	if home := os.Getenv("OM_DEVICE_TEST_HOME"); home != "" {
		daemon.HardwareID = func(string) (string, error) { return "hardware-A", nil }
		ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		fmt.Printf("Run = %v\n", runNative(ctx, daemon.Core{Home: home}))
		os.Exit(0)
	}
	enrolled := make(chan string, 16)
	route := regexp.MustCompile(`^/api/devices/([A-Za-z0-9_-]+)/(challenge|network/enroll)$`)
	cloud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m := route.FindStringSubmatch(r.URL.Path)
		switch {
		case m != nil && m[2] == "challenge":
			json.NewEncoder(w).Encode(map[string]any{"challenge_id": "c", "challenge": "c", "expires_at": time.Now().Unix() + 60})
		case m != nil:
			enrolled <- m[1]
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]string{"error": "network_unavailable"})
		default:
			http.NotFound(w, r)
		}
	}))
	defer cloud.Close()
	// A short HOME: the daemon's socket path has a 104-byte limit on macOS.
	home, err := os.MkdirTemp("/tmp", "omx-")
	if err != nil {
		t.Fatal(err)
	}
	home, _ = filepath.EvalSymlinks(home)
	defer os.RemoveAll(home)
	state := daemon.StateDir(home)
	// A stored device whose bound node this state lost, so its start needs recovery.
	_, hostKey, _ := ed25519.GenerateKey(nil)
	block, _ := ssh.MarshalPrivateKey(hostKey, "")
	files := map[string]string{
		"ssh_include":                             "recorded",
		"devices/dev_stored/enrollment.json":      fmt.Sprintf(`{"device_id":"dev_stored","origin":%q,"node_key":"nodekey:%064x","host_registered":true}`, cloud.URL, 1),
		"devices/dev_stored/ssh_host_ed25519_key": string(pem.EncodeToMemory(block)),
		"devices/dev_gone/enrollment.json":        `{"device_id":"dev_gone"}`,
	}
	for name, content := range files {
		os.MkdirAll(filepath.Dir(filepath.Join(state, name)), 0o700)
		os.WriteFile(filepath.Join(state, name), []byte(content), 0o600)
	}
	unchanged := func(names ...string) {
		t.Helper()
		for _, name := range names {
			if b, _ := os.ReadFile(filepath.Join(state, name)); string(b) != files[name] {
				t.Errorf("%s changed to %q", name, b)
			}
		}
	}
	type child struct {
		done      chan struct{}
		out, logs syncBuffer
	}
	start := func() *child {
		d := &child{done: make(chan struct{})}
		ownEntry(t)
		child := exec.Command(os.Args[0], "-test.run=^TestDeviceChange$")
		child.Env = append(os.Environ(), "OM_DEVICE_TEST_HOME="+home)
		child.Stdout, child.Stderr = &d.out, &d.logs
		if err := child.Start(); err != nil {
			t.Fatal(err)
		}
		go func() { child.Wait(); close(d.done) }()
		t.Cleanup(func() { child.Process.Kill(); <-d.done })
		return d
	}
	changed := func(d *child) {
		t.Helper()
		select {
		case <-d.done:
			if want := fmt.Sprintf("Run = %v\n", daemon.ErrDeviceChanged); d.out.String() != want {
				t.Fatalf("the daemon ended with %q, want %q; log: %s", d.out.String(), want, d.logs.String())
			}
		case <-time.After(15 * time.Second):
			t.Fatalf("the daemon kept running for the previous device; log: %s", d.logs.String())
		}
	}

	// Core's shared home is never touched; only its metadata is read here.
	shared := fmt.Sprintf("/Users/Shared/openmachine-sandbox-%d", os.Getuid())
	sharedBefore, sharedErr := os.Lstat(shared)

	newSeed, storedSeed := bytes.Repeat([]byte{9}, ed25519.SeedSize), bytes.Repeat([]byte{8}, ed25519.SeedSize)
	signIn(t, home, cloud.URL, "dev_new", newSeed, "dev_stored")
	first := start()
	select {
	case device := <-enrolled:
		if device != "dev_new" {
			t.Fatalf("enrolled %s, want dev_new", device)
		}
	case <-first.done:
		t.Fatalf("the daemon ended before enrolling: %s %s", first.out.String(), first.logs.String())
	case <-time.After(30 * time.Second):
		t.Fatal("no enrollment")
	}
	if _, err := os.Stat(filepath.Join(state, "devices/dev_new/ssh_host_ed25519_key")); err != nil {
		t.Errorf("the new device has no host key of its own: %v", err)
	}
	if _, err := os.Stat(filepath.Join(state, "devices/dev_gone")); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("the state of a device no longer stored remains: %v", err)
	}
	unchanged("ssh_include", "devices/dev_stored/enrollment.json", "devices/dev_stored/ssh_host_ed25519_key")

	// Signing in with the stored device's account moves this computer to it.
	signIn(t, home, cloud.URL, "dev_stored", storedSeed, "dev_new")
	changed(first)
	// The next daemon resumes that device's own state, which needs recovery
	// here, and a device change still ends it.
	second := start()
	for deadline := time.Now().Add(30 * time.Second); !strings.Contains(second.logs.String(), "the bound network identity is missing"); {
		if time.Now().After(deadline) {
			t.Fatalf("no recovery for the stored device's own state: %s", second.logs.String())
		}
		time.Sleep(50 * time.Millisecond)
	}
	unchanged("devices/dev_stored/enrollment.json", "devices/dev_stored/ssh_host_ed25519_key")
	signIn(t, home, cloud.URL, "dev_new", newSeed, "dev_stored")
	changed(second)
	select {
	case device := <-enrolled:
		t.Errorf("enrolled %s again", device)
	default:
	}
	unchanged("ssh_include")
	if after, err := os.Lstat(shared); (err == nil) != (sharedErr == nil) || err == nil && !after.ModTime().Equal(sharedBefore.ModTime()) {
		t.Errorf("Core's shared home %s changed", shared)
	}
}

// syncBuffer collects a child daemon's output while it runs.
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}
