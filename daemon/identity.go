package daemon

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"golang.org/x/sys/unix"
)

// Identity is this device's registration, owned by the Python Connector.
// Code only reads it: a missing or unsafe identity
// means sign-in or recovery is needed, never a new enrollment.
type Identity struct {
	Origin, DeviceID, PrincipalID string
	Key                           ed25519.PrivateKey
	dir                           string
	stored                        []string // the IDs of the devices the Connector stores
}

// ErrOffline reports that this computer is offline:
// Desktop is Disconnected, signed out or cannot be read safely, or the
// Connector is stopped, revoked or registered otherwise. Offline admits no
// SSH and closes every SSH connection into or out of this computer.
var ErrOffline = errors.New("native: this computer is disconnected from Open Machine")

const maxState = 65536

var (
	identifier   = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
	revisionForm = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)
	fingerprint  = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// connectorConfig is the part of the Connector's config.json Code reads.
type connectorConfig struct {
	Server  string `json:"server"`
	Enabled *bool  `json:"enabled"`
	Revoked bool   `json:"revoked"`
	Device  *struct {
		DeviceID       string `json:"device_id"`
		PrincipalID    string `json:"principal_id"`
		KeyFingerprint string `json:"key_fingerprint"`
		HardwareID     string `json:"hardware_id"`
	} `json:"device"`
	Devices []struct {
		DeviceID string `json:"device_id"`
	} `json:"devices"`
	RemoteAccess json.RawMessage `json:"remote_access"`
}

// HardwareID returns this machine's hardware ID: its IOPlatformUUID on macOS;
// on Linux the file hardware-id in the Connector's state directory dir if it
// exists, else /etc/machine-id. It is read once per
// process and directory, retried until read, and never leaves this machine.
// Tests replace it.
var HardwareID = func() func(dir string) (string, error) {
	var mu sync.Mutex
	ids := map[string]string{}
	return func(dir string) (string, error) {
		mu.Lock()
		defer mu.Unlock()
		if ids[dir] == "" {
			read, err := readHardwareID(dir)
			if err != nil {
				return "", err
			}
			ids[dir] = read
		}
		return ids[dir], nil
	}
}()

// ReadIdentity reads the device in use in the Connector's state directory: its
// binding and the 32-byte Ed25519 seed of the key file it names, which must be
// that key and bound to this machine. Anything else
// means this computer is not ready: it has yet to sign in.
func ReadIdentity(dir string) (*Identity, error) {
	c, err := readConfig(dir)
	if err != nil {
		return nil, err
	}
	origin, err := cloudOrigin(c.Server)
	if err != nil {
		return nil, err
	}
	if c.Device == nil || !identifier.MatchString(c.Device.DeviceID) || !identifier.MatchString(c.Device.PrincipalID) ||
		!fingerprint.MatchString(c.Device.KeyFingerprint) {
		return nil, errors.New("native: this computer is not registered; sign in first")
	}
	seed, err := readPrivate(dir, "key-"+c.Device.KeyFingerprint, maxState)
	if err == nil && len(seed) != ed25519.SeedSize {
		err = errors.New("not a 32-byte seed")
	}
	if err != nil {
		return nil, fmt.Errorf("native: no safe device key in %s (%v); sign in again", dir, err)
	}
	key := ed25519.NewKeyFromSeed(seed)
	if sum := sha256.Sum256(key.Public().(ed25519.PublicKey)); c.Device.KeyFingerprint != hex.EncodeToString(sum[:]) {
		return nil, errors.New("native: the device key is not this registration's; sign in again")
	}
	hardware, err := HardwareID(dir)
	if err != nil {
		return nil, fmt.Errorf("native: this machine's hardware ID is unreadable: %v", err)
	}
	if c.Device.HardwareID != hardware {
		return nil, errors.New("native: this registration is another computer's; sign in again")
	}
	id := &Identity{Origin: origin, DeviceID: c.Device.DeviceID, PrincipalID: c.Device.PrincipalID, Key: key, dir: dir}
	for _, d := range c.Devices {
		id.stored = append(id.stored, d.DeviceID)
	}
	return id, nil
}

func readConfig(dir string) (*connectorConfig, error) {
	raw, err := readPrivate(dir, "config.json", maxState)
	var c connectorConfig
	if err == nil {
		err = json.Unmarshal(raw, &c)
	}
	if err != nil {
		return nil, fmt.Errorf("native: the Connector configuration cannot be read safely: %v", err)
	}
	return &c, nil
}

// changed reports whether the Connector's configuration names another device
// than this identity's, as after a sign-in moved this computer to another one.
func (id *Identity) changed() bool {
	c, err := readConfig(id.dir)
	return err == nil && (c.Device == nil || c.Device.DeviceID != id.DeviceID)
}

// Admission reports whether this computer is online, read the way the
// Connector reads it and never written: still this registration and not
// revoked, Desktop's remote-access intent on, and the Connector enabled. It is
// nil or ErrOffline.
func (id *Identity) Admission() error {
	c, err := readConfig(id.dir)
	if err != nil || c.Device == nil || c.Device.DeviceID != id.DeviceID || c.Device.PrincipalID != id.PrincipalID ||
		c.Revoked || c.Enabled == nil || !*c.Enabled {
		return ErrOffline
	}
	if origin, err := cloudOrigin(c.Server); err != nil || origin != id.Origin {
		return ErrOffline
	}
	if len(c.RemoteAccess) != 0 && string(c.RemoteAccess) != "null" {
		var binding map[string]string
		if json.Unmarshal(c.RemoteAccess, &binding) != nil || len(binding) != 1 {
			return ErrOffline
		}
		path, ok := binding["intent_path"]
		if on, err := readIntent(path); !ok || err != nil || !on {
			return ErrOffline
		}
	}
	return nil
}

// cloudOrigin checks an origin as the Connector does: HTTPS, or HTTP on
// loopback, with no path or credentials.
func cloudOrigin(value string) (string, error) {
	u, err := url.Parse(value)
	if err != nil || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || u.Hostname() == "" {
		return "", errors.New("native: invalid Cloud origin")
	}
	loopback := map[string]bool{"127.0.0.1": true, "localhost": true, "::1": true}[u.Hostname()]
	if u.Scheme != "https" && !(u.Scheme == "http" && loopback) {
		return "", errors.New("native: Cloud requires HTTPS")
	}
	return u.Scheme + "://" + u.Host, nil
}

// readPrivate reads a bounded, owner-only regular file from an owner-only
// directory, following no link.
func readPrivate(dir, name string, max int64) ([]byte, error) {
	d, err := unix.Open(dir, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer unix.Close(d)
	if mode, _, err := owned(d, unix.S_IFDIR); err != nil || mode&0o077 != 0 {
		return nil, errors.New("not an owner-only directory")
	}
	return readAt(d, name, max, func(mode uint32, links uint64) bool { return mode&0o077 == 0 })
}

// owned stats fd, which must be of type kind and owned by this user, and
// returns its permission bits.
func owned(fd int, kind uint32) (uint32, unix.Stat_t, error) {
	var st unix.Stat_t
	if err := unix.Fstat(fd, &st); err != nil {
		return 0, st, err
	}
	if uint32(st.Mode)&unix.S_IFMT != kind || st.Uid != uint32(os.Getuid()) {
		return 0, st, errors.New("not owned")
	}
	return uint32(st.Mode) & 0o7777, st, nil
}

// readAt reads name in dir: a bounded regular file of this user, whose mode and link count ok accepts.
func readAt(dir int, name string, max int64, ok func(mode uint32, links uint64) bool) ([]byte, error) {
	fd, err := unix.Openat(dir, name, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), name)
	defer f.Close()
	mode, st, err := owned(fd, unix.S_IFREG)
	if err != nil || st.Size > max || !ok(mode, uint64(st.Nlink)) {
		return nil, errors.New("not a bounded private file")
	}
	b, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil || int64(len(b)) > max {
		return nil, errors.New("unreadable")
	}
	return b, nil
}

// readIntent reads Desktop's remote-access intent as the Connector's
// remote_access.read_intent does: an exact absolute path walked without
// following links, an owner-only parent, a single-link 0600 file and an exact
// format.
func readIntent(path string) (bool, error) {
	if path == "" || !filepath.IsAbs(path) || filepath.Clean(path) != path || strings.ContainsRune(path, 0) {
		return false, errors.New("invalid intent path")
	}
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	fd, err := unix.Open("/", unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return false, err
	}
	for _, part := range parts[:len(parts)-1] {
		next, err := unix.Openat(fd, part, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		unix.Close(fd)
		if err != nil {
			return false, err
		}
		fd = next
	}
	defer unix.Close(fd)
	if mode, _, err := owned(fd, unix.S_IFDIR); err != nil || mode != 0o700 {
		return false, errors.New("intent parent is not owner-only")
	}
	raw, err := readAt(fd, parts[len(parts)-1], 4096, func(mode uint32, links uint64) bool { return mode == 0o600 && links == 1 })
	if err != nil {
		return false, err
	}
	var intent map[string]json.RawMessage
	if json.Unmarshal(raw, &intent) != nil || len(intent) != 3 || string(intent["format"]) != "1" || !revisionForm.Match(intent["revision"]) {
		return false, errors.New("unsupported intent")
	}
	if _, err := strconv.ParseUint(string(intent["revision"]), 10, 64); err != nil {
		return false, errors.New("unsupported intent")
	}
	switch {
	case bytes.Equal(intent["enabled"], []byte("true")):
		return true, nil
	case bytes.Equal(intent["enabled"], []byte("false")):
		return false, nil
	}
	return false, errors.New("unsupported intent")
}
