package daemon

import (
	"encoding/binary"
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

// RFC 4254 section 8 terminal modes: control characters, and flags whose
// opcode ranges name their set (input 30-49, local 50-69, output 70-89,
// control 90-127). Opcodes either platform lacks, and the line speeds, which
// mean nothing on a pseudo-terminal, are skipped.
var (
	ttyChars = map[byte]int{1: unix.VINTR, 2: unix.VQUIT, 3: unix.VERASE, 4: unix.VKILL, 5: unix.VEOF,
		6: unix.VEOL, 7: unix.VEOL2, 8: unix.VSTART, 9: unix.VSTOP, 10: unix.VSUSP, 12: unix.VREPRINT,
		13: unix.VWERASE, 14: unix.VLNEXT, 18: unix.VDISCARD}
	ttyFlags = map[byte]tcflag{30: unix.IGNPAR, 31: unix.PARMRK, 32: unix.INPCK, 33: unix.ISTRIP,
		34: unix.INLCR, 35: unix.IGNCR, 36: unix.ICRNL, 38: unix.IXON, 39: unix.IXANY, 40: unix.IXOFF,
		41: unix.IMAXBEL, 42: unix.IUTF8, 50: unix.ISIG, 51: unix.ICANON, 53: unix.ECHO, 54: unix.ECHOE,
		55: unix.ECHOK, 56: unix.ECHONL, 57: unix.NOFLSH, 58: unix.TOSTOP, 59: unix.IEXTEN,
		60: unix.ECHOCTL, 61: unix.ECHOKE, 62: unix.PENDIN, 70: unix.OPOST, 72: unix.ONLCR,
		73: unix.OCRNL, 74: unix.ONOCR, 75: unix.ONLRET, 90: unix.CS7, 91: unix.CS8, 92: unix.PARENB,
		93: unix.PARODD}
)

// openTerminal returns a new terminal pair with a pty-req's encoded modes.
func openTerminal(modes string) (master, slave *os.File, err error) {
	if master, slave, err = openPTY(); err != nil {
		return nil, nil, err
	}
	if err = setModes(slave, modes); err != nil {
		master.Close()
		slave.Close()
		return nil, nil, err
	}
	return master, slave, nil
}

// setModes applies RFC 4254 encoded terminal modes to the terminal f.
func setModes(f *os.File, modes string) error {
	t, err := unix.IoctlGetTermios(int(f.Fd()), getTermios)
	if err != nil {
		return err
	}
	sets := [...]*tcflag{&t.Iflag, &t.Lflag, &t.Oflag, &t.Cflag}
	// Each mode is an opcode and a uint32; 0 ends them, and 160 and above are undefined.
	for m := []byte(modes); len(m) > 0 && m[0] != 0 && m[0] < 160; m = m[5:] {
		if len(m) < 5 {
			return errors.New("truncated terminal modes")
		}
		v := binary.BigEndian.Uint32(m[1:5])
		if c, ok := ttyChars[m[0]]; ok {
			if v == 255 { // the client's disabled character
				v = vdisable
			}
			t.Cc[c] = byte(v)
		} else if bit, ok := ttyFlags[m[0]]; ok {
			set := sets[(m[0]-30)/20]
			*set &^= bit
			if v != 0 {
				*set |= bit
			}
		}
	}
	return unix.IoctlSetTermios(int(f.Fd()), setTermios, t)
}
