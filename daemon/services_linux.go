package daemon

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"net/netip"
	"os"
	"strconv"
	"strings"
)

// listening reads the TCP sockets in LISTEN state from /proc/net and the
// processes holding them from their /proc/<pid>/fd links, and the ephemeral
// range. A socket whose process it cannot read, such as another user's, has
// none.
func listening(context.Context) (ls []Listener, first, last int, err error) {
	b, err := os.ReadFile("/proc/sys/net/ipv4/ip_local_port_range")
	if f := strings.Fields(string(b)); err == nil && len(f) == 2 {
		first, err = strconv.Atoi(f[0])
		if err == nil {
			last, err = strconv.Atoi(f[1])
		}
	} else if err == nil {
		err = errors.New("the ephemeral port range is unreadable")
	}
	if err != nil {
		return nil, 0, 0, err
	}
	sockets := map[string]netip.AddrPort{}
	for _, name := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		b, err := os.ReadFile(name)
		if err != nil && !(name == "/proc/net/tcp6" && errors.Is(err, os.ErrNotExist)) { // no tcp6 without IPv6
			return nil, 0, 0, err
		}
		maps.Copy(sockets, ProcNet(b))
	}
	held := map[string][]int{} // the PIDs holding each socket
	procs, _ := os.ReadDir("/proc")
	for _, p := range procs {
		pid, err := strconv.Atoi(p.Name())
		if err != nil {
			continue
		}
		fds, _ := os.ReadDir(fmt.Sprintf("/proc/%d/fd", pid))
		for _, fd := range fds {
			link, _ := os.Readlink(fmt.Sprintf("/proc/%d/fd/%s", pid, fd.Name()))
			if inode, ok := strings.CutPrefix(link, "socket:["); ok {
				if inode = strings.TrimSuffix(inode, "]"); sockets[inode].IsValid() {
					held[inode] = append(held[inode], pid)
				}
			}
		}
	}
	for inode, addr := range sockets {
		if len(held[inode]) == 0 {
			ls = append(ls, Listener{Addr: addr})
		}
		for _, pid := range held[inode] {
			// Its name only: reading a process's cmdline can wait for as long as the process holds its memory's lock.
			name, _ := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid))
			ls = append(ls, Listener{Addr: addr, PID: pid, Name: strings.TrimSuffix(string(name), "\n")})
		}
	}
	return ls, first, last, nil
}
