package daemon

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"time"

	"golang.org/x/sys/unix"
)

// listening lists the TCP sockets in LISTEN state with lsof, which sees this
// user's processes, each with its executable, and the ephemeral range.
func listening(ctx context.Context) ([]Listener, int, int, error) {
	first, err := unix.SysctlUint32("net.inet.ip.portrange.first")
	if err != nil {
		return nil, 0, 0, err
	}
	last, err := unix.SysctlUint32("net.inet.ip.portrange.last")
	if err != nil {
		return nil, 0, 0, err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "/usr/sbin/lsof", "-nP", "+c", "0", "-iTCP", "-sTCP:LISTEN", "-F", "pcn").Output()
	if exit := (*exec.ExitError)(nil); ctx.Err() != nil || err != nil && !errors.As(err, &exit) { // lsof exits 1 when it lists nothing
		return nil, 0, 0, errors.Join(ctx.Err(), err)
	}
	ls := Lsof(out)
	for i := range ls {
		ls[i].Exe = executable(ls[i].PID)
	}
	return ls, int(first), int(last), nil
}

// executable reads a process's executable from kern.procargs2: the argument
// count, then its path.
func executable(pid int) string {
	b, err := unix.SysctlRaw("kern.procargs2", pid)
	if err != nil || len(b) <= 4 {
		return ""
	}
	exe, _, _ := bytes.Cut(b[4:], []byte{0})
	return string(exe)
}
