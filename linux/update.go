package linux

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// ReleaseKey is the Ed25519 public key that signs Linux releases, in base64.
// Cloud carries the same key; its private half exists only where releases are
// signed. Tests replace it.
var ReleaseKey = "kfCsrDY+JI5/qVJLQf7xJ10BlHNSt/NrnErRLnopuhs="

// pointer is a channel's mutable pointer to its current release.
type pointer struct {
	Format  int    `json:"format"`
	Channel string `json:"channel"`
	Version string `json:"version"`
}

// manifest lists one release's archives.
type manifest struct {
	Format    int    `json:"format"`
	Product   string `json:"product"`
	Version   string `json:"version"`
	Artifacts map[string]struct {
		File   string `json:"file"`
		SHA256 string `json:"sha256"`
		Bytes  int64  `json:"bytes"`
	} `json:"artifacts"`
}

const (
	maxSigned  = 64 << 10 // a pointer or manifest
	maxArchive = 2 << 30
)

// fetch writes at most limit bytes of a published file to w.
func fetch(url string, limit int64, w io.Writer) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return err
	}
	res, err := daemon.CloudClient.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("%s answered %s", url, res.Status)
	}
	n, err := io.Copy(w, io.LimitReader(res.Body, limit+1))
	if err == nil && n > limit {
		err = fmt.Errorf("%s is too large", url)
	}
	return err
}

// signed reads a published JSON file into v once the release key's signature
// over its exact bytes, published beside it, is verified.
func signed(key ed25519.PublicKey, url string, v any) error {
	var b, sig bytes.Buffer
	if err := fetch(url, maxSigned, &b); err != nil {
		return err
	}
	if err := fetch(url+".sig", ed25519.SignatureSize, &sig); err != nil {
		return err
	}
	if !ed25519.Verify(key, b.Bytes(), sig.Bytes()) {
		return fmt.Errorf("%s is not signed by the release key", url)
	}
	return json.Unmarshal(b.Bytes(), v)
}

// update is om update: it verifies the pointer and the manifest, refuses a
// release that is not newer, downloads and verifies its archive, and runs that
// release's own install, which updates Core and the Connector under recorded
// operation IDs, moves the link, rewrites the unit and restarts the daemon.
// One runs at a time, and an interrupted one is finished by the next.
func (m *machine) update() error {
	if err := m.take(); err != nil {
		return err
	}
	if err := m.installed(); err != nil {
		return err
	}
	key, err := base64.StdEncoding.DecodeString(ReleaseKey)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return fail("this build carries no release key, so it cannot verify an update. Run the install command again.")
	}
	current := m.rec.Release
	var p pointer
	if err := signed(key, current.Downloads+"/"+current.Channel+".json", &p); err != nil {
		return fail("the release pointer could not be verified (%v). Run: om update", err)
	}
	if p.Format != 1 || p.Channel != current.Channel || !versionForm.MatchString(p.Version) {
		return fail("the release pointer is not the %s channel's. Nothing was changed.", current.Channel)
	}
	if !newer(p.Version, current.Version) {
		fmt.Fprintf(m.out, "Open Machine %s is current.\n", current.Version)
		return nil
	}
	var list manifest
	if err := signed(key, current.Downloads+"/"+p.Version+"/manifest.json", &list); err != nil {
		return fail("release %s could not be verified (%v). Run: om update", p.Version, err)
	}
	name := "openmachine-" + p.Version + "-linux-" + arch()
	archive := list.Artifacts[arch()]
	if list.Format != 1 || list.Product != "openmachine" || list.Version != p.Version || archive.File != name+".tar.gz" ||
		archive.Bytes < 1 || archive.Bytes > maxArchive {
		return fail("release %s has no archive for this machine. Nothing was changed.", p.Version)
	}
	dir := m.path("releases/" + p.Version)
	installer := filepath.Join(dir, name, "connector/om")
	if _, err := os.Stat(installer); err != nil { // else an interrupted update unpacked and verified it
		if err := unpack(current.Downloads+"/"+p.Version+"/"+archive.File, archive.Bytes, archive.SHA256, dir); err != nil {
			return fail("release %s could not be unpacked (%v). Nothing was changed. Run: om update", p.Version, err)
		}
	}
	m.lock.Close() // the release's own install takes the installation
	m.lock = nil
	cmd := exec.Command(installer, "install", "--no-up")
	cmd.Stdout, cmd.Stderr = m.out, m.err
	if err := cmd.Run(); err != nil {
		return &failure{} // it said why
	}
	return nil
}

// unpack downloads a release's archive, requires its size and SHA-256, and
// unpacks it as the install script does: owned by this user, whoever the
// archive names. It is unpacked beside dir and moved there whole, so a partly
// unpacked release is never run.
func unpack(url string, size int64, digest, dir string) error {
	partial := dir + ".unpacking"
	if err := os.RemoveAll(partial); err != nil {
		return err
	}
	if err := os.MkdirAll(partial, 0o700); err != nil {
		return err
	}
	defer os.RemoveAll(partial) // unless it was moved into place
	file, err := os.Create(partial + ".tar.gz")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	sum := sha256.New()
	if err := fetch(url, size, io.MultiWriter(file, sum)); err != nil {
		return err
	}
	if hex.EncodeToString(sum.Sum(nil)) != digest {
		return errors.New("its archive is not the one its manifest names")
	}
	if out, err := exec.Command("tar", "-xzof", file.Name(), "-C", partial).CombinedOutput(); err != nil {
		return fmt.Errorf("%v: %s", err, bytes.TrimSpace(out))
	}
	os.RemoveAll(dir)
	return os.Rename(partial, dir)
}
