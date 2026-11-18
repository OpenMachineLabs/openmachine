package daemon

import (
	"bufio"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"golang.org/x/sys/unix"
)

// The bounds of a machine's log.
const (
	logLine   = 48 << 10 // a record's line
	logFile   = 1 << 20  // a file of the queue
	logQueue  = 64 << 20 // the queue
	logFrame  = 64 << 10 // a frame of records
	logResend = 30 * time.Second
	visitWait = 10 * time.Minute // how long a connection waits for its person to return
	logTime   = "2006-01-02T15:04:05.000Z"
)

// Record is one record of a machine's log: its fields in the order they are
// signed, the absent ones left out.
type Record struct {
	Queue       string `json:"queue"`
	N           int64  `json:"n"`
	At          string `json:"at"`
	Type        string `json:"type"` // connect, disconnect, refuse, start, end, block, forward or boot
	Device      string `json:"device"`
	Visit       string `json:"visit,omitempty"`
	Source      string `json:"source,omitempty"`
	Session     string `json:"session,omitempty"`
	App         string `json:"app,omitempty"`
	Access      string `json:"access,omitempty"`
	Folder      string `json:"folder,omitempty"`
	Grant       int64  `json:"grant,omitempty"`
	SourceGrant int64  `json:"source_grant,omitempty"`
	Policy      int64  `json:"policy,omitempty"`
	Reason      string `json:"reason,omitempty"`
	Last        string `json:"last,omitempty"`
	Stage       string `json:"stage,omitempty"`
	Message     string `json:"message,omitempty"`
	Kind        string `json:"kind,omitempty"`
	Terminal    int    `json:"terminal,omitempty"`
	Command     string `json:"command,omitempty"` // only where the account records commands
	Script      string `json:"script,omitempty"`  // as Command
	Cut         int    `json:"cut,omitempty"`
	Exit        *int   `json:"exit,omitempty"`
	Signal      string `json:"signal,omitempty"`
	Error       string `json:"error,omitempty"`
	Rule        string `json:"rule,omitempty"`
	Host        string `json:"host,omitempty"`
	Port        int    `json:"port,omitempty"`
	Version     string `json:"version,omitempty"`
	Sig         string `json:"sig"`
}

// Sign returns r's signature: key over om-log-v1 and, for each present field
// in order, its name, its length in bytes and its value.
func (r Record) Sign(key ed25519.PrivateKey) string {
	signed := []byte("om-log-v1\n")
	v := reflect.ValueOf(r)
	for i := range v.NumField() - 1 { // all but Sig
		if f := v.Field(i); !f.IsZero() {
			name, _, _ := strings.Cut(v.Type().Field(i).Tag.Get("json"), ",")
			value := fmt.Sprint(reflect.Indirect(f))
			signed = fmt.Appendf(signed, "%s:%d:%s\n", name, len(value), value)
		}
	}
	return base64.RawURLEncoding.EncodeToString(ed25519.Sign(key, signed))
}

var (
	secretName = `[\w-]*(?i:PASS|PWD|SECRET|TOKEN|KEY|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|SIGNATURE)[\w-]*`
	// shellWord is the rest of a shell word: quoted strings, escapes and plain characters.
	shellWord = `(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s;|&<>'"\\])*`
	// redactions are redact's patterns: each hides what it matches outside its groups.
	redactions = []*regexp.Regexp{
		regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----(?s:.*?)(?:-----END [A-Z ]*PRIVATE KEY-----|$)`),
		// A header, as far as its quote or, unquoted, its command.
		regexp.MustCompile(`('` + secretName + `:[ \t]*)[^'\n]*|("` + secretName + `:[ \t]*)[^"\n]*|((?:^|[\s=])` + secretName + `:[ \t]*)[^'"\n;|&]*`),
		// NAME=value: an assignment, an option or a URL's query parameter.
		regexp.MustCompile(`(\b` + secretName + `=)` + shellWord),
		regexp.MustCompile(`(--` + secretName + `[ \t]+)(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s;|&<>'"\\-])` + shellWord),
		regexp.MustCompile(`(://[^/\s:@'"]*:)()()[^/\s@'"]*(@)`),
		regexp.MustCompile(`(^|[^A-Za-z0-9_-])(?:omkey_|ominv_|ommove_|gh[pousr]_|github_pat_|xox[abpr]-|AKIA|ASIA|sk-ant-|sk-|sk_live_|rk_live_|AIza|npm_|pypi-)[A-Za-z0-9_-]{16,}`),
		regexp.MustCompile(`eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`),
	}
)

// redact hides the passwords and tokens of a command or script.
func redact(s string) string {
	for _, r := range redactions {
		s = r.ReplaceAllString(s, "${1}${2}${3}[hidden]${4}")
	}
	return s
}

// cut returns s within max bytes, ending at a character, and whether it was shortened.
func cut(s string, max int) (string, bool) {
	if len(s) <= max {
		return s, false
	}
	for max > 0 && !utf8.RuneStart(s[max]) {
		max--
	}
	return s[:max], true
}

// LogFrame is the records a machine sends next: the queue's lines from the
// place From, each as stored, or null for one that is no record.
type LogFrame struct {
	Type    string            `json:"type"` // log
	Queue   string            `json:"queue"`
	From    int64             `json:"from"`
	Records []json.RawMessage `json:"records"`
}

// Log is a machine's own record of who connected to it and what they ran:
// signed lines in a queue on disk, each named by its place, sent to Cloud and
// deleted once Cloud has them. It records nothing until it is started.
type Log struct {
	dir, device string
	key         ed25519.PrivateKey

	mu        sync.Mutex
	started   bool
	queue     string        // the queue's ID
	file      *os.File      // the file records are appended to: nil until the queue opens
	files     []int64       // each file's first place, oldest first; the last is file's
	lines     int64         // file's lines
	size      int64         // file's bytes
	total     int64         // the queue's bytes
	confirmed int64         // Cloud has every place through it
	changed   chan struct{} // closed at each new record and confirmation
	visits    []*Visit      // the open connections
}

// OpenLog returns the log of device kept in dir. A queue made by an earlier
// run records and sends at once.
func OpenLog(dir, device string, key ed25519.PrivateKey) *Log {
	l := &Log{dir: dir, device: device, key: key, changed: make(chan struct{})}
	if _, err := os.Lstat(filepath.Join(dir, "queue.json")); err == nil {
		l.Start()
	}
	return l
}

// Start makes the queue, once a Cloud that takes its frames is known.
func (l *Log) Start() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.started = true
	if err := l.open(); err != nil {
		log.Printf("native: log: %v", err)
	}
}

// open opens the queue, cutting a partial last line, and records this start
// and the end of each connection the last run left open. It requires l.mu,
// and is tried again at each record until it succeeds.
func (l *Log) open() (err error) {
	if l.file != nil {
		return nil
	}
	defer func() {
		if err != nil {
			if l.file != nil {
				l.file.Close()
			}
			l.file, l.files, l.total = nil, nil, 0
		}
	}()
	if err := private(l.dir); err != nil {
		return err
	}
	st := &state{Dir: l.dir}
	var q struct {
		Queue string `json:"queue"`
	}
	if err := st.readJSON("queue.json", maxState, &q); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	entries, err := os.ReadDir(l.dir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if first, err := strconv.ParseInt(e.Name(), 10, 64); err == nil {
			if fi, err := e.Info(); err == nil {
				l.total += fi.Size()
			}
			l.files = append(l.files, first)
		}
	}
	// A queue with no file left no longer knows its next place, and Cloud
	// skips a place it has taken: a new queue, which Cloud records.
	if slices.Sort(l.files); q.Queue == "" || len(l.files) == 0 {
		q.Queue, l.files = random(), []int64{1}
		if err := st.writeJSON("queue.json", q); err != nil {
			return err
		}
	}
	if l.file, err = l.create(l.files[len(l.files)-1]); err != nil {
		return err
	}
	held, err := os.ReadFile(l.file.Name())
	if err != nil {
		return err
	}
	l.size = int64(bytes.LastIndexByte(held, '\n') + 1)
	if err := l.file.Truncate(l.size); err != nil {
		return err
	}
	l.total -= int64(len(held)) - l.size
	l.queue, l.lines, l.confirmed = q.Queue, int64(bytes.Count(held, []byte("\n"))), l.files[0]-1
	// One that cannot be read ends nothing, and stops nothing either.
	var left []struct{ ID, Source string }
	if err := st.readJSON("open.json", logQueue, &left); err != nil && !errors.Is(err, os.ErrNotExist) {
		log.Printf("native: log: open.json: %v", err)
		left = nil
	}
	if err := l.put(Record{Type: "boot", Version: version()}); err != nil {
		return err
	}
	now := time.Now().UTC().Format(logTime)
	for _, v := range left {
		// When its transports closed is not known: this run closes them now.
		if err := l.put(Record{Type: "disconnect", Visit: v.ID, Source: v.Source, Reason: "restart", Last: now}); err != nil {
			return err
		}
	}
	return l.save()
}

// create opens the queue's file whose first place is first, making it durably if it is new.
func (l *Log) create(first int64) (*os.File, error) {
	f, err := os.OpenFile(filepath.Join(l.dir, strconv.FormatInt(first, 10)), os.O_RDWR|os.O_APPEND|os.O_CREATE|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syncDir(l.dir); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}

// version is this om's release, which the bundle it runs from names.
func version() string {
	m := struct {
		Version string `json:"version"`
	}{"unknown"}
	if exe, err := os.Executable(); err == nil {
		if exe, err = filepath.EvalSymlinks(exe); err == nil {
			if b, err := os.ReadFile(filepath.Join(filepath.Dir(exe), "manifest.json")); err == nil {
				json.Unmarshal(b, &m)
			}
		}
	}
	return m.Version
}

// random returns 32 random hex digits: a queue's ID, or a connection's.
func random() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// append writes r to the queue, opening it first if an earlier try failed. It requires l.mu.
func (l *Log) append(r Record) error {
	err := l.open()
	if err == nil {
		err = l.put(r)
	}
	if err != nil {
		log.Printf("native: log: %v", err)
	}
	return err
}

// put signs r and writes it at the open queue's next place, durably: a failed
// write leaves no part of it. It requires l.mu.
func (l *Log) put(r Record) (err error) {
	r.Queue, r.N, r.At, r.Device = l.queue, l.files[len(l.files)-1]+l.lines, time.Now().UTC().Format(logTime), l.device
	var line bytes.Buffer
	for {
		r.Sig = r.Sign(l.key)
		line.Reset()
		enc := json.NewEncoder(&line)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(r); err != nil {
			return err
		}
		if line.Len() <= logLine || r.Script == "" && r.Command == "" {
			break
		}
		if r.Cut = 1; r.Script != "" {
			r.Script, _ = cut(r.Script, len(r.Script)/2)
		} else {
			r.Command, _ = cut(r.Command, len(r.Command)/2)
		}
	}
	n := int64(line.Len())
	if l.size > 0 && l.size+n > logFile {
		f, err := l.create(r.N)
		if err != nil {
			return err
		}
		l.file.Close()
		l.file, l.files, l.lines, l.size = f, append(l.files, r.N), 0, 0
	}
	for l.total+n > logQueue && len(l.files) > 1 {
		l.remove()
	}
	if _, err = l.file.Write(line.Bytes()); err == nil {
		err = l.file.Sync()
	}
	if err != nil {
		l.file.Truncate(l.size)
		return err
	}
	l.lines, l.size, l.total = l.lines+1, l.size+n, l.total+n
	l.wake()
	return nil
}

// remove deletes the queue's oldest file, never the one appended to. It requires l.mu.
func (l *Log) remove() {
	path := filepath.Join(l.dir, strconv.FormatInt(l.files[0], 10))
	if fi, err := os.Lstat(path); err == nil {
		l.total -= fi.Size()
	}
	os.Remove(path)
	l.files = l.files[1:]
}

// renew deletes the queue's files, then starts a new queue at place 1; a
// failure leaves no file, so the next open makes one. It requires l.mu.
func (l *Log) renew() error {
	l.file.Close()
	l.file = nil
	for len(l.files) > 0 {
		l.remove()
	}
	q := struct {
		Queue string `json:"queue"`
	}{random()}
	if err := (&state{Dir: l.dir}).writeJSON("queue.json", q); err != nil {
		return err
	}
	f, err := l.create(1)
	if err != nil {
		return err
	}
	l.file, l.files, l.lines, l.size, l.total, l.queue, l.confirmed = f, []int64{1}, 0, 0, 0, q.Queue, 0
	return nil
}

func (l *Log) wake() {
	close(l.changed)
	l.changed = make(chan struct{})
}

// Frame returns the frame to send next, from the first place Cloud has not
// confirmed, or nil when it has them all, and a channel closed when a record
// is written or Cloud confirms one.
func (l *Log) Frame() (*LogFrame, <-chan struct{}) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return nil, l.changed
	}
	from := l.confirmed + 1
	for i, first := range l.files {
		if i+1 < len(l.files) && l.files[i+1] <= from {
			continue
		}
		held, _ := os.ReadFile(filepath.Join(l.dir, strconv.FormatInt(first, 10)))
		lines := bytes.SplitAfter(held, []byte("\n"))
		lines = lines[:len(lines)-1]
		from = max(from, first) // the places before it are gone
		f, size := &LogFrame{Type: "log", Queue: l.queue, From: from}, 256
		for _, line := range lines[min(from-first, int64(len(lines))):] {
			if line = line[:len(line)-1]; len(line) > logLine || !json.Valid(line) {
				line = []byte("null")
			}
			if size += len(line) + 1; size > logFrame {
				break
			}
			f.Records = append(f.Records, line)
		}
		if len(f.Records) > 0 {
			return f, l.changed
		}
	}
	return nil, l.changed
}

// Logged takes Cloud's confirmation of every place of queue through n, and
// deletes each file it covers but the one appended to.
func (l *Log) Logged(queue string, through int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil || queue != l.queue || through <= l.confirmed {
		return
	}
	if through >= l.files[len(l.files)-1]+l.lines {
		// Cloud holds a place this queue has yet to write: its files went
		// back, deleted or restored from a copy. A new queue, which Cloud records.
		if err := l.renew(); err != nil {
			log.Printf("native: log: %v", err)
		}
		l.wake()
		return
	}
	l.confirmed = through
	for len(l.files) > 1 && l.files[1]-1 <= through {
		l.remove()
	}
	l.wake()
}

// Visit is one open connection: one source's app on this machine, from its
// first transport until it has had none for 10 minutes, or a lease or going
// offline ends it. A nil Visit records nothing.
type Visit struct {
	l           *Log
	id          string
	source, app string
	binding     *binding        // of its admitted transports; nil while it has only refusals
	open        int             // its open transports
	closed      time.Time       // when it last had one
	refused     map[string]bool // the stages and reasons it recorded
	noticed     map[string]bool // the notices it printed
}

// visit returns the open connection of source's app, opening one if there is none. It requires l.mu.
func (l *Log) visit(source, app string) *Visit {
	if i := slices.IndexFunc(l.visits, func(v *Visit) bool { return v.source == source && v.app == app }); i >= 0 {
		return l.visits[i]
	}
	v := &Visit{l: l, id: "vis_" + random(), source: source, app: app, closed: time.Now(), refused: map[string]bool{}, noticed: map[string]bool{}}
	l.visits = append(l.visits, v)
	l.save()
	return v
}

// save replaces open.json, which a later run reads to end what this one left
// open; until this run has read it, it stays the last run's. It requires l.mu.
func (l *Log) save() error {
	if l.file == nil {
		return nil
	}
	left := []map[string]string{}
	for _, v := range l.visits {
		left = append(left, map[string]string{"id": v.id, "source": v.source})
	}
	err := (&state{Dir: l.dir}).writeJSON("open.json", left)
	if err != nil {
		log.Printf("native: log: %v", err)
	}
	return err
}

// end records that v ended for reason and forgets it. It requires l.mu.
func (l *Log) end(v *Visit, reason string) {
	last := time.Now()
	if v.open == 0 {
		last = v.closed
	}
	v.write(Record{Type: "disconnect", Reason: reason, Last: last.UTC().Format(logTime)})
	l.visits = slices.DeleteFunc(l.visits, func(o *Visit) bool { return o == v })
	l.save()
}

// Enter records a transport from source s through app, admitted under lease:
// it joins that source and app's open connection, after ending one admitted
// under another binding, or opens one. The first admitted transport of a
// connection is its connect. A source limited to the AI models connects with
// that access, in no folder, whatever the machine's mode.
func (l *Log) Enter(lease *Lease, s Source, app string) *Visit {
	b, p, err := bindingOf(lease, s)
	l.mu.Lock()
	defer l.mu.Unlock()
	if !l.started || err != nil {
		return nil
	}
	v := l.visit(s.DeviceID, app)
	if v.binding != nil && *v.binding != b {
		l.end(v, "access")
		v = l.visit(s.DeviceID, app)
	}
	if v.open++; v.binding == nil {
		v.binding = &b
		access, folder := p.Mode, "~"
		if s.Limit == "models" {
			access = "models"
		} else if len(p.Folders) > 0 {
			folder = p.Folders[0]
		}
		v.write(Record{Type: "connect", App: app, Access: access, Folder: folder, Grant: b.TargetGrantRevision,
			SourceGrant: b.SourceGrantRevision, Policy: b.PolicyRevision})
	}
	return v
}

// ID returns v's ID, "" for a connection that is not recorded.
func (v *Visit) ID() string {
	if v == nil {
		return ""
	}
	return v.id
}

// Leave records that one of v's transports closed.
func (v *Visit) Leave() {
	if v == nil {
		return
	}
	v.l.mu.Lock()
	defer v.l.mu.Unlock()
	v.open--
	v.closed = time.Now()
}

// Refuse records a transport from source through app that was not admitted,
// in that source and app's connection, opening one if there is none.
func (l *Log) Refuse(source, app, reason, message string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.started && source != "" {
		l.visit(source, app).refuse("transport", reason, message)
	}
}

// Refuse records what om refused v at stage, transport or channel, once for each stage and reason.
func (v *Visit) Refuse(stage, reason, message string) {
	if v == nil {
		return
	}
	v.l.mu.Lock()
	defer v.l.mu.Unlock()
	v.refuse(stage, reason, message)
}

// refuse requires v.l.mu.
func (v *Visit) refuse(stage, reason, message string) {
	if v.open == 0 {
		v.closed = time.Now()
	}
	if !v.refused[stage+reason] {
		v.refused[stage+reason] = true
		v.write(Record{Type: "refuse", App: v.app, Reason: reason, Stage: stage, Message: message})
	}
}

// Close ends, for reason, each connected connection that lease no longer
// permits: every one when lease is nil, as the machine goes offline.
func (l *Log) Close(reason string, lease *Lease) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, v := range slices.Clone(l.visits) {
		if v.binding != nil && (lease == nil || !permits(lease, *v.binding)) {
			l.end(v, reason)
		}
	}
}

// Expire ends each connection that has had no transport for the 10 minutes before now.
func (l *Log) Expire(now time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, v := range slices.Clone(l.visits) {
		if v.open == 0 && now.Sub(v.closed) >= visitWait {
			l.end(v, "done")
		}
	}
}

// Notice returns what a session of kind is told before it runs, where the
// account records commands and tells people so with text: each shell, and an
// exec once for each text. SFTP runs no command.
func (v *Visit) Notice(kind, text string) string {
	if v == nil || text == "" || kind == "sftp" {
		return ""
	}
	v.l.mu.Lock()
	defer v.l.mu.Unlock()
	if kind == "exec" && v.noticed[text] {
		return ""
	}
	v.noticed[text] = true
	return text + "\r\n"
}

// Write records r in v, durably before it returns. Its texts are made valid
// UTF-8, a command and a script are redacted, and each is cut to its bound.
func (v *Visit) Write(r Record) error {
	if v == nil {
		return nil
	}
	v.l.mu.Lock()
	defer v.l.mu.Unlock()
	return v.write(r)
}

// write requires v.l.mu.
func (v *Visit) write(r Record) error {
	valid := func(s string) string { return strings.ToValidUTF8(s, "�") }
	var long, longer bool
	r.Command, long = cut(redact(valid(r.Command)), 4096)
	r.Script, longer = cut(redact(valid(r.Script)), 16384)
	if long || longer {
		r.Cut = 1
	}
	r.Message, _ = cut(valid(r.Message), 200)
	r.Host, _ = cut(valid(r.Host), 255)
	r.Visit, r.Source = v.id, v.source
	return v.l.append(r)
}

var (
	appName = regexp.MustCompile(`^OM-APP ([A-Za-z0-9 .+_-]{1,40})\r?\n$`)
	// apps are the apps a source's processes name, each with the names that match it.
	apps = [][]string{
		{"Claude Code", "claude", "claude-code"},
		{"Codex", "codex"},
		{"Cursor", "Cursor", "Cursor Helper*", "cursor", "cursor-agent"},
		{"Windsurf", "Windsurf", "Windsurf Helper*", "windsurf"},
		{"VS Code", "Code", "Code Helper*", "code", "code-insiders"},
		{"Zed", "Zed", "zed"},
		{"Gemini CLI", "gemini"},
		{"OpenCode", "opencode"},
		{"Aider", "aider"},
		{"Goose", "goose"},
		{"Amp", "amp"},
		{"GitHub Copilot", "copilot"},
	}
)

// App names the app that ran the ssh whose process is pid: the nearest of up
// to 8 processes, from ssh's parent upward, whose name, or the last path
// segment of its first or second argument, matches an app; else Terminal.
// process returns a process's parent, name and arguments.
func App(pid int, process func(pid int) (parent int, name string, args []string)) string {
	pid, _, _ = process(pid)
	for i := 0; i < 8 && pid > 0; i++ {
		parent, name, args := process(pid)
		names := []string{name}
		for _, arg := range args[:min(2, len(args))] {
			names = append(names, filepath.Base(arg))
		}
		for _, app := range apps {
			for _, match := range app[1:] {
				prefix, any := strings.CutSuffix(match, "*")
				if slices.ContainsFunc(names, func(n string) bool { return n == match || any && strings.HasPrefix(n, prefix) }) {
					return app[0]
				}
			}
		}
		pid = parent
	}
	return "Terminal"
}

// ReadApp reads the line a source's om proxy sends before SSH, OM-APP and
// the app's name, from a reader whose 64-byte buffer bounds it: the name, or
// "" for a line that is not one. A stream that starts otherwise is left unread.
func ReadApp(r *bufio.Reader) string {
	if head, _ := r.Peek(len("OM-APP ")); string(head) != "OM-APP " {
		return ""
	}
	line, _ := r.ReadSlice('\n')
	if m := appName.FindSubmatch(line); m != nil {
		return string(m[1])
	}
	return ""
}
