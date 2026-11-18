package linux

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

// ownerError is a refusal by Core or the Connector, with the owner's own code.
type ownerError struct{ code string }

func (e *ownerError) Error() string { return e.code }

// code is the owner's code behind err, else what failed.
func code(err error) string {
	if oe := (*ownerError)(nil); errors.As(err, &oe) {
		return oe.code
	}
	return err.Error()
}

// call runs one owner command with stdin, nothing of om's environment but
// HOME, and a deadline, and returns its two outputs.
func (m *machine) call(limit time.Duration, stdin []byte, argv ...string) (stdout, stderr []byte, err error) {
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Env = []string{"HOME=" + m.home, "PATH=/usr/bin:/bin:/usr/sbin:/sbin"}
	cmd.Dir = "/"
	cmd.Stdin = bytes.NewReader(stdin)
	var out, errOut bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errOut
	err = cmd.Run()
	return out.Bytes(), errOut.Bytes(), err
}

// connector runs one command of the Connector bundle at dir, with request as
// its JSON input, and returns its reply or its refusal.
func (m *machine) connector(dir string, request any, args ...string) (map[string]any, error) {
	var stdin []byte
	if request != nil {
		stdin, _ = json.Marshal(request)
	}
	argv := append([]string{filepath.Join(dir, "python/bin/python3"), "-I", "-B", filepath.Join(dir, "entry.py")}, args...)
	out, errOut, err := m.call(90*time.Second, stdin, argv...)
	var reply map[string]any
	if json.Unmarshal(out, &reply) == nil && reply != nil {
		return reply, nil // remove also prints its result when it is incomplete
	}
	var refusal struct{ Error struct{ Code string } }
	if json.Unmarshal(errOut, &refusal) == nil && refusal.Error.Code != "" {
		return nil, &ownerError{refusal.Error.Code}
	}
	return nil, fmt.Errorf("the Connector gave no answer (%v)", err)
}

// companion performs one Connector account action under a recorded operation
// ID and returns its result. Only the ID is recorded, never input, which may
// hold a key.
func (m *machine) companion(action string, input map[string]any) (map[string]any, error) {
	op, err := m.begin("companion", action, nil)
	if err != nil {
		return nil, err
	}
	input["operation_id"] = op.ID
	reply, err := m.connector(m.bundle, input, "companion", action)
	if err != nil {
		return nil, err // read back by its ID before the next action
	}
	return m.settled(reply)
}

// settled ends the recorded Connector operation that reply reports as
// finished and returns its result, or the code it was refused with.
func (m *machine) settled(reply map[string]any) (map[string]any, error) {
	refused, _ := reply["error"].(string)
	switch reply["status"] {
	case "verified":
		result, _ := reply["result"].(map[string]any)
		return result, m.end("companion")
	case "rejected":
		if refused == "" {
			refused = "not_admitted"
		}
		if err := m.end("companion"); err != nil {
			return nil, err
		}
	}
	return nil, &ownerError{refused} // unknown: it stays recorded
}

// settle reads back the Connector account action a previous om command left
// unfinished, by its recorded ID, before this one sends another: an effect
// whose outcome is unknown is never repeated.
func (m *machine) settle() error {
	op, ok := m.rec.Operations["companion"]
	if !ok {
		return nil
	}
	reply, err := m.connector(m.bundle, map[string]any{"operation_id": op.ID}, "companion", "operation")
	if err == nil {
		_, err = m.settled(reply)
	}
	if _, unfinished := m.rec.Operations["companion"]; unfinished {
		return fail("an earlier %s is unfinished (%s). Check the network and run the command again.", op.Action, code(err))
	}
	return nil
}

// connection is the Connector's connection, read from its own state.
func (m *machine) connection() (map[string]any, error) {
	return m.connector(m.bundle, map[string]any{}, "companion", "connection")
}

// snapshot is the Connector's account, device and connection, read from Cloud;
// it is also what records an approved device on this machine.
func (m *machine) snapshot() (map[string]any, error) {
	return m.connector(m.bundle, map[string]any{}, "companion", "snapshot")
}

// field reads a nested field of an owner's reply.
func field[T any](value any, path ...string) (T, bool) {
	for _, key := range path {
		object, _ := value.(map[string]any)
		value = object[key]
	}
	found, ok := value.(T)
	return found, ok
}

func text(value any, path ...string) string {
	s, _ := field[string](value, path...)
	return s
}

func (m *machine) coreConfig() string {
	return filepath.Join(m.home, ".config/openmachine/config.toml")
}
func (m *machine) coreState() string { return filepath.Join(m.home, ".local/state/openmachine") }

// manage runs one Core management action and returns Core's reply, whatever
// its status: through Core's bound entry, or, with payload, by that release
// payload's own interpreter, as a first install must.
func (m *machine) manage(payload string, args ...string) (map[string]any, error) {
	argv := []string{m.rec.Core.Python, "-I", m.rec.Core.Path, "--contract", "1"}
	if payload != "" {
		argv = []string{filepath.Join(payload, "python/bin/python3"), "-I", "-B", "-m", "openmachine_access.cli",
			"--config", m.coreConfig(), "--state-dir", m.coreState(), "manage", "--contract", "1"}
	}
	out, _, err := m.call(60*time.Second, nil, append(argv, args...)...)
	var reply map[string]any
	if json.Unmarshal(out, &reply) != nil || reply == nil {
		return nil, fmt.Errorf("Core gave no answer (%v)", err)
	}
	return reply, nil
}

// errRecovery is a Core operation that stopped where Core can neither finish
// nor end it: it blocks every later one.
var errRecovery = errors.New("recovery_required")

// coreOperation sends a recorded Core install or update, which Core admits
// once per ID, and follows it to its end.
func (m *machine) coreOperation(payload string, op operation, resumed bool) error {
	args := []string{op.Action, "--operation-id", op.ID, "--payload", op.Input["payload"], "--sha256", op.Input["sha256"]}
	if op.Action == "update" {
		args = append(args, "--expected-selection-sha256", op.Input["selection"])
	}
	reply, err := m.manage(payload, args...)
	if err != nil {
		return err
	}
	if _, admitted := reply["operation"]; !admitted && reply["status"] != "rejected" {
		return fail("Core did not take this release (%s). Run the command again.", text(reply, "reason"))
	}
	return m.follow(payload, op, reply, resumed)
}

// follow reads a Core operation, whose last reply was reply, until it ends,
// continuing it where Core asks. A resumed one that Core ends as rejected is
// over; any other outcome but completed is an error.
func (m *machine) follow(payload string, op operation, reply map[string]any, resumed bool) error {
	for deadline := time.Now().Add(10 * time.Minute); ; time.Sleep(time.Second) {
		reason := text(reply, "operation", "reason")
		if reason == "" {
			reason = text(reply, "reason")
		}
		switch reply["status"] {
		case "completed":
			return m.end("core")
		case "rejected":
			if err := m.end("core"); err != nil || resumed {
				return err
			}
			return fail("Core refused this release (%s). Run the command again.", reason)
		case "recovery_required":
			return fmt.Errorf("%w: %s", errRecovery, reason)
		case "continuation_required":
			if _, err := m.manage(payload, "continue", "--operation-id", op.ID); err != nil {
				return err
			}
		case "accepted", "busy":
		default:
			return fail("Core could not be changed (%s). Run the command again.", reason)
		}
		if time.Now().After(deadline) {
			return fail("Core is still being installed. Run the command again in a few minutes.")
		}
		var err error
		if reply, err = m.manage(payload, "operation", "--operation-id", op.ID); err != nil {
			return err
		}
	}
}

// daemonPID returns the process that holds the daemon's lock, 0 when none
// does, or -1 when its holder wrote no process ID.
func (m *machine) daemonPID() int {
	f, err := os.Open(m.path("daemon.lock"))
	if err != nil {
		return 0
	}
	defer f.Close()
	if unix.Flock(int(f.Fd()), unix.LOCK_SH|unix.LOCK_NB) == nil {
		return 0 // released when f closes
	}
	// A daemon that has just taken the lock writes its process ID next.
	for attempt := 0; attempt < 20; attempt++ {
		b := make([]byte, 16)
		n, _ := f.ReadAt(b, 0)
		if pid, err := strconv.Atoi(strings.TrimSpace(string(b[:n]))); err == nil && pid > 1 {
			return pid
		}
		time.Sleep(50 * time.Millisecond)
	}
	return -1
}

// ask sends the daemon one request on its socket and returns its one-line reply.
func (m *machine) ask(request string) ([]byte, error) {
	c, err := net.DialTimeout("unix", m.path("daemon.sock"), 5*time.Second)
	if err != nil {
		return nil, err
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := fmt.Fprintln(c, request); err != nil {
		return nil, err
	}
	return bufio.NewReader(c).ReadBytes('\n')
}
