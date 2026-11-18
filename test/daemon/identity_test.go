package daemon

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"testing"

	"github.com/OpenMachineLabs/openmachine/daemon"
)

// onHardware makes this machine's hardware ID id, or an unreadable one, for t.
func onHardware(t *testing.T, id string, err error) {
	saved := daemon.HardwareID
	daemon.HardwareID = func(string) (string, error) { return id, err }
	t.Cleanup(func() { daemon.HardwareID = saved })
}

// fingerprint is a config.device key_fingerprint: SHA-256 hex of the raw public key.
func fingerprint(seed []byte) string {
	sum := sha256.Sum256(ed25519.NewKeyFromSeed(seed).Public().(ed25519.PublicKey))
	return hex.EncodeToString(sum[:])
}

// TestIdentity reads the Connector's identity and Desktop's intent only as
// the Connector does, and never writes or repairs them.
func TestIdentity(t *testing.T) {
	seed := make([]byte, ed25519.SeedSize)
	seed[0] = 7
	key := "key-" + fingerprint(seed) // the key file config.device names
	onHardware(t, "hardware-A", nil)
	setup := func(t *testing.T) (dir, intent string) {
		root, _ := filepath.EvalSymlinks(t.TempDir()) // the intent's path may hold no link, as for the Connector
		dir, intent = filepath.Join(root, "connector"), filepath.Join(root, "desktop", "intent.json")
		os.MkdirAll(dir, 0o700)
		os.MkdirAll(filepath.Dir(intent), 0o700)
		os.WriteFile(filepath.Join(dir, key), seed, 0o600)
		b, _ := json.Marshal(map[string]any{"server": "https://cloud.test", "enabled": true,
			"device": map[string]any{"device_id": "dev_one", "principal_id": "acct_one",
				"key_fingerprint": fingerprint(seed), "hardware_id": "hardware-A"},
			"remote_access": map[string]any{"intent_path": intent}})
		os.WriteFile(filepath.Join(dir, "config.json"), b, 0o600)
		os.WriteFile(intent, []byte(`{"format":1,"enabled":true,"revision":3}`), 0o600)
		return dir, intent
	}
	rewrite := func(dir string, config map[string]any) {
		b, _ := os.ReadFile(filepath.Join(dir, "config.json"))
		var c map[string]any
		json.Unmarshal(b, &c)
		for k, v := range config {
			c[k] = v
		}
		b, _ = json.Marshal(c)
		os.WriteFile(filepath.Join(dir, "config.json"), b, 0o600)
	}

	t.Run("the original key and binding are read, and this computer is online only while Desktop and the Connector are on", func(t *testing.T) {
		dir, intent := setup(t)
		id, err := daemon.ReadIdentity(dir)
		if err != nil || id.DeviceID != "dev_one" || id.PrincipalID != "acct_one" || id.Origin != "https://cloud.test" ||
			!id.Key.Equal(ed25519.NewKeyFromSeed(seed)) {
			t.Fatalf("ReadIdentity = %+v, %v", id, err)
		}
		if err := id.Admission(); err != nil {
			t.Fatalf("Admission = %v", err)
		}
		original, _ := os.ReadFile(filepath.Join(dir, "config.json"))
		// Disconnect writes the intent Off and then stops the Connector; either one is offline.
		os.WriteFile(intent, []byte(`{"format":1,"enabled":false,"revision":4}`), 0o600)
		if err := id.Admission(); !errors.Is(err, daemon.ErrOffline) {
			t.Fatalf("Admission with Desktop Disconnected = %v, want ErrOffline", err)
		}
		os.WriteFile(intent, []byte(`{"format":1,"enabled":true,"revision":5}`), 0o600)
		for name, change := range map[string]map[string]any{
			"a sign-out or other Connector stop": {"enabled": false},
			"a revoked device":                   {"revoked": true},
			"another account":                    {"device": map[string]any{"device_id": "dev_one", "principal_id": "acct_two"}},
			"another Cloud origin":               {"server": "https://other.test"},
		} {
			rewrite(dir, change)
			if err := id.Admission(); !errors.Is(err, daemon.ErrOffline) {
				t.Errorf("Admission after %s = %v, want ErrOffline", name, err)
			}
			os.WriteFile(filepath.Join(dir, "config.json"), original, 0o600)
		}
		if err := id.Admission(); err != nil {
			t.Fatalf("Admission restored = %v", err)
		}
		// A standalone Connector has no Desktop intent and is on while enabled.
		rewrite(dir, map[string]any{"remote_access": nil})
		if err := id.Admission(); err != nil {
			t.Fatalf("standalone Admission = %v", err)
		}
	})

	t.Run("an unsafe identity means sign-in, and is never repaired", func(t *testing.T) {
		for name, spoil := range map[string]func(dir string){
			"a group-readable seed": func(dir string) { os.Chmod(filepath.Join(dir, key), 0o640) },
			"a short seed":          func(dir string) { os.WriteFile(filepath.Join(dir, key), seed[:31], 0o600) },
			"a linked seed": func(dir string) {
				os.Rename(filepath.Join(dir, key), filepath.Join(dir, "real.key"))
				os.Symlink(filepath.Join(dir, "real.key"), filepath.Join(dir, key))
			},
			"an open directory":   func(dir string) { os.Chmod(dir, 0o755) },
			"no device":           func(dir string) { rewrite(dir, map[string]any{"device": nil}) },
			"a plain HTTP origin": func(dir string) { rewrite(dir, map[string]any{"server": "http://cloud.test"}) },
		} {
			dir, _ := setup(t)
			spoil(dir)
			before, _ := os.Lstat(filepath.Join(dir, key))
			if id, err := daemon.ReadIdentity(dir); err == nil {
				t.Errorf("ReadIdentity with %s = %+v", name, id)
			}
			if after, _ := os.Lstat(filepath.Join(dir, key)); before.Mode() != after.Mode() {
				t.Errorf("ReadIdentity with %s changed the seed's mode", name)
			}
		}
	})

	t.Run("a binding to another key or machine, or an unreadable machine, is not ready", func(t *testing.T) {
		device := func(change map[string]any) map[string]any {
			d := map[string]any{"device_id": "dev_one", "principal_id": "acct_one", "key_fingerprint": fingerprint(seed), "hardware_id": "hardware-A"}
			for k, v := range change {
				d[k] = v
			}
			return map[string]any{"device": d}
		}
		for name, change := range map[string]map[string]any{
			"another key":         device(map[string]any{"key_fingerprint": fingerprint(make([]byte, ed25519.SeedSize))}),
			"a key named wrongly": device(map[string]any{"key_fingerprint": fingerprint(make([]byte, ed25519.SeedSize))}),
			"no key fingerprint":  device(map[string]any{"key_fingerprint": nil}),
			"another machine":     device(map[string]any{"hardware_id": "hardware-B"}),
			"no hardware ID":      device(map[string]any{"hardware_id": nil}),
		} {
			dir, _ := setup(t)
			if name == "a key named wrongly" { // this seed in the file of another key's fingerprint
				os.Rename(filepath.Join(dir, key), filepath.Join(dir, "key-"+fingerprint(make([]byte, ed25519.SeedSize))))
			}
			rewrite(dir, change)
			if id, err := daemon.ReadIdentity(dir); err == nil {
				t.Errorf("ReadIdentity with %s = %+v", name, id)
			}
		}
		dir, _ := setup(t)
		onHardware(t, "", errors.New("unreadable"))
		if id, err := daemon.ReadIdentity(dir); err == nil {
			t.Errorf("ReadIdentity with an unreadable hardware ID = %+v", id)
		}
		onHardware(t, "hardware-A", nil)
		if _, err := daemon.ReadIdentity(dir); err != nil {
			t.Errorf("ReadIdentity once readable = %v", err)
		}
	})

	t.Run("an intent that cannot be read safely is offline", func(t *testing.T) {
		for name, spoil := range map[string]func(intent string){
			"a wider mode":      func(intent string) { os.Chmod(intent, 0o640) },
			"a narrower mode":   func(intent string) { os.Chmod(intent, 0o400) },
			"a second link":     func(intent string) { os.Link(intent, intent+".copy") },
			"an open parent":    func(intent string) { os.Chmod(filepath.Dir(intent), 0o750) },
			"an unknown format": func(intent string) { os.WriteFile(intent, []byte(`{"format":2,"enabled":true,"revision":1}`), 0o600) },
			"an extra key": func(intent string) {
				os.WriteFile(intent, []byte(`{"format":1,"enabled":true,"revision":1,"x":1}`), 0o600)
			},
			"a string enabled":    func(intent string) { os.WriteFile(intent, []byte(`{"format":1,"enabled":"true","revision":1}`), 0o600) },
			"a negative revision": func(intent string) { os.WriteFile(intent, []byte(`{"format":1,"enabled":true,"revision":-1}`), 0o600) },
			"a linked parent": func(intent string) {
				parent := filepath.Dir(intent)
				os.Rename(parent, parent+"-real")
				os.Symlink(parent+"-real", parent)
			},
		} {
			dir, intent := setup(t)
			id, err := daemon.ReadIdentity(dir)
			if err != nil {
				t.Fatal(err)
			}
			spoil(intent)
			if err := id.Admission(); !errors.Is(err, daemon.ErrOffline) {
				t.Errorf("Admission with %s = %v, want ErrOffline", name, err)
			}
		}
	})
}

// TestHardwareIDOnLinux reads the hardware ID from the Connector's state file
// hardware-id if it exists, else the real /etc/machine-id, which never leaves the machine and is not printed.
// Elsewhere the real ID is not read in tests. A host without a well-formed one, such as a container, must get an
// error, never an ID, and om never creates the file.
func TestHardwareIDOnLinux(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("NOT RUN: the hardware-id file and /etc/machine-id are read only on Linux")
	}
	state := func() string { // an owner-only directory, as the Connector's state is
		dir := filepath.Join(t.TempDir(), "connector")
		os.Mkdir(dir, 0o700)
		return dir
	}
	dir := state()
	raw, readErr := os.ReadFile("/etc/machine-id")
	want := strings.TrimSpace(string(raw))
	first, err := daemon.HardwareID(dir)
	if readErr != nil || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(want) {
		if err == nil {
			t.Fatal("HardwareID returned an ID for a missing or malformed /etc/machine-id")
		}
	} else if err != nil || first != want {
		t.Fatalf("HardwareID: the host's machine ID = %t, %v", first == want, err)
	} else if again, _ := daemon.HardwareID(dir); again != first {
		t.Fatal("HardwareID changed within one process")
	}
	if _, err := os.Lstat(filepath.Join(dir, "hardware-id")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("reading the hardware ID left hardware-id in the Connector's state: %v", err)
	}
	// The Connector's file stands for the machine ID; a malformed one is an error, never the machine's.
	const own = "0123456789abcdef0123456789abcdef"
	for content, want := range map[string]string{own + "\n": own, "not an ID\n": ""} {
		dir := state()
		os.WriteFile(filepath.Join(dir, "hardware-id"), []byte(content), 0o600)
		if got, err := daemon.HardwareID(dir); (err == nil) != (want != "") || err == nil && got != want {
			t.Errorf("HardwareID with hardware-id %q: its ID = %t, %v", content, got == want, err)
		}
	}
}
