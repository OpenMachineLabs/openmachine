// Package linux is om, the Linux product: what the Desktop app is to macOS.
// It installs Core and the Connector, keeps the om daemon running, holds the
// connection intent and drives sign-in and updates, only through the owners
// Desktop calls: Core's manage, the Connector's own commands, and the
// daemon's socket.
package linux

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"

	"github.com/OpenMachineLabs/openmachine/daemon"
	"golang.org/x/sys/unix"
)

// What om reads of the machine it runs on; tests replace them.
var (
	// Executable is this om's path.
	Executable = os.Executable
	// User is the OS user om runs as.
	User = user.Current
	// Root prefixes the system's own paths: /run, /etc/systemd/system and /usr/local/bin.
	Root = ""
	// Input is om's standard input, where the daemon gives a move's secret.
	Input io.Reader = os.Stdin
)

// record is headless.json in the daemon's state.
type record struct {
	Format     int                  `json:"format"`
	Release    release              `json:"release"`              // the installed release and the pointer updates follow
	Core       coreEntry            `json:"core"`                 // Core's bound management entry
	Service    string               `json:"service,omitempty"`    // system, user or shell
	Daemon     []string             `json:"daemon,omitempty"`     // the daemon om started last
	AutoUpdate bool                 `json:"auto_update"`          // the daemon asks for updates
	Operations map[string]operation `json:"operations,omitempty"` // each owner's unfinished operation
}

type release struct {
	Version   string `json:"version"`   // empty until the first install completes
	Channel   string `json:"channel"`   // stable or staging
	Downloads string `json:"downloads"` // where releases are published
}

// coreEntry is the entry of Core's manage status: the pinned interpreter runs path.
type coreEntry struct {
	Python       string `json:"python"`
	Path         string `json:"path"`
	Config       string `json:"config"`
	State        string `json:"state"`
	AccessPython string `json:"access_python"` // the selected installation's interpreter, which the daemon runs
}

// operation is an effect on Core or the Connector, recorded before it is sent.
type operation struct {
	ID     string            `json:"id"`
	Action string            `json:"action"`
	Input  map[string]string `json:"input,omitempty"` // what reading it back or continuing it needs
}

// machine is this installation, for one om command.
type machine struct {
	home     string
	account  string // the home of the user's account, which systemd's managers use
	user     string
	uid      int
	state    string // the daemon's state directory
	bundle   string // the Connector bundle this om runs from
	rec      record
	lock     *os.File
	out, err io.Writer
}

// failure is a command's one line on stderr, which says what to do next.
type failure struct{ line string }

func (f *failure) Error() string { return f.line }

func fail(format string, args ...any) error { return &failure{fmt.Sprintf(format, args...)} }

// open finds the installation of the user om runs as; load or take reads it.
func open(stdout, stderr io.Writer) (*machine, error) {
	u, err := User()
	if err != nil {
		return nil, fail("this OS user cannot be read (%v).", err)
	}
	uid, err := strconv.Atoi(u.Uid)
	home, _ := os.UserHomeDir()
	self, serr := Executable()
	if serr == nil {
		self, serr = filepath.EvalSymlinks(self)
	}
	if err != nil || serr != nil || !filepath.IsAbs(home) {
		return nil, fail("this user's HOME or om's own path cannot be read.")
	}
	// Open Machine runs from HOME: in another user's, that user could replace what this one runs.
	if fi, err := os.Stat(home); err != nil || fi.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return nil, fail("HOME (%s) is not %s's own directory. Run this as the user Open Machine is for, with that user's HOME.", home, u.Username)
	}
	return &machine{home: home, account: u.HomeDir, user: u.Username, uid: uid, state: daemon.StateDir(home), bundle: filepath.Dir(self),
		out: stdout, err: stderr}, nil
}

// load reads headless.json; a machine without one updates itself once installed.
func (m *machine) load() error {
	m.rec = record{Format: 1, AutoUpdate: true}
	b, err := os.ReadFile(m.path("headless.json"))
	if err == nil {
		err = json.Unmarshal(b, &m.rec)
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fail("%s is unreadable (%v). Run the install command again.", m.path("headless.json"), err)
	}
	return nil
}

func (m *machine) path(name string) string { return filepath.Join(m.state, name) }

// installed requires a completed install.
func (m *machine) installed() error {
	if m.rec.Release.Version == "" {
		return fail("Open Machine is not installed for %s. Run the install command.", m.user)
	}
	return nil
}

// take takes the installation for this command, since one changes it at a time, and reads it.
func (m *machine) take() error {
	if err := os.MkdirAll(m.state, 0o700); err != nil {
		return fail("%s cannot be created (%v).", m.state, err)
	}
	// As the daemon requires. Root reads any directory, and what this state records is run.
	if fi, err := os.Stat(m.state); err != nil || fi.Mode().Perm()&0o077 != 0 || fi.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return fail("%s must be this user's own directory with mode 700.", m.state)
	}
	f, err := os.OpenFile(m.path("headless.lock"), os.O_RDWR|os.O_CREATE|unix.O_NOFOLLOW, 0o600)
	if err == nil {
		if err = unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
			f.Close()
			return fail("another om command is running. Run this one again when it ends.")
		}
	}
	if err != nil {
		return fail("%s cannot be opened (%v).", m.path("headless.lock"), err)
	}
	m.lock = f
	return m.load()
}

func (m *machine) save() error {
	b, err := json.MarshalIndent(m.rec, "", "  ")
	if err != nil {
		return err
	}
	return replace(m.path("headless.json"), append(b, '\n'), 0o600)
}

// begin records an operation before its effect is sent.
func (m *machine) begin(owner, action string, input map[string]string) (operation, error) {
	id := make([]byte, 16)
	rand.Read(id)
	op := operation{ID: hex.EncodeToString(id), Action: action, Input: input}
	if m.rec.Operations == nil {
		m.rec.Operations = map[string]operation{}
	}
	m.rec.Operations[owner] = op
	return op, m.save()
}

// end forgets an operation whose outcome its owner reported.
func (m *machine) end(owner string) error {
	delete(m.rec.Operations, owner)
	return m.save()
}

// replace replaces path atomically and durably with data and mode perm.
func replace(path string, data []byte, perm os.FileMode) error {
	f, err := os.CreateTemp(filepath.Dir(path), ".pending-")
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
		err = os.Rename(f.Name(), path)
	}
	if err != nil {
		return err
	}
	// Durable before it returns: an operation ID is recorded before its effect is sent.
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
