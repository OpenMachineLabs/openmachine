// Command node runs one connector node for the network proof. It answers
// each stdin command with one "= " line and reports connection closures as
// "! " lines. The registration key is read from OM_TEST_AUTH_KEY. "deny KEY"
// stops allowing the peer with node key KEY; "deny" alone allows every peer.
// "drop ADDR" dials ADDR, leaves its echo unread and closes the transport
// underneath the node, which resets it, then revalidates and answers with the
// connection's remote address.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/OpenMachineLabs/openmachine/connector"
)

var (
	outMu  sync.Mutex
	denied atomic.Value // node key the node does not allow
)

func say(format string, a ...any) {
	outMu.Lock()
	defer outMu.Unlock()
	fmt.Printf(format+"\n", a...)
}

func main() {
	dir := flag.String("dir", "", "state directory")
	control := flag.String("control", "", "control URL")
	hostname := flag.String("hostname", "", "host name")
	listen := flag.String("listen", "", "comma-separated echo ports")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	n, err := connector.Start(ctx, connector.Config{
		Dir: *dir, ControlURL: *control, Hostname: *hostname,
		AuthKey: os.Getenv("OM_TEST_AUTH_KEY"), Logf: log.Printf,
		Allow: func(p connector.Peer) bool { return p.NodeKey != denied.Load() },
	})
	cancel()
	if err != nil {
		log.Fatal(err)
	}
	for _, p := range strings.Split(*listen, ",") {
		if p == "" {
			continue
		}
		port, err := strconv.ParseUint(p, 10, 16)
		if err != nil {
			log.Fatal(err)
		}
		ln, err := n.Listen(uint16(port))
		if err != nil {
			log.Fatal(err)
		}
		go func() {
			for {
				c, err := ln.Accept()
				if err != nil {
					return
				}
				go func() {
					io.Copy(c, c)
					say("! accepted-closed %s", c.(*connector.Conn).Peer().Name)
				}()
			}
		}()
	}
	say("= ready")

	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		cmd, arg, _ := strings.Cut(in.Text(), " ")
		switch cmd {
		case "status":
			st, err := n.Status(context.Background())
			if err != nil {
				say("= err %v", err)
				continue
			}
			b, _ := json.Marshal(st)
			say("= %s", b)
		case "echo":
			say("= %s", echo(n, arg))
		case "hold":
			c, err := dial(n, arg)
			if err != nil {
				say("= err %v", err)
				continue
			}
			go func() {
				io.Copy(io.Discard, c)
				say("! held-closed")
			}()
			say("= ok")
		case "drop":
			c, err := dial(n, arg)
			if err != nil {
				say("= err %v", err)
				continue
			}
			io.WriteString(c, "ping\n")
			time.Sleep(500 * time.Millisecond) // until the echo waits unread
			c.Conn.Close()
			if c.Conn.RemoteAddr() != nil {
				say("= err the close did not reset the connection")
			} else {
				n.Revalidate()
				say("= %v", c.RemoteAddr())
			}
			c.Close()
		case "deny":
			denied.Store(arg)
			n.Revalidate()
			say("= ok")
		case "quit":
			n.Close()
			return
		}
	}
}

func dial(n *connector.Node, addr string) (*connector.Conn, error) {
	ap, err := netip.ParseAddrPort(addr)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return n.Dial(ctx, ap)
}

func echo(n *connector.Node, addr string) string {
	c, err := dial(n, addr)
	if err != nil {
		return "err " + err.Error()
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.WriteString(c, "ping\n"); err != nil {
		return "err " + err.Error()
	}
	line, err := bufio.NewReader(c).ReadString('\n')
	if err != nil || line != "ping\n" {
		return fmt.Sprintf("err echo %q: %v", line, err)
	}
	return "ok"
}
