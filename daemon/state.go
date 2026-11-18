package daemon

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/crypto/ssh"
	"golang.org/x/sys/unix"
)

// state is private Code state: this computer's daemon socket and SSH
// configuration, and under devices/ each device's own.
type state struct {
	Dir  string
	lock *os.File // held while this process owns the state
}

// openState opens dir, creating it owner-only if missing, and takes it for
// this process: another daemon on the same state is refused. The lock file
// holds the owner's process ID, for om to end it.
func openState(dir string) (*state, error) {
	if err := private(dir); err != nil {
		return nil, err
	}
	lock, err := os.OpenFile(filepath.Join(dir, "daemon.lock"), os.O_RDWR|os.O_CREATE|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, err
	}
	// om probes this lock for an instant to see whether a daemon runs: only one that stays is another daemon.
	for attempt := 0; unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB) != nil; attempt++ {
		if attempt == 10 {
			lock.Close()
			return nil, fmt.Errorf("native: another om daemon owns %s", dir)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err = lock.Truncate(0); err == nil {
		_, err = lock.WriteAt([]byte(strconv.Itoa(os.Getpid())), 0)
	}
	if err != nil {
		lock.Close()
		return nil, err
	}
	return &state{Dir: dir, lock: lock}, nil
}

// private creates dir owner-only if missing and requires an owner-only directory of this user.
func private(dir string) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	fi, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	if st := fi.Sys().(*syscall.Stat_t); !fi.IsDir() || fi.Mode().Perm()&0o077 != 0 || st.Uid != uint32(os.Getuid()) {
		return fmt.Errorf("%w: Code state %s must be an owner-only directory of this user", errRecovery, dir)
	}
	return nil
}

// device returns the Code state of one device: its SSH
// host key, network node, enrollment and lifetimes, kept while the Connector
// stores the device. The directories of devices it no longer stores are
// removed first.
func (s *state) device(id string, stored []string) (*state, error) {
	root := s.path("devices")
	if err := private(root); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		if e.Name() != id && !slices.Contains(stored, e.Name()) {
			if err := os.RemoveAll(filepath.Join(root, e.Name())); err != nil {
				return nil, err
			}
		}
	}
	dir := filepath.Join(root, id)
	if err := private(dir); err != nil {
		return nil, err
	}
	return &state{Dir: dir}, nil
}

func (s *state) path(name string) string { return filepath.Join(s.Dir, name) }

// read returns a state file of at most max bytes; a missing one is os.ErrNotExist.
func (s *state) read(name string, max int64) ([]byte, error) { return readPrivate(s.Dir, name, max) }

// write replaces a state file atomically, owner-only.
func (s *state) write(name string, data []byte) error { return replace(s.Dir, name, data, 0o600) }

// replace replaces the file name in dir atomically with data and mode perm.
func replace(dir, name string, data []byte, perm os.FileMode) error {
	f, err := os.CreateTemp(dir, ".pending-") // owner-only until it is complete
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		if err = f.Chmod(perm); err == nil {
			err = f.Sync()
		}
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(f.Name(), filepath.Join(dir, name))
	}
	if err != nil {
		return err
	}
	return syncDir(dir)
}

// syncDir makes dir's entries durable.
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

func (s *state) readJSON(name string, max int64, v any) error {
	b, err := s.read(name, max)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, v)
}

func (s *state) writeJSON(name string, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return s.write(name, append(b, '\n'))
}

// hostKey returns this device's SSH host key, generated once. Its
// private half never leaves this state; once registered, a lost key is never
// replaced, since that is recovery.
func (s *state) hostKey(registered bool) (ssh.Signer, error) {
	const name = "ssh_host_ed25519_key"
	if b, err := s.read(name, maxState); err == nil {
		signer, err := ssh.ParsePrivateKey(b)
		if err != nil {
			return nil, fmt.Errorf("%w: the SSH host key is unreadable: %v", errRecovery, err)
		}
		return signer, nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("%w: the SSH host key is unreadable: %v", errRecovery, err)
	} else if registered {
		return nil, fmt.Errorf("%w: the registered SSH host key is missing", errRecovery)
	}
	_, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	block, err := ssh.MarshalPrivateKey(key, "")
	if err != nil {
		return nil, err
	}
	if err := s.write(name, pem.EncodeToMemory(block)); err != nil {
		return nil, err
	}
	return ssh.NewSignerFromKey(key)
}

// enrollment records a device's network registration and host binding. It
// belongs to one device of one Cloud: other ones need recovery.
type enrollment struct {
	DeviceID   string `json:"device_id"`
	Origin     string `json:"origin"`
	ControlURL string `json:"control_url,omitempty"`
	NodeKey    string `json:"node_key,omitempty"` // set once Cloud bound the node
	Host       bool   `json:"host_registered,omitempty"`
}
