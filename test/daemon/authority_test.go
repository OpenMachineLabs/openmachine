// Package daemon tests the device's Code lease against a scripted Cloud, so
// replies can be delayed, reordered or withheld.
package daemon

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestSignMatchesSharedVector(t *testing.T) {
	var v struct {
		Origin, Method, Path, Challenge, Body, Signature string
		ChallengeID                                      string `json:"challenge_id"`
	}
	b, err := os.ReadFile("../vectors/proof.json")
	if err != nil || json.Unmarshal(b, &v) != nil {
		t.Fatalf("vector: %v", err)
	}
	seed := make([]byte, 32)
	for i := range seed {
		seed[i] = byte(i)
	}
	if got := daemon.Sign(ed25519.NewKeyFromSeed(seed), v.Origin, v.Method, v.Path, v.ChallengeID, v.Challenge, []byte(v.Body)); got != v.Signature {
		t.Fatalf("signature = %s, want %s", got, v.Signature)
	}
}

func TestLease(t *testing.T) {
	granted := func(sources ...string) daemon.Lease {
		l := daemon.Lease{Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(`{"mode":"full_access"}`)}}
		for _, s := range sources {
			l.Sources = append(l.Sources, daemon.Source{DeviceID: s, NodeKey: "nodekey:" + s, GrantRevision: 1})
		}
		return l
	}

	t.Run("a late reply never extends its lease, and a lease running out closes nothing", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		seq := p.renewal()
		time.Sleep(700 * time.Millisecond) // Cloud answers late
		p.lease(seq, "r1", 1000, granted("s"))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		if a.Current() == nil {
			t.Fatal("no authority under a current lease")
		}
		// The lease ran from the renewal, 700 ms before the reply: it ends ~300 ms after it.
		time.Sleep(400 * time.Millisecond)
		if a.Current() != nil {
			t.Fatal("a late reply extended its lease")
		}
		e.none(t, time.Second) // existing connections are not closed
		// A new session asks Cloud and waits through the change it brings.
		admitted := make(chan string, 1)
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			l, err := a.Wait(ctx)
			admitted <- fmt.Sprint(l != nil && l.Revision == "r2", err)
		}()
		e.holding.Store(true)
		p.lease(p.renewal(), "r2", 500, granted("s"))
		e.want(t, "apply r2")
		time.Sleep(100 * time.Millisecond) // Wait keeps waiting while r2 is applied
		e.release <- struct{}{}
		p.applied(t, "r2")
		if got := <-admitted; got != "true <nil>" {
			t.Fatalf("Wait across a change = %s, want r2", got)
		}
		time.Sleep(600 * time.Millisecond) // r2 runs out too
		// Left unanswered, a renewal replaces the session, and meanwhile Cloud
		// is reported unavailable.
		ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
		defer cancel()
		if _, err := a.Wait(ctx); err != context.DeadlineExceeded {
			t.Fatalf("Wait = %v, want it to wait for Cloud", err)
		}
		p.renewal()
		waitFor(t, 30*time.Second, "Cloud reported unavailable", func() bool { _, err := a.Wait(context.Background()); return err == daemon.ErrUnavailable })
		select { // and the session is replaced
		case <-c.conns:
		case <-time.After(15 * time.Second):
			t.Fatal("an unanswered renewal never replaced the session")
		}
	})

	t.Run("losing Cloud admits nothing new and closes nothing; recovery applies changes before admitting", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted("s", "t"))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		p.conn.CloseNow() // Cloud goes away with 60 s left on the lease
		waitFor(t, 2*time.Second, "admission to stop", func() bool { return a.Current() == nil })
		began := time.Now()
		if _, err := a.Wait(context.Background()); err != daemon.ErrUnavailable || time.Since(began) > time.Second {
			t.Fatalf("Wait = %v after %v, want ErrUnavailable at once", err, time.Since(began))
		}
		e.none(t, 300*time.Millisecond) // established connections stay open
		// Cloud returns with a narrower lease: it is applied before anything new is admitted.
		p = c.presence()
		e.holding.Store(true)
		p.lease(p.renewal(), "r2", 60000, granted("s"))
		e.want(t, "apply r2")
		if a.Current() != nil {
			t.Fatal("admitted under a lease still being applied")
		}
		e.release <- struct{}{}
		p.applied(t, "r2")
		if l := a.Current(); l == nil || l.Revision != "r2" {
			t.Fatalf("current lease = %+v, want r2", l)
		}
	})

	t.Run("a renewal left unanswered stops admission on time, whatever renewals follow it", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted("s"))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		p.changed()
		p.renewal() // never answered
		asked := time.Now()
		time.Sleep(15 * time.Second)
		p.changed()
		p.renewal() // a later renewal must not extend the first one's 20 s
		waitFor(t, 10*time.Second, "admission to stop", func() bool { return a.Current() == nil })
		if late := time.Since(asked) - 20*time.Second; late > time.Second {
			t.Fatalf("admission stopped %v after the unanswered renewal's 20 s", late)
		}
	})

	t.Run("replies apply only in the order their renewals were sent", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		first := p.renewal()
		p.changed() // asks for a second renewal at once
		second := p.renewal()
		p.lease(second, "new", 60000, granted("s"))
		e.want(t, "apply new")
		p.applied(t, "new")
		p.lease(first, "old", 60000, granted("s", "revoked"))
		e.none(t, 300*time.Millisecond)
		if l := a.Current(); l == nil || l.Revision != "new" {
			t.Fatalf("current lease = %+v, want revision new", l)
		}
	})

	t.Run("a change is applied before anything new is admitted, and acknowledged once", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		many := []string{"t"} // a full instance's account: more than one default WebSocket frame
		for i := range 400 {
			many = append(many, fmt.Sprintf("device-%04d-0123456789abcdef0123456789", i))
		}
		p.lease(p.renewal(), "r1", 60000, granted(many...))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		// A held lease admits without asking Cloud: the next renewal is the one a change asks for.
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if l, err := a.Wait(ctx); err != nil || l.Revision != "r1" {
			t.Fatalf("Wait = %v, %v; want r1 at once", l, err)
		}
		p.changed()
		if seq := p.renewal(); seq != 2 {
			t.Fatalf("renewal %d after Wait, want 2: Wait asked Cloud while a lease held", seq)
		}
		e.holding.Store(true)
		p.lease(2, "r2", 60000, granted("s"))
		e.want(t, "apply r2")
		if a.Current() != nil {
			t.Fatal("admitted under the old lease while a narrower one was applied")
		}
		e.release <- struct{}{}
		p.applied(t, "r2")
		if l := a.Current(); l == nil || l.Revision != "r2" {
			t.Fatalf("current lease = %+v, want r2", l)
		}
		// The same lease issued again is neither applied nor acknowledged again.
		p.changed()
		p.lease(p.renewal(), "r2", 60000, granted("s"))
		e.none(t, 300*time.Millisecond)
		p.changed()
		if m := p.read(); m["type"] != "renew" {
			t.Fatalf("frame %v after a repeated lease, want the next renewal", m)
		}
	})

	t.Run("a confirmed renewal extends the lease from when it was sent, applying and acknowledging nothing", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 1000, granted("s"))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		time.Sleep(600 * time.Millisecond)
		p.changed()
		p.write(map[string]any{"type": "renewed", "seq": p.renewal(), "ttl_ms": 1000})
		time.Sleep(600 * time.Millisecond) // r1 alone ran out 200 ms ago
		if l := a.Current(); l == nil || l.Revision != "r1" {
			t.Fatalf("current lease = %+v, want r1 extended", l)
		}
		e.none(t, 100*time.Millisecond)
		p.changed()
		if m := p.read(); m["type"] != "renew" {
			t.Fatalf("frame %v after a confirmed renewal, want the next renewal", m)
		}
		waitFor(t, time.Second, "the extended lease to run out", func() bool { return a.Current() == nil })
	})

	t.Run("a lease's entries are read strictly: a field or limit of a later Cloud is never today's permission", func(t *testing.T) {
		c, e, _ := start(t)
		p := c.presence()
		const policy, home = `{"revision":1,"mode":"full_access","folders":[],"later":true}`, "0123456789abcdef0123456789abcdef"
		send := func(revision, target string) *daemon.Lease {
			t.Helper()
			m := frame(p.renewal(), revision, 60000, daemon.Lease{})
			m["target"] = json.RawMessage(target)
			m["sources"] = json.RawMessage(`[{"device_id":"own","node_key":"nodekey:own","grant_revision":1,"allow":["github","later"]},
				{"device_id":"member","node_key":"nodekey:member","grant_revision":2,"allow":[],"limit":"read_only","home":"` + home + `"},
				{"device_id":"bare","node_key":"nodekey:bare","grant_revision":1},
				{"device_id":"models","node_key":"nodekey:models","grant_revision":3,"allow":["models"],"limit":"models"},
				{"device_id":"field","node_key":"nodekey:field","grant_revision":1,"allow":["github"],"later":true},
				{"device_id":"limit","node_key":"nodekey:limit","grant_revision":1,"allow":["github"],"limit":"later"}]`)
			m["hosts"] = json.RawMessage(`[{"device_id":"host","node_key":"nodekey:host","key":"k","user":"u","aliases":["dev-host","gpu-01.acme"]},
				{"device_id":"field","node_key":"nodekey:field","key":"k","user":"u","aliases":["dev-field"],"later":true}]`)
			p.write(m)
			e.want(t, "apply "+revision)
			p.applied(t, revision) // the revision as Cloud sent it, whatever was dropped
			return e.applied.Load()
		}
		// A source or host with an unknown field, and a source with an unknown limit, are dropped. An unknown
		// name in allow is unused, and a source without allow is allowed nothing. The policy is read as before.
		want := daemon.Lease{Epoch: 1, Revision: "r1",
			Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(policy)},
			Sources: []daemon.Source{{DeviceID: "own", NodeKey: "nodekey:own", GrantRevision: 1, Allow: []string{"github", "later"}},
				{DeviceID: "member", NodeKey: "nodekey:member", GrantRevision: 2, Allow: []string{}, Limit: "read_only", Home: home},
				{DeviceID: "bare", NodeKey: "nodekey:bare", GrantRevision: 1},
				{DeviceID: "models", NodeKey: "nodekey:models", GrantRevision: 3, Allow: []string{"models"}, Limit: "models"}}, // a source limited to the AI models
			Hosts: []daemon.Host{{DeviceID: "host", NodeKey: "nodekey:host", Key: "k", User: "u", Aliases: []string{"dev-host", "gpu-01.acme"}}}}
		if got := send("r1", `{"grant_id":"g","grant_revision":1,"policy":`+policy+`}`); !reflect.DeepEqual(*got, want) {
			t.Fatalf("lease applied %+v, target %+v; want %+v", *got, got.Target, want)
		}
		// A target with an unknown field keeps its grant, with a policy that cannot be read: TestLifetimes
		// shows that it closes nothing.
		p.changed()
		want.Revision, want.Target.Policy = "r2", nil
		if got := send("r2", `{"grant_id":"g","grant_revision":1,"policy":`+policy+`,"later":true}`); !reflect.DeepEqual(*got, want) {
			t.Fatalf("lease applied %+v, target %+v; want %+v", *got, got.Target, want)
		}
		// Rules are read with the policy; rules with an unknown field anywhere, an
		// unknown level, a pattern without a token, a token or an into without an alternative, or an allowed
		// command without a word leave the policy unreadable too.
		const rules = `{"head":"h","foot":"f","commands":[{"title":"t","note":"","match":[{"w":[["reboot"]]}]}],` +
			`"files":[{"title":"s","note":"","level":"none","paths":[".env"]}],"writes":[],"uses":[],"copies":["cp"],"lock":null`
		for i, c := range []struct{ rules, policy string }{
			{rules + `}`, policy},
			{rules + `,"later":true}`, ""},
			{strings.Replace(rules, `"title":"t"`, `"title":"t","later":true`, 1) + `}`, ""},
			{strings.Replace(rules, `"w":[["reboot"]]`, `"w":[["reboot"]],"later":true`, 1) + `}`, ""},
			{strings.Replace(rules, `"none"`, `"write"`, 1) + `}`, ""},
			{strings.Replace(rules, `[["reboot"]]`, `[]`, 1) + `}`, ""},
			{strings.Replace(rules, `[["reboot"]]`, `[["reboot"],[]]`, 1) + `}`, ""},
			{strings.Replace(rules, `[["reboot"]]`, `[["reboot"]],"into":[]`, 1) + `}`, ""},
			{strings.Replace(rules, `"lock":null`, `"lock":{"title":"l","note":"","allow":[["ls"],[]]}`, 1) + `}`, ""},
		} {
			p.changed()
			revision := fmt.Sprintf("r%d", i+3)
			got := send(revision, `{"grant_id":"g","grant_revision":1,"policy":`+policy+`,"rules":`+c.rules+`}`)
			if got.Target.GrantID != "g" || string(got.Target.Policy) != c.policy || got.Target.Rules == nil {
				t.Fatalf("%s: target %+v, want the policy %q", c.rules, got.Target, c.policy)
			}
			if stop := got.Target.Rules.Check("exec", "reboot", "/home/me"); i == 0 && (stop == nil || stop.Rule != "t") {
				t.Fatalf("rules read %+v stop reboot with %+v", got.Target.Rules, stop)
			}
		}
	})

	for name, bad := range map[string]func(seq int64) map[string]any{
		"a renewal confirmed before any lease": func(seq int64) map[string]any {
			return map[string]any{"type": "renewed", "seq": seq, "ttl_ms": 60000}
		},
		"a lease without devices": func(seq int64) map[string]any {
			m := frame(seq, "r1", 60000, granted("s"))
			delete(m, "devices")
			return m
		},
	} {
		t.Run(name+" ends the session", func(t *testing.T) {
			c, e, a := start(t)
			p := c.presence()
			p.write(bad(p.renewal()))
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if _, _, err := p.conn.Read(ctx); err == nil || ctx.Err() != nil {
				t.Fatalf("read = %v, want the session closed", err)
			}
			e.none(t, 100*time.Millisecond)
			if a.Current() != nil {
				t.Fatal("authority under a frame outside the protocol")
			}
		})
	}

	t.Run("a denied device closes all its Code connections and admits nothing", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted("s"))
		e.want(t, "apply r1")
		p.applied(t, "r1")
		p.changed()
		p.lease(p.renewal(), "denied", 60000, daemon.Lease{})
		e.want(t, "apply denied")
		p.applied(t, "denied")
		if a.Current() != nil {
			t.Fatal("a denied device kept authority")
		}
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		// Without authority, Wait waits for Cloud's next answer and reports it.
		go func() { p.lease(p.renewal(), "denied", 60000, daemon.Lease{}) }()
		if _, err := a.Wait(ctx); err != daemon.ErrNoAuthority {
			t.Fatalf("Wait = %v, want ErrNoAuthority", err)
		}
	})

	t.Run("the acknowledgement carries the folder check's refusal, and frames are read while it runs", func(t *testing.T) {
		c, e, a := start(t)
		checked := make(chan *daemon.Refusal)
		e.folders = func() *daemon.Refusal { return <-checked }
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted())
		e.want(t, "apply r1")
		// The lease admits while its folders are checked, and Cloud's next frame is read.
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := a.Wait(ctx); err != nil {
			t.Fatalf("Wait during the folder check = %v", err)
		}
		p.changed()
		p.write(map[string]any{"type": "renewed", "seq": p.renewal(), "ttl_ms": 60000})
		checked <- &daemon.Refusal{Code: "folder_missing", Index: 1}
		p.refused(t, "r1", map[string]any{"code": "folder_missing", "index": 1.0})
	})

	t.Run("a lease replaced while folders are checked is neither checked nor acknowledged", func(t *testing.T) {
		c, e, _ := start(t)
		checked := make(chan *daemon.Refusal)
		var checks atomic.Int32
		e.folders = func() *daemon.Refusal { checks.Add(1); return <-checked }
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted())
		e.want(t, "apply r1")
		waitFor(t, 5*time.Second, "r1's folder check", func() bool { return checks.Load() == 1 })
		for _, revision := range []string{"r2", "r3"} {
			p.changed()
			p.lease(p.renewal(), revision, 60000, granted())
			e.want(t, "apply "+revision)
		}
		checked <- &daemon.Refusal{Code: "folder_missing"} // r1's, which no longer holds
		checked <- nil
		p.applied(t, "r3")
		if n := checks.Load(); n != 2 {
			t.Fatalf("%d folder checks, want r1's and r3's", n)
		}
	})

	t.Run("admissions ask Cloud at most once a second, however many there are", func(t *testing.T) {
		c, e, a := start(t)
		p := c.presence()
		var renewals atomic.Int32
		go func() { // Cloud answers each renewal at once: this device has no authority
			for {
				var m map[string]any
				if wsjson.Read(context.Background(), p.conn, &m) != nil {
					return
				}
				if m["type"] == "renew" { // written without t, which may have ended
					renewals.Add(1)
					wsjson.Write(context.Background(), p.conn, frame(int64(m["seq"].(float64)), "denied", 60000, daemon.Lease{}))
				}
			}
		}()
		e.want(t, "apply denied")
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		before := renewals.Load()
		for ctx.Err() == nil {
			a.Wait(ctx)
		}
		if n := renewals.Load() - before; n < 1 || n > 3 {
			t.Fatalf("admissions over 2 s sent %d renewals, want 1 to 3", n)
		}
	})
}

// TestLeaseRecord reads a lease's record: its
// key, null included, starts the log, which is then sent whatever leases follow.
func TestLeaseRecord(t *testing.T) {
	c, e, _ := start(t)
	p := c.presence()
	seq := p.renewal()
	// apply sends a lease whose target has record, if any, and returns it as applied, with the log frame that followed if one is due.
	apply := func(revision, record string, logs bool) (daemon.Lease, map[string]any) {
		t.Helper()
		l := daemon.Lease{Sources: []daemon.Source{{DeviceID: "s", NodeKey: "nodekey:s", GrantRevision: 1}},
			Target: &daemon.Target{GrantID: "g", GrantRevision: 1, Policy: json.RawMessage(`{"revision":1,"mode":"full_access","folders":[]}`)}}
		if l.Target.Record = json.RawMessage(record); record == "" {
			l.Target.Record = nil
		} else if record == "none" {
			l.Target = nil
		}
		p.lease(seq, revision, 60000, l)
		e.want(t, "apply "+revision)
		var frame map[string]any
		frames := 1 // the acknowledgement and, when it has records, their frame, in either order
		if logs {
			frames = 2
		}
		for range frames {
			if m := p.read(); m["type"] == "log" {
				frame = m
			} else if m["type"] != "applied" || m["revision"] != revision {
				t.Fatalf("frame %v", m)
			}
		}
		p.changed()
		seq = p.renewal()
		return *e.applied.Load(), frame
	}
	// A Cloud without logs sends no record: nothing is recorded or sent.
	l, _ := apply("r1", "", false)
	if f, _ := e.log.Frame(); l.Target.Record != nil || l.Target.Policy == nil || e.log.Enter(&l, l.Sources[0], "") != nil || f != nil {
		t.Fatalf("without record the target is %+v, and the log sends %v", l.Target, f)
	}
	l, frame := apply("r2", "null", true)
	if l.Target.Policy == nil || frame["from"] != 1.0 || frame["records"].([]any)[0].(map[string]any)["type"] != "boot" {
		t.Fatalf("with a null record the target is %+v, and the log sends %v", l.Target, frame)
	}
	p.write(map[string]any{"type": "logged", "queue": frame["queue"], "through": 1})
	v := e.log.Enter(&l, l.Sources[0], "")
	if frame = p.read(); frame["from"] != 2.0 || frame["records"].([]any)[0].(map[string]any)["type"] != "connect" {
		t.Fatalf("a connection is sent as %v", frame)
	}
	p.write(map[string]any{"type": "logged", "queue": frame["queue"], "through": 2})
	if l, _ = apply("r3", `{"notice":"Commands are recorded."}`, false); l.Target.Policy == nil || string(l.Target.Record) != `{"notice":"Commands are recorded."}` {
		t.Fatalf("with a notice the target is %+v", l.Target)
	}
	// A record om cannot read is a policy it cannot read: nothing new is admitted.
	if l, _ = apply("r4", `{"notice":"Commands are recorded.","more":1}`, false); l.Target.Policy != nil {
		t.Fatalf("with an unknown field in record the policy is %s", l.Target.Policy)
	}
	// A machine that lost its authority still sends what it records.
	if l, _ = apply("r5", "none", false); l.Target != nil {
		t.Fatalf("the target is %+v", l.Target)
	}
	v.Write(daemon.Record{Type: "end", Session: "s", Error: "launch"})
	if frame = p.read(); frame["from"] != 3.0 || frame["records"].([]any)[0].(map[string]any)["type"] != "end" {
		t.Fatalf("without a target the log sends %v", frame)
	}
}

// enforcer is the device of an Authority under test: it records what the
// Authority asks of it, and is offline while offline is set.
type enforcer struct {
	events  chan string
	holding atomic.Bool // Apply waits for release once
	release chan struct{}
	offline atomic.Bool
	folders func() *daemon.Refusal       // its folder check, if any
	applied atomic.Pointer[daemon.Lease] // the lease it was last given
	updates atomic.Bool                  // its update, or its move, starts
	log     *daemon.Log                  // the device's log
}

func (e *enforcer) Update() bool {
	e.events <- "update"
	return e.updates.Load()
}

func (e *enforcer) Move(secret string) bool {
	e.events <- "move " + secret
	return e.updates.Load()
}

func (e *enforcer) Folders(daemon.Lease) *daemon.Refusal {
	if e.folders == nil {
		return nil
	}
	return e.folders()
}

func (e *enforcer) Admission() error {
	if e.offline.Load() {
		return errors.New("offline")
	}
	return nil
}

func (e *enforcer) Apply(l daemon.Lease) {
	e.applied.Store(&l)
	e.events <- "apply " + l.Revision
	if e.holding.CompareAndSwap(true, false) {
		<-e.release
	}
}

func (e *enforcer) want(t *testing.T, event string) {
	t.Helper()
	select {
	case got := <-e.events:
		if got != event {
			t.Fatalf("enforcer: %s, want %s", got, event)
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("enforcer: no %s", event)
	}
}

func (e *enforcer) none(t *testing.T, d time.Duration) {
	t.Helper()
	select {
	case got := <-e.events:
		t.Fatalf("enforcer: unexpected %s", got)
	case <-time.After(d):
	}
}

// cloud accepts Code presences and hands each to the test.
type cloud struct {
	t     *testing.T
	conns chan *websocket.Conn
	newer atomic.Bool // its Code protocol is newer than this om's
}

// codeCloud starts a Cloud that answers any device's challenge and hands each
// Code presence that names this protocol to the test; while its protocol is
// newer, it refuses them as Cloud does.
func codeCloud(t *testing.T) (*cloud, *httptest.Server) {
	c := &cloud{t: t, conns: make(chan *websocket.Conn, 4)}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == "POST" && strings.HasSuffix(r.URL.Path, "/challenge"):
			w.Write([]byte(`{"challenge_id":"id","challenge":"c"}`))
		case strings.HasSuffix(r.URL.Path, "/code") && c.newer.Load():
			http.Error(w, `{"error":"code_upgrade_required"}`, http.StatusUpgradeRequired)
		case strings.HasSuffix(r.URL.Path, "/code") && r.Header.Get("X-OM-Signature") != "" && r.Header.Get("X-OM-Protocol") == "4":
			conn, err := websocket.Accept(w, r, nil)
			if err != nil {
				return
			}
			c.conns <- conn
			<-r.Context().Done()
		default:
			http.Error(w, "unexpected", http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)
	return c, srv
}

func start(t *testing.T) (*cloud, *enforcer, *daemon.Authority) {
	c, srv := codeCloud(t)
	_, key, _ := ed25519.GenerateKey(nil)
	e := &enforcer{events: make(chan string, 16), release: make(chan struct{}), log: daemon.OpenLog(filepath.Join(t.TempDir(), "log"), "dev", key)}
	a := daemon.NewAuthority(srv.URL, "dev", key, e, e.log, srv.Client())
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go a.Run(ctx)
	return c, e, a
}

type presence struct {
	t    *testing.T
	conn *websocket.Conn
}

func (c *cloud) presence() *presence {
	select {
	case conn := <-c.conns:
		c.t.Cleanup(func() { conn.CloseNow() })
		return &presence{c.t, conn}
	case <-time.After(5 * time.Second):
		c.t.Fatal("no Code presence")
		return nil
	}
}

func (p *presence) read() map[string]any {
	p.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var m map[string]any
	if err := wsjson.Read(ctx, p.conn, &m); err != nil {
		p.t.Fatalf("read: %v", err)
	}
	return m
}

// next reads the next frame other than a renewal. A daemon renews at once whenever the programs listening on
// its computer change, which any process may cause at any time.
func (p *presence) next() map[string]any {
	p.t.Helper()
	for {
		if m := p.read(); m["type"] != "renew" {
			return m
		}
	}
}

// renewal reads the next renewal and returns its sequence number.
func (p *presence) renewal() int64 {
	p.t.Helper()
	m := p.read()
	if m["type"] != "renew" {
		p.t.Fatalf("frame %v, want a renewal", m)
	}
	return int64(m["seq"].(float64))
}

// applied reads the acknowledgment that a lease was applied: what it no longer permits is closed. Its folders,
// this machine's own check of the lease's Workspace folders, are null.
func (p *presence) applied(t *testing.T, revision string) {
	t.Helper()
	p.refused(t, revision, nil)
}

// refused reads the acknowledgment of a lease whose Workspace folders the machine's check refused as folders:
// its code and index, or nil for none.
func (p *presence) refused(t *testing.T, revision string, folders any) {
	t.Helper()
	m := p.next()
	if got, present := m["folders"]; m["type"] != "applied" || m["revision"] != revision || !present || !reflect.DeepEqual(got, folders) || len(m) != 3 {
		t.Fatalf("frame %v, want applied %s with folders %v", m, revision, folders)
	}
}

func (p *presence) write(v any) {
	if err := wsjson.Write(context.Background(), p.conn, v); err != nil {
		p.t.Errorf("write: %v", err)
	}
}

func (p *presence) changed() { p.write(map[string]string{"type": "changed"}) }

func (p *presence) lease(seq int64, revision string, ttlMs int, l daemon.Lease) {
	p.write(frame(seq, revision, ttlMs, l))
}

func frame(seq int64, revision string, ttlMs int, l daemon.Lease) map[string]any {
	l.Epoch, l.Revision = 1, revision
	b, _ := json.Marshal(l)
	var m map[string]any
	json.Unmarshal(b, &m)
	m["type"], m["seq"], m["ttl_ms"], m["devices"] = "lease", seq, ttlMs, []any{}
	return m
}

func waitFor(t *testing.T, timeout time.Duration, what string, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(timeout); !ok(); time.Sleep(50 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
	}
}

// Each renewal carries the machine's listening programs, null before the first pass, and a
// change renews at once.
func TestServicesRenewal(t *testing.T) {
	c, _, a := start(t)
	p := c.presence()
	if m := p.read(); m["type"] != "renew" || m["services"] != nil {
		t.Fatalf("first renewal %v, want services null", m)
	} else if _, present := m["services"]; !present {
		t.Fatal("the first renewal has no services field")
	}
	a.Services(nil) // a pass that found nothing is an empty list, not null
	if m := p.read(); fmt.Sprint(m["services"]) != "[]" {
		t.Fatalf("renewal services %v, want []", m["services"])
	}
	list := []daemon.Service{{Port: 8188, Name: "ComfyUI", Web: true}, {Port: 6379, Name: "redis-server"}}
	a.Services(list)
	if m := p.read(); fmt.Sprint(m["services"]) != "[map[name:ComfyUI port:8188 web:true] map[name:redis-server port:6379 web:false]]" {
		t.Fatalf("renewal services %v, want the list", m["services"])
	}
	a.Services(slices.Clone(list)) // unchanged: no renewal, so the next frame is the change below
	time.Sleep(300 * time.Millisecond)
	a.Services(list[:1])
	if m := p.read(); fmt.Sprint(m["services"]) != "[map[name:ComfyUI port:8188 web:true]]" {
		t.Fatalf("renewal services %v, want the changed list at once", m["services"])
	}
	// A model server carries its models, also when it has none, and a change in them renews.
	models := []daemon.Service{{Port: 11434, Name: "ollama", Models: []daemon.Model{{ID: "qwen3:latest", Chat: true}, {ID: "bge"}}}, {Port: 8000, Name: "python3", Models: []daemon.Model{}}}
	a.Services(models)
	if b, _ := json.Marshal(p.read()["services"]); string(b) != `[{"models":[{"chat":true,"id":"qwen3:latest"},{"chat":false,"id":"bge"}],"name":"ollama","port":11434,"web":false},`+
		`{"models":[],"name":"python3","port":8000,"web":false}]` {
		t.Fatalf("renewal services %s, want each model server's models", b)
	}
	a.Services([]daemon.Service{{Port: 11434, Name: "ollama", Models: models[0].Models[:1]}, models[1]})
	if b, _ := json.Marshal(p.read()["services"]); !strings.Contains(string(b), `"models":[{"chat":true,"id":"qwen3:latest"}],"name":"ollama"`) {
		t.Fatalf("renewal services %s, want the changed models at once", b)
	}
}
