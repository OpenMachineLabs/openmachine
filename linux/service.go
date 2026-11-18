package linux

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	unitName = "openmachine.service"
	// shellMark ends the line om adds to a shell start file, so that it is added once and removed whole.
	shellMark = "# Open Machine: start at login"
)

// daemonArgs is the daemon as every service kind runs it, as Desktop does on macOS.
func (m *machine) daemonArgs() []string {
	return []string{filepath.Join(m.bundle, "om"), "daemon", "-core-python", m.rec.Core.AccessPython,
		"-core-config", m.rec.Core.Config, "-core-state", m.rec.Core.State}
}

// unit is the systemd unit of both kinds: a restart or an update ends no
// detached job, and a job killed for memory does not stop the service.
func unit(argv []string, system bool) string {
	quoted := make([]string, len(argv))
	for i, arg := range argv {
		quoted[i] = `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`, `%`, `%%`, `$`, `$$`).Replace(arg) + `"`
	}
	user, wanted := "", "default.target"
	if system {
		user, wanted = "User=root\n", "multi-user.target"
	}
	return "[Unit]\nDescription=Open Machine\n\n[Service]\n" + user + "ExecStart=" + strings.Join(quoted, " ") +
		"\nRestart=always\nRestartSec=5\nKillMode=process\nOOMPolicy=continue\n\n[Install]\nWantedBy=" + wanted + "\n"
}

// systemctl runs systemctl for the service's manager: the system's, or this user's.
func (m *machine) systemctl(args ...string) error {
	if m.rec.Service == "user" {
		args = append([]string{"--user"}, args...)
	}
	return m.tool("systemctl", args...)
}

// tool runs a system tool, with the user manager's runtime directory when
// the environment names none, as in a session that is not a login.
func (m *machine) tool(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Env = os.Environ()
	if os.Getenv("XDG_RUNTIME_DIR") == "" {
		cmd.Env = append(cmd.Env, "XDG_RUNTIME_DIR="+filepath.Join(Root+"/run/user", strconv.Itoa(m.uid)))
	}
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("%s %s: %v: %s", name, strings.Join(args, " "), err, bytes.TrimSpace(out))
	}
	return nil
}

func (m *machine) unitPath() string {
	if m.rec.Service == "system" {
		return filepath.Join(Root+"/etc/systemd/system", unitName)
	}
	return filepath.Join(m.home, ".config/systemd/user", unitName)
}

// chooseService sets up the first service kind that keeps the daemon running
// on this machine and records it: for root the system unit, then for any user
// a user unit with linger, then the shell line. A kind whose daemon does not
// start, as a system unit's under SELinux, whose systemd may not run a program
// in a home, leaves nothing behind. systemd keeps a unit only for a user in
// their account's own home: its managers read units and start the daemon
// there.
func (m *machine) chooseService() error {
	kinds := []string{"shell"}
	if fi, err := os.Stat(Root + "/run/systemd/system"); err == nil && fi.IsDir() && m.account == m.home {
		kinds = []string{"user", "shell"}
		if m.uid == 0 {
			kinds = []string{"system", "user", "shell"}
		}
	}
	var err error
	for _, kind := range kinds {
		m.rec.Service = kind
		lingered := true
		if kind == "user" {
			// A user unit runs from boot only with linger, which also starts a user manager where none runs.
			before, _ := exec.Command("loginctl", "show-user", m.user, "--property=Linger", "--value").Output()
			if m.tool("loginctl", "enable-linger", m.user) != nil {
				continue
			}
			lingered = strings.TrimSpace(string(before)) == "yes"
			// The manager linger has just started takes a moment to answer.
			for deadline := time.Now().Add(10 * time.Second); m.systemctl("show-environment") != nil && time.Now().Before(deadline); {
				time.Sleep(200 * time.Millisecond)
			}
		}
		if err = m.serve(); err == nil {
			return nil
		}
		m.unserve()
		if !lingered { // nothing runs under the manager yet
			m.tool("loginctl", "disable-linger", m.user)
		}
	}
	return err
}

// serve keeps the daemon running as daemonArgs by the recorded kind, and
// requires that it runs. A daemon om started otherwise is replaced: after an
// update the old interpreter and om are no longer kept. The daemon it
// started last is recorded once it runs, so an interrupted run is finished by
// the next.
func (m *machine) serve() error {
	argv := m.daemonArgs()
	current := slices.Equal(argv, m.rec.Daemon)
	if m.rec.Service == "shell" {
		if err := m.shellLine(true); err != nil {
			return err
		}
		if current && m.daemonPID() != 0 {
			return nil
		}
		if err := m.stop(); err != nil {
			return err
		}
		return m.start()
	}
	text, path := unit(argv, m.rec.Service == "system"), m.unitPath()
	if written, _ := os.ReadFile(path); string(written) != text {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return err
		}
		if err := replace(path, []byte(text), 0o644); err != nil {
			return err
		}
		if err := m.systemctl("daemon-reload"); err != nil {
			return err
		}
	}
	if err := m.systemctl("enable", unitName); err != nil {
		return err
	}
	verb := "restart"
	if current {
		verb = "start"
	}
	if err := m.systemctl(verb, unitName); err != nil {
		return err
	}
	return m.started(argv)
}

// started records argv as the daemon om started once it holds its lock: a
// unit that systemd restarts for ever has started nothing.
func (m *machine) started(argv []string) error {
	for deadline := time.Now().Add(10 * time.Second); m.daemonPID() == 0; time.Sleep(100 * time.Millisecond) {
		if time.Now().After(deadline) {
			return errors.New("the daemon did not start")
		}
	}
	m.rec.Daemon = argv
	return m.save()
}

// start starts the daemon detached, logging to daemon.log, for the shell
// kind. It leads its own session and keeps the default signal actions: a
// daemon that ignored a hang-up would pass that on to every session.
func (m *machine) start() error {
	log, err := os.OpenFile(m.path("daemon.log"), os.O_WRONLY|os.O_APPEND|os.O_CREATE, 0o600)
	if err != nil {
		return err
	}
	defer log.Close()
	argv := m.daemonArgs()
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.Stdout, cmd.Stderr, cmd.Dir = log, log, "/"
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	cmd.Process.Release()
	return m.started(argv)
}

// stop ends the shell kind's daemon, and only it: its sessions lead their own.
func (m *machine) stop() error {
	pid := m.daemonPID()
	if pid == 0 {
		return nil
	}
	// Its lock names the daemon; where the system lists processes, that process must be one.
	if argv, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid)); pid < 0 || err == nil && !strings.Contains(string(argv), "\x00daemon\x00") {
		return errors.New("the daemon's lock does not name its process")
	}
	if err := syscall.Kill(pid, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
		return err
	}
	for deadline := time.Now().Add(15 * time.Second); m.daemonPID() != 0; time.Sleep(100 * time.Millisecond) {
		if time.Now().After(deadline) {
			return fmt.Errorf("the daemon (process %d) did not stop", pid)
		}
	}
	return nil
}

// shellLine adds to the user's shell start file the one marked line that
// starts the daemon again when it is not running, or removes it from every
// file it may be in.
func (m *machine) shellLine(add bool) error {
	file := ".profile"
	switch filepath.Base(os.Getenv("SHELL")) {
	case "zsh":
		file = ".zshrc"
	case "bash":
		file = ".bashrc"
	}
	for _, name := range []string{".zshrc", ".bashrc", ".profile"} {
		path := filepath.Join(m.home, name)
		old, err := os.ReadFile(path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		var lines []string
		for _, line := range strings.SplitAfter(string(old), "\n") {
			if !strings.Contains(line, shellMark) {
				lines = append(lines, line)
			}
		}
		text := strings.Join(lines, "")
		if add && name == file {
			if text != "" && !strings.HasSuffix(text, "\n") {
				text += "\n"
			}
			text += fmt.Sprintf("'%s' start >/dev/null 2>&1 %s\n", strings.ReplaceAll(m.link(), "'", `'\''`), shellMark)
		}
		if text == string(old) {
			continue
		}
		mode := os.FileMode(0o644)
		if fi, err := os.Stat(path); err == nil {
			mode = fi.Mode().Perm()
		}
		if err := replace(path, []byte(text), mode); err != nil {
			return err
		}
	}
	return nil
}

// unserve removes the service: the unit, or the shell line and the daemon.
// Jobs keep running, so a user's linger stays: their manager, which the jobs
// of a user unit run under, otherwise stops with the user's last session and
// ends them. It removes all it can and returns the first failure.
func (m *machine) unserve() error {
	if m.rec.Service == "shell" || m.rec.Service == "" {
		return errors.Join(m.shellLine(false), m.stop())
	}
	if _, err := os.Stat(m.unitPath()); err != nil {
		return nil
	}
	return errors.Join(m.systemctl("disable", "--now", unitName), os.Remove(m.unitPath()), m.systemctl("daemon-reload"))
}

// link is where om is found: a link to the selected bundle's om.
func (m *machine) link() string {
	if m.uid == 0 {
		return Root + "/usr/local/bin/om"
	}
	return filepath.Join(m.home, ".local/bin/om")
}
