package daemon

import (
	"bytes"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// process reads a process's parent, name and arguments from /proc; an
// unreadable one has no parent.
func process(pid int) (parent int, name string, args []string) {
	stat, _ := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	open, end := bytes.IndexByte(stat, '('), bytes.LastIndexByte(stat, ')') // the name may hold either
	if open < 0 || end < open {
		return 0, "", nil
	}
	if fields := strings.Fields(string(stat[end+1:])); len(fields) > 1 { // its state, then its parent
		parent, _ = strconv.Atoi(fields[1])
	}
	cmdline, _ := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	return parent, string(stat[open+1 : end]), strings.Split(string(cmdline), "\x00")
}
