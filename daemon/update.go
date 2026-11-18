package daemon

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// AutoUpdate asks for updates of a headless installation until ctx ends:
// after first and then once each daily, and at once when refused receives,
// Cloud having refused this om as too old, then at most once each every while
// it does. A request calls start when auto_update in state's headless.json,
// read then, is on and open reports no SSH connection; while one is open it
// waits and looks again each every. A Mac has no headless.json, so it never
// asks.
func AutoUpdate(ctx context.Context, state string, refused <-chan struct{}, open func() bool, start func(),
	first, daily, every time.Duration) {
	next := time.NewTimer(first)
	defer next.Stop()
	again := time.NewTicker(every)
	defer again.Stop()
	var asked time.Time // when a refusal last asked
	pending := false
	for {
		select {
		case <-ctx.Done():
			return
		case <-next.C:
			next.Reset(daily)
			pending = true
		case <-refused:
			if time.Since(asked) < every {
				continue
			}
			asked, pending = time.Now(), true
		case <-again.C:
		}
		if !pending {
			continue
		}
		var headless struct {
			AutoUpdate bool `json:"auto_update"`
		}
		if b, err := readPrivate(state, "headless.json", maxState); err != nil || json.Unmarshal(b, &headless) != nil || !headless.AutoUpdate {
			pending = false
		} else if !open() {
			pending = false
			start()
		}
	}
}

// StartUpdate starts this binary's `om update` in its own session: the update
// restarts this daemon and must outlive it. It reports whether it started.
// Tests replace it.
var StartUpdate = func() bool { return startOM("update", "") }

// StartMove starts `om move` as StartUpdate starts an update: the move makes
// this daemon exit for the device it registers. The move's secret goes to its
// standard input, never to a command line or the environment. Tests replace it.
var StartMove = func(secret string) bool { return startOM("move", secret) }

// startOM starts om with one command and input in its own session and
// reports whether it started.
func startOM(command, input string) bool {
	self, err := os.Executable()
	if err != nil {
		log.Printf("native: %s: %v", command, err)
		return false
	}
	cmd := exec.Command(self, command)
	if input != "" {
		cmd.Stdin = strings.NewReader(input)
	}
	cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		log.Printf("native: %s: %v", command, err)
		return false
	}
	go cmd.Wait()
	return true
}
