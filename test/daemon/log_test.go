package daemon

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// logVectors are the cases Cloud's tests share.
type logVectors struct {
	Seed    string
	Records []struct{ Line, Signed string }
	Redact  []struct{ In, Out string }
	Cut     []struct {
		Field   string
		In, Out [][2]any // parts, each some bytes, in hex for In, and how often they repeat
		Cut     int
	}
}

func readLogVectors(t *testing.T) (logVectors, ed25519.PrivateKey) {
	var v logVectors
	b, err := os.ReadFile("../vectors/logs.json")
	if err != nil || json.Unmarshal(b, &v) != nil {
		t.Fatalf("vectors: %v", err)
	}
	seed, _ := hex.DecodeString(v.Seed)
	return v, ed25519.NewKeyFromSeed(seed)
}

// openLog returns a started log of dev_target, and its directory.
func openLog(t *testing.T) (*daemon.Log, string) {
	dir := filepath.Join(t.TempDir(), "log")
	_, key, _ := ed25519.GenerateKey(nil)
	l := daemon.OpenLog(dir, "dev_target", key)
	l.Start()
	return l, dir
}

// held returns the records the log holds that Cloud has not confirmed, and the place of the first.
func held(t *testing.T, l *daemon.Log) ([]daemon.Record, int64) {
	t.Helper()
	f, _ := l.Frame()
	if f == nil {
		return nil, 0
	}
	var records []daemon.Record
	for _, raw := range f.Records {
		var r daemon.Record
		if err := json.Unmarshal(raw, &r); err != nil {
			t.Fatalf("record %s: %v", raw, err)
		}
		records = append(records, r)
	}
	return records, f.From
}

// types names each record after the log's boot, as its type and what tells it apart.
func types(t *testing.T, l *daemon.Log) string {
	t.Helper()
	records, _ := held(t, l)
	var names []string
	for _, r := range records[1:] {
		names = append(names, strings.TrimSpace(r.Type+" "+r.Reason+r.Stage))
	}
	return strings.Join(names, ", ")
}

// lease admits dev_source under the target's policy revision.
func lease(policy int) (*daemon.Lease, daemon.Source) {
	s := daemon.Source{DeviceID: "dev_source", NodeKey: "nodekey:s", GrantRevision: 5}
	return &daemon.Lease{Target: &daemon.Target{GrantID: "g", GrantRevision: 3,
		Policy: json.RawMessage(fmt.Sprintf(`{"revision":%d,"mode":"workspace","folders":["/srv/app","/tmp"]}`, policy))}, Sources: []daemon.Source{s}}, s
}

func TestLogVectors(t *testing.T) {
	v, key := readLogVectors(t)
	t.Run("a record signs its present fields in order", func(t *testing.T) {
		for _, c := range v.Records {
			var r daemon.Record
			if err := json.Unmarshal([]byte(c.Line), &r); err != nil {
				t.Fatal(err)
			}
			if got := r.Sign(key); got != r.Sig {
				t.Errorf("%s %d signs %s, want %s", r.Type, r.N, got, r.Sig)
			}
			if sig, _ := base64.RawURLEncoding.DecodeString(r.Sig); !ed25519.Verify(key.Public().(ed25519.PublicKey), []byte(c.Signed), sig) {
				t.Errorf("%s %d does not sign %q", r.Type, r.N, c.Signed)
			}
		}
	})
	l, _ := openLog(t)
	source, s := lease(1)
	visit := l.Enter(source, s, "")
	written := func(r daemon.Record) daemon.Record {
		t.Helper()
		if err := visit.Write(r); err != nil {
			t.Fatal(err)
		}
		records, _ := held(t, l)
		l.Logged(records[0].Queue, records[len(records)-1].N)
		return records[len(records)-1]
	}
	t.Run("passwords and tokens are hidden", func(t *testing.T) {
		for _, c := range v.Redact {
			if got := written(daemon.Record{Type: "start", Kind: "exec", Command: c.In, Script: c.In}); got.Command != c.Out || got.Script != c.Out {
				t.Errorf("%q is recorded as %q and %q, want %q", c.In, got.Command, got.Script, c.Out)
			}
		}
	})
	t.Run("a text is valid UTF-8, redacted, then cut at a character", func(t *testing.T) {
		for i, c := range v.Cut {
			var in, out string
			for _, part := range c.In {
				b, _ := hex.DecodeString(part[0].(string))
				in += strings.Repeat(string(b), int(part[1].(float64)))
			}
			for _, part := range c.Out {
				out += strings.Repeat(part[0].(string), int(part[1].(float64)))
			}
			r := daemon.Record{Type: "start", Kind: "exec", Command: in}
			if c.Field == "script" {
				r.Command, r.Script = "", in
			}
			got := written(r)
			if got.Command+got.Script != out || got.Cut != c.Cut {
				t.Errorf("case %d: %d bytes with cut %d, want %d with %d", i, len(got.Command+got.Script), got.Cut, len(out), c.Cut)
			}
		}
	})
}

func TestLogQueue(t *testing.T) {
	source, s := lease(1)
	files := func(dir string) (names []string) {
		entries, _ := os.ReadDir(dir)
		for _, e := range entries {
			if _, err := strconv.Atoi(e.Name()); err == nil {
				names = append(names, e.Name())
			}
		}
		slices.SortFunc(names, func(a, b string) int { x, _ := strconv.Atoi(a); y, _ := strconv.Atoi(b); return x - y })
		return names
	}
	// big is a record of about 20 KiB where commands are recorded.
	big := daemon.Record{Type: "start", Kind: "exec", Command: strings.Repeat("c", 4096), Script: strings.Repeat("s", 16384)}

	t.Run("nothing is recorded before a lease has record", func(t *testing.T) {
		dir := filepath.Join(t.TempDir(), "log")
		_, key, _ := ed25519.GenerateKey(nil)
		l := daemon.OpenLog(dir, "dev_target", key)
		if v := l.Enter(source, s, "Codex"); v != nil || v.Write(big) != nil {
			t.Fatal("a log that was not started took a connection")
		}
		l.Refuse("dev_source", "", "user", "refused")
		if f, _ := l.Frame(); f != nil {
			t.Fatalf("a log that was not started sends %v", f)
		}
		if _, err := os.Stat(dir); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("a log that was not started made its queue: %v", err)
		}
		l.Start()
		records, from := held(t, l)
		if len(records) != 1 || from != 1 || records[0].Type != "boot" || records[0].N != 1 || records[0].Device != "dev_target" || records[0].Version == "" ||
			len(records[0].Queue) != 32 || records[0].Sign(key) != records[0].Sig {
			t.Fatalf("a new queue starts with %+v at %d", records, from)
		}
		if b, _ := os.ReadFile(filepath.Join(dir, "queue.json")); strings.TrimSpace(string(b)) != `{"queue":"`+records[0].Queue+`"}` {
			t.Fatalf("queue.json holds %s", b)
		}
		// The queue made, a later run records at once, in the same queue.
		again, _ := held(t, daemon.OpenLog(dir, "dev_target", key))
		if len(again) != 2 || again[1].Type != "boot" || again[1].N != 2 || again[1].Queue != records[0].Queue {
			t.Fatalf("the next run holds %+v", again)
		}
		// With every file gone, places Cloud took would be used again: a new queue.
		for _, name := range files(dir) {
			os.Remove(filepath.Join(dir, name))
		}
		fresh, from := held(t, daemon.OpenLog(dir, "dev_target", key))
		if len(fresh) != 1 || from != 1 || fresh[0].Type != "boot" || len(fresh[0].Queue) != 32 || fresh[0].Queue == records[0].Queue {
			t.Fatalf("a queue with no file left starts with %+v at %d", fresh, from)
		}
	})

	t.Run("a queue restored from a copy starts a new queue once Cloud confirms past it", func(t *testing.T) {
		l, dir := openLog(t)
		copied := filepath.Join(t.TempDir(), "copy")
		os.CopyFS(copied, os.DirFS(dir))
		v := l.Enter(source, s, "")
		for range 5 {
			v.Write(daemon.Record{Type: "start", Kind: "shell"})
		}
		records, _ := held(t, l)
		queue, through := records[0].Queue, records[len(records)-1].N
		l.Logged(queue, through)
		// The machine's state goes back to the copy, which held place 1 only.
		os.RemoveAll(dir)
		os.CopyFS(dir, os.DirFS(copied))
		filepath.WalkDir(dir, func(path string, d os.DirEntry, _ error) error {
			return os.Chmod(path, map[bool]os.FileMode{true: 0o700, false: 0o600}[d.IsDir()])
		})
		_, key, _ := ed25519.GenerateKey(nil)
		l = daemon.OpenLog(dir, "dev_target", key)
		if again, from := held(t, l); from != 1 || again[len(again)-1].N >= through || again[0].Queue != queue {
			t.Fatalf("the restored queue holds %+v from %d", again, from)
		}
		l.Logged(queue, through)
		if f, _ := l.Frame(); f != nil {
			t.Fatalf("a queue Cloud confirmed past still sends %+v", f)
		}
		l.Enter(source, s, "Zed").Write(daemon.Record{Type: "start", Kind: "shell"})
		next, from := held(t, l)
		if from != 1 || len(next) == 0 || next[0].N != 1 || next[0].Queue == queue || len(next[0].Queue) != 32 || files(dir)[0] != "1" {
			t.Fatalf("after Cloud confirmed past it, the log holds %+v from %d", next, from)
		}
	})

	t.Run("a file holds 1 MiB and is named by its first place; a place is its file's name and its line", func(t *testing.T) {
		l, dir := openLog(t)
		v := l.Enter(source, s, "")
		for range 120 {
			if err := v.Write(big); err != nil {
				t.Fatal(err)
			}
		}
		names := files(dir)
		if len(names) != 3 || names[0] != "1" {
			t.Fatalf("120 records of 20 KiB are in files %v", names)
		}
		for _, name := range names {
			b, _ := os.ReadFile(filepath.Join(dir, name))
			first, _ := strconv.ParseInt(name, 10, 64)
			if len(b) > 1<<20 || b[len(b)-1] != '\n' {
				t.Fatalf("file %s holds %d bytes", name, len(b))
			}
			for i, line := range strings.Split(strings.TrimSuffix(string(b), "\n"), "\n") {
				var r daemon.Record
				if json.Unmarshal([]byte(line), &r) != nil || r.N != first+int64(i) {
					t.Fatalf("line %d of file %s is place %d", i, name, r.N)
				}
			}
		}
		// A frame is at most 64 KiB, and Cloud's confirmation deletes the files it covers, never the one appended to.
		records, from := held(t, l)
		f, _ := l.Frame()
		if b, _ := json.Marshal(f); from != 1 || len(b) > 64<<10 || len(records) != 5 {
			t.Fatalf("the first frame holds %d records from %d in %d bytes", len(records), from, len(b))
		}
		second, _ := strconv.ParseInt(names[1], 10, 64)
		l.Logged(records[0].Queue, second-2)
		if got := files(dir); !slices.Equal(got, names) {
			t.Fatalf("a confirmation short of a file's end left %v", got)
		}
		if records, from = held(t, l); from != second-1 || records[0].N != second-1 {
			t.Fatalf("the next frame starts at %d", from)
		}
		b, _ := os.ReadFile(filepath.Join(dir, names[2]))
		var last daemon.Record
		json.Unmarshal(b[bytes.LastIndexByte(b[:len(b)-1], '\n')+1:], &last)
		l.Logged("another queue", 1<<40)
		l.Logged(records[0].Queue, last.N)
		if got := files(dir); !slices.Equal(got, names[2:]) {
			t.Fatalf("a confirmation of every place left %v", got)
		}
		if f, _ := l.Frame(); f != nil {
			t.Fatalf("a confirmed queue sends %v", f)
		}
	})

	t.Run("past 64 MiB the oldest file goes first, and the frame starts at the first place held", func(t *testing.T) {
		if testing.Short() {
			t.Skip("writes 64 MiB")
		}
		l, dir := openLog(t)
		v := l.Enter(source, s, "")
		for range 3400 {
			if err := v.Write(big); err != nil {
				t.Fatal(err)
			}
		}
		names, total := files(dir), int64(0)
		for _, name := range names {
			fi, _ := os.Stat(filepath.Join(dir, name))
			total += fi.Size()
		}
		first, _ := strconv.ParseInt(names[0], 10, 64)
		if _, from := held(t, l); names[0] == "1" || total > 64<<20 || total < 62<<20 || from != first {
			t.Fatalf("the queue holds %d bytes from file %s, and sends from %d", total, names[0], from)
		}
	})

	t.Run("a start cuts a partial last line, and a line that is no record travels as null", func(t *testing.T) {
		l, dir := openLog(t)
		l.Enter(source, s, "")
		before, _ := held(t, l)
		f, _ := os.OpenFile(filepath.Join(dir, "1"), os.O_WRONLY|os.O_APPEND, 0)
		f.WriteString("not a record\n" + `{"queue":"` + before[0].Queue + `","n":4,"type":"st`)
		f.Close()
		_, key, _ := ed25519.GenerateKey(nil)
		l = daemon.OpenLog(dir, "dev_target", key)
		frame, _ := l.Frame()
		var boot daemon.Record
		json.Unmarshal(frame.Records[3], &boot)
		if len(frame.Records) != 5 || string(frame.Records[2]) != "null" || boot.Type != "boot" || boot.N != 4 {
			t.Fatalf("after a partial line the queue holds %s", frame.Records)
		}
	})

	t.Run("a record that cannot be written is an error, and the next is tried again", func(t *testing.T) {
		dir := filepath.Join(t.TempDir(), "log")
		os.WriteFile(dir, nil, 0o600) // the queue cannot be made
		_, key, _ := ed25519.GenerateKey(nil)
		l := daemon.OpenLog(dir, "dev_target", key)
		l.Start()
		v := l.Enter(source, s, "")
		if err := v.Write(daemon.Record{Type: "start", Kind: "shell"}); err == nil {
			t.Fatal("a start that was not written returned no error")
		}
		os.Remove(dir)
		if err := v.Write(daemon.Record{Type: "start", Kind: "shell"}); err != nil {
			t.Fatal(err)
		}
		if records, _ := held(t, l); len(records) != 2 || records[0].Type != "boot" || records[1].Type != "start" {
			t.Fatalf("the queue holds %+v", records)
		}
	})
}

func TestLogVisits(t *testing.T) {
	source, s := lease(1)
	t.Run("transports of one source and app share a connection, which its first admitted one connects", func(t *testing.T) {
		l, _ := openLog(t)
		v := l.Enter(source, s, "Codex")
		if again := l.Enter(source, s, "Codex"); again != v {
			t.Fatal("a second transport opened another connection")
		}
		other := l.Enter(source, s, "Claude Code")
		records, _ := held(t, l)
		c := records[1]
		if len(records) != 3 || c.Type != "connect" || c.Visit != v.ID() || len(c.Visit) != 36 || c.Source != "dev_source" || c.App != "Codex" ||
			c.Access != "workspace" || c.Folder != "/srv/app" || c.Grant != 3 || c.SourceGrant != 5 || c.Policy != 1 ||
			records[2].Type != "connect" || records[2].Visit != other.ID() || other == v {
			t.Fatalf("the log holds %+v", records)
		}
		// A source limited to Read only connects with that access, in no folder.
		limited := s
		limited.DeviceID, limited.Limit = "dev_limited", "read_only"
		l.Enter(source, limited, "")
		if records, _ = held(t, l); records[3].Access != "read_only" || records[3].Folder != "~" || records[3].App != "" {
			t.Fatalf("a limited source connects as %+v", records[3])
		}
		// One limited to the AI models connects with that access, whatever the machine's mode.
		limited.DeviceID, limited.Limit = "dev_models", "models"
		l.Enter(source, limited, "Models")
		if records, _ = held(t, l); records[4].Access != "models" || records[4].Folder != "~" || records[4].App != "Models" {
			t.Fatalf("a source limited to AI models connects as %+v", records[4])
		}
	})

	t.Run("a refusal is recorded once for its stage and reason, in a connection a later transport connects", func(t *testing.T) {
		l, _ := openLog(t)
		l.Refuse("dev_source", "Codex", "user", "log in as dana; other users are refused")
		l.Refuse("dev_source", "Codex", "user", "log in as dana; other users are refused")
		l.Refuse("dev_source", "Codex", "unavailable", strings.Repeat("é", 150))
		l.Refuse("", "Codex", "user", "a transport of no source")
		v := l.Enter(source, s, "Codex")
		v.Refuse("channel", "busy", "too many channels on this connection")
		v.Refuse("channel", "busy", "too many channels on this connection")
		records, _ := held(t, l)
		if got := types(t, l); got != "refuse usertransport, refuse unavailabletransport, connect, refuse busychannel" {
			t.Fatalf("the log holds %s", got)
		}
		if r := records[2]; records[1].Visit != v.ID() || records[1].App != "Codex" || r.Message != strings.Repeat("é", 100) {
			t.Fatalf("the refusals are %+v", records[1:3])
		}
	})

	t.Run("a connection ends 10 minutes after its last transport closed, unless one returned", func(t *testing.T) {
		l, _ := openLog(t)
		v := l.Enter(source, s, "")
		l.Enter(source, s, "")
		v.Leave()
		l.Expire(time.Now().Add(time.Hour)) // a transport is still open
		v.Leave()
		left := time.Now()
		l.Expire(left.Add(9 * time.Minute))
		if again := l.Enter(source, s, ""); again != v {
			t.Fatal("a transport that returned within 10 minutes opened another connection")
		}
		l.Expire(left.Add(time.Hour))
		v.Leave()
		left = time.Now()
		l.Expire(left.Add(9 * time.Minute))
		if got := types(t, l); got != "connect" {
			t.Fatalf("before its 10 minutes the log holds %s", got)
		}
		l.Expire(left.Add(10*time.Minute + time.Second))
		l.Expire(left.Add(time.Hour))
		records, _ := held(t, l)
		last, err := time.Parse("2006-01-02T15:04:05.000Z", records[2].Last)
		if got := types(t, l); got != "connect, disconnect done" || err != nil || last.Sub(left).Abs() > time.Second || records[2].Visit != v.ID() {
			t.Fatalf("the log holds %s, last %s", got, records[2].Last)
		}
		if again := l.Enter(source, s, ""); again == v {
			t.Fatal("a transport joined a connection that had ended")
		}
	})

	t.Run("a lease that no longer admits a connection ends it, as going offline does", func(t *testing.T) {
		l, _ := openLog(t)
		v := l.Enter(source, s, "Codex")
		l.Refuse("dev_other", "", "changed", "the connecting computer may not reach the target")
		l.Close("access", source) // the lease it was admitted under
		changed, _ := lease(2)
		if again := l.Enter(changed, s, "Codex"); again == v { // a transport under another binding
			t.Fatal("a transport joined a connection of another binding")
		}
		l.Enter(source, daemon.Source{DeviceID: "dev_third", NodeKey: "nodekey:t", GrantRevision: 1}, "")
		l.Close("access", changed) // which no longer lists dev_third
		l.Close("offline", nil)
		l.Close("offline", nil)
		if got := types(t, l); got != "connect, refuse changedtransport, disconnect access, connect, connect, disconnect access, disconnect offline" {
			t.Fatalf("the log holds %s", got)
		}
		// The connection that only refusals opened is left to its 10 minutes.
		l.Expire(time.Now().Add(11 * time.Minute))
		if got := types(t, l); !strings.HasSuffix(got, "disconnect offline, disconnect done") {
			t.Fatalf("the log holds %s", got)
		}
	})

	t.Run("a start ends each connection the last run left open", func(t *testing.T) {
		l, dir := openLog(t)
		v := l.Enter(source, s, "Codex")
		ended := l.Enter(source, s, "Zed")
		ended.Leave()
		l.Expire(time.Now().Add(time.Hour))
		_, key, _ := ed25519.GenerateKey(nil)
		l = daemon.OpenLog(dir, "dev_target", key)
		records, _ := held(t, l)
		if got, r := types(t, l), records[len(records)-1]; got != "connect, connect, disconnect done, boot, disconnect restart" ||
			r.Visit != v.ID() || r.Source != "dev_source" || r.Last == "" || r.N != 6 {
			t.Fatalf("the log holds %s, at last %+v", got, r)
		}
		if b, _ := os.ReadFile(filepath.Join(dir, "open.json")); strings.TrimSpace(string(b)) != "[]" {
			t.Fatalf("open.json holds %s", b)
		}
	})

	t.Run("a start that cannot open the queue yet still ends what the last run left open, however many", func(t *testing.T) {
		l, dir := openLog(t)
		v := l.Enter(source, s, "Codex")
		left := []map[string]string{{"id": v.ID(), "source": "dev_source"}}
		for len(left) < 1200 { // more than 64 KiB of open.json
			left = append(left, map[string]string{"id": fmt.Sprintf("vis_%032d", len(left)), "source": "dev_source"})
		}
		b, _ := json.Marshal(left)
		if err := os.WriteFile(filepath.Join(dir, "open.json"), b, 0o600); err != nil {
			t.Fatal(err)
		}
		queue := filepath.Join(dir, "queue.json")
		os.Rename(queue, queue+".held")
		os.Mkdir(queue, 0o700) // the queue cannot be read
		_, key, _ := ed25519.GenerateKey(nil)
		l = daemon.OpenLog(dir, "dev_target", key)
		live := l.Enter(source, s, "Zed")
		os.Remove(queue)
		os.Rename(queue+".held", queue)
		if err := live.Write(daemon.Record{Type: "start", Kind: "shell"}); err != nil {
			t.Fatal(err)
		}
		// The last run's boot and connect, then this run's boot, an end for each, and the live connection's start.
		records, _ := held(t, l)
		if r := records[3]; r.Type != "disconnect" || r.Reason != "restart" || r.Visit != v.ID() {
			t.Fatalf("the first connection ended is %+v", r)
		}
		l.Logged(records[0].Queue, 2+1+1199)
		if records, _ = held(t, l); len(records) != 2 || records[0].Visit != left[1199]["id"] || records[1].Type != "start" || records[1].Visit != live.ID() {
			t.Fatalf("the log ends with %+v", records)
		}
	})

	t.Run("an open.json that cannot be read ends nothing, and stops nothing", func(t *testing.T) {
		l, dir := openLog(t)
		l.Enter(source, s, "Codex")
		os.WriteFile(filepath.Join(dir, "open.json"), []byte("[{"), 0o600)
		_, key, _ := ed25519.GenerateKey(nil)
		l = daemon.OpenLog(dir, "dev_target", key)
		if err := l.Enter(source, s, "Zed").Write(daemon.Record{Type: "start", Kind: "shell"}); err != nil {
			t.Fatal(err)
		}
		if got := types(t, l); got != "connect, boot, connect, start" {
			t.Fatalf("the log holds %s", got)
		}
	})

	t.Run("a recorded session is told once for each text, a shell each time, sftp never", func(t *testing.T) {
		l, _ := openLog(t)
		v := l.Enter(source, s, "")
		var got []string
		for _, c := range [][2]string{{"exec", "one"}, {"exec", "one"}, {"shell", "one"}, {"shell", "one"}, {"sftp", "one"}, {"exec", ""}, {"exec", "two"}, {"exec", "two"}, {"exec", "one"}} {
			got = append(got, v.Notice(c[0], c[1]))
		}
		if want := []string{"one\r\n", "", "one\r\n", "one\r\n", "", "", "two\r\n", "", ""}; !slices.Equal(got, want) {
			t.Fatalf("the notices are %q, want %q", got, want)
		}
	})
}

func TestLogSending(t *testing.T) {
	c, e, _ := start(t)
	p := c.presence()
	l := daemon.Lease{Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(`{"revision":1,"mode":"full_access","folders":[]}`), Record: json.RawMessage("null")}}
	// next reads frames until the next log frame, answering a new presence's first renewal with the lease and the
	// later ones as renewed.
	var size int // of the frame read last
	fresh := false
	next := func(p *presence, wait time.Duration) map[string]any {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), wait)
		defer cancel()
		p.conn.SetReadLimit(1 << 20)
		for {
			var m map[string]any
			_, b, err := p.conn.Read(ctx)
			if size = len(b); err != nil || json.Unmarshal(b, &m) != nil {
				t.Fatalf("no log frame: %v", err)
			}
			switch m["type"] {
			case "log":
				return m
			case "renew":
				if fresh {
					p.lease(int64(m["seq"].(float64)), "r1", 60000, l)
				} else {
					p.write(map[string]any{"type": "renewed", "seq": m["seq"], "ttl_ms": 60000})
				}
				fresh = false
			}
		}
	}
	p.lease(p.renewal(), "r1", 60000, l)
	e.want(t, "apply r1")
	first := next(p, 5*time.Second)
	queue := first["queue"].(string)
	if records := first["records"].([]any); first["from"] != 1.0 || len(records) != 1 || records[0].(map[string]any)["type"] != "boot" || len(first) != 4 {
		t.Fatalf("the first frame is %v", first)
	}
	// One frame at a time: a record written meanwhile waits, and an unanswered frame is sent again after 30 seconds.
	source, s := lease(1)
	e.log.Enter(source, s, "")
	sent := time.Now()
	again := next(p, 40*time.Second)
	if waited := time.Since(sent); again["from"] != 1.0 || len(again["records"].([]any)) != 2 || waited < 25*time.Second {
		t.Fatalf("after %v the frame sent again is %v", waited, again)
	}
	// A new presence sends again from the first place not confirmed, before its first renewal or after.
	p.conn.CloseNow()
	select {
	case conn := <-c.conns:
		t.Cleanup(func() { conn.CloseNow() })
		p = &presence{t, conn}
	case <-time.After(20 * time.Second):
		t.Fatal("no new presence")
	}
	fresh = true
	if again = next(p, 5*time.Second); again["from"] != 1.0 || len(again["records"].([]any)) != 2 {
		t.Fatalf("a new presence sends %v", again)
	}
	p.write(map[string]any{"type": "logged", "queue": queue, "through": 1})
	if after := next(p, 5*time.Second); after["from"] != 2.0 || len(after["records"].([]any)) != 1 {
		t.Fatalf("after a confirmation the frame is %v", after)
	}
	// A frame travels as the queue holds it, so its bound is what Cloud receives: no character grows on the way.
	v := e.log.Enter(source, s, "")
	for range 3 {
		v.Write(daemon.Record{Type: "start", Session: strings.Repeat("a", 24), Kind: "exec", Script: strings.Repeat("&<>", 5000)})
	}
	p.write(map[string]any{"type": "logged", "queue": queue, "through": 2})
	if scripts := next(p, 5*time.Second); scripts["from"] != 3.0 || len(scripts["records"].([]any)) != 3 || size > 64<<10 {
		t.Fatalf("three scripts travel in a frame of %d bytes from %v", size, scripts["from"])
	}
}

func TestApp(t *testing.T) {
	type proc struct {
		parent int
		name   string
		args   []string
	}
	app := func(tree map[int]proc) string {
		return daemon.App(100, func(pid int) (int, string, []string) { p := tree[pid]; return p.parent, p.name, p.args })
	}
	ssh := proc{parent: 99, name: "ssh", args: []string{"ssh", "codex"}} // its own arguments name no app
	for want, tree := range map[string]map[int]proc{
		"Codex":       {100: ssh, 99: {98, "sh", []string{"sh", "-c", "ssh m.om ls"}}, 98: {1, "codex", []string{"codex"}}},
		"Claude Code": {100: ssh, 99: {98, "node", []string{"node", "/usr/local/bin/claude"}}, 98: {1, "Code", nil}},
		"Cursor":      {100: ssh, 99: {98, "zsh", nil}, 98: {1, "Cursor Helper (Plugin)", []string{"/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/Contents/MacOS/Cursor Helper (Plugin)"}}},
		"VS Code":     {100: ssh, 99: {98, "bash", []string{"/bin/bash"}}, 98: {1, "exe", []string{"/usr/share/code/code-insiders", "--type=utility"}}},
		"Terminal":    {100: ssh, 99: {98, "zsh", []string{"-zsh"}}, 98: {1, "Terminal", []string{"/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"}}},
	} {
		if got := app(tree); got != want {
			t.Errorf("the app of %v is %s, want %s", tree, got, want)
		}
	}
	// Only the 8 processes above ssh are read.
	deep := map[int]proc{100: ssh, 91: {1, "codex", nil}}
	for pid := 99; pid > 91; pid-- {
		deep[pid] = proc{parent: pid - 1, name: "sh"}
	}
	if got := app(deep); got != "Terminal" {
		t.Errorf("the ninth process above ssh named %s", got)
	}
	deep[92] = proc{parent: 91, name: "aider"}
	if got := app(deep); got != "Aider" {
		t.Errorf("the eighth process above ssh named %s", got)
	}

	for stream, want := range map[string][2]string{
		"OM-APP Claude Code\r\nSSH-2.0-OpenSSH_9.9\r\n": {"Claude Code", "SSH-2.0-OpenSSH_9.9\r\n"},
		"SSH-2.0-OpenSSH_9.9\r\n":                       {"", "SSH-2.0-OpenSSH_9.9\r\n"},
		"OM-APP bad/name\r\nSSH-2.0-OpenSSH_9.9\r\n":    {"", "SSH-2.0-OpenSSH_9.9\r\n"},
		"OM-APP \r\nSSH-2.0-OpenSSH_9.9\r\n":            {"", "SSH-2.0-OpenSSH_9.9\r\n"},
		"OM-APP " + strings.Repeat("a", 41) + "\r\nSSH": {"", "SSH"},
		"OM-APP " + strings.Repeat("a", 80) + "\r\nSSH": {"", strings.Repeat("a", 23) + "\r\nSSH"},
	} {
		r := bufio.NewReaderSize(strings.NewReader(stream), 64)
		got := daemon.ReadApp(r)
		if rest, _ := io.ReadAll(r); got != want[0] || string(rest) != want[1] {
			t.Errorf("%q names %q and leaves %q, want %q", stream, got, rest, want)
		}
	}
}
