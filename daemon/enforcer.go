package daemon

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"slices"
)

// binding is the authority one piece of Code work was admitted under: its
// source's grant revision and limit, and this target's grant and policy
// revisions. The Code epoch is not
// part of it, since every Cloud reconnect takes a new one.
type binding struct {
	Source              string `json:"source"`
	SourceGrantRevision int64  `json:"source_grant_revision"`
	Limit               string `json:"limit,omitempty"` // the source's
	TargetGrant         string `json:"target_grant_id"`
	TargetGrantRevision int64  `json:"target_grant_revision"`
	PolicyRevision      int64  `json:"policy_revision"`
}

// policy is this target's Cloud device policy.
type policy struct {
	Revision int64    `json:"revision"`
	Mode     string   `json:"mode"`
	Folders  []string `json:"folders"`
}

// parsePolicy reads a policy whose folders are 1-32 for workspace and none otherwise;
// Core checks the folders themselves.
func parsePolicy(raw json.RawMessage) (*policy, error) {
	var p policy
	if json.Unmarshal(raw, &p) != nil || p.Revision < 1 ||
		(p.Mode != "read_only" && p.Mode != "workspace" && p.Mode != "full_access") ||
		(p.Mode == "workspace") != (len(p.Folders) > 0) || len(p.Folders) > 32 {
		return nil, errors.New("Cloud's policy for the target is unavailable")
	}
	return &p, nil
}

// bindingOf returns the binding that a current lease l gives new work from
// source s, and the policy its sessions run under: the target's, or Read only
// for a source limited to it.
func bindingOf(l *Lease, s Source) (binding, *policy, error) {
	p, err := parsePolicy(l.Target.Policy)
	if err != nil {
		return binding{}, nil, err
	}
	b := binding{s.DeviceID, s.GrantRevision, s.Limit, l.Target.GrantID, l.Target.GrantRevision, p.Revision}
	if s.Limit == "read_only" {
		p = &policy{Revision: p.Revision, Mode: "read_only"}
	}
	return b, p, nil
}

// permits reports whether l still permits work bound to b.
func permits(l *Lease, b binding) bool {
	if l.Target == nil || l.Target.GrantID != b.TargetGrant || l.Target.GrantRevision != b.TargetGrantRevision {
		return false
	}
	// A policy Cloud could not read admits nothing new but closes nothing: existing sessions only.
	if p, err := parsePolicy(l.Target.Policy); err == nil && p.Revision != b.PolicyRevision {
		return false
	}
	i := slices.IndexFunc(l.Sources, func(s Source) bool { return s.DeviceID == b.Source })
	return i >= 0 && l.Sources[i].GrantRevision == b.SourceGrantRevision && l.Sources[i].Limit == b.Limit
}

// lifetime is one Core lifetime: all the Code work of one source under one
// binding, so later SSH sessions see earlier detached jobs. Only its ID and
// binding are kept, never commands or output.
type lifetime struct {
	ID      string  `json:"id"`
	Binding binding `json:"binding"`
}

type lifetimesFile struct {
	Format    int         `json:"format"`
	Lifetimes []*lifetime `json:"lifetimes"`
}

// maxLifetimes bounds lifetimes.json, about 10,000 lifetimes.
const maxLifetimes = 4 << 20

// Apply enforces l: before it returns, it closes the
// connections l no longer permits, with their channels and forwards, and drops
// their lifetimes, so no later session joins them. One network filter gates
// connections in and out, so it follows the sources and the hosts of the lease
// applied last; revalidating it closes the streams out
// to devices that left the lease.
func (d *daemon) Apply(l Lease) {
	allowed := map[string]bool{}
	for _, s := range l.Sources {
		allowed[s.NodeKey] = true
	}
	for _, h := range l.Hosts {
		allowed[h.NodeKey] = true
	}
	d.allowed.Store(&allowed)
	d.mu.Lock()
	d.applied = &l
	d.log.Close("access", &l)
	var closing []*conn
	for c := range d.conns {
		// A connection from before the log started has no place in it: it closes, and the next is recorded.
		if !permits(&l, c.binding) || c.visit == nil && l.Target.Record != nil {
			closing = append(closing, c)
			delete(d.conns, c)
		}
	}
	dropped := false
	for b := range d.lifetimes {
		if !permits(&l, b) {
			delete(d.lifetimes, b)
			dropped = true
		}
	}
	if dropped {
		d.saveLifetimes()
	}
	d.mu.Unlock()
	for _, c := range closing {
		c.ssh.Close()
	}
	if node := d.node.Load(); node != nil {
		go node.Revalidate()
	}
	go d.writeConfig()
}

// lifetimeFor returns the lifetime new work on c runs in, starting one if
// needed. It re-checks c, so no launch begins once a change or going offline
// has closed c; a launch already past this check keeps running.
func (d *daemon) lifetimeFor(c *conn) (*lifetime, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if err := d.admissible(c); err != nil {
		return nil, err
	}
	b := c.binding
	if lt := d.lifetimes[b]; lt != nil {
		return lt, nil
	}
	lt := &lifetime{ID: "c" + newID(), Binding: b}
	d.lifetimes[b] = lt
	if err := d.saveLifetimes(); err != nil {
		delete(d.lifetimes, b)
		return nil, err
	}
	return lt, nil
}

// newID returns a random identifier; lifetime IDs are never reused.
func newID() string {
	b := make([]byte, 12)
	rand.Read(b)
	return hex.EncodeToString(b)
}

// retire drops a lifetime Core refused, so later work uses a new one.
func (d *daemon) retire(lt *lifetime) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lifetimes[lt.Binding] == lt {
		delete(d.lifetimes, lt.Binding)
		d.saveLifetimes()
	}
}

// admissible requires d.mu: new work on c needs c still registered (going
// offline or a change unregisters it) and c's binding to hold under the lease
// applied last, whose policy is readable.
func (d *daemon) admissible(c *conn) error {
	if _, open := d.conns[c]; !open {
		return refused{errors.New("this connection has ended"), "changed"}
	}
	if d.applied == nil || !permits(d.applied, c.binding) {
		return refused{errors.New("Code authority changed; reconnect"), "changed"}
	}
	if _, err := parsePolicy(d.applied.Target.Policy); err != nil {
		return refused{err, "unavailable"}
	}
	return nil
}

// saveLifetimes records the lifetimes that admit sessions; it requires d.mu.
// A restarted daemon drops those its first lease no longer permits.
func (d *daemon) saveLifetimes() error {
	f := lifetimesFile{Format: 1}
	for _, lt := range d.lifetimes {
		f.Lifetimes = append(f.Lifetimes, lt)
	}
	err := d.device.writeJSON("lifetimes.json", f)
	if err != nil {
		log.Printf("native: recording lifetimes: %v", err)
	}
	return err
}

func (d *daemon) loadLifetimes() error {
	var f lifetimesFile
	if err := d.device.readJSON("lifetimes.json", maxLifetimes, &f); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return fmt.Errorf("%w: lifetime metadata is unreadable: %v", errRecovery, err)
	} else if f.Format != 1 || slices.Contains(f.Lifetimes, nil) {
		return fmt.Errorf("%w: lifetime metadata has an unknown format", errRecovery)
	}
	for _, lt := range f.Lifetimes {
		d.lifetimes[lt.Binding] = lt
	}
	return nil
}
