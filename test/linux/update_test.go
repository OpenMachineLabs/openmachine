package linux

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/OpenMachineLabs/openmachine/linux"
	"golang.org/x/sys/unix"
)

// storage is the download storage of the staging channel, signed by a key made for the test.
type storage struct {
	mu    sync.Mutex
	files map[string][]byte
	key   ed25519.PrivateKey
}

// newStorage serves a storage for h's releases and makes its key the release key.
func newStorage(t *testing.T, h *host) *storage {
	public, key, _ := ed25519.GenerateKey(rand.Reader)
	s := &storage{files: map[string][]byte{}, key: key}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		defer s.mu.Unlock()
		if b, ok := s.files[r.URL.Path]; ok {
			w.Write(b)
		} else {
			http.NotFound(w, r)
		}
	}))
	saved := linux.ReleaseKey
	linux.ReleaseKey = base64.StdEncoding.EncodeToString(public)
	t.Cleanup(func() { server.Close(); linux.ReleaseKey = saved })
	h.downloads = server.URL + "/linux"
	return s
}

// put publishes a file and the release key's signature of it.
func (s *storage) put(path string, content []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.files["/linux/"+path] = content
	s.files["/linux/"+path+".sig"] = ed25519.Sign(s.key, content)
}

// publish publishes release r: its archive and manifest, and the staging pointer to it.
func (s *storage) publish(r release) {
	var archive bytes.Buffer
	zipped := gzip.NewWriter(&archive)
	files := tar.NewWriter(zipped)
	name := filepath.Base(r.dir)
	filepath.WalkDir(r.dir, func(path string, d fs.DirEntry, _ error) error {
		info, _ := d.Info()
		header, _ := tar.FileInfoHeader(info, "")
		header.Name = filepath.Join(name, strings.TrimPrefix(path, r.dir))
		files.WriteHeader(header)
		if !d.IsDir() {
			b, _ := os.ReadFile(path)
			files.Write(b)
		}
		return nil
	})
	files.Close()
	zipped.Close()
	sum := sha256.Sum256(archive.Bytes())
	arch := strings.TrimPrefix(name, "openmachine-"+r.version+"-linux-")
	s.put(r.version+"/"+name+".tar.gz", archive.Bytes())
	s.put(r.version+"/manifest.json", fmt.Appendf(nil, `{"format":1,"product":"openmachine","version":%q,"artifacts":{%q:{"file":%q,"sha256":%q,"bytes":%d}}}`,
		r.version, arch, name+".tar.gz", hex.EncodeToString(sum[:]), archive.Len()))
	s.put("staging.json", fmt.Appendf(nil, `{"format":1,"channel":"staging","version":%q}`, r.version))
}

// TestUpdate verifies the pointer, the manifest and the archive before
// anything of a release runs, refuses what is not a newer release of the
// machine's channel, and updates Core and the Connector once each, moves the
// link, rewrites the unit and restarts the daemon; an update interrupted in
// any phase is finished by the next om update.
func TestUpdate(t *testing.T) {
	// updated checks that the machine runs release r by a new daemon.
	updated := func(t *testing.T, h *host, r release, before int) {
		t.Helper()
		selected := filepath.Join(h.home, ".local/share/openmachine/connector/versions", r.connector, "om")
		if target, _ := os.Readlink(h.link()); target != selected {
			t.Fatalf("om links to %s, not %s", target, selected)
		}
		daemon := h.daemon()
		calls := h.calls()
		python := filepath.Join(h.home, ".local/state/openmachine/service/payloads", r.core, "python/bin/python3")
		if started := calls[len(calls)-1]; daemon == before || !strings.HasPrefix(started, "daemon "+selected+" -core-python "+python+" ") {
			t.Fatalf("the daemon %d (was %d) is not the release's: %s", daemon, before, started)
		}
		var w world
		edit(h.world, func(current *world) { w = *current })
		if w.Core.Current != r.core || w.Connector.Bundle != r.connector || len(w.Core.Ops) != 2 || len(w.Connector.Upgrades) != 1 {
			t.Fatalf("Core or the Connector is not updated once: %+v %+v", w.Core, w.Connector)
		}
		record := h.record()
		if _, err := os.Stat(filepath.Join(h.home, ".local/state/openmachine-om/releases")); err == nil || record.Release.Version != r.version || len(record.Operations) != 0 {
			t.Fatalf("after the update: %+v, releases kept: %v", record, err == nil)
		}
		out, _, _ := h.om("version")
		want(t, "om version", out, "Open Machine "+r.version+"\nCore 0.2.29\n")
		h.calls()
	}

	t.Run("to a newer release", func(t *testing.T) {
		h := newHost(t, "1000", false)
		s := newStorage(t, h)
		h.installed("0.0.16")
		before := h.daemon()
		next := h.release("0.0.17")
		s.publish(next)
		out, errOut, code := h.om("update")
		if code != 0 || !strings.HasPrefix(out, "Open Machine 0.0.17 installed for tester. It starts when you open a shell") {
			t.Fatalf("om update = %d\n%s%s", code, out, errOut)
		}
		updated(t, h, next, before)
		out, errOut, code = h.om("update")
		want(t, "a second om update", fmt.Sprint(code, out, errOut), "0Open Machine 0.0.17 is current.\n")
		want(t, "its owner calls", h.calls(), []string{})
	})

	t.Run("as root with systemd it rewrites the unit and restarts it", func(t *testing.T) {
		h := newHost(t, "0", true)
		t.Setenv("PATH", filepath.Join(h.root, "usr/local/bin")+":"+os.Getenv("PATH"))
		s := newStorage(t, h)
		h.installed("0.0.16")
		before := h.daemon()
		next := h.release("0.0.17")
		s.publish(next)
		edit(h.world, func(w *world) { w.Faults["systemctl restart"] = "fail" })
		// The unit was rewritten and the restart failed: the next update still restarts the daemon.
		if _, errOut, code := h.om("update"); code != 1 || !strings.HasPrefix(errOut, "om: the service could not be set up") || h.record().Release.Version != "0.0.16" {
			t.Fatalf("om update with a failing restart = %d\n%s", code, errOut)
		}
		h.calls()
		if out, errOut, code := h.om("update"); code != 0 {
			t.Fatalf("om update = %d\n%s%s", code, out, errOut)
		}
		unit, _ := os.ReadFile(filepath.Join(h.root, "etc/systemd/system/openmachine.service"))
		if !strings.Contains(string(unit), `ExecStart="`+filepath.Join(h.home, ".local/share/openmachine/connector/versions", next.connector, "om")+`" "daemon" "-core-python" "`+
			filepath.Join(h.home, ".local/state/openmachine/service/payloads", next.core, "python/bin/python3")+`"`) {
			t.Fatalf("the unit was not rewritten:\n%s", unit)
		}
		updated(t, h, next, before)
	})

	t.Run("refuses what it cannot verify, and what is not newer", func(t *testing.T) {
		h := newHost(t, "1000", false)
		s := newStorage(t, h)
		h.installed("0.0.16")
		next, older := h.release("0.0.17"), h.release("0.0.15")
		_, other, _ := ed25519.GenerateKey(rand.Reader)
		for _, c := range []struct {
			name    string
			publish func()
			code    int
			out     string
		}{
			{"the installed release", func() { s.publish(h.release("0.0.16")) }, 0, "Open Machine 0.0.16 is current.\n"},
			{"an older release", func() { s.publish(older) }, 0, "Open Machine 0.0.16 is current.\n"},
			{"a pointer another key signed", func() {
				s.publish(next)
				s.files["/linux/staging.json.sig"] = ed25519.Sign(other, s.files["/linux/staging.json"])
			}, 1, "om: the release pointer could not be verified (" + h.downloads + "/staging.json is not signed by the release key). Run: om update\n"},
			{"a manifest another key signed", func() {
				s.publish(next)
				s.files["/linux/0.0.17/manifest.json.sig"] = ed25519.Sign(other, s.files["/linux/0.0.17/manifest.json"])
			}, 1, "om: release 0.0.17 could not be verified (" + h.downloads + "/0.0.17/manifest.json is not signed by the release key). Run: om update\n"},
			{"a pointer signed for the other channel", func() {
				s.publish(next)
				s.put("staging.json", []byte(`{"format":1,"channel":"stable","version":"0.0.17"}`))
			}, 1, "om: the release pointer is not the staging channel's. Nothing was changed.\n"},
			{"an archive that is not the manifest's", func() {
				s.publish(next)
				for path := range s.files {
					if strings.HasSuffix(path, ".tar.gz") {
						s.files[path] = bytes.Clone(s.files[path])
						s.files[path][len(s.files[path])/2] ^= 1
					}
				}
			}, 1, "om: release 0.0.17 could not be unpacked (its archive is not the one its manifest names). Nothing was changed. Run: om update\n"},
			{"a second update at once", func() {
				s.publish(next)
				lock, _ := os.OpenFile(filepath.Join(h.home, ".local/state/openmachine-om/headless.lock"), os.O_RDWR, 0)
				unix.Flock(int(lock.Fd()), unix.LOCK_EX)
				t.Cleanup(func() { lock.Close() })
			}, 1, "om: another om command is running. Run this one again when it ends.\n"},
		} {
			c.publish()
			out, errOut, code := h.om("update")
			want(t, c.name, fmt.Sprint(code, out, errOut), fmt.Sprint(c.code, c.out))
			want(t, c.name+": owner calls", h.calls(), []string{})
			left, _ := os.ReadDir(filepath.Join(h.home, ".local/state/openmachine-om/releases"))
			if want(t, c.name+": the installed release", h.record().Release.Version, "0.0.16"); len(left) != 0 {
				t.Fatalf("%s left %v unpacked", c.name, left)
			}
		}
	})

	t.Run("interrupted in any phase, the next om update finishes it", func(t *testing.T) {
		for _, c := range []struct{ name, call, fault string }{
			{"while it unpacked the archive", "", ""},
			{"Core's reply was lost", "core update", "lost"},
			{"Core's process died before activation", "core update", "died"},
			{"the Connector had retained the bundle", "upgrade", "interrupt"},
		} {
			t.Run(c.name, func(t *testing.T) {
				h := newHost(t, "1000", false)
				s := newStorage(t, h)
				h.installed("0.0.16")
				before := h.daemon()
				next := h.release("0.0.17")
				s.publish(next)
				if c.call == "" {
					partial := filepath.Join(h.home, ".local/state/openmachine-om/releases/0.0.17.unpacking")
					os.MkdirAll(filepath.Join(partial, filepath.Base(next.dir), "connector"), 0o755)
					os.WriteFile(partial+".tar.gz", []byte("half an archive"), 0o600)
				} else {
					edit(h.world, func(w *world) { w.Faults[c.call] = c.fault })
					if _, errOut, code := h.om("update"); code != 1 || strings.Count(errOut, "\n") != 1 || !strings.HasPrefix(errOut, "om: ") {
						t.Fatalf("interrupted om update = %d\n%s", code, errOut)
					}
					if h.record().Release.Version != "0.0.16" || len(h.record().Operations) != 1 {
						t.Fatalf("after the interruption: %+v", h.record())
					}
				}
				if out, errOut, code := h.om("update"); code != 0 {
					t.Fatalf("the next om update = %d\n%s%s", code, out, errOut)
				}
				updated(t, h, next, before)
			})
		}
	})
}
