package daemon

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// Core runs Core's access launcher, the one boundary through which Code work
// starts: Go checks Cloud authority, and Core checks
// paths and confines the command.
type Core struct {
	Python string   // Core's interpreter
	Args   []string // optional --config and --state-dir for Core
	Home   string   // HOME for Core, so its control paths cover this daemon's state
}

// coreRequest is one channel's launch request, Core access protocol 4.
type coreRequest struct {
	Protocol    int               `json:"protocol"`
	Connection  string            `json:"connection"` // SSH_CONNECTION: the client's and the server's address and port
	User        string            `json:"user"`
	Lifetime    string            `json:"lifetime"`
	Policy      string            `json:"policy"`
	Folders     []string          `json:"folders,omitempty"` // exactly for workspace
	Home        string            `json:"home,omitempty"`    // the source's, whose sandbox home a restricted session then uses
	Kind        string            `json:"kind"`              // shell, exec or sftp
	Command     *string           `json:"command,omitempty"`
	Terminal    *coreTerminal     `json:"terminal,omitempty"`
	Environment map[string]string `json:"environment"`
	GitHub      *coreGitHub       `json:"github,omitempty"` // only to a launcher that lists github
}

// coreGitHub is a session's Git routing: the broker's environment, and
// the om client the session runs as its credential helper and om github.
type coreGitHub struct {
	Environment map[string]string `json:"environment"`
	Client      string            `json:"client"`
}

type coreTerminal struct {
	Term string `json:"term"`
}

// coreDenied is a launch Core refused. Effect is known_no_effect or
// effect_unknown; a lost reply is effect_unknown too.
type coreDenied struct{ Code, Message, Effect string }

func (e *coreDenied) Error() string {
	if e.Effect == "known_no_effect" {
		return e.Message + " (" + e.Code + "; nothing ran)"
	}
	return e.Message + " (" + e.Code + "; it may have run)"
}

const launchTimeout = 30 * time.Second // Core's own launch steps are bounded by about 15 s

func (c Core) command(ctx context.Context, args ...string) *exec.Cmd {
	cmd := exec.CommandContext(ctx, c.Python, append(append([]string{"-I", "-m", "openmachine_access"}, c.Args...), args...)...)
	// Nothing of this daemon's environment, such as the network library's
	// process-wide settings, reaches Core or the command.
	cmd.Env = []string{"HOME=" + c.Home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin"}
	cmd.Dir = "/"
	return cmd
}

// coreCapabilities is what Core's access launcher says it supports.
type coreCapabilities struct {
	Protocol int               `json:"protocol"`
	GitHub   string            `json:"github"`        // include: it takes a session's Git routing to a TLS broker as a file in GitHubRouting
	Client   string            `json:"github_client"` // its own copy of om, which restricted sessions run
	Version  string            `json:"version"`       // Core's version
	Policies map[string]string `json:"policies"`      // each mode: available, or why Core cannot enforce it
}

// errCoreProtocol is a Core that answered in another access protocol.
var errCoreProtocol = errors.New("native: Core speaks another access protocol; update Open Machine")

// capabilities asks Core's access launcher what it supports; an error means it
// runs no session for this daemon. A Core of another access protocol is one:
// its answer comes back with errCoreProtocol.
func (c Core) capabilities() (coreCapabilities, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := c.command(ctx, "capabilities").Output()
	var caps coreCapabilities
	if err == nil {
		err = json.Unmarshal(out, &caps)
	}
	if err == nil && caps.Protocol != 4 {
		err = errCoreProtocol
	}
	if err == nil && caps.Client != "" && !filepath.IsAbs(caps.Client) {
		err = errors.New("native: Core named a relative github_client")
	}
	return caps, err
}

const (
	foldersTimeout = 120 * time.Second // covers a macOS privacy prompt the check may raise
	maxFolders     = 64 << 10
)

// Folders runs Core's Workspace folder check in its
// own Core process, with request on its stdin, for at most 120 seconds, and
// returns the JSON object Core writes: the canonical folders or Core's
// refusal. Anything else, such as more than 64 KiB or a failed launcher, is an
// error.
func (c Core) Folders(request []byte) (json.RawMessage, error) {
	ctx, cancel := context.WithTimeout(context.Background(), foldersTimeout)
	defer cancel()
	cmd := c.command(ctx, "folders")
	cmd.Stdin = bytes.NewReader(request)
	out := &bounded{max: maxFolders}
	cmd.Stdout = out
	// Ending it ends what it started too.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	cmd.WaitDelay = time.Second
	if err := cmd.Run(); err != nil {
		return nil, err
	}
	if b := bytes.TrimSpace(out.buf.Bytes()); len(b) > 0 && b[0] == '{' && json.Valid(b) {
		return b, nil
	}
	return nil, errors.New("native: Core's folder check answered no JSON object")
}

// bounded is a buffer that fails a write past max bytes. It has no ReadFrom,
// so io.Copy writes through Write.
type bounded struct {
	buf bytes.Buffer
	max int
}

func (b *bounded) Write(p []byte) (int, error) {
	if b.buf.Len()+len(p) > b.max {
		return 0, errors.New("native: Core's answer is too large")
	}
	return b.buf.Write(p)
}

// session starts one channel's command with IO on stdio and returns it once
// Core admitted it. The launcher leads its own session, apart from the
// daemon's, as Core expects; signals sent to it are forwarded to the command.
func (c Core) session(r coreRequest, stdio [3]*os.File) (*exec.Cmd, error) {
	pair, err := socketpair()
	if err != nil {
		return nil, err
	}
	parent, child := pair[0], pair[1]
	defer parent.Close()
	cmd := c.command(context.Background(), "session", "--control-fd", "3")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = stdio[0], stdio[1], stdio[2]
	cmd.ExtraFiles = []*os.File{child}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	err = cmd.Start()
	child.Close()
	if err != nil {
		return nil, &coreDenied{"launcher_unavailable", "Core's access launcher could not start", "known_no_effect"}
	}
	b, _ := json.Marshal(r)
	if err := parent.SetDeadline(time.Now().Add(launchTimeout)); err != nil {
		go cmd.Wait() // closing parent ends the launcher before any request reached it
		return nil, err
	}
	var reply struct {
		Status string     `json:"status"`
		Error  coreDenied `json:"error"`
	}
	_, err = parent.Write(append(b, '\n'))
	if err == nil {
		var line []byte
		line, err = bufio.NewReaderSize(parent, 4096).ReadSlice('\n')
		if err == nil {
			err = json.Unmarshal(line, &reply)
		}
	}
	switch {
	case err == nil && reply.Status == "admitted":
		return cmd, nil
	case err == nil && reply.Status == "denied" && reply.Error.Effect == "known_no_effect":
		cmd.Wait()
		return nil, &reply.Error
	}
	// The command may be running: it stays in its lifetime, never replayed or killed.
	go cmd.Wait()
	if err == nil && reply.Status == "denied" {
		return nil, &coreDenied{reply.Error.Code, reply.Error.Message, "effect_unknown"}
	}
	return nil, &coreDenied{"launch_unverified", "the launch was not acknowledged; the command may be running in its lifetime", "effect_unknown"}
}

// socketpair returns a connected close-on-exec pair, made under the fork lock
// so no concurrently started command inherits it. The first end is ours and
// non-blocking, so its deadlines apply; the second is the child's.
func socketpair() ([2]*os.File, error) {
	syscall.ForkLock.RLock()
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err == nil {
		unix.CloseOnExec(fds[0])
		unix.CloseOnExec(fds[1])
		err = unix.SetNonblock(fds[0], true)
	}
	syscall.ForkLock.RUnlock()
	if err != nil {
		if fds[0] != 0 {
			unix.Close(fds[0])
			unix.Close(fds[1])
		}
		return [2]*os.File{}, err
	}
	return [2]*os.File{os.NewFile(uintptr(fds[0]), "control"), os.NewFile(uintptr(fds[1]), "control")}, nil
}

// control runs f on c's descriptor without changing its blocking mode.
func control(c syscall.Conn, f func(fd uintptr) error) error {
	raw, err := c.SyscallConn()
	if err != nil {
		return err
	}
	var ferr error
	if err := raw.Control(func(fd uintptr) { ferr = f(fd) }); err != nil {
		return err
	}
	return ferr
}

// setSize sets a terminal's window size.
func setSize(master *os.File, cols, rows uint32) error {
	return control(master, func(fd uintptr) error {
		return unix.IoctlSetWinsize(int(fd), unix.TIOCSWINSZ, &unix.Winsize{Col: uint16(min(cols, 0xffff)), Row: uint16(min(rows, 0xffff))})
	})
}
