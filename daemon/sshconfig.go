package daemon

import (
	"bytes"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"syscall"

	"golang.org/x/crypto/ssh"
)

const label = `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`

var (
	alias = regexp.MustCompile(`^` + label + `$`) // a canonical alias: one DNS label
	// display is any alias: one label, or a team's machine's two, its name and the team's.
	display = regexp.MustCompile(`^` + label + `(\.` + label + `)?$`)
	osUser  = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9._-]{0,31}$`)
)

// writeConfig keeps the OM-owned SSH configuration and known hosts current
// with the hosts of the lease applied last: one exact
// block per alias, under its device's canonical HostKeyAlias, strict host
// checking against Cloud's binding, no credentials offered or forwarded, and
// no ControlMaster unless asked for. Blocks are
// Match host, which OpenSSH compares lowercased, so no spelling of a .om name
// escapes to DNS. It is included, once, from ~/.ssh/config at the first applied
// lease that has a host, so the SSH configuration of a device with none, as a
// team's machine, is never touched.
func (d *daemon) writeConfig() {
	d.configMu.Lock()
	defer d.configMu.Unlock()
	d.mu.Lock()
	l := d.applied
	d.mu.Unlock()
	err := d.config(*l)
	if err == nil && len(l.Hosts) > 0 {
		err = Include(d.core.Home, d.state.Dir)
	}
	if err != nil {
		log.Printf("native: SSH configuration: %v", err)
	}
}

func (d *daemon) config(l Lease) error {
	exe, err := os.Executable()
	if err == nil {
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		return err
	}
	known := d.state.path("known_hosts")
	for _, p := range []string{exe, d.state.Dir} {
		if strings.ContainsAny(p, "\"\n\r") {
			return fmt.Errorf("unsupported path %q", p)
		}
	}
	var config, hosts bytes.Buffer
	config.WriteString("# Open Machine Code hosts, generated from Cloud; do not edit.\n")
	block := func(pattern, target, extra string) {
		fmt.Fprintf(&config, "\nMatch host %s\n%s", pattern, extra)
		fmt.Fprintf(&config, "  ProxyCommand %s proxy -state %s %s\n", tokens(quote(exe)), tokens(quote(d.state.Dir)), target)
		fmt.Fprintf(&config, "  UserKnownHostsFile \"%s\"\n  GlobalKnownHostsFile none\n  StrictHostKeyChecking yes\n  CheckHostIP no\n  UpdateHostKeys no\n", tokens(known))
		config.WriteString("  ForwardAgent no\n  ForwardX11 no\n  IdentityAgent none\n  PubkeyAuthentication no\n" +
			"  PasswordAuthentication no\n  KbdInteractiveAuthentication no\n  GSSAPIAuthentication no\n" +
			"  HostbasedAuthentication no\n  ControlMaster no\n" +
			"  ServerAliveInterval 15\n  ServerAliveCountMax 3\n") // a vanished target is hung up, as the daemon does a source
	}
	listed := slices.Clone(l.Hosts)
	slices.SortFunc(listed, func(a, b Host) int { return strings.Compare(a.DeviceID, b.DeviceID) })
	for _, h := range listed {
		if len(h.Aliases) == 0 || !alias.MatchString(h.Aliases[0]) || !osUser.MatchString(h.User) {
			continue
		}
		key, _, _, rest, err := ssh.ParseAuthorizedKey([]byte(h.Key))
		if err != nil || len(rest) != 0 || key.Type() != ssh.KeyAlgoED25519 {
			continue
		}
		canonical := h.Aliases[0] + ".om"
		for _, a := range h.Aliases {
			if display.MatchString(a) { // one exact block per alias, which the ProxyCommand names
				block(a+".om,"+a+".om.", a+".om", fmt.Sprintf("  User %s\n  HostKeyAlias %s\n  HostKeyAlgorithms ssh-ed25519\n", h.User, canonical))
			}
		}
		fmt.Fprintf(&hosts, "%s %s", canonical, ssh.MarshalAuthorizedKey(key)) // one canonical line
	}
	// Any other .om name, unknown or no longer permitted, still goes to the
	// daemon, which refuses it with a reason: never to DNS or another host. It
	// passes no %n, so no name typed reaches a shell.
	block("*.om,*.om.", ".om", "")
	// Known hosts first, so the configuration never names a host without its key.
	for _, f := range []struct {
		name string
		b    []byte
	}{{"known_hosts", hosts.Bytes()}, {"ssh_config", config.Bytes()}} {
		if old, err := d.state.read(f.name, maxState); err == nil && bytes.Equal(old, f.b) {
			continue
		}
		if err := d.state.write(f.name, f.b); err != nil {
			return err
		}
	}
	return nil
}

// quote quotes a word for the shell OpenSSH runs ProxyCommand with.
func quote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

// tokens escapes OpenSSH's % tokens.
func tokens(s string) string { return strings.ReplaceAll(s, "%", "%%") }

// includeRecord, in the daemon's state, records that ~/.ssh/config was handled.
const includeRecord = "ssh_include"

// Include adds one line, an Include of the generated ssh_config in the
// daemon's state dir, at the top of ~/.ssh/config under home, once: it creates
// ~/.ssh (0700) and the file (0600) if missing, writes
// atomically, keeps every other byte and the file's mode, and records in dir
// that it is done, so it never touches the file again and a user who removes
// the line keeps it removed. A ~/.ssh/config that is a symbolic link, as
// dotfile managers make, is left untouched for good; one that is not this
// user's regular file is an error, tried again with a later lease. Nothing is
// checked for conflicts or ever undone.
func Include(home, dir string) error {
	st := &state{Dir: dir}
	if _, err := st.read(includeRecord, 64); !errors.Is(err, os.ErrNotExist) {
		return err // nil: done before
	}
	config := st.path("ssh_config")
	// The path is absolute, since Include resolves ~ by HOME, and holds no quote or glob.
	if strings.ContainsAny(config, "\"\\\n\r*?[") {
		return fmt.Errorf("unsupported path %q", config)
	}
	if err := prepend(filepath.Join(home, ".ssh"), "Include \""+config+"\"\n"); err != nil {
		return err
	}
	return st.write(includeRecord, nil)
}

// prepend puts line at the top of the file config in dir, unless it is a
// link or the line is there already.
func prepend(dir, line string) error {
	fi, err := os.Stat(dir)
	if errors.Is(err, os.ErrNotExist) {
		if err = os.Mkdir(dir, 0o700); err == nil {
			fi, err = os.Stat(dir)
		}
	}
	if err != nil {
		return err
	}
	if !fi.IsDir() || fi.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) {
		return errors.New("~/.ssh is not a directory of this user")
	}
	var old []byte
	perm := os.FileMode(0o600)
	if fi, err := os.Lstat(filepath.Join(dir, "config")); err == nil {
		if fi.Mode()&os.ModeSymlink != 0 {
			return nil
		}
		if !fi.Mode().IsRegular() || fi.Sys().(*syscall.Stat_t).Uid != uint32(os.Getuid()) {
			return errors.New("~/.ssh/config is not a file of this user")
		}
		if fi.Size() > 1<<20 {
			return errors.New("~/.ssh/config is over 1 MiB")
		}
		if old, err = os.ReadFile(filepath.Join(dir, "config")); err != nil {
			return err
		}
		perm = fi.Mode().Perm()
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if bytes.Contains(old, []byte(strings.TrimSuffix(line, "\n"))) {
		return nil // added before its record was written
	}
	return replace(dir, "config", append([]byte(line), old...), perm)
}
