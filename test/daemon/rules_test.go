package daemon

import (
	"bytes"
	"cmp"
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// vectors is test/vectors/rules.json, which Cloud's reader passes too.
type vectors struct {
	Home  string              `json:"home"`
	Rules daemon.Rules        `json:"rules"`
	Sets  map[string][]string `json:"sets"`
	Cases []struct {
		Line  string     `json:"line"`
		Stop  *string    `json:"stop"`
		Set   string     `json:"set"`
		Allow [][]string `json:"allow"`
		Kind  string     `json:"kind"`
	} `json:"cases"`
	Long [][2]string `json:"long"`
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	b, err := os.ReadFile("../vectors/rules.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields() // the vectors' rules are a lease's
	if err := dec.Decode(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

// outcome names a stop as the vectors do.
func outcome(s *daemon.Stop) string {
	switch {
	case s == nil:
		return ""
	case s.Lock:
		return "lock"
	case s.Rule == "":
		return "much"
	}
	return s.Rule
}

func TestRulesPassEveryVector(t *testing.T) {
	v := readVectors(t)
	for _, c := range v.Cases {
		rules := v.Rules
		if c.Allow != nil {
			rules.Commands, rules.Files, rules.Lock = nil, nil, &daemon.Lock{Allow: c.Allow}
		} else if c.Set != "" {
			keep := v.Sets[c.Set]
			rules.Commands = slices.DeleteFunc(slices.Clone(rules.Commands), func(r daemon.CommandRule) bool { return !slices.Contains(keep, r.Title) })
			rules.Files = slices.DeleteFunc(slices.Clone(rules.Files), func(r daemon.FileRule) bool { return !slices.Contains(keep, r.Title) })
		}
		if got, want := outcome(rules.Check(cmp.Or(c.Kind, "exec"), c.Line, v.Home)), *cmp.Or(c.Stop, new(string)); got != want {
			t.Errorf("%q: stopped by %q, want %q", c.Line, got, want)
		}
	}
}

func TestRulesDecideEachLongLineWithinASecond(t *testing.T) {
	v := readVectors(t)
	for _, l := range v.Long {
		line := strings.Repeat(l[1], 65536/len(l[1])+1)
		line = (l[0] + line)[:65536]
		start := time.Now()
		stop := v.Rules.Check("exec", line, v.Home)
		if took := time.Since(start); took > time.Second {
			t.Errorf("%q: %v", l[0]+l[1], took)
		} else {
			t.Logf("%-40q %4d ms %s", l[0]+l[1], took.Milliseconds(), cmp.Or(outcome(stop), "passes"))
		}
	}
}

func TestRulesMessage(t *testing.T) {
	head, foot := "Blocked by Open Machine on prod-us-01", "Do not try another way. An admin of Acme Robotics can change this in Rules."
	restart := daemon.CommandRule{Title: "Production restarts", Note: "Ask Dana before restarting anything in production.",
		Match: []daemon.Pattern{{W: [][]string{{"systemctl"}, {"restart"}}}}}
	push := daemon.CommandRule{Title: "Force push", Match: []daemon.Pattern{{W: [][]string{{"git"}, {"push"}, {"--force"}}}}}
	rules := &daemon.Rules{Head: head, Foot: foot, Commands: []daemon.CommandRule{restart, push}}
	locked := &daemon.Rules{Head: head, Foot: foot, Lock: &daemon.Lock{Title: "Only listed commands run here", Note: "Allowed: ls.", Allow: [][]string{{"ls"}}}}
	for _, c := range []struct {
		rules      *daemon.Rules
		kind, line string
		want       string
	}{
		{rules, "exec", "systemctl restart api", head + ": Production restarts.\r\nAsk Dana before restarting anything in production.\r\n" + foot + "\r\n"},
		{rules, "exec", "git push --force", head + ": Force push.\r\n" + foot + "\r\n"},
		{locked, "exec", "whoami", head + ": Only listed commands run here.\r\nAllowed: ls.\r\n" + foot + "\r\n"},
		{locked, "sftp", "", head + ": Only listed commands run here.\r\nAllowed: ls.\r\n" + foot + "\r\n"},
		{rules, "exec", strings.Repeat("$(", 9) + "reboot" + strings.Repeat(")", 9),
			head + ": This line is too long or too nested to check.\r\nSend it as shorter commands.\r\n"},
	} {
		stop := c.rules.Check(c.kind, c.line, "/home/me")
		if stop == nil || stop.Message != c.want {
			t.Errorf("%s %q: %#v, want the message %q", c.kind, c.line, stop, c.want)
		}
	}
	if stop := rules.Check("shell", "", "/home/me"); stop != nil {
		t.Errorf("a shell without a lock: %#v", stop)
	}
	if stop := (*daemon.Rules)(nil).Check("exec", "reboot", "/home/me"); stop != nil {
		t.Errorf("no rules: %#v", stop)
	}
}

func TestSendsScript(t *testing.T) {
	// A command of the line may run what the connection's input holds.
	for _, line := range []string{"bash", "bash -s", "bash -s -- deploy", "sh -", "bash -euo pipefail", "bash -o errexit", "bash +x",
		"bash -O extglob -s", "bash -euxo pipefail -s", "bash -s -- -c", "bash -s x -c", "cd /srv && bash -s", "true; bash", "sh -c bash", "cat f | bash",
		"sudo sh", "env FOO=1 bash", "su", "su - app", "runuser -l app", "sudo -i", "sudo -u app -s", "sudo --login",
		"doas -s", "systemd-run -S", "run0", "pkexec", "chroot /srv/jail"} {
		if !daemon.SendsScript(line) {
			t.Errorf("%q does not send a script", line)
		}
	}
	for _, line := range []string{"echo bash", "bash deploy.sh", "bash -e deploy.sh", "bash -o errexit deploy.sh", "bash -c 'echo hi'",
		"su -c id app", "su app -c id", "su --session-command=id app", "runuser --session-command id app", "runuser -u app id",
		"sudo -u app id", "sudo -l", "doas id", "chroot /srv/jail /bin/true", "python3 -"} {
		if daemon.SendsScript(line) {
			t.Errorf("%q sends a script", line)
		}
	}
}

func TestRulesReadAHomeAloneAsTheHome(t *testing.T) {
	// A rule's ~ or $HOME alone is the home, not a name.
	for _, path := range []string{"~", "$HOME", "${HOME}", "~/"} {
		rules := &daemon.Rules{Files: []daemon.FileRule{{Title: "Home", Level: "read", Paths: []string{path}}}}
		if stop := rules.Check("exec", "echo x > ~/notes.txt", "/home/me"); stop == nil || stop.Rule != "Home" {
			t.Errorf("%s: a write into the home = %#v", path, stop)
		}
		if stop := rules.Check("exec", "echo x > /tmp/home/me/notes.txt", "/home/me"); stop != nil {
			t.Errorf("%s: a write elsewhere = %#v", path, stop)
		}
	}
}
