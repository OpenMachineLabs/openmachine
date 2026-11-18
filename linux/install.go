package linux

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
)

// bundled is release.json of an unpacked release.
type bundled struct {
	Format       int    `json:"format"`
	Version      string `json:"version"`
	Arch         string `json:"arch"`
	ConnectorSHA string `json:"connector_manifest_sha256"`
	CoreSHA      string `json:"core_manifest_sha256"`
}

var versionForm = regexp.MustCompile(`^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$`)

// arch is this machine's architecture as releases name it.
func arch() string {
	return map[string]string{"amd64": "x86_64", "arm64": "aarch64"}[runtime.GOARCH]
}

// newer reports whether release version a is newer than b.
func newer(a, b string) bool {
	x, y := versionForm.FindStringSubmatch(a), versionForm.FindStringSubmatch(b)
	for i := 1; i < len(x) && i < len(y); i++ {
		n, _ := strconv.Atoi(x[i])
		m, _ := strconv.Atoi(y[i])
		if n != m {
			return n > m
		}
	}
	return false
}

// install installs or updates this machine from the unpacked release this
// om runs from: Core, the Connector, the link and the service, then om up.
// Every step is skipped when already true, so an interrupted run is finished
// by the next one.
func (m *machine) install(server, channel, downloads, key, name string, noUp bool) error {
	if err := m.take(); err != nil {
		return err
	}
	root := filepath.Dir(m.bundle)
	var rel bundled
	b, err := os.ReadFile(filepath.Join(root, "release.json"))
	if err == nil {
		err = json.Unmarshal(b, &rel)
	}
	if err != nil || rel.Format != 1 || !versionForm.MatchString(rel.Version) || rel.Arch != arch() {
		return fail("this is not a release for this machine. Run the install command again.")
	}
	if m.rec.Release.Version == "" && m.rec.Release.Channel == "" {
		if (channel != "stable" && channel != "staging") || downloads == "" {
			return fail("the install command names no release channel. Run the install command again.")
		}
		m.rec.Release.Channel, m.rec.Release.Downloads = channel, strings.TrimRight(downloads, "/")
		if err := m.save(); err != nil {
			return err
		}
	}
	if m.rec.Release.Version == "" || newer(rel.Version, m.rec.Release.Version) {
		if err := m.installCore(filepath.Join(root, "core"), rel.CoreSHA); err != nil {
			return err
		}
		if err := m.installConnector(filepath.Join(root, "connector"), rel.ConnectorSHA, server); err != nil {
			return err
		}
		if err := m.relink(); err != nil {
			return fail("%s could not be linked (%v).", m.link(), err)
		}
		service := m.serve
		if m.rec.Service == "" {
			service = m.chooseService
		}
		if err := service(); err != nil {
			return fail("the service could not be set up (%v). Run the install command again.", err)
		}
		m.rec.Release.Version = rel.Version
		if err := m.save(); err != nil {
			return err
		}
		boot := "It starts at boot."
		if m.rec.Service == "shell" {
			boot = "It starts when you open a shell: this machine has no service manager."
		}
		fmt.Fprintf(m.out, "Open Machine %s installed for %s. %s\n", rel.Version, m.user, boot)
		if dir := filepath.Dir(m.link()); !slices.Contains(filepath.SplitList(os.Getenv("PATH")), dir) {
			fmt.Fprintf(m.out, "om is %s: %s is not on PATH.\n", m.link(), dir)
		}
	} else if target, err := filepath.EvalSymlinks(m.link()); err == nil {
		m.bundle = filepath.Dir(target) // installed, and this release is not newer: only om up
	} else {
		return fail("%s is missing (%v). Run: %s uninstall, then the install command.", m.link(), err, filepath.Join(m.bundle, "om"))
	}
	os.RemoveAll(m.path("releases")) // Core and the Connector keep their own copies
	if noUp {
		return nil
	}
	if err := m.settle(); err != nil {
		return err
	}
	return m.up(key, name)
}

// installCore installs Core's payload, or updates Core to it, under a recorded
// operation ID, and records Core's bound entry.
func (m *machine) installCore(payload, sha string) error {
	if op, ok := m.rec.Operations["core"]; ok {
		if err := m.resumeCore(op, payload); err != nil {
			return m.coreFailure(err)
		}
	}
	status, err := m.manage(payload, "status")
	if err != nil {
		return m.coreFailure(err)
	}
	switch {
	case status["status"] == "not_installed":
		configure := []string{"configure"}
		if _, err := os.Stat(m.coreConfig()); err == nil {
			configure = append(configure, "--verify-existing") // an interrupted install wrote it
		}
		if reply, err := m.manage(payload, configure...); err != nil || reply["status"] != "configured" {
			return fail("Core could not be configured (%s). Run the install command again.", text(reply, "reason"))
		}
		op, err := m.begin("core", "install", map[string]string{"payload": payload, "sha256": sha})
		if err == nil {
			err = m.coreOperation(payload, op, false)
		}
		if err != nil {
			return m.coreFailure(err)
		}
	case status["status"] != "installed":
		return fail("Core is not ready (%s%s). Run the command again in a minute.", status["status"], text(status, "reason"))
	case status["current"] != sha:
		if err := m.bind(status); err != nil { // the installed Core admits its own update
			return err
		}
		op, err := m.begin("core", "update", map[string]string{"payload": payload, "sha256": sha,
			"selection": text(status, "selection_sha256")})
		if err == nil {
			err = m.coreOperation("", op, false)
		}
		if err != nil {
			return m.coreFailure(err)
		}
	default:
		return m.bind(status)
	}
	if status, err = m.manage(payload, "status"); err != nil {
		return m.coreFailure(err)
	}
	return m.bind(status)
}

// bind records Core's management entry from its status.
func (m *machine) bind(status map[string]any) error {
	entry, _ := field[map[string]any](status, "entry")
	b, _ := json.Marshal(entry)
	var bound coreEntry
	if json.Unmarshal(b, &bound) != nil || bound.Python == "" || bound.Path == "" || bound.AccessPython == "" {
		return fail("Core reports no installation (%s). Run the command again.", status["status"])
	}
	m.rec.Core = bound
	return m.save()
}

// resumeCore follows a recorded Core operation to its end. Core admits an ID
// once, so sending it again reads it back. Without its payload it can only be
// read, by the installed Core or else this release's, and one Core holds no
// record of was never admitted.
func (m *machine) resumeCore(op operation, payload string) error {
	if op.Action == "update" {
		payload = "" // the installed Core admitted it
	}
	if _, err := os.Stat(op.Input["payload"]); err == nil {
		if op.Action == "install" {
			payload = op.Input["payload"]
		}
		return m.coreOperation(payload, op, true)
	}
	reply, err := m.manage(payload, "operation", "--operation-id", op.ID)
	if err != nil {
		return err
	}
	if _, admitted := reply["operation"]; !admitted {
		return m.end("core")
	}
	return m.follow(payload, op, reply, true)
}

// command is how this machine's om is run: its link, or before one exists the om that is installing.
func (m *machine) command() string {
	if _, err := os.Lstat(m.link()); err == nil {
		return "om"
	}
	return filepath.Join(m.bundle, "om")
}

// coreFailure is the line for a Core operation that did not complete.
func (m *machine) coreFailure(err error) error {
	if errors.Is(err, errRecovery) {
		return fail("Core stopped an update it can neither finish nor end (%v), and it blocks every later one. Run: %s uninstall, then the install command.", err, m.command())
	}
	if f := (*failure)(nil); errors.As(err, &f) {
		return err
	}
	return fail("Core could not be installed (%v). Run the command again.", err)
}

// installConnector installs the Connector bundle, or upgrades to it, under
// recorded operation IDs, and moves om to the selected bundle.
func (m *machine) installConnector(source, sha, server string) error {
	selected := filepath.Join(m.home, ".local/share/openmachine/connector/versions", sha)
	done := func(reply map[string]any, err error, owner string) error {
		if err == nil && reply["status"] != "verified" {
			err = &ownerError{fmt.Sprint(reply["status"])}
		}
		if err != nil {
			return fail("the Connector could not be installed (%s). Run the install command for this version again.", code(err))
		}
		return m.end(owner)
	}
	if op, ok := m.rec.Operations["setup"]; ok {
		// The Connector continues a setup only from the bundle it began with.
		began := op.Input["bundle"]
		if _, err := os.Stat(began); err != nil {
			began = source
		}
		request := map[string]any{"operation_id": op.ID}
		reply, err := m.connector(began, request, "setup", "status")
		if err == nil && reply["can_continue"] == true {
			reply, err = m.connector(began, request, "setup", "continue")
		}
		if err == nil && (reply["status"] == "rejected" || reply["status"] == "cancelled") {
			reply["status"] = "verified" // over without an installation: set up again below
		}
		if err := done(reply, err, "setup"); err != nil {
			return err
		}
	}
	if err := m.resumeUpgrade(source); err != nil {
		return err
	}
	installed, err := m.connector(source, nil, "inspect")
	if err != nil {
		return fail("the Connector could not be read (%s). Run the install command again.", code(err))
	}
	switch current := text(installed, "bundle_sha256"); {
	case installed["status"] == "not_installed":
		if _, err := m.connector(source, nil, "configure", "--server", server); err != nil {
			return fail("the Connector could not be configured (%s). Run the install command again.", code(err))
		}
		op, err := m.begin("setup", "start", map[string]string{"bundle": source})
		if err != nil {
			return err
		}
		reply, err := m.connector(source, map[string]any{"operation_id": op.ID}, "setup", "start")
		if err := done(reply, err, "setup"); err != nil {
			return err
		}
	case current != sha:
		op, err := m.begin("upgrade", "upgrade", map[string]string{"current": current, "target": sha})
		if err != nil {
			return err
		}
		reply, err := m.connector(source, nil, "upgrade", "--operation-id", op.ID, "--bundle", source,
			"--expected-current", current, "--expected-target", sha)
		if err := done(reply, err, "upgrade"); err != nil {
			return err
		}
	}
	m.bundle = selected
	return nil
}

// resumeUpgrade reads back a recorded Connector upgrade by its ID and
// continues one the Connector had prepared.
func (m *machine) resumeUpgrade(bundle string) error {
	op, ok := m.rec.Operations["upgrade"]
	if !ok {
		return nil
	}
	reply, err := m.connector(bundle, nil, "upgrade", "--status", "--operation-id", op.ID)
	if err == nil && reply["status"] != "verified" && reply["status"] != "not_admitted" {
		reply, err = m.connector(bundle, nil, "upgrade", "--continue", "--operation-id", op.ID,
			"--expected-current", op.Input["current"], "--expected-target", op.Input["target"])
	}
	if err == nil && reply["status"] != "verified" && reply["status"] != "not_admitted" {
		err = &ownerError{fmt.Sprint(reply["status"])}
	}
	if err != nil {
		return fail("an earlier Connector upgrade is unfinished (%s). Run the command again.", code(err))
	}
	return m.end("upgrade")
}

// relink points om at the selected bundle's om. It replaces only a link.
func (m *machine) relink() error {
	link, target := m.link(), filepath.Join(m.bundle, "om")
	if fi, err := os.Lstat(link); err == nil && fi.Mode()&fs.ModeSymlink == 0 {
		return errors.New("it exists and is not Open Machine's link")
	}
	if current, _ := os.Readlink(link); current == target {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(link), 0o755); err != nil {
		return err
	}
	os.Remove(link + ".new")
	if err := os.Symlink(target, link+".new"); err != nil {
		return err
	}
	return os.Rename(link+".new", link)
}

// uninstall revokes this machine's devices, removes the service, the link
// and the shell line, then deletes Open Machine's directories. The sandbox
// home and running jobs are kept.
func (m *machine) uninstall() error {
	if err := m.take(); err != nil {
		return err
	}
	if err := m.resumeUpgrade(m.bundle); err != nil { // the Connector removes nothing while one is pending
		return err
	}
	// The service goes first: the Connector's removal deletes the bundle om and the daemon run from.
	if err := m.unserve(); err != nil {
		return fail("the service could not be removed (%v). Run: %s uninstall", err, m.command())
	}
	reply, err := m.connector(m.bundle, nil, "remove")
	if err == nil && reply["status"] != "removed" {
		err = &ownerError{text(reply, "errors", "cloud_revocation") + text(reply, "errors", "local_removal")}
	}
	if err != nil {
		return fail("this machine could not be removed from the account (%s); its service is stopped. Check the network and run: %s uninstall", code(err), m.command())
	}
	if fi, err := os.Lstat(m.link()); err == nil && fi.Mode()&fs.ModeSymlink != 0 {
		os.Remove(m.link())
	}
	for _, dir := range []string{filepath.Join(m.home, ".local/share/openmachine"), m.coreState(), filepath.Dir(m.coreConfig()),
		filepath.Join(m.home, ".local/state/openmachine-connector"), m.state} {
		// Core's payloads are read-only.
		filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
			if err == nil && d.IsDir() {
				os.Chmod(path, 0o700)
			}
			return nil
		})
		if err := os.RemoveAll(dir); err != nil {
			return fail("%s could not be deleted (%v). Delete it to finish.", dir, err)
		}
	}
	fmt.Fprintln(m.out, "Open Machine was removed. Running jobs were kept.")
	if m.rec.Service == "user" {
		fmt.Fprintf(m.out, "Linger stays on for %s, which keeps them running after logout. To turn it off: loginctl disable-linger %s\n", m.user, m.user)
	}
	return nil
}
