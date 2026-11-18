package daemon

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"regexp"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

var platformUUID = regexp.MustCompile(`"IOPlatformUUID" = "([0-9A-F-]{36})"`)

// readHardwareID reads this Mac's IOPlatformUUID.
func readHardwareID(string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output()
	if err != nil {
		return "", err
	}
	if m := platformUUID.FindSubmatch(out); m != nil {
		return string(m[1]), nil
	}
	return "", errors.New("no IOPlatformUUID")
}

// Terminal attributes, for setModes.
const getTermios, setTermios, vdisable = unix.TIOCGETA, unix.TIOCSETA, 0xff

type tcflag = uint64

// openPTY returns a new terminal pair. The master is close-on-exec, so no
// other command can hold this terminal open.
func openPTY() (master, slave *os.File, err error) {
	master, err = os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		return nil, nil, err
	}
	var name [128]byte
	err = control(master, func(fd uintptr) error {
		for _, req := range []uintptr{unix.TIOCPTYGRANT, unix.TIOCPTYUNLK} {
			if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, req, 0); e != 0 {
				return e
			}
		}
		if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, fd, unix.TIOCPTYGNAME, uintptr(unsafe.Pointer(&name[0]))); e != 0 {
			return e
		}
		return nil
	})
	if err == nil {
		slave, err = os.OpenFile(unix.ByteSliceToString(name[:]), os.O_RDWR|unix.O_NOCTTY, 0)
	}
	if err != nil {
		master.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

// peerUID returns the user of a Unix socket's peer.
func peerUID(c syscall.Conn) (uint32, error) {
	var uid uint32
	err := control(c, func(fd uintptr) error {
		cred, err := unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
		if err == nil {
			uid = cred.Uid
		}
		return err
	})
	return uid, err
}
