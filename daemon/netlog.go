package daemon

import (
	"fmt"
	"log"
	"regexp"
	"strings"
	"sync"
	"time"

	"tailscale.com/types/logger"
)

var (
	authKeyForm  = regexp.MustCompile(`\b(hskey|tskey)-[A-Za-z0-9_-]+`)
	urlQueryForm = regexp.MustCompile(`(https?://[^\s"'?]*)\?[^\s"']*`)
)

// RedactNetwork removes from a network log line what must not reach a log:
// what redact hides, the node's registration key, any other key of that form
// and URL queries; and writes home as ~, so a log can be shared.
func RedactNetwork(line, home, authKey string) string {
	if authKey != "" {
		line = strings.ReplaceAll(line, authKey, "[key]")
	}
	line = redact(line)
	line = authKeyForm.ReplaceAllString(line, "$1-[key]")
	line = urlQueryForm.ReplaceAllString(line, "$1?[query]")
	if home != "" && home != "/" {
		line = strings.ReplaceAll(line, home+"/", "~/")
	}
	return line
}

// netLog is om's log of its network node: each line, redacted and prefixed
// network:. The most verbose lines are dropped, and each kind of line is
// rate-limited over the daemon's life, as Tailscale limits its own log, so a
// node that keeps failing to start logs its start once and cannot push om's
// own lines out of a bounded daemon.log.
type netLog struct {
	home string
	logf logger.Logf
	mu   sync.Mutex
	key  string // the registration key of the node starting, to redact
}

func newNetLog(home string) *netLog {
	n := &netLog{home: home}
	n.logf = logger.RateLimitedFn(n.print, 10*time.Minute, 3, 500)
	return n
}

func (n *netLog) print(format string, args ...any) {
	if strings.Contains(format, "[v2] ") {
		return
	}
	log.Print("network: " + n.redact(strings.TrimSpace(fmt.Sprintf(format, args...))))
}

func (n *netLog) redact(line string) string {
	n.mu.Lock()
	defer n.mu.Unlock()
	return RedactNetwork(line, n.home, n.key)
}
