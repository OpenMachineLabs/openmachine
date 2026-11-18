package daemon

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// TestAutoUpdate asks for an update of a headless installation once a day,
// and at once when Cloud refuses this om as too old, then at most once
// per interval; never while auto_update is off or a Mac has no headless.json;
// and it waits while a connection is open.
func TestAutoUpdate(t *testing.T) {
	const every = 100 * time.Millisecond
	// run starts the daemon's update requests on a state with headless, the file's content or none.
	run := func(t *testing.T, headless string, first, daily time.Duration) (asked *atomic.Int32, open *atomic.Bool, refused chan struct{}, state string) {
		state = t.TempDir()
		os.Chmod(state, 0o700)
		if headless != "" {
			os.WriteFile(filepath.Join(state, "headless.json"), []byte(headless), 0o600)
		}
		asked, open, refused = new(atomic.Int32), new(atomic.Bool), make(chan struct{}, 1)
		ctx, cancel := context.WithCancel(context.Background())
		t.Cleanup(cancel)
		go daemon.AutoUpdate(ctx, state, refused, open.Load, func() { asked.Add(1) }, first, daily, every)
		return asked, open, refused, state
	}
	wait := func(t *testing.T, asked *atomic.Int32, n int32) {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); asked.Load() != n; time.Sleep(5 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("asked for %d updates, want %d", asked.Load(), n)
			}
		}
	}

	t.Run("daily, and never without headless.json or with auto_update off", func(t *testing.T) {
		on, _, _, _ := run(t, `{"auto_update":true}`, every, 3*every)
		mac, _, macRefused, _ := run(t, "", every, 3*every)
		off, _, offRefused, _ := run(t, `{"auto_update":false}`, every, 3*every)
		wait(t, on, 1)
		macRefused <- struct{}{}
		offRefused <- struct{}{}
		wait(t, on, 2) // a day later
		if mac.Load() != 0 || off.Load() != 0 {
			t.Fatalf("asked %d (no headless.json), %d (off)", mac.Load(), off.Load())
		}
	})

	t.Run("a refusal asks at once, then once per interval while refused", func(t *testing.T) {
		asked, _, refused, _ := run(t, `{"auto_update":true}`, time.Hour, time.Hour)
		refused <- struct{}{}
		wait(t, asked, 1)
		for end := time.Now().Add(every / 2); time.Now().Before(end); time.Sleep(time.Millisecond) {
			select { // Cloud refuses every attempt, each few seconds
			case refused <- struct{}{}:
			default:
			}
		}
		if asked.Load() != 1 {
			t.Fatalf("asked %d times within one interval", asked.Load())
		}
		time.Sleep(every)
		refused <- struct{}{}
		wait(t, asked, 2)
	})

	t.Run("an open connection makes it wait and look again", func(t *testing.T) {
		asked, open, refused, _ := run(t, `{"auto_update":true}`, time.Hour, time.Hour)
		open.Store(true)
		refused <- struct{}{}
		time.Sleep(3 * every)
		if asked.Load() != 0 {
			t.Fatal("asked for an update while a connection was open")
		}
		open.Store(false)
		wait(t, asked, 1)
	})
}

// TestUpdateRequest starts this machine's update when Cloud asks for it on
// the Code stream, at once or never: only while the machine is online under a
// current lease, and confirmed under the request's ID only once the update
// started.
func TestUpdateRequest(t *testing.T) {
	const id = "0123456789abcdef0123456789abcdef"
	ask := func(p *presence, id string) { p.write(map[string]string{"type": "update", "id": id}) }
	// nothing requires that the machine started and sent nothing since its last
	// frame was read: the next one is the renewal a change notice asks for.
	nothing := func(t *testing.T, e *enforcer, p *presence) int64 {
		t.Helper()
		p.changed()
		seq := p.renewal()
		e.none(t, 100*time.Millisecond)
		return seq
	}
	leased := func(t *testing.T) (*enforcer, *presence) {
		c, e, _ := start(t)
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted())
		e.want(t, "apply r1")
		p.applied(t, "r1")
		return e, p
	}

	t.Run("under a current lease the update starts and is confirmed, each request once", func(t *testing.T) {
		e, p := leased(t)
		e.updates.Store(true)
		for _, id := range []string{id, "ffffffffffffffffffffffffffffffff"} {
			ask(p, id)
			e.want(t, "update")
			if m := p.next(); !reflect.DeepEqual(m, map[string]any{"type": "updating", "id": id}) {
				t.Fatalf("frame %v, want the confirmation of %s", m, id)
			}
		}
		p.write(map[string]any{"type": "renewed", "seq": nothing(t, e, p), "ttl_ms": 60000}) // the session goes on
		nothing(t, e, p)
	})

	t.Run("an update that cannot start is not confirmed", func(t *testing.T) {
		e, p := leased(t)
		ask(p, id)
		e.want(t, "update")
		nothing(t, e, p)
	})

	t.Run("a request before the lease starts nothing, then or once the lease comes", func(t *testing.T) {
		c, e, _ := start(t)
		e.updates.Store(true)
		p := c.presence()
		p.renewal()
		ask(p, id)
		p.lease(nothing(t, e, p), "r1", 60000, granted())
		e.want(t, "apply r1")
		p.applied(t, "r1")
		nothing(t, e, p)
	})

	t.Run("a machine that is offline, denied or past its lease starts nothing", func(t *testing.T) {
		e, p := leased(t)
		e.updates.Store(true)
		e.offline.Store(true)
		ask(p, id)
		seq := nothing(t, e, p)
		e.offline.Store(false)
		p.lease(seq, "denied", 60000, daemon.Lease{})
		e.want(t, "apply denied")
		p.applied(t, "denied")
		ask(p, id)
		p.lease(nothing(t, e, p), "r2", 300, granted())
		e.want(t, "apply r2")
		p.applied(t, "r2")
		time.Sleep(400 * time.Millisecond) // the lease ran out
		ask(p, id)
		nothing(t, e, p)
	})

	t.Run("a request whose ID is not 32 hex digits starts nothing", func(t *testing.T) {
		e, p := leased(t)
		e.updates.Store(true)
		for _, bad := range []string{"", "0123456789ABCDEF0123456789ABCDEF", id + "0", "../../../../../../../../etc/passwd"} {
			ask(p, bad)
		}
		nothing(t, e, p)
	})

	t.Run("a move starts with its secret and is confirmed, as an update is", func(t *testing.T) {
		const secret = "ommove_0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"
		move := func(p *presence, id, key string) { p.write(map[string]string{"type": "move", "id": id, "key": key}) }
		e, p := leased(t)
		// One that cannot start is not confirmed.
		move(p, id, secret)
		e.want(t, "move "+secret)
		nothing(t, e, p)
		e.updates.Store(true)
		move(p, id, secret)
		e.want(t, "move "+secret)
		if m := p.next(); !reflect.DeepEqual(m, map[string]any{"type": "moving", "id": id}) {
			t.Fatalf("frame %v, want the confirmation of the move", m)
		}
		// Anything but a move's secret and a request ID starts nothing, as does a machine that is offline.
		for _, bad := range [][2]string{{id, ""}, {id, "omkey_0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"}, {id, secret + "x"}, {"", secret}, {id + "0", secret}} {
			move(p, bad[0], bad[1])
		}
		e.offline.Store(true)
		move(p, id, secret)
		nothing(t, e, p)
	})

	t.Run("the daemon starts om update only for a headless installation", func(t *testing.T) {
		var started atomic.Int32
		saved := daemon.StartUpdate
		daemon.StartUpdate = func() bool { started.Add(1); return true }
		t.Cleanup(func() { daemon.StartUpdate = saved })
		c, srv := codeCloud(t)
		home := runDaemon(t, srv.URL, "dev_update", "exit 1")
		p := c.presence()
		p.lease(p.renewal(), "r1", 60000, granted())
		p.applied(t, "r1")
		// A Mac, and a bare om, have no headless.json.
		ask(p, id)
		p.changed()
		p.write(map[string]any{"type": "renewed", "seq": p.renewal(), "ttl_ms": 60000})
		if n := started.Load(); n != 0 {
			t.Fatalf("started %d updates without headless.json", n)
		}
		// With one it starts, whatever auto_update says.
		if err := os.WriteFile(filepath.Join(daemon.StateDir(home), "headless.json"), []byte(`{"auto_update":false}`), 0o600); err != nil {
			t.Fatal(err)
		}
		ask(p, id)
		if m := p.next(); !reflect.DeepEqual(m, map[string]any{"type": "updating", "id": id}) || started.Load() != 1 {
			t.Fatalf("frame %v after %d starts, want one confirmed update", m, started.Load())
		}
		// So does om move, with the secret Cloud sent.
		const secret = "ommove_0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"
		var moved atomic.Value
		savedMove := daemon.StartMove
		daemon.StartMove = func(secret string) bool { moved.Store(secret); return true }
		t.Cleanup(func() { daemon.StartMove = savedMove })
		p.write(map[string]string{"type": "move", "id": id, "key": secret})
		if m := p.next(); !reflect.DeepEqual(m, map[string]any{"type": "moving", "id": id}) || moved.Load() != secret {
			t.Fatalf("frame %v after a move with %v, want one confirmed move", m, moved.Load())
		}
	})
}
