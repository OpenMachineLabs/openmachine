package daemon

import (
	"log"
	"os"
	"path/filepath"
	"sync"

	"golang.org/x/sys/unix"
)

// MaxDaemonLog bounds daemon.log; one earlier file, daemon.log.1, is kept. Tests shorten it.
var MaxDaemonLog int64 = 1 << 20

// LogDiscarded sends the daemon's log to daemon.log in its state directory dir
// when its standard error is closed or /dev/null, as launchd leaves a Mac's
// agent, so why it is not connected can be read. A supervisor that keeps the
// log, as systemd or om's shell kind does, keeps it alone.
func LogDiscarded(dir string) {
	var stderr, null unix.Stat_t
	if err := unix.Fstat(2, &stderr); err == nil {
		if unix.Stat(os.DevNull, &null) != nil || stderr.Mode&unix.S_IFMT != unix.S_IFCHR || stderr.Rdev != null.Rdev {
			return
		}
	}
	log.SetOutput(&daemonLog{path: filepath.Join(dir, "daemon.log")})
}

// daemonLog appends to an owner-only file, opened once the state directory
// exists, and starts a new one past MaxDaemonLog. A write that fails is
// dropped: the log never stops the daemon.
type daemonLog struct {
	mu   sync.Mutex
	path string
	f    *os.File
	size int64
}

func (l *daemonLog) Write(b []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.f == nil && !l.open() {
		return len(b), nil
	}
	if l.size > 0 && l.size+int64(len(b)) > MaxDaemonLog {
		l.f.Close()
		l.f = nil
		moved := os.Rename(l.path, l.path+".1") == nil
		if !l.open() {
			return len(b), nil
		}
		if !moved && l.f.Truncate(0) == nil { // no earlier file can be kept: start over, within the bound
			l.size = 0
		}
	}
	n, _ := l.f.Write(b)
	l.size += int64(n)
	return len(b), nil
}

func (l *daemonLog) open() bool {
	f, err := os.OpenFile(l.path, os.O_WRONLY|os.O_APPEND|os.O_CREATE|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0o600)
	if err != nil {
		return false
	}
	fi, err := f.Stat()
	if err != nil || !fi.Mode().IsRegular() {
		f.Close()
		return false
	}
	l.f, l.size = f, fi.Size()
	return true
}
