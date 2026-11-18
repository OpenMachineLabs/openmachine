// Command om is the program every Open Machine computer runs: a daemon
// that serves OM SSH on the private network and dials it for local SSH clients,
// the ProxyCommand those clients run, and, inside an SSH session, the Git
// credential helper and fixed GitHub operations of its broker.
//
//	om daemon -core-python PATH [-core-state DIR] [-core-config FILE]
//	om proxy [-state DIR] HOST
//	om git-credential get|store|erase
//	om github pr create|view|list ... | ci [REF]
//
// The daemon takes its state and the Connector's identity from HOME. On
// Linux every other command is the headless product's.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime"
	"slices"
	"syscall"

	"github.com/OpenMachineLabs/openmachine/daemon"
	"github.com/OpenMachineLabs/openmachine/linux"
)

// hardwareID, set only by test builds (-ldflags -X main.hardwareID=...), replaces this machine's hardware ID.
var hardwareID string

func main() {
	if hardwareID != "" {
		daemon.HardwareID = func(string) (string, error) { return hardwareID, nil }
	}
	home, _ := os.UserHomeDir()
	if runtime.GOOS == "linux" && (len(os.Args) < 2 || !slices.Contains([]string{"daemon", "proxy", "git-credential", "github"}, os.Args[1])) {
		os.Exit(linux.Main(os.Args[1:], os.Stdout, os.Stderr))
	}
	if len(os.Args) < 2 {
		usage()
	}
	flags := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	switch os.Args[1] {
	case "daemon":
		python := flags.String("core-python", "", "Core's Python interpreter")
		coreState := flags.String("core-state", "", "Core's state directory, if not its default")
		coreConfig := flags.String("core-config", "", "Core's configuration, if not its default")
		flags.Parse(os.Args[2:])
		if *python == "" || flags.NArg() != 0 {
			usage()
		}
		core := daemon.Core{Python: *python, Home: home}
		if *coreConfig != "" {
			core.Args = append(core.Args, "--config", *coreConfig)
		}
		if *coreState != "" {
			core.Args = append(core.Args, "--state-dir", *coreState)
		}
		daemon.LogDiscarded(daemon.StateDir(home))
		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		if err := daemon.Run(ctx, core); err != nil {
			log.Fatal(err)
		}
	case "proxy":
		state := flags.String("state", daemon.StateDir(home), "the daemon's state directory")
		flags.Parse(os.Args[2:])
		if flags.NArg() != 1 {
			usage()
		}
		if err := daemon.Proxy(*state, flags.Arg(0), os.Stdin, os.Stdout); err != nil {
			fmt.Fprintf(os.Stderr, "om: %v\n", err)
			os.Exit(255)
		}
	case "git-credential":
		os.Exit(daemon.GitCredential(os.Args[2:], os.Stdin, os.Stdout, os.Stderr))
	case "github":
		os.Exit(daemon.GitHub(os.Args[2:], os.Stdout, os.Stderr))
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: om daemon -core-python PATH [-core-state DIR] [-core-config FILE]\n       om proxy [-state DIR] HOST\n"+
		"       om git-credential get|store|erase\n       om github pr create|view|list ... | ci [REF]")
	os.Exit(2)
}
