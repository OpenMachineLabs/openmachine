package linux

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/user"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/linux"
)

func TestMain(m *testing.M) {
	if kind := os.Getenv("OM_STUB"); kind != "" {
		stub(kind, os.Getenv("OM_STUB_SELF"), os.Args[1:])
	}
	os.Exit(m.Run())
}

// host is one machine om runs on: a user's home, the system's own paths
// under root, and the stub owners' world.
type host struct {
	t                      *testing.T
	dir, home, root, world string
	user                   *user.User
	downloads              string // where the machine's releases are published
}

// newHost makes a machine for uid, with or without systemd, and points om at it.
func newHost(t *testing.T, uid string, systemd bool) *host {
	dir, err := os.MkdirTemp("/tmp", "omh-") // short: a Unix socket's path is limited
	if err != nil {
		t.Fatal(err)
	}
	dir, _ = filepath.EvalSymlinks(dir)
	h := &host{t: t, dir: dir, home: filepath.Join(dir, "home"), root: filepath.Join(dir, "root"), world: filepath.Join(dir, "world.json"),
		downloads: "http://127.0.0.1:1/linux"}
	h.user = &user.User{Uid: uid, Username: map[string]string{"0": "root"}[uid], HomeDir: h.home}
	if h.user.Username == "" {
		h.user.Username = "tester"
	}
	os.MkdirAll(h.home, 0o755)
	if systemd {
		os.MkdirAll(filepath.Join(h.root, "run/systemd/system"), 0o755)
	}
	for _, tool := range []string{"systemctl", "loginctl"} {
		h.script(filepath.Join(dir, "bin", tool), tool)
	}
	t.Setenv("HOME", h.home)
	t.Setenv("PATH", filepath.Join(dir, "bin")+":"+os.Getenv("PATH"))
	t.Setenv("SHELL", "/bin/bash")
	t.Setenv("XDG_RUNTIME_DIR", "")
	executable, current, root := linux.Executable, linux.User, linux.Root
	linux.User, linux.Root = func() (*user.User, error) { return h.user, nil }, h.root
	t.Cleanup(func() {
		linux.Executable, linux.User, linux.Root = executable, current, root
		if pid := holder(h.home); pid > 0 {
			syscall.Kill(pid, syscall.SIGKILL)
		}
		os.RemoveAll(dir)
	})
	return h
}

// script writes a stub owner: a script that runs this test binary as kind.
func (h *host) script(path, kind string) {
	self, _ := os.Executable()
	os.MkdirAll(filepath.Dir(path), 0o755)
	text := fmt.Sprintf("#!/bin/sh\nOM_STUB=%s OM_STUB_WORLD='%s' OM_STUB_SELF=\"$0\" OM_STUB_UID=%s OM_STUB_USER=%s OM_STUB_ACCOUNT_HOME='%s' OM_STUB_ROOT='%s' exec '%s' \"$@\"\n",
		kind, h.world, h.user.Uid, h.user.Username, h.user.HomeDir, h.root, self)
	if err := os.WriteFile(path, []byte(text), 0o755); err != nil {
		h.t.Fatal(err)
	}
}

// release is an unpacked release of version, as the install script leaves it.
type release struct{ dir, version, connector, core string }

func (h *host) release(version string) release {
	arch := map[string]string{"amd64": "x86_64", "arm64": "aarch64"}[runtime.GOARCH]
	r := release{dir: filepath.Join(h.dir, "release-"+version, "openmachine-"+version+"-linux-"+arch), version: version}
	h.script(filepath.Join(r.dir, "connector/python/bin/python3"), "connector")
	h.script(filepath.Join(r.dir, "connector/om"), "om")
	h.script(filepath.Join(r.dir, "core/python/bin/python3"), "core")
	digest := func(name, content string) string {
		os.WriteFile(filepath.Join(r.dir, name), []byte(content), 0o644)
		sum := sha256.Sum256([]byte(content))
		return hex.EncodeToString(sum[:])
	}
	digest("connector/entry.py", "")
	r.connector, r.core = digest("connector/manifest.json", `{"connector":"`+version+`"}`), digest("core/manifest.json", `{"core":"`+version+`"}`)
	digest("release.json", fmt.Sprintf(`{"format":1,"version":%q,"arch":%q,"connector_manifest_sha256":%q,"core_manifest_sha256":%q}`,
		version, arch, r.connector, r.core))
	return r
}

// link is where om is on this machine.
func (h *host) link() string {
	if h.user.Uid == "0" {
		return filepath.Join(h.root, "usr/local/bin/om")
	}
	return filepath.Join(h.home, ".local/bin/om")
}

// main runs om as the binary at self.
func (h *host) main(self string, args ...string) (stdout, stderr string, code int) {
	linux.Executable = func() (string, error) { return self, nil }
	var out, errOut bytes.Buffer
	code = linux.Main(args, &out, &errOut)
	return out.String(), errOut.String(), code
}

// install runs a release's om install with the install script's options.
func (h *host) install(r release, args ...string) (stdout, stderr string, code int) {
	return h.main(filepath.Join(r.dir, "connector/om"), append([]string{"install", "--server", "https://mcp.example",
		"--channel", "staging", "--downloads", h.downloads}, args...)...)
}

// om runs the installed om.
func (h *host) om(args ...string) (stdout, stderr string, code int) {
	return h.main(h.link(), args...)
}

// installed installs version without connecting and forgets the calls that made.
func (h *host) installed(version string) release {
	h.t.Helper()
	r := h.release(version)
	if out, errOut, code := h.install(r, "--no-up"); code != 0 {
		h.t.Fatalf("install = %d\n%s%s", code, out, errOut)
	}
	h.daemon()
	h.calls()
	return r
}

// stopped waits until no daemon runs.
func (h *host) stopped() {
	h.t.Helper()
	for deadline := time.Now().Add(10 * time.Second); holder(h.home) != 0; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			h.t.Fatal("the daemon still runs")
		}
	}
}

// daemon waits for the daemon om started and returns its process.
func (h *host) daemon() int {
	h.t.Helper()
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if pid := holder(h.home); pid > 0 {
			return pid
		}
	}
	h.t.Fatal("no daemon is running")
	return 0
}

// calls returns the owner calls since the last read. With limit, each is cut
// after that many words, and a daemon's start to its first.
func (h *host) calls(limit ...int) []string {
	var calls []string
	edit(h.world, func(w *world) { calls, w.Calls = w.Calls, nil })
	for i, call := range calls {
		if len(limit) == 1 {
			words := strings.Fields(call)
			calls[i] = strings.Join(words[:min(len(words), map[bool]int{true: 1, false: limit[0]}[words[0] == "daemon"])], " ")
		}
	}
	return calls
}

// record is headless.json.
func (h *host) record() (r struct {
	Release    struct{ Version, Channel, Downloads string }
	Service    string
	AutoUpdate bool `json:"auto_update"`
	Operations map[string]struct{ ID, Action string }
}) {
	h.t.Helper()
	b, err := os.ReadFile(filepath.Join(h.home, ".local/state/openmachine-om/headless.json"))
	if err != nil || json.Unmarshal(b, &r) != nil {
		h.t.Fatalf("headless.json: %v\n%s", err, b)
	}
	return r
}

func want(t *testing.T, what string, got, wanted any) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(wanted) {
		t.Fatalf("%s:\n got %v\nwant %v", what, got, wanted)
	}
}

// TestInstall installs Core and the Connector once, each under an operation
// ID, links om, and keeps the daemon running by the first service kind the
// machine offers, written exactly; om uninstall removes all of it.
func TestInstall(t *testing.T) {
	t.Run("a user without a service manager", func(t *testing.T) {
		h := newHost(t, "1000", false)
		os.WriteFile(filepath.Join(h.home, ".bashrc"), []byte("alias ll='ls -l'"), 0o600)
		r := h.release("0.0.16")
		out, errOut, code := h.install(r, "--no-up")
		selected := filepath.Join(h.home, ".local/share/openmachine/connector/versions", r.connector)
		want(t, "install", fmt.Sprint(code, out, errOut), fmt.Sprint(0, "Open Machine 0.0.16 installed for tester. "+
			"It starts when you open a shell: this machine has no service manager.\nom is "+h.link()+": "+filepath.Dir(h.link())+" is not on PATH.\n"))
		python := filepath.Join(h.home, ".local/state/openmachine/service/payloads", r.core, "python/bin/python3")
		daemon := h.daemon()
		calls := h.calls()
		want(t, "the daemon", calls[len(calls)-1], "daemon "+filepath.Join(selected, "om")+" -core-python "+python+" -core-config "+
			filepath.Join(h.home, ".config/openmachine/config.toml")+" -core-state "+filepath.Join(h.home, ".local/state/openmachine"))
		for i, call := range calls {
			calls[i] = strings.Join(strings.Fields(call)[:2], " ")
		}
		want(t, "owner calls", calls, []string{"core status", "core configure", "core install", "core operation", "core status",
			"connector inspect", "connector configure", "connector setup", "daemon " + filepath.Join(selected, "om")})
		if target, _ := os.Readlink(h.link()); target != filepath.Join(selected, "om") {
			t.Fatalf("om links to %s", target)
		}
		record := h.record()
		want(t, "headless.json", fmt.Sprint(record.Release, " ", record.Service, " ", record.AutoUpdate, " ", len(record.Operations)),
			"{0.0.16 staging http://127.0.0.1:1/linux} shell true 0")
		line := "'" + h.link() + "' start >/dev/null 2>&1 # Open Machine: start at login\n"
		rc, _ := os.ReadFile(filepath.Join(h.home, ".bashrc"))
		want(t, ".bashrc", string(rc), "alias ll='ls -l'\n"+line)
		if fi, _ := os.Stat(filepath.Join(h.home, ".bashrc")); fi.Mode().Perm() != 0o600 {
			t.Fatalf(".bashrc mode = %v", fi.Mode())
		}

		// Run again, the release installs nothing twice and adds no second line.
		if out, errOut, code := h.install(r, "--no-up"); code != 0 || out != "" {
			t.Fatalf("second install = %d\n%s%s", code, out, errOut)
		}
		want(t, "owner calls of a second install", h.calls(), []string{})
		if rc, _ := os.ReadFile(filepath.Join(h.home, ".bashrc")); string(rc) != "alias ll='ls -l'\n"+line || holder(h.home) != daemon {
			t.Fatalf("a second install changed the shell line or the daemon: %q", rc)
		}

		// The shell line starts the daemon again when it is not running, and only then.
		syscall.Kill(daemon, syscall.SIGKILL)
		h.stopped()
		h.om("start")
		h.daemon()
		h.om("start")
		want(t, "daemons started by the shell line", len(h.calls()), 1)

		// Without Cloud the machine cannot be removed from the account: its files stay, and om with them.
		edit(h.world, func(w *world) { w.Faults["remove"] = "offline" })
		_, errOut, code = h.om("uninstall")
		want(t, "uninstall without Cloud", fmt.Sprint(code, errOut), "1om: this machine could not be removed from the account (cloud_unavailable); "+
			"its service is stopped. Check the network and run: om uninstall\n")
		// The line is removed whatever shell uninstalls.
		t.Setenv("SHELL", "/bin/zsh")
		os.WriteFile(filepath.Join(h.home, ".bashrc"), rc, 0o600)
		out, errOut, code = h.om("uninstall")
		want(t, "uninstall", fmt.Sprint(code, out, errOut), "0Open Machine was removed. Running jobs were kept.\n")
		if rc, _ := os.ReadFile(filepath.Join(h.home, ".bashrc")); string(rc) != "alias ll='ls -l'\n" {
			t.Fatalf(".bashrc after uninstall = %q", rc)
		}
		left, _ := filepath.Glob(filepath.Join(h.home, ".local/*/openmachine*"))
		config, _ := filepath.Glob(filepath.Join(h.home, ".config/openmachine"))
		if _, err := os.Lstat(h.link()); err == nil || len(left)+len(config) != 0 || holder(h.home) != 0 {
			t.Fatalf("uninstall left %v %v, the link (%v) or the daemon", left, config, err)
		}
	})

	unit := func(h *host, r release, user, wanted string) string {
		selected := filepath.Join(h.home, ".local/share/openmachine/connector/versions", r.connector)
		python := filepath.Join(h.home, ".local/state/openmachine/service/payloads", r.core, "python/bin/python3")
		return "[Unit]\nDescription=Open Machine\n\n[Service]\n" + user + `ExecStart="` + filepath.Join(selected, "om") + `" "daemon" "-core-python" "` + python +
			`" "-core-config" "` + filepath.Join(h.home, ".config/openmachine/config.toml") + `" "-core-state" "` + filepath.Join(h.home, ".local/state/openmachine") +
			"\"\nRestart=always\nRestartSec=5\nKillMode=process\nOOMPolicy=continue\n\n[Install]\nWantedBy=" + wanted + "\n"
	}

	t.Run("root with systemd", func(t *testing.T) {
		h := newHost(t, "0", true)
		t.Setenv("PATH", filepath.Join(h.root, "usr/local/bin")+":"+os.Getenv("PATH"))
		r := h.release("0.0.16")
		out, errOut, code := h.install(r, "--no-up")
		want(t, "install", fmt.Sprint(code, out, errOut), "0Open Machine 0.0.16 installed for root. It starts at boot.\n")
		path := filepath.Join(h.root, "etc/systemd/system/openmachine.service")
		text, _ := os.ReadFile(path)
		want(t, "the system unit", string(text), unit(h, r, "User=root\n", "multi-user.target"))
		h.daemon()
		want(t, "service calls", h.calls(3)[8:], []string{"systemctl daemon-reload", "systemctl enable openmachine.service", "systemctl restart openmachine.service", "daemon"})
		want(t, "the recorded kind", h.record().Service, "system")
		if out, errOut, code := h.om("uninstall"); code != 0 {
			t.Fatalf("uninstall = %d\n%s%s", code, out, errOut)
		}
		want(t, "uninstall's calls", h.calls(), []string{"systemctl disable --now openmachine.service", "systemctl daemon-reload", "connector remove"})
		if _, err := os.Stat(path); err == nil || holder(h.home) != 0 {
			t.Fatal("uninstall left the unit or the daemon")
		}
	})

	t.Run("a user with systemd", func(t *testing.T) {
		h := newHost(t, "1000", true)
		r := h.release("0.0.16")
		out, errOut, code := h.install(r, "--no-up")
		if code != 0 || !strings.HasPrefix(out, "Open Machine 0.0.16 installed for tester. It starts at boot.\n") {
			t.Fatalf("install = %d\n%s%s", code, out, errOut)
		}
		path := filepath.Join(h.home, ".config/systemd/user/openmachine.service")
		text, _ := os.ReadFile(path)
		want(t, "the user unit", string(text), unit(h, r, "", "default.target"))
		h.daemon()
		want(t, "service calls", h.calls(4)[8:], []string{"loginctl show-user tester --property=Linger", "loginctl enable-linger tester", "systemctl --user show-environment",
			"systemctl --user daemon-reload", "systemctl --user enable openmachine.service", "systemctl --user restart openmachine.service", "daemon"})
		want(t, "the recorded kind", h.record().Service, "user")
		// Jobs run under the user's manager, which stops without linger: uninstall leaves it on and says so.
		out, errOut, code = h.om("uninstall")
		want(t, "uninstall", fmt.Sprint(code, out, errOut), "0Open Machine was removed. Running jobs were kept.\n"+
			"Linger stays on for tester, which keeps them running after logout. To turn it off: loginctl disable-linger tester\n")
		want(t, "uninstall's calls", h.calls(), []string{"systemctl --user disable --now openmachine.service", "systemctl --user daemon-reload", "connector remove"})
		if _, err := os.Stat(path); err == nil || holder(h.home) != 0 {
			t.Fatal("uninstall left the unit or the daemon")
		}
	})

	t.Run("root whose system unit cannot run the daemon gets a user unit, and the system unit is removed", func(t *testing.T) {
		h := newHost(t, "0", true)
		t.Setenv("PATH", filepath.Join(h.root, "usr/local/bin")+":"+os.Getenv("PATH"))
		edit(h.world, func(w *world) { w.Faults["systemctl restart"] = "dead" }) // as under SELinux: systemd may not run a program in /root
		r := h.release("0.0.16")
		out, errOut, code := h.install(r, "--no-up")
		want(t, "install", fmt.Sprint(code, out, errOut), "0Open Machine 0.0.16 installed for root. It starts at boot.\n")
		h.daemon()
		want(t, "service calls", h.calls(4)[8:], []string{"systemctl daemon-reload", "systemctl enable openmachine.service", "systemctl restart openmachine.service",
			"systemctl disable --now openmachine.service", "systemctl daemon-reload",
			"loginctl show-user root --property=Linger", "loginctl enable-linger root", "systemctl --user show-environment",
			"systemctl --user daemon-reload", "systemctl --user enable openmachine.service", "systemctl --user restart openmachine.service", "daemon"})
		want(t, "the recorded kind", h.record().Service, "user")
		if _, err := os.Stat(filepath.Join(h.root, "etc/systemd/system/openmachine.service")); err == nil {
			t.Fatal("the system unit that could not run the daemon was left")
		}
		text, _ := os.ReadFile(filepath.Join(h.home, ".config/systemd/user/openmachine.service"))
		want(t, "root's user unit", string(text), unit(h, r, "", "default.target"))
	})

	t.Run("a user whose manager cannot be reached gets the shell kind, and linger is turned off again", func(t *testing.T) {
		h := newHost(t, "1000", true)
		edit(h.world, func(w *world) {
			w.Faults["systemctl daemon-reload"] = "broken"
			w.Faults["systemctl show-environment"] = "broken"
		})
		out, _, code := h.install(h.release("0.0.16"), "--no-up")
		if code != 0 || !strings.Contains(out, "It starts when you open a shell") {
			t.Fatalf("install = %d\n%s", code, out)
		}
		h.daemon()
		calls := h.calls(2)
		want(t, "the first and last service calls", fmt.Sprint(calls[8:11], calls[len(calls)-5:]), fmt.Sprint(
			[]string{"loginctl show-user", "loginctl enable-linger", "systemctl --user"},
			[]string{"systemctl --user", "systemctl --user", "systemctl --user", "loginctl disable-linger", "daemon"}))
		want(t, "the recorded kind", h.record().Service, "shell")
		if _, err := os.Stat(filepath.Join(h.home, ".config/systemd/user/openmachine.service")); err == nil {
			t.Fatal("the user unit no manager reads was left")
		}
	})

	t.Run("an interrupted install is finished by its recorded operation IDs", func(t *testing.T) {
		for _, c := range []struct {
			name, call, fault, then string
			calls                   []string // the second run's
		}{
			{"Core's reply was lost", "core install", "lost", "", []string{"core install", "core status", "connector inspect", "connector configure", "connector setup", "daemon"}},
			{"Core's process died and Core continues it", "core install", "died", "", []string{"core install", "core continue", "core operation", "core status",
				"connector inspect", "connector configure", "connector setup", "daemon"}},
			{"Core's process died and Core ends it", "core install", "died", "reject", []string{"core install", "core continue", "core operation", "core status", "core configure",
				"core install", "core operation", "core status", "connector inspect", "connector configure", "connector setup", "daemon"}},
			{"the Connector's setup died", "setup start", "interrupt", "", []string{"core status", "connector setup", "connector setup", "connector inspect", "daemon"}},
		} {
			t.Run(c.name, func(t *testing.T) {
				h := newHost(t, "1000", false)
				r := h.release("0.0.16")
				edit(h.world, func(w *world) { w.Faults[c.call] = c.fault })
				if _, errOut, code := h.install(r, "--no-up"); code != 1 || strings.Count(errOut, "\n") != 1 || !strings.HasPrefix(errOut, "om: ") {
					t.Fatalf("interrupted install = %d\n%s", code, errOut)
				}
				owner := strings.Fields(c.call)[0]
				if _, recorded := h.record().Operations[owner]; !recorded {
					t.Fatalf("the %s operation is not recorded: %+v", owner, h.record().Operations)
				}
				h.calls()
				if c.then != "" {
					edit(h.world, func(w *world) { w.Faults["core continue"] = c.then })
				}
				if out, errOut, code := h.install(r, "--no-up"); code != 0 || len(h.record().Operations) != 0 {
					t.Fatalf("second install = %d, %+v\n%s%s", code, h.record().Operations, out, errOut)
				}
				h.daemon()
				want(t, "the second run's owner calls", h.calls(2), c.calls)
			})
		}
	})

	t.Run("a Core operation that needs recovery is reported with what to do", func(t *testing.T) {
		h := newHost(t, "1000", false)
		edit(h.world, func(w *world) { w.Faults["core install"] = "recovery" })
		r := h.release("0.0.16")
		_, errOut, code := h.install(r, "--no-up")
		// No om is linked yet: the line names the om that can uninstall.
		want(t, "install", fmt.Sprint(code, errOut), "1om: Core stopped an update it can neither finish nor end (recovery_required: maintenance_effect_unverified), "+
			"and it blocks every later one. Run: "+filepath.Join(r.dir, "connector/om")+" uninstall, then the install command.\n")
	})

	t.Run("a HOME that is another user's is refused", func(t *testing.T) {
		if os.Geteuid() == 0 {
			t.Skip("NOT RUN: root owns the directory this uses as another user's")
		}
		h := newHost(t, "1000", false)
		r := h.release("0.0.16")
		t.Setenv("HOME", "/")
		_, errOut, code := h.install(r, "--no-up")
		want(t, "install", fmt.Sprint(code, errOut), "1om: HOME (/) is not tester's own directory. Run this as the user Open Machine is for, with that user's HOME.\n")
		want(t, "owner calls", h.calls(), []string{})
	})

	t.Run("state that others can read, or that is another user's, is refused before it is read", func(t *testing.T) {
		h := newHost(t, "1000", false)
		r := h.release("0.0.16")
		state := filepath.Join(h.home, ".local/state/openmachine-om")
		os.MkdirAll(state, 0o700)
		// What a recorded operation names would be run: this one must never be.
		os.WriteFile(filepath.Join(state, "headless.json"), []byte(`{"operations":{"core":{"id":"x","action":"update","input":{"payload":"/"}}}}`), 0o600)
		refused := "1om: " + state + " must be this user's own directory with mode 700.\n"
		os.Chmod(state, 0o750)
		_, errOut, code := h.install(r, "--no-up")
		want(t, "install into readable state", fmt.Sprint(code, errOut), refused)
		os.Chmod(state, 0o700)
		if os.Geteuid() == 0 { // only root can make, and read, another user's directory
			os.Chown(state, 1, 1)
			_, errOut, code = h.install(r, "--no-up")
			want(t, "install into another user's state", fmt.Sprint(code, errOut), refused)
		}
		want(t, "owner calls", h.calls(), []string{})
	})

	t.Run("a release for another machine is refused", func(t *testing.T) {
		h := newHost(t, "1000", false)
		r := h.release("0.0.16")
		os.WriteFile(filepath.Join(r.dir, "release.json"), []byte(`{"format":1,"version":"0.0.16","arch":"riscv64"}`), 0o644)
		_, errOut, code := h.install(r, "--no-up")
		want(t, "install", fmt.Sprint(code, errOut), "1om: this is not a release for this machine. Run the install command again.\n")
		want(t, "owner calls", h.calls(), []string{})
	})
}

// TestUp signs in with a key or a printed link, turns the connection on and
// waits for Connected; with nothing to do it only reports; interrupted, it is
// resumed by its recorded operation ID.
func TestUp(t *testing.T) {
	connected := "Connected: gpu-01.om (user tester), Full access.\n"
	intent := func(h *host) string {
		b, _ := os.ReadFile(filepath.Join(h.home, ".local/state/openmachine-om/remote-access.json"))
		return string(b)
	}
	// actions are the Connector account actions among calls.
	actions := func(calls []string) (found []string) {
		for _, call := range calls {
			if action, ok := strings.CutPrefix(call, "connector companion "); ok {
				found = append(found, action)
			}
		}
		return found
	}

	t.Run("with a key, and again with nothing to do", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		out, errOut, code := h.om("up", "--key", "omkey_secret", "--name", "gpu-01")
		want(t, "om up", fmt.Sprint(code, out, errOut), "0"+connected)
		want(t, "account actions", actions(h.calls(3)), []string{"connection", "remote-access", "connection", "snapshot", "login", "snapshot"})
		want(t, "the intent", intent(h), `{"format":1,"enabled":true,"revision":1}`)
		var w world
		edit(h.world, func(current *world) { w = *current })
		want(t, "the sign-in's key and name", w.Connector.Key+" "+w.Connector.Name, "omkey_secret gpu-01")
		state, _ := os.ReadFile(filepath.Join(h.home, ".local/state/openmachine-om/headless.json"))
		if bytes.Contains(state, []byte("omkey_secret")) || len(h.record().Operations) != 0 {
			t.Fatalf("headless.json holds the key or an operation:\n%s", state)
		}

		out, errOut, code = h.om("up")
		want(t, "a second om up", fmt.Sprint(code, out, errOut), "0"+connected)
		want(t, "a second om up's account actions", actions(h.calls(3)), []string{"connection", "snapshot"})
		want(t, "the intent after a second om up", intent(h), `{"format":1,"enabled":true,"revision":1}`)

		// An intent the Connector never applied, as when om was interrupted, is applied again at its revision.
		edit(h.world, func(w *world) { w.Connector.Stopped = true })
		out, errOut, code = h.om("up")
		want(t, "om up after an apply that never finished", fmt.Sprint(code, out, errOut), "0"+connected)
		want(t, "its account actions", actions(h.calls(3)), []string{"connection", "remote-access", "connection", "snapshot"})
		want(t, "the intent", intent(h), `{"format":1,"enabled":true,"revision":1}`)
	})

	t.Run("a key file", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		os.WriteFile(filepath.Join(h.dir, "key"), []byte("omkey_from_file\n"), 0o600)
		if out, errOut, code := h.om("up", "--key-file", filepath.Join(h.dir, "key")); code != 0 {
			t.Fatalf("om up = %d\n%s%s", code, out, errOut)
		}
		edit(h.world, func(w *world) { want(t, "the key", w.Connector.Key, "omkey_from_file") })
	})

	t.Run("without a key it prints the link and waits for approval", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		edit(h.world, func(w *world) { w.Connector.Approve = 1 })
		out, errOut, code := h.om("up")
		want(t, "om up", fmt.Sprint(code, out, errOut), "0Open this link in a browser to connect this machine:\n  https://mcp.example/native-login?id=session\n"+
			"Connected: gpu-01.om (user tester), Full access.\n")
	})

	t.Run("a command that holds the Connector during the wait does not end it", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		edit(h.world, func(w *world) { w.Faults["snapshot"] = "busy" })
		out, errOut, code := h.om("up")
		want(t, "om up", fmt.Sprint(code, out, errOut), "0Open this link in a browser to connect this machine:\n  https://mcp.example/native-login?id=session\n"+connected)
		want(t, "account actions", actions(h.calls(3)), []string{"connection", "remote-access", "connection", "login", "snapshot", "snapshot"})
	})

	t.Run("a key ends a sign-in that is still pending first", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		edit(h.world, func(w *world) { w.Connector.Session = "pending"; w.Connector.Approve = 1 << 30 })
		if out, errOut, code := h.om("up", "--key", "omkey_secret"); code != 0 {
			t.Fatalf("om up = %d\n%s%s", code, out, errOut)
		}
		want(t, "account actions", actions(h.calls(3)), []string{"connection", "remote-access", "connection", "snapshot", "cancel-login", "login", "snapshot"})
	})

	t.Run("a refused key is reported with Cloud's code", func(t *testing.T) {
		for _, refusal := range []string{"key_invalid", "key_platform", "native_device_limit", "native_login_settled"} {
			h := newHost(t, "1000", false)
			h.installed("0.0.16")
			edit(h.world, func(w *world) { w.Connector.KeyRefusal = refusal })
			_, errOut, code := h.om("up", "--key", "omkey_secret")
			want(t, "om up", fmt.Sprint(code, errOut), "1om: the key was refused ("+refusal+"). Create a new key in the Console and run the command again.\n")
			want(t, "operations left", len(h.record().Operations), 0)
		}
	})

	t.Run("an interrupted om up is resumed by its recorded ID", func(t *testing.T) {
		for _, c := range []struct{ name, action, fault string }{
			{"the sign-in's reply was lost", "login", "lost"},
			{"Cloud could not be reached during the sign-in", "login", "unknown"},
			{"the intent's reply was lost", "remote-access", "lost"},
		} {
			t.Run(c.name, func(t *testing.T) {
				h := newHost(t, "1000", false)
				h.installed("0.0.16")
				edit(h.world, func(w *world) { w.Faults[c.action] = c.fault })
				if _, errOut, code := h.om("up", "--key", "omkey_secret"); code != 1 || strings.Count(errOut, "\n") != 1 || !strings.HasPrefix(errOut, "om: ") {
					t.Fatalf("interrupted om up = %d\n%s", code, errOut)
				}
				want(t, "the recorded operation", h.record().Operations["companion"].Action, c.action)
				first := actions(h.calls(3))
				out, errOut, code := h.om("up", "--key", "omkey_secret")
				want(t, "the second om up", fmt.Sprint(code, out, errOut), "0"+connected)
				second := actions(h.calls(3))
				if second[0] != "operation" || slices.Contains(second[1:], c.action) || !slices.Contains(first, c.action) {
					t.Fatalf("the second om up did not read %s back, or sent it again:\n%v\n%v", c.action, first, second)
				}
			})
		}
	})

	t.Run("om down, om logout and om set", func(t *testing.T) {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		h.om("up", "--key", "omkey_secret")
		out, errOut, code := h.om("down")
		want(t, "om down", fmt.Sprint(code, out, errOut), "0Disconnected. Jobs keep running.\n")
		want(t, "the intent", intent(h), `{"format":1,"enabled":false,"revision":2}`)
		daemon := holder(h.home)
		if out, _, code := h.om("down"); code != 0 || intent(h) != `{"format":1,"enabled":false,"revision":2}` || holder(h.home) != daemon || daemon == 0 {
			t.Fatalf("a second om down = %d\n%s%s", code, out, intent(h))
		}
		if out, errOut, code := h.om("up"); code != 0 || intent(h) != `{"format":1,"enabled":true,"revision":3}` {
			t.Fatalf("om up after om down = %d\n%s%s%s", code, out, errOut, intent(h))
		}
		// Without Cloud nothing says the machine is signed out, so om does not say it.
		edit(h.world, func(w *world) { w.Faults["snapshot"] = "unreachable" })
		out, errOut, code = h.om("logout")
		want(t, "om logout without Cloud", fmt.Sprint(code, out, errOut), "1om: reading the sign-in could not reach Cloud (cloud_unavailable). Check the network and run the command again.\n")
		out, errOut, code = h.om("logout")
		want(t, "om logout", fmt.Sprint(code, out, errOut), "0Signed out.\n")
		edit(h.world, func(w *world) { want(t, "the session", w.Connector.Session, "signed_out") })
		if _, _, code := h.om("set", "auto-update", "off"); code != 0 || h.record().AutoUpdate {
			t.Fatalf("om set auto-update off = %d, %v", code, h.record().AutoUpdate)
		}
		if _, _, code := h.om("set", "auto-update", "on"); code != 0 || !h.record().AutoUpdate {
			t.Fatalf("om set auto-update on = %d, %v", code, h.record().AutoUpdate)
		}
	})

	t.Run("usage and a machine that is not installed", func(t *testing.T) {
		h := newHost(t, "1000", false)
		os.WriteFile(filepath.Join(h.dir, "om"), nil, 0o755)
		for _, args := range [][]string{{}, {"sideways"}, {"up", "--json"}, {"up", "--key", "a", "--key-file", "b"}, {"set", "auto-update", "maybe"}, {"status", "extra"}} {
			if _, errOut, code := h.main(filepath.Join(h.dir, "om"), args...); code != 2 || !strings.HasPrefix(errOut, "usage: om up ") {
				t.Fatalf("om %v = %d\n%s", args, code, errOut)
			}
		}
		for _, command := range []string{"up", "down", "status", "logout", "update", "version"} {
			_, errOut, code := h.main(filepath.Join(h.dir, "om"), command)
			want(t, "om "+command, fmt.Sprint(code, errOut), "1om: Open Machine is not installed for tester. Run the install command.\n")
		}
	})
}

// TestMove is om move, which the daemon runs when the machine is moved in the
// Console: the move's secret arrives on standard input and goes only to the
// Connector, and a move that fails leaves the machine as it was.
func TestMove(t *testing.T) {
	actions := func(calls []string) (found []string) {
		for _, call := range calls {
			if action, ok := strings.CutPrefix(call, "connector companion "); ok {
				found = append(found, strings.Fields(action)[0])
			}
		}
		return found
	}
	connected := func(t *testing.T) *host {
		h := newHost(t, "1000", false)
		h.installed("0.0.16")
		if out, errOut, code := h.om("up", "--key", "omkey_secret", "--name", "gpu-01"); code != 0 {
			t.Fatalf("om up = %d\n%s%s", code, out, errOut)
		}
		h.calls()
		return h
	}
	move := func(h *host) (stdout, stderr string, code int) {
		linux.Input = strings.NewReader("ommove_secret\n")
		t.Cleanup(func() { linux.Input = os.Stdin })
		return h.om("move")
	}
	device := func(h *host) (device int, key string) {
		edit(h.world, func(w *world) { device, key = w.Connector.Device, w.Connector.Key })
		return
	}

	t.Run("a connected machine registers again with the move's secret", func(t *testing.T) {
		h := connected(t)
		before, _ := device(h)
		out, errOut, code := move(h)
		want(t, "om move", fmt.Sprint(code, out, errOut), "0Connected: gpu-01.om (user tester), Full access.\n")
		want(t, "account actions", actions(h.calls()), []string{"connection", "move", "snapshot"})
		after, key := device(h)
		want(t, "the device in use and the secret the Connector got", fmt.Sprint(after-before, key), "1ommove_secret")
		state, _ := os.ReadFile(filepath.Join(h.home, ".local/state/openmachine-om/headless.json"))
		if bytes.Contains(state, []byte("ommove_secret")) || len(h.record().Operations) != 0 {
			t.Fatalf("headless.json holds the secret or an operation:\n%s", state)
		}
	})

	t.Run("a refused move leaves the machine as it was", func(t *testing.T) {
		for _, refusal := range []string{"move_refused", "key_invalid", "move_unavailable"} {
			h := connected(t)
			before, _ := device(h)
			edit(h.world, func(w *world) { w.Connector.KeyRefusal = refusal })
			_, errOut, code := move(h)
			want(t, "om move", fmt.Sprint(code, errOut), "1om: this machine was not moved ("+refusal+"). Move it again in the Console.\n")
			after, _ := device(h)
			want(t, "the device in use and the operations left", fmt.Sprint(after-before, len(h.record().Operations)), "0 0")
			if out, errOut, code := h.om("status"); code != 0 {
				t.Fatalf("om status after a refused move = %d\n%s%s", code, out, errOut)
			}
		}
	})

	t.Run("a machine that is not connected is not moved", func(t *testing.T) {
		h := connected(t)
		if _, errOut, code := h.om("logout"); code != 0 {
			t.Fatalf("om logout = %d\n%s", code, errOut)
		}
		h.calls()
		_, errOut, code := move(h)
		want(t, "om move", fmt.Sprint(code, errOut), "1om: this machine is not connected, so it was not moved. Run: om up\n")
		want(t, "account actions", actions(h.calls()), []string{"connection"})
	})

	// Nobody is at a machine moved from the Console to run om again: om move itself reads the outcome back.
	t.Run("a move whose reply was lost is read back by its recorded ID, never sent again", func(t *testing.T) {
		wait, step := linux.MoveWait, linux.MoveStep
		t.Cleanup(func() { linux.MoveWait, linux.MoveStep = wait, step })
		linux.MoveStep = time.Millisecond
		for _, fault := range []string{"lost", "unknown"} {
			h := connected(t)
			before, _ := device(h)
			edit(h.world, func(w *world) { w.Faults["move"] = fault })
			out, errOut, code := move(h)
			want(t, "om move, "+fault, fmt.Sprint(code, out, errOut), "0Connected: gpu-01.om (user tester), Full access.\n")
			want(t, "account actions", actions(h.calls()), []string{"connection", "move", "operation", "snapshot"})
			after, _ := device(h)
			want(t, "the device in use and the operations left", fmt.Sprint(after-before, len(h.record().Operations)), "1 0")
		}
		// An outcome Cloud cannot give in time stays recorded for the next om command.
		linux.MoveWait = 0
		h := connected(t)
		edit(h.world, func(w *world) { w.Faults["move"] = "lost" })
		if _, errOut, code := move(h); code != 1 || strings.Count(errOut, "\n") != 1 || !strings.HasPrefix(errOut, "om: ") {
			t.Fatalf("interrupted om move = %d\n%s", code, errOut)
		}
		want(t, "the recorded operation", h.record().Operations["companion"].Action, "move")
		h.calls()
		if out, errOut, code := h.om("up"); code != 0 {
			t.Fatalf("om up after the move = %d\n%s%s", code, out, errOut)
		}
		if second := actions(h.calls()); second[0] != "operation" || slices.Contains(second, "move") || len(h.record().Operations) != 0 {
			t.Fatalf("the move was not read back once: %v", second)
		}
	})
}

// TestStatus prints one state line, then what is known of the machine, and
// exits 0 only when Connected.
func TestStatus(t *testing.T) {
	h := newHost(t, "1000", false)
	h.installed("0.0.16")
	h.om("up", "--key", "omkey_secret", "--name", "gpu-01")
	details := "Name: gpu-01\nAlias: gpu-01.om\nUser: tester\nAccount: alice\nPermission: Full access\nVersion: 0.0.16\n"
	known := "User: tester\nVersion: 0.0.16\n"
	for _, c := range []struct {
		name   string
		change func(w *world)
		code   int
		out    string
	}{
		{"connected", func(w *world) {}, 0, "Connected\n" + details},
		{"joining the network", func(w *world) { w.Reason = "joining" }, 1, "Connecting\n" + details},
		{"refused as too old", func(w *world) { w.Reason = "update_required" }, 1, "Update required. Run: om update\n" + details},
		{"signed out", func(w *world) { w.Connector.Session = "signed_out" }, 1, "Signed out. Run: om up\n" + known},
		{"removed in the Console", func(w *world) { w.Connector.Revoked = true }, 1, "Removed from the account. Run: om up\n" + known},
	} {
		edit(h.world, func(w *world) { w.Reason, w.Connector.Session, w.Connector.Revoked = "", "active", false; c.change(w) })
		out, errOut, code := h.om("status")
		want(t, c.name, fmt.Sprint(code, out, errOut), fmt.Sprint(c.code, c.out))
	}
	edit(h.world, func(w *world) { w.Reason, w.Connector.Session, w.Connector.Revoked = "", "active", false })

	out, _, code := h.om("status", "--json")
	want(t, "om status --json", fmt.Sprint(code, out), `0{"account":"alice","alias":"gpu-01.om","name":"gpu-01","permission":"full_access",`+
		`"state":"connected","user":"tester","version":"0.0.16"}`+"\n")
	out, _, _ = h.om("version")
	want(t, "om version", out, "Open Machine 0.0.16\nCore 0.2.29\n")

	// A team's machine shows the team as its account and the name its members type.
	edit(h.world, func(w *world) { w.Team = "acme" })
	out, _, code = h.om("status")
	want(t, "a team's machine", fmt.Sprint(code, out), "0Connected\nName: gpu-01\nAlias: gpu-01.acme.om\nUser: tester\nAccount: acme\nPermission: Full access\nVersion: 0.0.16\n")
	edit(h.world, func(w *world) { w.Team = "" })

	h.om("down")
	out, errOut, code := h.om("status")
	want(t, "disconnected", fmt.Sprint(code, out, errOut), "1Disconnected. Run: om up\n"+known)
	h.om("up")
	daemon := holder(h.home)
	syscall.Kill(daemon, syscall.SIGKILL)
	h.stopped()
	out, errOut, code = h.om("status")
	want(t, "not running", fmt.Sprint(code, out, errOut), "1Not running. Run: om up\n"+known)
	// om up starts it again: the shell kind has no supervisor.
	if out, errOut, code := h.om("up"); code != 0 || holder(h.home) == 0 {
		t.Fatalf("om up with no daemon = %d\n%s%s", code, out, errOut)
	}
}
