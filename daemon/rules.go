package daemon

import (
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Rules are what the target's account blocks on it.
// om checks every new session against them before Core runs it.
type Rules struct {
	Head     string        `json:"head"` // Blocked by Open Machine on <machine>
	Foot     string        `json:"foot"` // who can change the rules
	Commands []CommandRule `json:"commands"`
	Files    []FileRule    `json:"files"`
	Writes   []Pattern     `json:"writes"` // the commands that change the files they name
	Uses     []Pattern     `json:"uses"`   // those that may name a No access file
	Copies   []string      `json:"copies"` // those that change only the last file they name
	Lock     *Lock         `json:"lock"`   // nil unless only listed commands run
}

// CommandRule stops a line one of whose commands its patterns hold, or that
// touches its paths at the level read.
type CommandRule struct {
	Title string    `json:"title"`
	Note  string    `json:"note"`
	Match []Pattern `json:"match"`
	Paths []string  `json:"paths"`
}

// FileRule stops a line one of whose commands touches its paths at its
// level, none or read.
type FileRule struct {
	Title string   `json:"title"`
	Note  string   `json:"note"`
	Level string   `json:"level"`
	Paths []string `json:"paths"`
}

// Pattern holds a command whose name matches its first token and whose words
// match each other one.
type Pattern struct {
	W    [][]string `json:"w"`    // tokens, each a list of alternatives
	Into []string   `json:"into"` // when not nil, what the command's output goes into matches one
	Not  bool       `json:"not"`  // an exception
}

// Lock runs only the commands that begin with an allowed one.
type Lock struct {
	Title string     `json:"title"`
	Note  string     `json:"note"`
	Allow [][]string `json:"allow"`
}

// Stop is what stops a session: a rule, the lock, or a line too long or too
// nested to check.
type Stop struct {
	Rule    string // the title of the rule or the lock; "" for a line not checked
	Lock    bool
	Message string // the lines its stderr gets
}

func (s *Stop) Error() string { return s.Message }

// readable reports whether every level of r is none or read, every pattern
// has a token, every token and into an alternative, and every allowed command
// a word: om reads other rules as an unreadable policy.
func (r *Rules) readable() bool {
	if r == nil {
		return true
	}
	patterns := slices.Concat(r.Writes, r.Uses)
	for _, rule := range r.Commands {
		patterns = append(patterns, rule.Match...)
	}
	empty := func(words []string) bool { return len(words) == 0 }
	return !slices.ContainsFunc(patterns, func(p Pattern) bool {
		return len(p.W) == 0 || slices.ContainsFunc(p.W, empty) || p.Into != nil && empty(p.Into)
	}) &&
		!slices.ContainsFunc(r.Files, func(f FileRule) bool { return f.Level != "none" && f.Level != "read" }) &&
		(r.Lock == nil || !slices.ContainsFunc(r.Lock.Allow, empty))
}

// Check returns what stops a session of kind with line, its exec command, on
// the machine whose user's home is home, or nil. A shell, sftp or forward is
// stopped only by the lock; nil rules stop nothing.
func (r *Rules) Check(kind, line, home string) *Stop {
	if r == nil {
		return nil
	}
	if kind != "exec" {
		if r.Lock != nil {
			return r.stop(r.Lock.Title, r.Lock.Note, true)
		}
		return nil
	}
	commands, ok := parse(line)
	if !ok {
		return &Stop{Message: r.Head + ": This line is too long or too nested to check.\r\nSend it as shorter commands.\r\n"}
	}
	for _, rule := range r.Commands {
		for _, c := range commands {
			if held(rule.Match, c) || (len(rule.Paths) > 0 && r.touches("read", rule.Paths, c, home)) {
				return r.stop(rule.Title, rule.Note, false)
			}
		}
	}
	for _, rule := range r.Files {
		for _, c := range commands {
			if r.touches(rule.Level, rule.Paths, c, home) {
				return r.stop(rule.Title, rule.Note, false)
			}
		}
	}
	if r.Lock != nil {
		for _, c := range commands {
			if !c.part && len(c.words) > 0 && !slices.ContainsFunc(r.Lock.Allow, func(allowed []string) bool { return begins(allowed, c) }) {
				return r.stop(r.Lock.Title, r.Lock.Note, true)
			}
		}
	}
	return nil
}

func (r *Rules) stop(title, note string, lock bool) *Stop {
	message := r.Head + ": " + title + ".\r\n"
	if note != "" {
		message += note + "\r\n"
	}
	return &Stop{Rule: title, Lock: lock, Message: message + r.Foot + "\r\n"}
}

// SendsScript reports whether line runs what the connection's input holds:
// anywhere in it, a pipe's included, a shell given no
// -c and no file, su given no -c, or one of starts given nothing to run, such
// as bash -euo pipefail, cd /srv && bash -s or sudo -i.
func SendsScript(line string) bool {
	commands, _ := parse(line)
	return slices.ContainsFunc(commands, func(c *command) bool {
		own, words := runs(c), c.words
		if shell, ok := starts[own]; ok {
			return len(c.skip) == 0 && (shell == "" || slices.ContainsFunc(words[1:], func(w string) bool {
				return w == "--shell" || w == "--login" || len(w) > 1 && w[0] == '-' && w[1] != '-' && strings.ContainsAny(w, shell)
			}))
		}
		if !shells[own] {
			return false
		}
		// A -c among a shell's options gives it a command; su hands the words after its user to the shell, so any -c does.
		if own == "su" {
			return commandAt(words) == 0
		}
		at := operand(words, 1)
		return commandAt(words[:at]) == 0 && (at == len(words) || slices.ContainsFunc(words[1:at], func(w string) bool {
			return strings.HasPrefix(w, "-") && strings.Trim(w[1:], letters) == "" && strings.Contains(w, "s")
		}))
	})
}

// Reading a line, as a POSIX shell reads it, without running or expanding
// anything.

const (
	maxDepth = 8
	maxWords = 4096
	maxSteps = 1 << 20
	maxPath  = 4096
)

// wrapper runs the command after its own options: the letters of its options
// that take a value, the long options that take the next word, and the words
// it skips after its options.
type wrapper struct {
	values string
	long   []string
	skip   int
}

var (
	wrappers = map[string]wrapper{
		"sudo":        {"ughpCDRTUrt", strings.Fields("user group host prompt chdir role type close-from other-user chroot"), 0},
		"doas":        {"uC", nil, 0},
		"pkexec":      {"", []string{"user"}, 0},
		"run0":        {"ugD", strings.Fields("user group chdir setenv unit property machine slice description nice"), 0},
		"runuser":     {"ug", strings.Fields("user group supp-group"), 0},
		"env":         {"uCS", strings.Fields("unset chdir split-string"), 0},
		"nohup":       {},
		"nice":        {"n", []string{"adjustment"}, 0},
		"ionice":      {"cnp", strings.Fields("class classdata pid"), 0},
		"time":        {"fo", strings.Fields("format output"), 0},
		"timeout":     {"sk", strings.Fields("signal kill-after"), 1},
		"stdbuf":      {"ioe", strings.Fields("input output error"), 0},
		"setsid":      {},
		"exec":        {"a", nil, 0},
		"command":     {},
		"builtin":     {},
		"busybox":     {},
		"xargs":       {"adEIJLnPRSs", strings.Fields("arg-file delimiter max-args max-procs max-lines max-chars"), 0},
		"watch":       {"n", []string{"interval"}, 0},
		"chroot":      {"", strings.Fields("userspec groups"), 1},
		"flock":       {"wE", strings.Fields("wait timeout conflict-exit-code"), 1},
		"caffeinate":  {"tw", nil, 0},
		"systemd-run": {"pEuMH", strings.Fields("unit property setenv machine host uid gid working-directory slice description"), 0},
	}
	shells   = set("sh bash rbash zsh dash ksh ash fish csh tcsh su source .")
	targets  = set("cp ln install") // the copies whose -t or --target-directory names the folder they write into
	reserved = set("if then elif else do while until ! { } fi done esac coproc")
	execs    = set("-exec -execdir -ok -okdir")
	// starts holds the wrappers that start a shell given nothing to run, with
	// the option letters that make them, if they need any, as --shell and
	// --login do.
	starts = map[string]string{"sudo": "is", "doas": "s", "systemd-run": "S", "run0": "", "pkexec": "", "chroot": ""}
)

func set(words string) map[string]bool {
	m := map[string]bool{}
	for _, w := range strings.Fields(words) {
		m[w] = true
	}
	return m
}

// command is one command a line runs.
type command struct {
	words, outs, ins, texts []string
	into                    []string // the names of what its output goes into
	env                     bool     // it sets a variable
	part                    bool     // a command a wrapper or find runs
	of                      *command // the command a part is in
	skip                    map[int]bool
	shell                   bool // it or its part is a shell
	tidied                  []string
	targeted                bool // a copy whose -t or --target-directory names what it writes
}

func name(c *command) string {
	if len(c.words) == 0 {
		return ""
	}
	return c.words[0][strings.LastIndexByte(c.words[0], '/')+1:]
}

type reader struct {
	found        []*command
	words, steps int
}

type tooMuch struct{}

// parse returns the commands line runs, or false for a line too long or too
// nested to read; a reader that fails on a line refuses it.
func parse(line string) (found []*command, ok bool) {
	r := &reader{}
	defer func() {
		if recover() != nil {
			found, ok = nil, false
		}
	}()
	r.read(line, 0, nil)
	return r.found, true
}

func (r *reader) keep(c *command) {
	if name(c) == "dd" {
		outs := slices.Clip(c.outs)
		for _, w := range c.words {
			if strings.HasPrefix(w, "of=") {
				outs = append(outs, w[3:])
			}
		}
		c.outs = outs
	}
	// The folder that cp's, ln's or install's -t or --target-directory names is
	// what it writes, in place of its last word.
	if targets[name(c)] {
		for k := 1; k < len(c.words); k++ {
			option, value, next := valued(c.words[k])
			if next {
				if k++; k == len(c.words) {
					break
				}
				value = c.words[k]
			}
			if option == "t" || option == "target-directory" {
				c.outs, c.targeted = append(slices.Clip(c.outs), value), true
			}
		}
	}
	if r.words += len(c.words) + 1; r.words > maxWords {
		panic(tooMuch{})
	}
	r.found = append(r.found, c)
}

// valued reads a word of cp, ln or install: its option that takes a value, -S,
// -t or a long one, and that value, or next when it is the next word.
func valued(w string) (option, value string, next bool) {
	for _, long := range []string{"target-directory", "suffix", "strip-program"} {
		if v, ok := strings.CutPrefix(w, "--"+long+"="); ok {
			return long, v, false
		} else if w == "--"+long {
			return long, "", true
		}
	}
	if strings.HasPrefix(w, "-") && !strings.HasPrefix(w, "--") {
		if k := strings.IndexAny(w[1:], "St") + 1; k > 0 {
			return w[k : k+1], w[k+1:], k == len(w)-1
		}
	}
	return "", "", false
}

type heredoc struct {
	mark    string
	tabs    bool
	expands bool // its word is unquoted, so its text's substitutions run
	command *command
	called  []string // the names of the command and its parts
}

func (r *reader) read(line string, depth int, into []string) {
	if depth > maxDepth {
		panic(tooMuch{})
	}
	var words, outs, ins, subs, texts []string
	var docs, waiting []heredoc
	var pipe, mine []*command
	var buf []byte // the word being read
	inWord, pending, begun := false, "", 0
	n, i := len(line), 0
	at := func(k int) byte {
		if k < n {
			return line[k]
		}
		return 0
	}
	add := func(s string) {
		if !inWord {
			begun = i
		}
		buf, inWord = append(buf, s...), true
	}
	end := func() {
		if !inWord {
			return
		}
		word := string(buf)
		switch pending {
		case "out":
			outs = append(outs, word)
		case "in":
			ins = append(ins, word)
		case "here":
			texts = append(texts, word)
		case "doc", "tabs":
			docs = append(docs, heredoc{mark: word, tabs: pending == "tabs", expands: !strings.ContainsAny(line[begun:min(i, n)], `'"\`)})
		case "skip":
		default:
			words = append(words, word)
		}
		buf, inWord, pending = buf[:0], false, ""
	}
	// closing returns one past the bracket that closes the one opened just
	// before from, or n+1 when none does; quotes and nesting are read over.
	closing := func(from int) int {
		level, j := 1, from
		for ; j < n && level > 0; j++ {
			switch line[j] {
			case '\\':
				j++
			case '\'':
				if k := strings.IndexByte(line[j+1:], '\''); k < 0 {
					j = n
				} else {
					j += 1 + k
				}
			case '"':
				for j++; j < n && line[j] != '"'; j++ {
					if line[j] == '\\' {
						j++
					}
				}
			case '(':
				level++
			case ')':
				level--
			}
		}
		if r.steps += j - from; r.steps > maxSteps { // brackets inside brackets are read once a level
			panic(tooMuch{})
		}
		if level > 0 {
			return n + 1
		}
		return j
	}
	var within func(from, to int, found *[]string)
	// quoted reads the backtick at j into found: its text, to the next
	// backtick no backslash keeps or to, in which a backslash before $, ` or \
	// keeps only that one. It returns one past its end.
	quoted := func(j, to int, found *[]string) int {
		var text []byte
		for j++; j < to && line[j] != '`'; j++ {
			if line[j] == '\\' && j+1 < to && strings.IndexByte("$`\\", line[j+1]) >= 0 {
				j++
			}
			text = append(text, line[j])
		}
		*found = append(*found, string(text))
		return j + 1
	}
	// substitution reads $( at j into found: a substitution, read as a line,
	// or $((…)), arithmetic, whose substitutions are. It returns one past its
	// end.
	substitution := func(j, to int, found *[]string) int {
		if at(j+2) == '(' {
			if inner := closing(j + 3); at(inner) == ')' {
				within(j+3, inner-1, found)
				return inner + 1
			}
		}
		k := min(closing(j+2), to+1)
		*found = append(*found, line[j+2:k-1])
		return k
	}
	// within reads the substitutions from `from` to `to` of arithmetic or of a
	// here-document whose word is unquoted into found: a backslash keeps the
	// next character, and nothing else quotes.
	within = func(from, to int, found *[]string) {
		if r.steps += to - from; r.steps > maxSteps {
			panic(tooMuch{})
		}
		for j := from; j < to; j++ {
			switch {
			case line[j] == '\\':
				j++
			case line[j] == '`':
				j = quoted(j, to, found) - 1
			case strings.HasPrefix(line[j:], "$("):
				j = substitution(j, to, found) - 1
			}
		}
	}
	// dollar and tick read a substitution or a backtick at i; its text stays
	// in the word.
	dollar := func() int {
		to := substitution(i, n, &subs)
		add(line[i:min(to, n)])
		return to
	}
	tick := func() int {
		to := quoted(i, n, &subs)
		add(line[i:min(to, n)])
		return to
	}
	finish := func(piped bool) {
		end()
		pending = ""
		env := false
		for len(words) > 0 {
			if assigns(words[0]) {
				env, words = true, words[1:]
			} else if words[0] == "coproc" && len(words) > 2 && reserved[words[2]] { // coproc NAME { … }
				words = words[2:]
			} else if reserved[words[0]] {
				words = words[1:]
			} else if words[0] == "function" {
				words = words[min(2, len(words)):]
			} else {
				break
			}
		}
		var c *command
		var called []string
		if len(words) > 0 || len(outs) > 0 || len(ins) > 0 {
			c = &command{words: words, outs: outs, ins: ins, into: into, env: env, texts: texts}
			r.keep(c)
			mine = append(mine, c)
			called = append(called, runs(c))
			for _, p := range r.inside(c, depth) {
				called = append(called, runs(p))
			}
			c.shell = slices.ContainsFunc(called, func(one string) bool { return shells[one] })
			for _, one := range pipe { // every command later in its pipeline
				one.into = append(slices.Clip(one.into), called...)
			}
			if piped {
				pipe = append(pipe, c)
			} else {
				pipe = nil
			}
			for _, d := range docs {
				waiting = append(waiting, heredoc{d.mark, d.tabs, d.expands, c, called})
			}
		}
		for _, text := range subs {
			if c != nil {
				r.read(text, depth+1, called)
			} else {
				r.read(text, depth+1, into)
			}
		}
		words, outs, ins, subs, docs, texts = nil, nil, nil, nil, nil, nil
	}
	// bodies reads the here-documents that begin on the line just ended.
	bodies := func() {
		for _, h := range waiting {
			var text strings.Builder
			from, through := i, i
			for i < n {
				row := line[i:]
				if k := strings.IndexByte(row, '\n'); k < 0 {
					i = n
				} else {
					row, i = row[:k], i+k+1
				}
				mark := row
				if h.tabs {
					mark = strings.TrimLeft(mark, "\t")
				}
				if strings.TrimSuffix(mark, "\r") == h.mark {
					break
				}
				text.WriteString(row + "\n")
				through = i
			}
			h.command.texts = append(h.command.texts, text.String())
			if h.expands {
				var found []string
				within(from, through, &found)
				for _, one := range found {
					r.read(one, depth+1, h.called)
				}
			}
		}
		waiting = nil
	}
	for i < n {
		c, two := line[i], line[i:min(i+2, n)]
		switch {
		case c == '\\':
			if i+1 == n {
				add("")
			} else if line[i+1] != '\n' {
				add(line[i+1 : i+2])
			}
			i += 2
		case c == '\'':
			if k := strings.IndexByte(line[i+1:], '\''); k < 0 {
				add(line[i+1:])
				i = n
			} else {
				add(line[i+1 : i+1+k])
				i += k + 2
			}
		case two == "$'":
			add("")
			for i += 2; i < n && line[i] != '\''; i++ {
				if line[i] == '\\' {
					i++
				}
				if i < n {
					add(line[i : i+1])
				}
			}
			i++
		case two == `$"`: // $"…" is "…"
			i++
		case c == '"':
			add("")
			for i++; i < n && line[i] != '"'; {
				if line[i] == '\\' && i+1 < n && strings.IndexByte("\"\\$`\n", line[i+1]) >= 0 {
					if line[i+1] != '\n' {
						add(line[i+1 : i+2])
					}
					i += 2
				} else if strings.HasPrefix(line[i:], "$(") {
					i = dollar()
				} else if line[i] == '`' {
					i = tick()
				} else {
					add(line[i : i+1])
					i++
				}
			}
			i++
		case two == "$(":
			i = dollar()
		case two == "<(" || two == ">(":
			to := closing(i + 2)
			subs = append(subs, line[i+2:to-1])
			add(line[i:min(to, n)])
			i = to
		case c == '`':
			i = tick()
		case c == '#' && !inWord:
			if k := strings.IndexByte(line[i:], '\n'); k < 0 {
				i = n
			} else {
				i += k
			}
		case c == ' ' || c == '\t' || c == '\r':
			end()
			i++
		case c == '\n':
			finish(false)
			i++
			bodies()
		case two == "((" && !inWord && len(words) == 0 && at(closing(i+2)) == ')': // ((…)), arithmetic
			to := closing(i + 2)
			within(i+2, to-1, &subs)
			i = to + 1
		case c == ';' || c == '(' || c == ')':
			finish(false)
			i++
		case two == "&>":
			end()
			pending = "out"
			i += 2
			if at(i) == '>' {
				i++
			}
		case c == '&':
			finish(false)
			i++
			if two == "&&" {
				i++
			}
		case c == '|':
			finish(two != "||")
			if two == "||" || two == "|&" {
				i += 2
			} else {
				i++
			}
		case c == '>' || c == '<':
			if inWord && len(buf) > 0 && strings.Trim(string(buf), "0123456789") == "" { // the descriptor a redirection names
				buf, inWord = buf[:0], false
			}
			end()
			switch d := at(i + 2); {
			case strings.HasPrefix(line[i:], "<<<"):
				pending = "here"
				i += 3
			case two == "<<" && d == '-':
				pending = "tabs"
				i += 3
			case two == "<<":
				pending = "doc"
				i += 2
			case two == ">&" || two == "<&":
				if d >= '0' && d <= '9' || d == '-' {
					pending = "skip"
				} else if c == '>' {
					pending = "out"
				} else {
					pending = "in"
				}
				i += 2
			default:
				pending = "in"
				if c == '>' || two == "<>" {
					pending = "out"
				}
				i++
				if two == ">>" || two == ">|" || two == "<>" {
					i++
				}
			}
		default:
			add(line[i : i+1])
			i++
		}
	}
	finish(false)
	// Text given to a command: a shell's, or one piped into a shell, is a line
	// of its own; any other is one more word.
	for _, c := range mine {
		piped := slices.ContainsFunc(c.into, func(one string) bool { return shells[one] })
		if c.shell || piped {
			for _, text := range c.texts {
				r.read(text, depth+1, nil)
			}
		} else if r.words += len(c.texts); r.words > maxWords {
			panic(tooMuch{})
		} else {
			c.words = append(c.words, c.texts...)
		}
		if piped && !c.shell && (name(c) == "echo" || name(c) == "printf") {
			r.read(strings.ReplaceAll(strings.Join(c.words[1:], " "), `\n`, "\n"), depth+1, nil)
		}
	}
}

// inside returns the parts c runs, a wrapper's command and what find
// executes, and reads the lines c runs: a shell's -c line, the words of eval
// and watch, env's -S.
func (r *reader) inside(c *command, depth int) (made []*command) {
	words, own, top := c.words, runs(c), c
	if c.of != nil {
		top = c.of
	}
	c.skip = map[int]bool{} // the words read as a command of their own, which file rules leave to it
	mark := func(from int) {
		for k := from; k < len(words); k++ {
			c.skip[k] = true
		}
	}
	part := func(from, to int) {
		for from < to && assigns(words[from]) {
			from++
		}
		if from >= to {
			return
		}
		if depth >= maxDepth {
			panic(tooMuch{})
		}
		one := &command{words: words[from:to:to], part: true, of: top}
		r.keep(one)
		made = append(made, one)
		made = append(made, r.inside(one, depth+1)...)
	}
	if w, ok := wrappers[own]; ok {
		at := 1
		for ; at < len(words); at++ {
			word := words[at]
			if word == "--" {
				at++
				break
			}
			if word == "" || word[0] != '-' {
				break
			}
			if own == "env" && word == "--split-string" && at+1 < len(words) {
				r.read(words[at+1], depth+1, nil)
			}
			if own == "env" && strings.HasPrefix(word, "--split-string=") {
				r.read(word[15:], depth+1, nil)
			}
			if strings.HasPrefix(word, "--") && slices.Contains(w.long, word[2:]) {
				at++
			}
			if word == "-" || word[1] == '-' {
				continue
			}
			if own == "command" && strings.ContainsAny(word, "vV") { // it only says what a name is
				return made
			}
			takes := strings.IndexAny(word[1:], w.values) + 1 // the first letter that takes a value
			if takes == 0 {
				continue
			}
			value, has := word[takes+1:], true
			if takes == len(word)-1 {
				at++
				value, has = "", at < len(words)
				if has {
					value = words[at]
				}
			}
			if own == "env" && word[takes] == 'S' && has {
				r.read(value, depth+1, nil)
			}
		}
		at += w.skip
		mark(at)
		if own == "watch" {
			if at < len(words) {
				r.read(strings.Join(words[at:], " "), depth+1, nil)
			}
		} else if at+1 < len(words) && words[at] == "-c" {
			r.read(words[at+1], depth+1, nil)
		} else {
			part(at, len(words))
		}
	} else if own == "find" {
		for k, word := range words {
			if !execs[word] {
				continue
			}
			to := k + 1
			for to < len(words) && words[to] != ";" && words[to] != "+" {
				to++
			}
			part(k+1, to)
		}
	} else if shells[own] {
		// The value of --command or --session-command, or the first word after -c that is no option.
		if at := commandAt(words); at > 0 {
			if _, value, ok := strings.Cut(words[at], "="); ok {
				c.skip[at] = true
				r.read(value, depth+1, nil)
			} else if at = operand(words, at+1); at < len(words) {
				c.skip[at] = true
				r.read(words[at], depth+1, nil)
			}
		}
	} else if own == "eval" && len(words) > 1 {
		mark(1)
		r.read(strings.Join(words[1:], " "), depth+1, nil)
	}
	return made
}

const letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

// commandAt returns where a shell's words give it a command, by --command,
// --session-command or one dash and letters with a c among them, or 0.
func commandAt(words []string) int {
	return slices.IndexFunc(words[1:], func(w string) bool {
		long, _, _ := strings.Cut(w, "=")
		return long == "--command" || long == "--session-command" ||
			len(w) > 1 && w[0] == '-' && strings.IndexByte(w, 'c') > 0 && strings.Trim(w[1:], letters) == ""
	}) + 1
}

// operand returns where the first of words from at that is no option of a
// shell is, or their end: an option begins with - or +, takes the next word
// too when it is letters ending in o or O, and - or -- ends them.
func operand(words []string, at int) int {
	for ; at < len(words) && len(words[at]) > 1 && strings.IndexByte("-+", words[at][0]) >= 0 && words[at] != "--"; at++ {
		if w := words[at]; strings.Trim(w[1:], letters) == "" && strings.IndexByte("oO", w[len(w)-1]) >= 0 {
			at++
		}
	}
	if at < len(words) && (words[at] == "-" || words[at] == "--") {
		at++
	}
	return min(at, len(words))
}

// runs is the name c is read by: runuser without -u or --user is su.
func runs(c *command) string {
	if own := name(c); own != "runuser" || slices.ContainsFunc(c.words, userOption) {
		return own
	}
	return "su"
}

// userOption reports whether w is --user or begins with one dash and letters up to a u.
func userOption(w string) bool {
	for k := 1; k < len(w) && w[0] == '-' && isLetter(w[k]); k++ {
		if w[k] == 'u' {
			return true
		}
	}
	return strings.HasPrefix(w, "--user")
}

// assigns reports whether w sets a variable: NAME=, NAME+= or NAME[…]=.
func assigns(w string) bool {
	k := 0
	for k < len(w) && (w[k] == '_' || isLetter(w[k]) || k > 0 && w[k] >= '0' && w[k] <= '9') {
		k++
	}
	if k == 0 {
		return false
	}
	if k < len(w) && w[k] == '[' {
		j := strings.IndexByte(w[k:], ']')
		if j < 0 {
			return false
		}
		k += j + 1
	}
	if k < len(w) && w[k] == '+' {
		k++
	}
	return k < len(w) && w[k] == '='
}

func isLetter(b byte) bool { return b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' }

// Matching. Case is ignored for the letters A to Z only.

func lower(r rune) rune {
	if r >= 'A' && r <= 'Z' {
		return r + 'a' - 'A'
	}
	return r
}

func equalLow(a, b string) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range len(a) {
		if lower(rune(a[k])) != lower(rune(b[k])) {
			return false
		}
	}
	return true
}

const (
	star  rune = -1 // *: one or more letters or digits of any script, or . _ + @ -
	stars rune = -2 // **: anything, nothing included
)

// glob reports whether pattern matches the whole of text, in one pass over
// text with no backtracking; \* is a star.
func glob(pattern, text string) bool {
	var space [64]rune
	tokens, plain := space[:0], true
	for k := 0; k < len(pattern); {
		r, size := utf8.DecodeRuneInString(pattern[k:])
		double := strings.HasPrefix(pattern[k+size:], "*")
		switch {
		case r == '\\' && double:
			tokens, k = append(tokens, '*'), k+2
		case r == '*' && double:
			tokens, k, plain = append(tokens, stars), k+2, false
		case r == '*':
			tokens, k, plain = append(tokens, star), k+1, false
		default:
			tokens, k = append(tokens, lower(r)), k+size
		}
	}
	if plain {
		k := 0
		for _, r := range text {
			if k == len(tokens) || lower(r) != tokens[k] {
				return false
			}
			k++
		}
		return k == len(tokens)
	}
	// A state is 2p, before token p, or 2p+1, inside the * that is token p.
	m := len(tokens)
	settle := func(set []bool) {
		for s := 0; s < 2*m; s++ {
			if set[s] && (s%2 == 1 || tokens[s/2] == stars) {
				set[s/2*2+2] = true
			}
		}
	}
	var spaceNow, spaceNext [129]bool
	now, next := spaceNow[:], spaceNext[:]
	if 2*m+1 > len(now) {
		now, next = make([]bool, 2*m+1), make([]bool, 2*m+1)
	}
	now, next = now[:2*m+1], next[:2*m+1]
	now[0] = true
	settle(now)
	for _, r := range text {
		r, alive := lower(r), false
		clear(next)
		for s := 0; s < 2*m; s++ {
			if !now[s] {
				continue
			}
			switch t := tokens[s/2]; {
			case t == stars:
				next[s], alive = true, true
			case t == star:
				if unicode.IsLetter(r) || unicode.IsNumber(r) || strings.ContainsRune("._+@-", r) {
					next[s|1], alive = true, true
				}
			case t == r:
				next[s+2], alive = true, true
			}
		}
		if !alive {
			return false
		}
		settle(next)
		now, next = next, now
	}
	return now[2*m]
}

func pieces(path string) []string {
	var out []string
	for _, piece := range strings.Split(path, "/") {
		switch piece {
		case "..":
			if len(out) > 0 {
				out = out[:len(out)-1]
			}
		case "", ".":
		default:
			out = append(out, piece)
		}
	}
	return out
}

// tidy returns word as a pattern compares it: without trailing slashes, and a
// path from / ~ or $HOME without its . and .. pieces.
func tidy(word string) string {
	head := ""
	for _, h := range []string{"~", "$HOME", "${HOME}"} {
		if word == h || strings.HasPrefix(word, h+"/") {
			head = h
		}
	}
	if strings.HasPrefix(word, "/") {
		head = "/"
	}
	if head == "" {
		return strings.TrimRight(word, "/")
	}
	rest := strings.Join(pieces(word[len(head):]), "/")
	if head == "/" {
		return "/" + rest
	}
	if rest != "" {
		return head + "/" + rest
	}
	return head
}

// alternative matches one word: -x by its letter, --x up to an =, anything
// else as a glob.
func alternative(alt, word string) bool {
	if strings.HasPrefix(alt, "--") {
		return equalLow(word, alt) || len(word) > len(alt) && word[len(alt)] == '=' && equalLow(word[:len(alt)], alt)
	}
	if len(alt) == 2 && alt[0] == '-' && isLetter(alt[1]) {
		return len(word) > 1 && word[0] == '-' && word[1] != '-' && strings.IndexByte(word[1:], alt[1]) >= 0
	}
	return glob(alt, word)
}

func token(alts []string, word string) bool {
	return slices.ContainsFunc(alts, func(alt string) bool { return alternative(alt, word) })
}

// holds reports whether p's first token matches c's name and every other
// token its name or one of its later words.
func holds(p Pattern, c *command) bool {
	if len(c.words) == 0 || len(p.W) == 0 || !token(p.W[0], name(c)) {
		return false
	}
	top := c
	if c.of != nil {
		top = c.of
	}
	if p.Into != nil && !slices.ContainsFunc(top.into, func(one string) bool { return token(p.Into, one) }) {
		return false
	}
	if c.tidied == nil {
		c.tidied = []string{tidy(name(c))}
		for _, w := range c.words[1:] {
			c.tidied = append(c.tidied, tidy(w))
		}
	}
	for _, alts := range p.W[1:] {
		if !slices.ContainsFunc(c.tidied, func(w string) bool { return token(alts, w) }) {
			return false
		}
	}
	return true
}

// held reports whether a pattern of list holds c and none of its exceptions does.
func held(list []Pattern, c *command) bool {
	return slices.ContainsFunc(list, func(p Pattern) bool { return !p.Not && holds(p, c) }) &&
		!slices.ContainsFunc(list, func(p Pattern) bool { return p.Not && holds(p, c) })
}

func expand(path, home string) string {
	switch {
	case path == "~":
		return home
	case strings.HasPrefix(path, "~/"):
		return home + path[1:]
	case strings.HasPrefix(path, "$HOME"):
		return home + path[5:]
	case strings.HasPrefix(path, "${HOME}"):
		return home + path[7:]
	}
	return path
}

// names reports whether word names the path pattern: a bare name is any
// piece of it, a path from / or ~ its start, any other path a run of pieces.
func names(word, pattern, home string) bool {
	text, rule := expand(word, home), expand(pattern, home)
	if !strings.Contains(rule, "/") {
		return slices.ContainsFunc(pieces(text), func(piece string) bool { return glob(pattern, piece) })
	}
	want := pieces(rule)
	run := func(have []string, from int) bool {
		if len(have)-from < len(want) {
			return false
		}
		for j, piece := range want {
			if !glob(piece, have[from+j]) {
				return false
			}
		}
		return true
	}
	if rule[0] == '/' || rule[0] == '~' {
		return run(pieces(text), 0) || !strings.HasPrefix(text, "/") && run(pieces(home+"/"+text), 0)
	}
	have := pieces(text)
	for from := range have {
		if run(have, from) {
			return true
		}
	}
	return false
}

// paths returns the paths word may name: itself, what follows its first =,
// and each of those without a leading @. A link, text of several lines, or a
// word longer than a path, as a JavaScript string counts it, names none.
func paths(word string) []string {
	if len(word) > maxPath {
		units := 0
		for _, r := range word {
			units++
			if r > 0xFFFF {
				units++
			}
		}
		if units > maxPath {
			return nil
		}
	}
	if strings.Contains(word, "://") || strings.Contains(word, "\n") {
		return nil
	}
	candidates := []string{word}
	if k := strings.IndexByte(word, '='); k >= 0 {
		candidates = append(candidates, word[k+1:])
	}
	var out []string
	for _, text := range candidates {
		out = append(out, text)
		if strings.HasPrefix(text, "@") {
			out = append(out, text[1:])
		}
	}
	return out
}

// covers reports whether one of list's paths names word and none of its
// exceptions (!path) names it too.
func covers(list []string, word, home string) bool {
	return slices.ContainsFunc(paths(word), func(path string) bool {
		return slices.ContainsFunc(list, func(one string) bool { return !strings.HasPrefix(one, "!") && names(path, one, home) }) &&
			!slices.ContainsFunc(list, func(one string) bool { return strings.HasPrefix(one, "!") && names(path, one[1:], home) })
	})
}

// touches reports whether c does to a file list covers what level forbids.
func (r *Rules) touches(level string, list []string, c *command, home string) bool {
	covered := func(w string) bool { return covers(list, w, home) }
	if slices.ContainsFunc(c.outs, covered) || level == "none" && slices.ContainsFunc(c.ins, covered) {
		return true
	}
	changes, used := held(r.Writes, c), held(r.Uses, c)
	copies := false
	if !c.targeted && slices.Contains(r.Copies, name(c)) {
		named := 0
		for k, w := range c.words {
			if k > 0 && !strings.HasPrefix(w, "-") {
				named++
			}
		}
		copies = named > 1
	}
	for k, w := range c.words {
		if !c.skip[k] && (changes || copies && k == len(c.words)-1 || level == "none" && !used) && covered(w) {
			return true
		}
	}
	return false
}

// begins reports whether c, with no variable set before it, begins with
// exactly the allowed words, case ignored.
func begins(allowed []string, c *command) bool {
	if c.env || len(allowed) > len(c.words) {
		return false
	}
	for k, w := range allowed {
		if !equalLow(c.words[k], w) {
			return false
		}
	}
	return true
}
