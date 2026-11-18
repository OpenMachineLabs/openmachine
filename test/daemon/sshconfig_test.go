package daemon

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// TestInclude adds the daemon's SSH configuration to ~/.ssh/config once, in a
// temporary HOME, and checks the result with the system OpenSSH client.
func TestInclude(t *testing.T) {
	setup := func(t *testing.T) (home, state, line string) {
		home, _ = filepath.EvalSymlinks(t.TempDir())
		state = daemon.StateDir(home)
		os.MkdirAll(state, 0o700)
		os.WriteFile(filepath.Join(state, "ssh_config"), []byte("Match host dev-1.om\n  User omuser\n  Port 2222\n"), 0o600)
		return home, state, "Include \"" + filepath.Join(state, "ssh_config") + "\"\n"
	}
	include := func(t *testing.T, home, state string) {
		t.Helper()
		if err := daemon.Include(home, state); err != nil {
			t.Fatalf("Include = %v", err)
		}
	}

	t.Run("the line goes on top once, keeping every other byte and the mode", func(t *testing.T) {
		home, state, line := setup(t)
		config := filepath.Join(home, ".ssh/config")
		os.MkdirAll(filepath.Dir(config), 0o700)
		mine := "# mine\nHost *\n  User me\n  Port 2200\nHost other\n  HostName 192.0.2.1" // no final newline
		os.WriteFile(config, []byte(mine), 0o640)
		os.Chmod(config, 0o640)
		include(t, home, state)
		got, _ := os.ReadFile(config)
		if string(got) != line+mine {
			t.Fatalf("config = %q", got)
		}
		if fi, _ := os.Stat(config); fi.Mode().Perm() != 0o640 {
			t.Fatalf("mode = %v, want 0640", fi.Mode().Perm())
		}
		if ssh, err := exec.LookPath("ssh"); err == nil {
			// .om names get OM's values even over the user's Host *; other hosts keep theirs.
			for host, want := range map[string]string{"dev-1.om": "user omuser\nport 2222\n", "other": "user me\nport 2200\n"} {
				out, err := exec.Command(ssh, "-G", "-F", config, host).Output()
				for _, w := range strings.SplitAfter(want, "\n")[:2] {
					if err != nil || !bytes.Contains(out, []byte(w)) {
						t.Errorf("ssh -G %s: %v, missing %q", host, err, w)
					}
				}
			}
		} else {
			t.Log("NOT RUN: no ssh client to read the result")
		}
		include(t, home, state)
		if again, _ := os.ReadFile(config); !bytes.Equal(again, got) {
			t.Fatalf("a second call changed the config to %q", again)
		}
		// A user who removes the line keeps it removed.
		os.WriteFile(config, []byte(mine), 0o640)
		include(t, home, state)
		if after, _ := os.ReadFile(config); string(after) != mine {
			t.Fatalf("the removed line came back: %q", after)
		}
	})

	t.Run("a missing ~/.ssh and config are created owner-only", func(t *testing.T) {
		home, state, line := setup(t)
		include(t, home, state)
		dir, _ := os.Stat(filepath.Join(home, ".ssh"))
		file, _ := os.Stat(filepath.Join(home, ".ssh/config"))
		got, _ := os.ReadFile(filepath.Join(home, ".ssh/config"))
		if dir == nil || dir.Mode().Perm() != 0o700 || file == nil || file.Mode().Perm() != 0o600 || string(got) != line {
			t.Fatalf("~/.ssh %v, config %v %q", dir, file, got)
		}
	})

	t.Run("a config added before its record is not added twice", func(t *testing.T) {
		home, state, line := setup(t)
		config := filepath.Join(home, ".ssh/config")
		os.MkdirAll(filepath.Dir(config), 0o700)
		os.WriteFile(config, []byte(line+"Host x\n"), 0o600)
		include(t, home, state)
		if got, _ := os.ReadFile(config); string(got) != line+"Host x\n" {
			t.Fatalf("config = %q", got)
		}
	})

	t.Run("a linked config, as dotfile managers make, is never touched; a linked ~/.ssh is followed", func(t *testing.T) {
		home, state, line := setup(t)
		dotfiles := filepath.Join(home, "dotfiles")
		os.MkdirAll(dotfiles, 0o700)
		os.WriteFile(filepath.Join(dotfiles, "config"), []byte("Host *\n"), 0o600)
		os.MkdirAll(filepath.Join(home, ".ssh"), 0o700)
		config := filepath.Join(home, ".ssh/config")
		os.Symlink(filepath.Join(dotfiles, "config"), config)
		include(t, home, state)
		if got, _ := os.ReadFile(filepath.Join(dotfiles, "config")); string(got) != "Host *\n" {
			t.Errorf("a linked config was changed to %q", got)
		}
		if fi, err := os.Lstat(config); err != nil || fi.Mode()&os.ModeSymlink == 0 {
			t.Error("a linked config was replaced")
		}
		// Done once: a regular file there later is left alone too.
		os.Remove(config)
		os.WriteFile(config, []byte("Host *\n"), 0o600)
		include(t, home, state)
		if got, _ := os.ReadFile(config); string(got) != "Host *\n" {
			t.Errorf("after a linked config, a later config was changed to %q", got)
		}

		// A linked ~/.ssh, as GNU Stow folds it, holds a regular config: it gains the line.
		home, state, line = setup(t)
		dotfiles = filepath.Join(home, "dotfiles")
		os.MkdirAll(dotfiles, 0o700)
		os.WriteFile(filepath.Join(dotfiles, "config"), []byte("Host *\n"), 0o600)
		os.Symlink(dotfiles, filepath.Join(home, ".ssh"))
		include(t, home, state)
		if got, _ := os.ReadFile(filepath.Join(dotfiles, "config")); string(got) != line+"Host *\n" {
			t.Errorf("the config in a linked ~/.ssh = %q", got)
		}
		if fi, err := os.Lstat(filepath.Join(home, ".ssh")); err != nil || fi.Mode()&os.ModeSymlink == 0 {
			t.Error("a linked ~/.ssh was replaced")
		}
	})
}

// TestHosts leases a daemon its sources and hosts from a scripted Cloud. It dials and
// configures SSH for its hosts alone, never for a source, and leaves ~/.ssh/config alone until it has a host,
// as a team's machine never has. A host that is no source is refused in TestSSH, which has the network.
func TestHosts(t *testing.T) {
	c, srv := codeCloud(t)
	home := runDaemon(t, srv.URL, "dev_hosts", "exit 1")
	state := daemon.StateDir(home)
	dial := func(host string) string { // the daemon's answer to a ProxyCommand
		t.Helper()
		conn, err := net.Dial("unix", filepath.Join(state, "daemon.sock"))
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(20 * time.Second))
		io.WriteString(conn, "dial "+host+"\n")
		line, _ := bufio.NewReader(conn).ReadString('\n')
		return line
	}
	file := func(name string) string { b, _ := os.ReadFile(filepath.Join(state, name)); return string(b) }
	const key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPQcTuV71zUx6zmgFiMsR1uILCcQqf7WRM1j5saY5tCb"
	p := c.presence()
	// A Mac may connect in, and this device, as a team's machine, connects to nothing.
	lease := granted()
	lease.Sources = []daemon.Source{{DeviceID: "dev_mac", NodeKey: "nodekey:mac", GrantRevision: 1}}
	p.lease(p.renewal(), "r1", 60000, lease)
	p.applied(t, "r1")
	waitFor(t, 10*time.Second, "the SSH configuration", func() bool { return file("ssh_config") != "" })
	time.Sleep(200 * time.Millisecond) // an Include would follow the configuration at once
	if _, err := os.Lstat(filepath.Join(home, ".ssh")); !errors.Is(err, os.ErrNotExist) || strings.Contains(file("ssh_config"), "User ") {
		t.Fatalf("without a host, ~/.ssh: %v; ssh_config %q", err, file("ssh_config"))
	}
	// The devices it may connect to are its only hosts: here two Linux machines run by root, its account's
	// and a team's of the same name, which its members type with the team's.
	lease.Hosts = []daemon.Host{{DeviceID: "dev_gpu", NodeKey: "nodekey:gpu", Key: key, User: "root", Aliases: []string{"dev-gpu", "gpu-01"}},
		{DeviceID: "dev_team", NodeKey: "nodekey:team", Key: key, User: "root", Aliases: []string{"dev-team", "gpu-01.acme"}}}
	p.changed()
	p.lease(p.renewal(), "r2", 60000, lease)
	p.applied(t, "r2")
	include := "Include \"" + filepath.Join(state, "ssh_config") + "\"\n"
	waitFor(t, 10*time.Second, "the Include", func() bool { b, _ := os.ReadFile(filepath.Join(home, ".ssh/config")); return string(b) == include })
	if config := file("ssh_config"); !strings.Contains(config, "Match host gpu-01.om,gpu-01.om.\n  User root\n  HostKeyAlias dev-gpu.om\n") ||
		strings.Contains(config, "dev-mac") || file("known_hosts") != "dev-gpu.om "+key+"\ndev-team.om "+key+"\n" {
		t.Fatalf("ssh_config %q, known_hosts %q", config, file("known_hosts"))
	}
	// OpenSSH reads the two-label name as the team's machine, under its canonical host key, and any other
	// name of two labels as the block the daemon refuses.
	if ssh, err := exec.LookPath("ssh"); err == nil {
		proxy := " proxy -state '" + state + "' "
		for host, want := range map[string][]string{"gpu-01.acme.om": {"user root\n", "hostkeyalias dev-team.om\n", proxy + "gpu-01.acme.om\n"},
			"gpu-01.other.om": {proxy + ".om\n"}} {
			out, err := exec.Command(ssh, "-G", "-F", filepath.Join(state, "ssh_config"), host).Output()
			for _, w := range want {
				if err != nil || !bytes.Contains(out, []byte(w)) {
					t.Errorf("ssh -G %s: %v, missing %q", host, err, w)
				}
			}
		}
	} else {
		t.Log("NOT RUN: no ssh client to read the result")
	}
	// A host is dialed by any alias its lease gives it, which here ends at the network this daemon never
	// joined; the source is no host, and neither is the machine under another team's name.
	const joining = "error the Code service is still joining the private network\n"
	for host, want := range map[string]string{"gpu-01.om": joining, "gpu-01.acme.om": joining, "dev-team.om": joining,
		"dev-mac.om":      "error dev-mac.om is not a machine this computer can use\n",
		"gpu-01.other.om": "error gpu-01.other.om is not a machine this computer can use\n"} {
		if got := dial(host); got != want {
			t.Errorf("dial %s = %q, want %q", host, got, want)
		}
	}
}
