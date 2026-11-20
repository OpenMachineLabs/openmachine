package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net"
	"sync"
	"time"
)

// The pace of a person's GitHub on team machines; tests shorten them.
var (
	HoldEvery     = 2 * time.Minute  // a held session is posted again
	HoldGrace     = time.Minute      // a session outlasts its last dial
	HoldWait      = 5 * time.Second  // a connected dial waits for its session's post
	RequestsEvery = 10 * time.Second // the person's requests are read at most
)

// Holds keeps the person's GitHub session open on each team's machine this
// computer dials, through post: an opening post alongside the first dial, again
// every HoldEvery while a dial lasts, and a closing one once none has for
// HoldGrace. After held: false it posts nothing for that machine until Changed.
type Holds struct {
	post    func(target string, open bool) (held bool, err error)
	mu      sync.Mutex
	targets map[string]*hold
}

type hold struct {
	dials   int
	idle    time.Time     // when the last dial ended
	posting chan struct{} // closed when the post in flight ends; nil when none is
	held    bool          // the last opening post answered held: true
	open    bool          // an opening post may have opened a session the closing one has not ended
	refused bool          // answered held: false
	again   bool          // Changed while a post was in flight
	next    time.Time     // the earliest next opening post: HoldEvery after the last one's answer
}

var posted = func() chan struct{} { c := make(chan struct{}); close(c); return c }()

// NewHolds returns Holds that post through post; Run paces them.
func NewHolds(post func(target string, open bool) (bool, error)) *Holds {
	return &Holds{post: post, targets: map[string]*hold{}}
}

// Dial counts a dial to the team's machine target until end. The channel
// closes when its session's post in flight, if any, has ended.
func (h *Holds) Dial(target string) (<-chan struct{}, func()) {
	h.mu.Lock()
	defer h.mu.Unlock()
	t := h.targets[target]
	if t == nil {
		t = &hold{}
		h.targets[target] = t
	}
	t.dials++
	if !t.held && !t.refused && t.posting == nil && !time.Now().Before(t.next) {
		h.send(target, t, true)
	}
	wait := posted
	if t.posting != nil {
		wait = t.posting
	}
	var once sync.Once
	return wait, func() {
		once.Do(func() {
			h.mu.Lock()
			defer h.mu.Unlock()
			if t.dials--; t.dials == 0 {
				t.idle = time.Now()
			}
		})
	}
}

// Changed lifts every held: false and posts again for each machine a dial lasts to.
func (h *Holds) Changed() {
	h.mu.Lock()
	defer h.mu.Unlock()
	for target, t := range h.targets {
		t.refused = false
		if t.posting != nil {
			t.again = true
		} else if t.dials > 0 {
			h.send(target, t, true)
		}
	}
}

// Run posts what is due each second until ctx ends.
func (h *Holds) Run(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		h.mu.Lock()
		now := time.Now()
		for target, t := range h.targets {
			gone := t.dials == 0 && now.Sub(t.idle) >= HoldGrace
			switch {
			case t.posting != nil:
			case t.dials > 0 && !t.refused && !now.Before(t.next):
				h.send(target, t, true)
			case gone && t.open:
				h.send(target, t, false)
			case gone && !t.refused:
				delete(h.targets, target)
			}
		}
		h.mu.Unlock()
	}
}

// send posts for target under h.mu.
func (h *Holds) send(target string, t *hold, open bool) {
	done := make(chan struct{})
	t.posting = done
	t.open = t.open || open
	go func() {
		held, err := h.post(target, open)
		h.mu.Lock()
		defer h.mu.Unlock()
		switch {
		case !open: // a session starts again with the next dial, a minute or more after this one started
			t.held, t.open, t.next = false, false, time.Time{}
		case err != nil || held:
			t.held, t.next = held, time.Now().Add(HoldEvery)
		default:
			t.held, t.open, t.refused = false, false, true
		}
		t.posting = nil
		close(done)
		if t.again {
			t.again, t.refused = false, false
			if t.dials > 0 {
				h.send(target, t, true)
			}
		}
	}()
}

// hold counts a socket dial to device until end: for a team's machine the view
// lists, in d.holds. The channel closes when the dial may answer ok.
func (d *daemon) hold(device string) (<-chan struct{}, func()) {
	for _, v := range d.authority.Devices() {
		if v.DeviceID == device && v.Team != nil {
			return d.holds.Dial(device)
		}
	}
	return posted, func() {}
}

// Changed holds sessions anew and reads the person's GitHub requests.
func (d *daemon) Changed() {
	d.holds.Changed()
	d.readRequests(false)
}

// githubRequests is the reading of the person's GitHub requests for Desktop.
type githubRequests struct {
	reading  sync.Mutex // reads run one at a time
	mu       sync.Mutex
	watchers int         // watch clients connected
	last     time.Time   // when the last read began
	timer    *time.Timer // the read scheduled; nil when none is
	after    int64       // Cloud's now at the last read; 0 before the first
}

// watching counts a watch client until the returned func; the first reads at once.
func (d *daemon) watching() func() {
	g := &d.requests
	g.mu.Lock()
	g.watchers++
	first := g.watchers == 1
	g.mu.Unlock()
	if first {
		d.readRequests(true)
	}
	return func() {
		g.mu.Lock()
		g.watchers--
		g.mu.Unlock()
	}
}

// unread reads the requests when none has been read yet, as when the view's
// account arrived after its watch client.
func (d *daemon) unread() {
	d.requests.mu.Lock()
	none := d.requests.after == 0
	d.requests.mu.Unlock()
	if none {
		d.readRequests(false)
	}
}

// readRequests reads the person's requests while a watch client is connected
// and the view's account is a person: at once when now, else no sooner than
// RequestsEvery after the last read, one read serving every trigger before it.
func (d *daemon) readRequests(now bool) {
	g := &d.requests
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.watchers == 0 || !d.authority.Person() {
		return
	}
	wait := time.Until(g.last.Add(RequestsEvery))
	if now || wait < 0 {
		wait = 0
	}
	if g.timer != nil && (wait > 0 || !g.timer.Stop()) {
		return // the read scheduled serves this trigger
	}
	g.timer = time.AfterFunc(wait, d.readRequestsNow)
}

func (d *daemon) readRequestsNow() {
	g := &d.requests
	g.reading.Lock()
	defer g.reading.Unlock()
	g.mu.Lock()
	g.timer, g.last = nil, time.Now()
	after := g.after
	g.mu.Unlock()
	var r struct {
		Now      int64             `json:"now"`
		Requests []map[string]any  `json:"requests"`
		Notices  []json.RawMessage `json:"notices"`
	}
	if err := d.post(context.Background(), "github/requests", map[string]any{"v": 1, "after": after}, &r); err != nil {
		log.Printf("native: reading the person's GitHub requests: %v", err)
		return
	}
	now := time.Now().Unix()
	for _, req := range r.Requests {
		left, _ := req["expires_in"].(float64)
		delete(req, "expires_in")
		req["expires_at"] = now + int64(left)
	}
	if r.Requests == nil {
		r.Requests = []map[string]any{}
	}
	if after == 0 || r.Notices == nil { // the first read takes notices from its own moment
		r.Notices = []json.RawMessage{}
	}
	view, _ := json.Marshal(map[string]any{"requests": r.Requests, "notices": r.Notices})
	d.authority.GitHub(view)
	g.mu.Lock()
	g.after = r.Now
	g.mu.Unlock()
}

// decide answers the socket line github {"request","decision","choice"} with
// Cloud's {result} or {error: {code, message}}, within 35 seconds, then reads
// the requests at once.
func (d *daemon) decide(uc *net.UnixConn, line string) []byte {
	uc.SetDeadline(time.Now().Add(35 * time.Second))
	var c struct {
		Request  string `json:"request"`
		Decision string `json:"decision"`
		Choice   string `json:"choice"`
	}
	var reply struct {
		Result string `json:"result"`
	}
	err := json.Unmarshal([]byte(line), &c)
	if err == nil {
		body := map[string]any{"v": 1, "request": c.Request, "decision": c.Decision}
		if c.Choice != "" {
			body["choice"] = c.Choice
		}
		err = d.post(context.Background(), "github/decide", body, &reply)
		d.readRequests(true)
	}
	var out any = reply
	var ce *cloudError
	switch {
	case errors.As(err, &ce):
		out = map[string]any{"error": map[string]string{"code": ce.Code, "message": ce.Message}}
	case err != nil:
		out = map[string]any{"error": map[string]string{"code": "cloud_unavailable", "message": err.Error()}}
	}
	b, _ := json.Marshal(out)
	return append(b, '\n')
}
