package daemon

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os/exec"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// A machine's models channel, and this computer's address of the models it
// reaches, against model servers that answer with what they were asked.

const address = "127.0.0.1:11400"

// asked is what a model server was asked, as it answers.
type asked struct {
	Server, Method, URL, Host, Origin, Referer, Key, Agent, Connection, Body string
	Length                                                                   int64
}

// models is an entry whose machines and computer have model servers.
type models struct {
	*entry
	studio  *machine
	release chan struct{} // lets a streamed answer end
	ports   map[string]int
}

// newModels starts the model servers of gpu-01.acme (Ollama), studio and this computer, laptop.
func newModels(t *testing.T) *models {
	t.Helper()
	m := &models{entry: newEntry(t), release: make(chan struct{}), ports: map[string]int{}}
	t.Cleanup(func() { close(m.release) })
	for _, name := range []string{"ollama", "python3", "llama-server"} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			w.Header().Set("Access-Control-Allow-Origin", "")
			if strings.Contains(string(body), `"stream":true`) {
				w.Header().Set("Content-Type", "text/event-stream")
				io.WriteString(w, "data: 1\n\n")
				w.(http.Flusher).Flush()
				select {
				case <-m.release:
				case <-r.Context().Done():
				}
				io.WriteString(w, "data: 2\n\n")
				return
			}
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(asked{name, r.Method, r.URL.String(), r.Host, r.Header.Get("Origin"), r.Header.Get("Referer"), r.Header.Get("X-Api-Key"),
				strings.Join(r.Header.Values("User-Agent"), ","), strings.Join(r.Header.Values("Connection"), ","), string(body), r.ContentLength})
		}))
		t.Cleanup(server.Close)
		m.ports[name] = server.Listener.Addr().(*net.TCPAddr).Port
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	m.studio = &machine{ln: ln, signer: m.machine.signer}
	go m.studio.serve()
	server := func(name string, models ...daemon.Model) daemon.Service {
		return daemon.Service{Port: m.ports[name], Name: name, Models: models, Addr: netip.AddrPortFrom(netip.MustParseAddr("127.0.0.1"), uint16(m.ports[name]))}
	}
	m.machine.services = []daemon.Service{{Port: 22, Name: "sshd"}, server("ollama", daemon.Model{ID: "qwen3:latest", Chat: true}, daemon.Model{ID: "shared", Chat: true})}
	m.studio.services = []daemon.Service{server("python3", daemon.Model{ID: "shared", Chat: true}, daemon.Model{ID: "bge"})}
	m.computer.services = []daemon.Service{server("llama-server", daemon.Model{ID: "local.gguf", Chat: true}, daemon.Model{ID: "shared", Chat: true})}
	m.computer.others = map[string]string{"studio": ln.Addr().String()}
	return m
}

// post sends an inference request to the address and returns what the model's server was asked.
func (m *models) post(path, body string, header ...string) asked {
	m.t.Helper()
	res, text := m.do("POST", address, path, body, header...)
	var a asked
	if err := json.Unmarshal([]byte(text), &a); res.StatusCode != http.StatusOK || err != nil {
		m.t.Fatalf("POST %s %s = %d %q", path, body, res.StatusCode, text)
	}
	return a
}

// refused requires the address to answer with status and om's own error.
func (m *models) refused(status int, message, method, host, path, body string, header ...string) {
	m.t.Helper()
	res, text := m.do(method, host, path, body, header...)
	var e struct{ Error struct{ Message string } }
	if json.Unmarshal([]byte(text), &e); res.StatusCode != status || method != "HEAD" && (e.Error.Message != message || res.Header.Get("Content-Type") != "application/json") {
		m.t.Errorf("%s %s%s %.80s = %d %q, want %d %q", method, host, path, body, res.StatusCode, text, status, message)
	}
}

func TestModelsList(t *testing.T) {
	m := newModels(t)
	for _, host := range []string{address, "localhost:11400", "[::1]:11400", "LOCALHOST:11400"} {
		res, text := m.do("GET", host, "/v1/models?limit=1000", "")
		var list any
		json.Unmarshal([]byte(text), &list)
		model := func(id, server string, chat bool) any {
			return map[string]any{"id": id, "object": "model", "owned_by": server, "chat": chat}
		}
		// In alias order, a duplicate bare on the first machine: gpu-01.acme, laptop, studio.
		want := map[string]any{"object": "list", "data": []any{model("qwen3:latest", "ollama", true), model("shared", "ollama", true),
			model("local.gguf", "llama-server", true), model("shared@laptop", "llama-server", true),
			model("shared@studio", "python3", true), model("bge", "python3", false)}}
		if res.StatusCode != http.StatusOK || !reflect.DeepEqual(list, want) || res.Header.Get("Content-Type") != "application/json" {
			t.Fatalf("GET %s/v1/models = %d %s", host, res.StatusCode, text)
		}
	}
	// One connection for each machine's models, opened for Models, and one list in 15 seconds.
	m.computer.set(func() {
		if slices.Sort(m.computer.apps); !slices.Equal(m.computer.apps, []string{"Models gpu-01.acme", "Models studio"}) {
			t.Errorf("the computer opened %q, want one connection for each machine's models", m.computer.apps)
		}
	})
	if m.machine.asked != 1 || m.studio.asked != 1 {
		t.Errorf("4 lists asked the machines %d and %d times, want once each", m.machine.asked, m.studio.asked)
	}
}

// A machine that refuses, or cannot be reached, is left out; the others answer.
func TestModelsListWithoutAMachine(t *testing.T) {
	m := newModels(t)
	m.studio.off = true
	closed, _ := net.Listen("tcp", "127.0.0.1:0")
	closed.Close()
	m.computer.services[0].Addr = netip.MustParseAddrPort(closed.Addr().String()) // nothing listens there
	m.computer.set(func() {
		m.computer.lease.Hosts = append(m.computer.lease.Hosts, daemon.Host{DeviceID: "dev-far", Key: "none", Aliases: []string{"far"}})
	})
	_, text := m.do("GET", address, "/v1/models", "")
	if strings.Contains(text, "studio") || strings.Contains(text, "bge") || !strings.Contains(text, `"qwen3:latest"`) || !strings.Contains(text, `"shared@laptop"`) {
		t.Fatalf("the list without studio = %s", text)
	}
	m.refused(http.StatusNotFound, `No machine this computer reaches has the model "bge".`, "POST", address, "/v1/embeddings", `{"model":"bge"}`)
	// A model is looked for in a new list once, so a machine that answers again is found.
	m.studio.off = false
	if a := m.post("/v1/embeddings", `{"model":"bge"}`); a.Server != "python3" {
		t.Errorf("bge went to %+v, want studio's server", a)
	}
	m.machine.off = true // its models are still in the list this computer keeps
	m.refused(http.StatusBadGateway, "AI models are off for you on this machine", "POST", address, "/v1/chat/completions", `{"model":"qwen3:latest"}`)
	m.refused(http.StatusBadGateway, "This computer can’t reach the model’s machine.", "POST", address, "/v1/chat/completions", `{"model":"local.gguf"}`)
}

func TestModelsRequest(t *testing.T) {
	m := newModels(t)
	body := func(model string) string {
		return `{"messages":[{"model":"x","content":"héllo \u00e9"}],  "model" : ` + model + ` ,"stream":false}`
	}
	for _, c := range []struct{ path, model, server, sent string }{
		{"/v1/chat/completions", `"shared"`, "ollama", `"shared"`}, // the first machine in alias order
		{"/v1/completions", `"shared@studio"`, "python3", `"shared"`},
		{"/v1/responses", `"shared@laptop"`, "llama-server", `"shared"`},
		{"/v1/messages?beta=true", `"QWEN3"`, "ollama", `"qwen3:latest"`}, // as Ollama answers to a name
		{"/v1/v1/messages?beta=true", `"qwen3"`, "ollama", `"qwen3:latest"`},
		{"/v1/messages/count_tokens", `"Qwen3:Latest@gpu-01.acme"`, "ollama", `"qwen3:latest"`},
		{"/v1/embeddings", `"b\u0067e"`, "python3", `"bge"`},
		{"/v1/embeddings", `"BGE@studio"`, "python3", `"bge"`}, // whatever the server: om cannot always name it
		{"/v1/audio/speech", `"local.gguf"`, "llama-server", `"local.gguf"`},
	} {
		a := m.post(c.path, body(c.model), "X-Api-Key", "k", "Referer", "http://127.0.0.1:11400/try", "User-Agent", "app/1")
		want := asked{c.server, "POST", strings.Replace(c.path, "/v1/v1/", "/v1/", 1), fmt.Sprint("localhost:", m.ports[c.server]), "", "", "k", "app/1", "close",
			body(c.sent), int64(len(body(c.sent)))}
		if a != want {
			t.Errorf("%s as %s:\n got %+v\nwant %+v", c.path, c.model, a, want)
		}
	}
	// A body sent in chunks arrives with its length, and an app's own origin is not the server's to see.
	r, w := io.Pipe()
	go func() { io.WriteString(w, `{"model":`); io.WriteString(w, `"shared"}`); w.Close() }()
	req, _ := http.NewRequest("POST", m.url+"/v1/chat/completions", r)
	req.Host = address
	req.Header.Set("Origin", "chrome-extension://abc")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var a asked
	if json.NewDecoder(res.Body).Decode(&a); a.Body != `{"model":"shared"}` || a.Length != 18 || a.Origin != "" || a.Agent != "Go-http-client/1.1" ||
		!slices.Equal(res.Header.Values("Access-Control-Allow-Origin"), []string{"chrome-extension://abc"}) || !slices.Contains(res.Header.Values("Vary"), "Origin") ||
		!slices.Equal(res.Header.Values("Content-Type"), []string{"application/json"}) {
		t.Errorf("a chunked request from an extension reached the server as %+v and answered %v", a, res.Header)
	}
	if got, want := m.machine.forwarded, slices.Repeat([]int{m.ports["ollama"]}, 5); !slices.Equal(got, want) {
		t.Errorf("gpu-01.acme recorded requests to %v, want %v", got, want)
	}
}

// An answer streams as the server writes it.
func TestModelsStream(t *testing.T) {
	m := newModels(t)
	for _, model := range []string{"qwen3", "local.gguf"} {
		req, _ := http.NewRequest("POST", m.url+"/v1/chat/completions", strings.NewReader(`{"model":"`+model+`","stream":true}`))
		req.Host = address
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		first := make(chan string, 1)
		lines := bufio.NewReader(res.Body)
		go func() { line, _ := lines.ReadString('\n'); first <- line }()
		select {
		case line := <-first:
			if line != "data: 1\n" || res.Header.Get("Content-Type") != "text/event-stream" {
				t.Fatalf("%s: the first event = %q as %q", model, line, res.Header.Get("Content-Type"))
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("%s: no event before the answer ended", model)
		}
		m.release <- struct{}{}
		if rest, _ := io.ReadAll(lines); string(rest) != "\ndata: 2\n\n" {
			t.Errorf("%s: the rest = %q", model, rest)
		}
	}
}

// A machine's connection carries 29 requests at once, and its list beside them: with a request on each stream the
// list is still read, and a 30th request waits for the first that ends.
func TestModelsListBesideOpenRequests(t *testing.T) {
	m := newModels(t)
	stream := func() *http.Response {
		req, _ := http.NewRequest("POST", m.url+"/v1/chat/completions", strings.NewReader(`{"model":"qwen3","stream":true}`))
		req.Host = address
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Error(err)
			return nil
		}
		t.Cleanup(func() { res.Body.Close() })
		return res
	}
	first := stream()
	for range 28 {
		stream()
	}
	// A name no machine has makes the address read every list anew.
	m.refused(http.StatusNotFound, `No machine this computer reaches has the model "none".`, "POST", address, "/v1/chat/completions", `{"model":"none"}`)
	if _, list := m.do("GET", address, "/v1/models", ""); !strings.Contains(list, `"qwen3:latest"`) {
		t.Fatalf("with 29 requests open on gpu-01.acme the list is %s, want its models", list)
	}
	next := make(chan *http.Response, 1)
	go func() { next <- stream() }()
	select {
	case <-next:
		t.Fatal("a 30th request did not wait for a stream")
	case <-time.After(500 * time.Millisecond):
	}
	first.Body.Close()
	select {
	case <-next:
	case <-time.After(5 * time.Second):
		t.Fatal("the waiting request got no stream when one ended")
	}
}

// A machine's answer leads nowhere else: the address follows no redirect out of the channel.
func TestModelsListFollowsNoRedirect(t *testing.T) {
	m := newModels(t)
	var asked atomic.Int32
	elsewhere := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked.Add(1)
		io.WriteString(w, `{"object":"list","data":[{"id":"planted"}]}`)
	}))
	defer elsewhere.Close()
	m.studio.answer = "HTTP/1.1 302 Found\r\nLocation: " + elsewhere.URL + "/v1/models\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
	if _, list := m.do("GET", address, "/v1/models", ""); asked.Load() != 0 || strings.Contains(list, "planted") || !strings.Contains(list, `"qwen3:latest"`) {
		t.Fatalf("a machine's redirect was followed %d times; the list is %s", asked.Load(), list)
	}
}

// A machine that gives no answer holds up only the requests that need a new list, and those together.
func TestModelsListWaitsForNoOtherRequest(t *testing.T) {
	m := newModels(t)
	m.do("GET", address, "/v1/models", "")
	m.studio.mu.Lock()
	m.studio.answer = "hold"
	m.studio.mu.Unlock()
	began, missed := time.Now(), make(chan bool, 3)
	for range 3 {
		go func() {
			res, _ := m.do("POST", address, "/v1/chat/completions", `{"model":"none"}`)
			missed <- res.StatusCode == http.StatusNotFound
		}()
	}
	time.Sleep(200 * time.Millisecond)
	if m.post("/v1/chat/completions", `{"model":"local.gguf"}`); time.Since(began) > 2*time.Second {
		t.Errorf("a request for a listed model waited %v behind the list being read", time.Since(began))
	}
	for range 3 {
		if !<-missed {
			t.Error("a name no machine has was not refused")
		}
	}
	if took := time.Since(began); took > 5*time.Second {
		t.Errorf("3 requests for a name no machine has took %v, want one reading of the list among them", took)
	}
}

// A request goes to the address discovery read its model at, never to a name: another program may hold the same
// port on the other loopback address, which localhost names first on many systems.
func TestModelsReachTheServerThatListsThem(t *testing.T) {
	m := newModels(t)
	var other atomic.Int32
	for _, name := range []string{"ollama", "llama-server"} {
		ln, err := net.Listen("tcp", fmt.Sprint("[::1]:", m.ports[name]))
		if err != nil {
			t.Skipf("no [::1]:%d to hold here: %v", m.ports[name], err)
		}
		decoy := &http.Server{Handler: http.HandlerFunc(func(http.ResponseWriter, *http.Request) { other.Add(1) })}
		go decoy.Serve(ln)
		t.Cleanup(func() { decoy.Close() })
	}
	for model, server := range map[string]string{"qwen3": "ollama", "local.gguf": "llama-server"} {
		if a := m.post("/v1/chat/completions", `{"model":"`+model+`"}`); a.Server != server {
			t.Errorf("%s was answered by %q, want %s", model, a.Server, server)
		}
	}
	if n := other.Load(); n != 0 {
		t.Errorf("the program on the other loopback address got %d requests", n)
	}
}

// Apps, not websites, and only inference.
func TestModelsRefusals(t *testing.T) {
	m := newModels(t)
	const website = "Websites can’t use the models here."
	one := `{"model":"shared"}`
	for _, origin := range []string{"https://evil.example", "http://localhost:3000", "http://127.0.0.1:11401", "null", "HTTP://127.0.0.1:11400", "evil"} {
		m.refused(http.StatusForbidden, website, "GET", address, "/v1/models", "", "Origin", origin)
		m.refused(http.StatusForbidden, website, "POST", address, "/v1/chat/completions", one, "Origin", origin)
		m.refused(http.StatusForbidden, website, "OPTIONS", address, "/v1/chat/completions", "", "Origin", origin, "Access-Control-Request-Method", "POST")
	}
	for _, origin := range []string{"http://127.0.0.1:11400", "chrome-extension://abc", "moz-extension://1-2", "safari-web-extension://x", "app://obsidian.md", "tauri://localhost"} {
		res, text := m.do("GET", address, "/v1/models", "", "Origin", origin)
		if res.StatusCode != http.StatusOK || res.Header.Get("Access-Control-Allow-Origin") != origin || res.Header.Get("Vary") != "Origin" {
			t.Errorf("GET /v1/models from %s = %d %v %s", origin, res.StatusCode, res.Header, text)
		}
		res, _ = m.do("OPTIONS", address, "/v1/chat/completions", "", "Origin", origin, "Access-Control-Request-Method", "POST",
			"Access-Control-Request-Headers", "authorization, content-type, x-stainless-os")
		if h := res.Header; res.StatusCode != http.StatusNoContent || h.Get("Access-Control-Allow-Origin") != origin || h.Get("Vary") != "Origin" ||
			h.Get("Access-Control-Allow-Methods") != "POST" || h.Get("Access-Control-Allow-Headers") != "authorization, content-type, x-stainless-os" {
			t.Errorf("OPTIONS from %s = %d %v", origin, res.StatusCode, h)
		}
		// om's own refusals carry the origin's answer too, so the app reads them.
		res, _ = m.do("POST", address, "/v1/files", one, "Origin", origin)
		if res.StatusCode != http.StatusNotFound || res.Header.Get("Access-Control-Allow-Origin") != origin {
			t.Errorf("POST /v1/files from %s = %d %v", origin, res.StatusCode, res.Header)
		}
	}
	const none, bad = "Not found.", `Send a JSON object with one "model", a string.`
	for _, c := range [][3]string{{"GET", "/v1/", ""}, {"GET", "/v1/models/shared", ""}, {"POST", "/v1/models", one}, {"DELETE", "/v1/models", ""},
		{"GET", "/v1/chat/completions", ""}, {"PUT", "/v1/chat/completions", one}, {"POST", "/v1/chat/completions/", one}, {"POST", "/v1/files", one},
		{"POST", "/v1/audio/transcriptions", one}, {"POST", "/v1/responses/resp_1/cancel", one}, {"POST", "/v1/../api/pull", one}, {"OPTIONS", "/v1/chat/completions", ""},
		{"POST", "/v1/api/pull", one}, {"POST", "/v1/v1/v1/chat/completions", one}} {
		for _, host := range []string{address, "localhost:11400", "[::1]:11400"} {
			m.refused(http.StatusNotFound, none, c[0], host, c[1], c[2])
		}
	}
	// Outside /v1/ the port is the entry of pages: nothing there reaches a model server, from an app or a website.
	for _, c := range [][3]string{{"HEAD", "/api/hello", ""}, {"GET", "/v1", ""}, {"POST", "/api/pull", one}, {"DELETE", "/api/delete", one},
		{"POST", "/api/chat", one}, {"POST", "/V1/chat/completions", one}, {"POST", "/chat/completions", one}} {
		for _, host := range []string{address, "localhost:11400", "[::1]:11400"} {
			if res, _ := m.do(c[0], host, c[1], c[2], "Origin", "chrome-extension://abc"); res.StatusCode != http.StatusNotFound || res.Header.Get("Access-Control-Allow-Origin") != "" {
				t.Errorf("%s %s%s = %d %v, want 404", c[0], host, c[1], res.StatusCode, res.Header)
			}
		}
	}
	// A machine named v1 keeps its page at the entry.
	if res, _ := m.do("GET", "localhost:11400", "/v1/8080", ""); res.StatusCode != http.StatusFound || !strings.HasPrefix(res.Header.Get("Location"), "http://127.0.0.1:114") {
		t.Errorf("GET localhost:11400/v1/8080 = %d to %q, want the page's port", res.StatusCode, res.Header.Get("Location"))
	}
	for _, body := range []string{``, `[]`, `"shared"`, `{}`, `{"model":5}`, `{"model":null}`, `{"model":["shared"]}`, `{"Model":"shared"}`, `{"model":"shared","model":"shared"}`,
		`{"model":"shared","mod\u0065l":"bge"}`, `{"model":"shared"`, `{"model":"shared",}`, `{"input":{"model":"shared"}}`} {
		m.refused(http.StatusBadRequest, bad, "POST", address, "/v1/chat/completions", body)
	}
	m.refused(http.StatusNotFound, `No machine this computer reaches has the model "shared@far".`, "POST", address, "/v1/chat/completions", `{"model":"shared@far"}`)
	m.refused(http.StatusNotFound, `No machine this computer reaches has the model "".`, "POST", address, "/v1/chat/completions", `{"model":""}`)
	m.refused(http.StatusRequestEntityTooLarge, "A request can be at most 32 MiB.", "POST", address, "/v1/chat/completions", `{"model":"shared","input":"`+strings.Repeat("a", 32<<20)+`"}`)
	if a := m.post("/v1/chat/completions", `{"model":"shared","input":"`+strings.Repeat("a", 32<<20-len(`{"model":"shared","input":""}`))+`"}`); a.Length != 32<<20 {
		t.Errorf("a request of 32 MiB arrived as %d bytes", a.Length)
	}
	if n := len(m.machine.forwarded) + len(m.studio.forwarded); n != 1 {
		t.Errorf("the refused requests reached a server %d times", n-1)
	}
}

// A machine answers the one request of a models channel, whoever sends it.
func TestServeModels(t *testing.T) {
	m := newModels(t)
	var mu sync.Mutex
	var forwarded []int
	ask := func(request string) (*http.Response, string) {
		t.Helper()
		here, there := net.Pipe()
		defer here.Close()
		go func() {
			defer there.Close()
			daemon.ServeModels(there, m.machine.services, func(port int) { mu.Lock(); defer mu.Unlock(); forwarded = append(forwarded, port) })
		}()
		go io.WriteString(here, request)
		res, err := http.ReadResponse(bufio.NewReader(here), nil)
		if err != nil {
			t.Fatalf("%q: %v", request, err)
		}
		body, _ := io.ReadAll(res.Body)
		if !res.Close {
			t.Errorf("%q answered without Connection: close", request)
		}
		return res, string(body)
	}
	if res, text := ask("GET /v1/models?x=1 HTTP/1.1\r\nHost: gpu\r\n\r\n"); res.StatusCode != http.StatusOK ||
		text != `{"data":[{"chat":true,"id":"qwen3:latest","object":"model","owned_by":"ollama"},{"chat":true,"id":"shared","object":"model","owned_by":"ollama"}],"object":"list"}` {
		t.Errorf("GET /v1/models = %d %s", res.StatusCode, text)
	}
	body := `{ "model":"Qwen3" , "n": 1}`
	res, text := ask("POST /v1/chat/completions?a=b HTTP/1.1\r\nHost: gpu\r\nOrigin: https://evil.example\r\nReferer: https://evil.example/\r\nX-Api-Key: k\r\n" +
		"Connection: close\r\nExpect: 100-continue\r\nContent-Length: " + fmt.Sprint(len(body)) + "\r\n\r\n" + body)
	var a asked
	json.Unmarshal([]byte(text), &a)
	if want := (asked{"ollama", "POST", "/v1/chat/completions?a=b", fmt.Sprint("localhost:", m.ports["ollama"]), "", "", "k", "", "close", body, int64(len(body))}); a != want ||
		res.StatusCode != http.StatusOK || !slices.Equal(forwarded, []int{m.ports["ollama"]}) {
		t.Errorf("a request with a model:\n got %d %+v to %v\nwant %+v", res.StatusCode, a, forwarded, want)
	}
	for request, want := range map[string]string{
		"POST /v1/chat/completions HTTP/1.1\r\nHost: gpu\r\nContent-Length: 15\r\n\r\n{\"model\":\"bge\"}":    `404 {"error":{"message":"This machine has no model \"bge\"."}}`,
		"POST /api/pull HTTP/1.1\r\nHost: gpu\r\nContent-Length: 18\r\n\r\n{\"model\":\"shared\"}":            `404 {"error":{"message":"Not found."}}`,
		"GET /v1/chat/completions HTTP/1.1\r\nHost: gpu\r\n\r\n":                                              `404 {"error":{"message":"Not found."}}`,
		"POST /v1/chat/completions HTTP/1.1\r\nHost: gpu\r\nContent-Length: 11\r\n\r\n{\"model\":1}":          `400 {"error":{"message":"Send a JSON object with one \"model\", a string."}}`,
		"POST /v1/embeddings HTTP/1.1\r\nHost: gpu\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n": `400 {"error":{"message":"Send a JSON object with one \"model\", a string."}}`,
	} {
		if res, text := ask(request); fmt.Sprint(res.StatusCode, " ", text) != want || res.Header.Get("Content-Type") != "application/json" {
			t.Errorf("%q = %d %s, want %s", request, res.StatusCode, text, want)
		}
	}
	if len(forwarded) != 1 {
		t.Errorf("the refused requests reached a server: %v", forwarded)
	}
}

// om's chat page, at the root of 127.0.0.1 and the entry's port alone.
func TestTry(t *testing.T) {
	m := newModels(t)
	option := func(name, machine, chosen string) string {
		return fmt.Sprintf(`<option value="%s" data-machine="%s"%s>%s</option>`, name, machine, chosen, name)
	}
	// The chat models the address lists, by its names, each with its machine's name; the one asked for is chosen.
	res, page := m.do("GET", address, "/?model=shared@studio", "")
	_, script, _ := strings.Cut(page, "<script>")
	script, _, _ = strings.Cut(script, "</script>")
	sum := sha256.Sum256([]byte(script))
	for _, want := range []string{"<title>Try a model · Open Machine</title>", `<span>Open Machine</span></span><span class="pick"><select id="model" aria-label="Model">` +
		option("qwen3:latest", "GPU &lt;01&gt;", "") + option("shared", "GPU &lt;01&gt;", "") + option("local.gguf", "My Laptop", "") + option("shared@laptop", "My Laptop", "") +
		option("shared@studio", "Studio &lt;1&gt;", " selected") + "</select>", "<h1>Chat with a model</h1>", `placeholder="Ask anything"`, `<button id="send">Send</button>`,
		`<p class="why">Runs on your machine. Nothing is kept after you close this page.</p>`, "fetch('/v1/chat/completions'"} {
		if !strings.Contains(page, want) {
			t.Errorf("the page has no %s:\n%s", want, page)
		}
	}
	// Only its own script runs, and reaches only this address.
	if policy := "default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src 'sha256-" + base64.StdEncoding.EncodeToString(sum[:]) + "'; connect-src 'self'; frame-ancestors 'none'"; res.StatusCode != 200 ||
		res.Header.Get("Content-Type") != "text/html; charset=utf-8" || res.Header.Get("Content-Security-Policy") != policy || res.Header.Get("Cache-Control") != "no-store" || strings.Count(page, "<script") != 1 {
		t.Errorf("the page = %d %v", res.StatusCode, res.Header)
	}
	// A model this computer does not list is chosen all the same, so its answer says why; with none asked for, none is marked.
	if _, page := m.do("GET", address, "/?model=%3Cb%3E%22", ""); !strings.Contains(page, `aria-label="Model"><option value="&lt;b&gt;&#34;" selected>&lt;b&gt;&#34;</option><option value="qwen3:latest"`) {
		t.Errorf("an unlisted model gives %s", page)
	}
	if res, page := m.do("GET", address, "/", ""); res.StatusCode != 200 || strings.Contains(page, " selected") {
		t.Errorf("no model asked for gives %d %s", res.StatusCode, page)
	}
	// Nowhere else: localhost's root is the entry's.
	for _, c := range [][3]string{{"GET", "localhost:11400", "/"}, {"GET", "[::1]:11400", "/"}, {"GET", address, "/try"}, {"POST", address, "/"}} {
		if res, body := m.do(c[0], c[1], c[2], ""); res.StatusCode != 404 || body != "Not found.\n" {
			t.Errorf("%s %s%s = %d %q, want 404", c[0], c[1], c[2], res.StatusCode, body)
		}
	}
	if res, _ := m.do("HEAD", address, "/", ""); res.StatusCode != 404 {
		t.Errorf("HEAD / = %d, want 404", res.StatusCode)
	}
}

// TestTryScript runs the page's script in Node against a stub page and model server. Each line is what the first
// turn shows, then how many messages the next question sends: 3 when the turn was kept as context, 1 when it failed.
func TestTryScript(t *testing.T) {
	_, page := newModels(t).do("GET", address, "/", "")
	_, script, _ := strings.Cut(page, "<script>")
	script, _, _ = strings.Cut(script, "</script>")
	// A timer left running keeps Node open.
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	node := exec.CommandContext(ctx, "node", "-e", `
const script = require('fs').readFileSync(0, 'utf8');
const piece = (delta, finish) => 'data: ' + JSON.stringify({ choices: [{ delta, finish_reason: finish ?? null }] });
const says = text => piece({ content: text });
// An answer over three lines; an error after the answer began, which a length limit before it leaves unnoted, as data and as its own line; a refusal;
// thinking, then an indented answer after blank lines, cut off at the length limit, with thinking after the answer began left out;
// thinking cut off before any answer. LOOK records what the page shows at that point of the stream.
const cases = [[200, says('Hel'), says('lo\u2028there'), 'data: [DONE]'], [200, says('Hi'), piece({}, 'length'), 'data: {"error":{"message":"boom"}}'],
  [200, 'error: {"code":500,"message":"no slot"}'], [500, '{"error":"bad"}'],
  [200, piece({ role: 'assistant', content: '', reasoning_content: 'Let me' }), 'LOOK', piece({ reasoning_content: ' see.' }), says('\n\n'), 'LOOK', says('  42'), 'LOOK',
    piece({ reasoning_content: 'late' }), piece({}, 'length'), 'data: [DONE]'],
  [200, piece({ reasoning: 'Hmm' }), piece({ reasoning: '…' }, 'length')]];
// What a node shows: a reply's thinking line and text, or its text, marked when hidden.
const shown = n => n.className === 'think' ? [n.tag + '>' + n.kids.map(k => k.tag), n.firstChild.textContent, n.lastChild.textContent, n.open] : (n.hidden ? 'hidden:' : '') + n.textContent;
(async () => {
  for (const [status, ...lines] of cases) {
    const made = [], on = {}, at = {};
    const item = id => ({ value: 'm', dataset: {}, selectedOptions: [{ value: 'm', dataset: {} }], addEventListener: (type, run) => { on[id + type] = run; }, append: node => made.push(node), focus() {} });
    const element = tag => ({ tag, textContent: '', open: false, kids: [], scrollIntoView() {}, append(...kids) { this.kids.push(...kids); }, get firstChild() { return this.kids[0]; }, get lastChild() { return this.kids.at(-1); },
      before(node) { made.splice(made.indexOf(this), 0, node); } });
    globalThis.document = { getElementById: id => at[id] ??= item(id), createElement: element };
    let sent;
    const looks = [];
    // The answer arrives 7 bytes at a time, so a line is split across reads.
    globalThis.fetch = async (url, { body }) => {
      sent = JSON.parse(body).messages.length;
      const reads = lines.flatMap(line => {
        if (line === 'LOOK') return [line];
        const bytes = Buffer.from(line + '\r\n\r\n'), parts = [];
        for (let i = 0; i < bytes.length; i += 7) parts.push(bytes.subarray(i, i + 7));
        return parts;
      });
      return new Response(new ReadableStream({ async pull(to) {
        while (reads[0] === 'LOOK') { reads.shift(); await new Promise(done => setTimeout(done, 10)); looks.push(made.slice(1).map(shown)); }
        reads.length ? to.enqueue(reads.shift()) : to.close();
      } }), { status });
    };
    new Function(script)();
    at.ask.value = 'q'; await on.formsubmit({ preventDefault() {} });
    const first = made.slice(1).map(shown);
    looks.splice(0).forEach(look => console.log(JSON.stringify(look)));
    at.ask.value = 'q'; await on.formsubmit({ preventDefault() {} });
    console.log(JSON.stringify([...first, sent]));
  }
})();`)
	node.Stdin = strings.NewReader(script)
	out, err := node.CombinedOutput()
	if want := "[\"Hello\u2028there\",3]\n" + `["Hi\n\nboom",1]
["no slot",1]
["bad",1]
[["details>summary,div","Thinking · 0s","Let me",false],"hidden:…"]
[["details>summary,div","Thinking · 0s","Let me see.",false],"hidden:…"]
[["details>summary,div","Thought for 0s","Let me see.",false],"  42"]
[["details>summary,div","Thought for 0s","Let me see.",false],"  42","The answer was cut off here: it reached the model’s length limit.",3]
[["details>summary,div","Thought for 0s","Hmm…",false],"hidden:","The model reached its length limit before it could answer.",1]
`; err != nil || string(out) != want {
		t.Fatalf("the script gave %v %s, want %s", err, out, want)
	}
}
