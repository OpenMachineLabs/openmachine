package daemon

import (
	"bytes"
	"encoding/binary"
	"strings"

	"golang.org/x/sys/unix"
)

// process reads a process's parent, name and arguments from sysctl; an
// unreadable one has no parent.
func process(pid int) (parent int, name string, args []string) {
	k, err := unix.SysctlKinfoProc("kern.proc.pid", pid)
	if err != nil {
		return 0, "", nil
	}
	// kern.procargs2 is the argument count, the executable's path, padding, then the arguments.
	if b, err := unix.SysctlRaw("kern.procargs2", pid); err == nil && len(b) > 4 {
		_, rest, _ := bytes.Cut(b[4:], []byte{0})
		args = strings.Split(string(bytes.TrimLeft(rest, "\x00")), "\x00")
		args = args[:min(int(binary.LittleEndian.Uint32(b)), len(args))]
	}
	return int(k.Eproc.Ppid), unix.ByteSliceToString(k.Proc.P_comm[:]), args
}
