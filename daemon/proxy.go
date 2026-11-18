package daemon

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Proxy is the ProxyCommand: it asks the local daemon, over its owner-only
// socket, for an SSH stream to host and copies it between stdin and stdout.
// It starts no network node of its own; stdout carries SSH bytes only. Before
// ssh's bytes it tells the target which app ran ssh,
// in a line an older target's SSH server skips.
func Proxy(state, host string, stdin io.Reader, stdout io.Writer) error {
	c, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: filepath.Join(state, socketName), Net: "unix"})
	if err != nil {
		return errors.New("the Open Machine Code service is not running on this computer")
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(40 * time.Second))
	if _, err := fmt.Fprintf(c, "dial %s\n", host); err != nil {
		return err
	}
	r := bufio.NewReader(c)
	line, err := r.ReadString('\n')
	if reason, failed := strings.CutPrefix(line, "error "); failed {
		return errors.New(strings.TrimSpace(reason))
	}
	if err != nil || line != "ok\n" {
		return errors.New("the Open Machine Code service did not answer")
	}
	if _, err := fmt.Fprintf(c, "OM-APP %s\r\n", App(os.Getppid(), process)); err != nil {
		return err
	}
	c.SetDeadline(time.Time{})
	go func() {
		io.Copy(c, stdin)
		c.CloseWrite()
	}()
	_, err = io.Copy(stdout, r)
	return err
}
