package linux

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"slices"
	"strings"
	"syscall"
)

const usage = `usage: om up [--key KEY | --key-file FILE] [--name NAME]
       om down
       om status [--json]
       om logout
       om update
       om set auto-update on|off
       om uninstall
       om version`

// Main runs om with args and returns its exit status:
// 0, or 1 with one line on stderr that says what to do next, or 2 for a
// command it does not have.
func Main(args []string, stdout, stderr io.Writer) int {
	syscall.Umask(0o022) // Core refuses group-writable files, which a umask of 002 would make
	err := run(args, stdout, stderr)
	var f *failure
	switch {
	case err == nil:
		return 0
	case errors.Is(err, errUsage):
		fmt.Fprintln(stderr, usage)
		return 2
	case !errors.As(err, &f):
		fmt.Fprintf(stderr, "om: %v. Run the command again.\n", err)
	case f.line != "":
		fmt.Fprintln(stderr, "om: "+f.line)
	}
	return 1
}

var errUsage = errors.New("usage")

func run(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 || !slices.Contains(strings.Fields("install up down status logout update set uninstall version start move"), args[0]) {
		return errUsage
	}
	flags := flag.NewFlagSet(args[0], flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	key := flags.String("key", "", "")
	keyFile := flags.String("key-file", "", "")
	name := flags.String("name", "", "")
	asJSON := flags.Bool("json", false, "")
	server := flags.String("server", "", "")
	channel := flags.String("channel", "", "")
	downloads := flags.String("downloads", "", "")
	noUp := flags.Bool("no-up", false, "")
	if flags.Parse(args[1:]) != nil {
		return errUsage
	}
	// Each command takes only its own options.
	allowed := map[string]string{"up": "key key-file name", "status": "json", "install": "server channel downloads key-file name no-up"}[args[0]]
	known := true
	flags.Visit(func(f *flag.Flag) { known = known && slices.Contains(strings.Fields(allowed), f.Name) })
	rest := flags.Args()
	if !known || len(rest) != map[string]int{"set": 2}[args[0]] || *key != "" && *keyFile != "" {
		return errUsage
	}
	m, err := open(stdout, stderr)
	if err != nil {
		return err
	}
	defer func() {
		if m.lock != nil {
			m.lock.Close()
		}
	}()
	secret := func() (string, error) {
		if *keyFile == "" {
			return *key, nil
		}
		b, err := os.ReadFile(*keyFile)
		if err != nil {
			return "", fail("the key file cannot be read (%v).", err)
		}
		return strings.TrimSpace(string(b)), nil
	}
	// change runs a command that changes an installed machine, alone, after
	// the Connector action an earlier command left unfinished is read back.
	change := func(command func() error) error {
		if err := m.take(); err != nil {
			return err
		}
		if err := m.installed(); err != nil {
			return err
		}
		if err := m.settle(); err != nil {
			return err
		}
		return command()
	}
	switch args[0] {
	case "install":
		k, err := secret()
		if err != nil {
			return err
		}
		if k == "" {
			k = os.Getenv("OM_KEY") // the install script passes a key here, not on a command line
		}
		os.Unsetenv("OM_KEY") // the daemon and the tools install starts do not inherit it
		return m.install(*server, *channel, *downloads, k, *name, *noUp)
	case "up":
		k, err := secret()
		if err != nil {
			return err
		}
		return change(func() error { return m.up(k, *name) })
	case "down":
		return change(func() error {
			if _, err := m.connect(false); err != nil {
				return refused("turning the connection off", err)
			}
			fmt.Fprintln(stdout, "Disconnected. Jobs keep running.")
			return nil
		})
	case "logout":
		return change(m.logout)
	case "move": // what the daemon runs when this machine is moved in the Console: never typed
		secret, _ := io.ReadAll(io.LimitReader(Input, 128))
		return change(func() error { return m.move(strings.TrimSpace(string(secret))) })
	case "set":
		if rest[0] != "auto-update" || rest[1] != "on" && rest[1] != "off" {
			return errUsage
		}
		return change(func() error {
			m.rec.AutoUpdate = rest[1] == "on"
			return m.save()
		})
	case "update":
		return m.update()
	case "uninstall":
		return m.uninstall()
	case "start": // the shell line: quiet, and never beside another om command
		if m.take() == nil && m.rec.Release.Version != "" && m.rec.Service == "shell" && m.daemonPID() == 0 {
			m.start()
		}
		return nil
	case "status", "version":
		if err := m.load(); err != nil {
			return err
		}
		if err := m.installed(); err != nil {
			return err
		}
		if args[0] == "status" {
			return m.status(*asJSON)
		}
		fmt.Fprintf(stdout, "Open Machine %s\nCore %s\n", m.rec.Release.Version, m.coreVersion())
	}
	return nil
}
