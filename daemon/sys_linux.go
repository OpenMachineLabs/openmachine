package daemon

import (
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

var machineID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// readHardwareID reads the file hardware-id in the Connector's state directory
// dir if it exists, else this machine's /etc/machine-id. Only the Connector
// creates that file, for a machine without a readable /etc/machine-id.
func readHardwareID(dir string) (string, error) {
	name := "hardware-id"
	b, err := readPrivate(dir, name, 64)
	if errors.Is(err, os.ErrNotExist) {
		name = "/etc/machine-id"
		b, err = os.ReadFile(name)
	}
	id := strings.TrimSpace(string(b))
	if err == nil && !machineID.MatchString(id) {
		err = errors.New("malformed " + name)
	}
	return id, err
}

// Terminal attributes, for setModes.
const getTermios, setTermios, vdisable = unix.TCGETS, unix.TCSETS, 0

type tcflag = uint32

// openPTY returns a new terminal pair. The master is close-on-exec, so no
// other command can hold this terminal open.
func openPTY() (master, slave *os.File, err error) {
	master, err = os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		return nil, nil, err
	}
	var n uint32
	err = control(master, func(fd uintptr) error {
		if err := unix.IoctlSetPointerInt(int(fd), unix.TIOCSPTLCK, 0); err != nil {
			return err
		}
		n, err = unix.IoctlGetUint32(int(fd), unix.TIOCGPTN)
		return err
	})
	if err == nil {
		slave, err = os.OpenFile(fmt.Sprintf("/dev/pts/%d", n), os.O_RDWR|unix.O_NOCTTY, 0)
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
		cred, err := unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
		if err == nil {
			uid = cred.Uid
		}
		return err
	})
	return uid, err
}
