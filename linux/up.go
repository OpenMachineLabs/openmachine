package linux

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// intent is the connection intent, in Desktop's format.
type intent struct {
	Format   int    `json:"format"`
	Enabled  bool   `json:"enabled"`
	Revision uint64 `json:"revision"`
}

// connect turns the connection on or off: it writes the intent at a new
// revision and has the Connector bind and apply it. With nothing to change it
// changes nothing. It returns the Connector's connection.
func (m *machine) connect(on bool) (map[string]any, error) {
	dir, err := filepath.EvalSymlinks(m.state) // the Connector binds the resolved path
	if err != nil {
		return nil, err
	}
	path := filepath.Join(dir, "remote-access.json")
	var current intent
	b, err := os.ReadFile(path)
	if err == nil {
		err = json.Unmarshal(b, &current)
	}
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("the connection intent is unreadable (%v)", err)
	}
	connection, err := m.connection()
	if err != nil {
		return nil, err
	}
	bound, _ := field[map[string]any](connection, "remote_access")
	revision, _ := bound["revision"].(float64)
	// The Connector reads the same file, so only its state shows that it applied the intent: starting is an apply that never finished.
	if current.Format == 1 && current.Enabled == on && bound["managed"] == true && uint64(revision) == current.Revision && bound["state"] != "starting" {
		return connection, nil
	}
	if current.Format != 1 || current.Enabled != on {
		current = intent{1, on, current.Revision + 1}
		b, _ := json.Marshal(current)
		if err := replace(path, b, 0o600); err != nil {
			return nil, err
		}
	}
	if _, err := m.companion("remote-access", map[string]any{"expected_binding": connection["binding"],
		"intent_path": path, "revision": current.Revision}); err != nil {
		return nil, err
	}
	return m.connection()
}

// up is om up: each step is skipped when already true.
func (m *machine) up(key, name string) error {
	if err := m.serve(); err != nil {
		return fail("the service could not be started (%v). Run: om up", err)
	}
	// The intent is bound before login: a sign-in connects only if the configuration has not changed since it began.
	connection, err := m.connect(true)
	if err != nil {
		return refused("turning the connection on", err)
	}
	if text(connection, "remote_access", "state") == "pairing_required" {
		if err := m.login(connection, key, name); err != nil {
			return err
		}
	}
	return m.approved()
}

// MoveWait is how long om move reads back a move whose reply was lost, and
// MoveStep how often: Cloud decides a move within its secret's minute. Tests
// replace them.
var MoveWait, MoveStep = 90 * time.Second, 3 * time.Second

// move is om move: this machine registers again in the account it was moved
// to in the Console, with that move's one-time secret, and the device it
// leaves is removed with that approval. A move that fails leaves the machine
// connected as it was.
func (m *machine) move(secret string) error {
	connection, err := m.connection()
	if err != nil {
		return refused("reading the connection", err)
	}
	if text(connection, "remote_access", "state") != "ready" {
		return fail("this machine is not connected, so it was not moved. Run: om up")
	}
	result, err := m.companion("move", map[string]any{"expected_binding": connection["binding"], "key": secret})
	// A reply that was lost leaves the move recorded. It may have removed the
	// device in use, and nobody is at this machine to run om again: its outcome
	// is read back by its ID, never sent again, until Cloud has decided it.
	read := false
	for deadline := time.Now().Add(MoveWait); err != nil; {
		op, unknown := m.rec.Operations["companion"]
		if !unknown || time.Now().After(deadline) {
			break
		}
		time.Sleep(MoveStep)
		var reply map[string]any
		if reply, err = m.connector(m.bundle, map[string]any{"operation_id": op.ID}, "companion", "operation"); err == nil {
			result, err = m.settled(reply)
			read = err == nil
		}
	}
	if err != nil {
		switch c := code(err); c {
		case "key_invalid", "key_platform", "native_device_limit", "native_login_settled", "move_refused", "move_unavailable":
			return fail("this machine was not moved (%s). Move it again in the Console.", c)
		}
		return refused("the move", err)
	}
	// Read back, a move Cloud did not approve says how its sign-in ended.
	if status := text(result, "status"); read && status != "active" {
		return fail("this machine was not moved (%s). Move it again in the Console.", status)
	}
	return m.approved()
}

// approved waits until an approved sign-in's device is in use and connected.
func (m *machine) approved() error {
	// snapshot records an approved device on this machine; a link has the sign-in's five minutes.
	for deadline := time.Now().Add(5 * time.Minute); ; time.Sleep(2 * time.Second) {
		snapshot, err := m.snapshot()
		// Another command, such as om status, holds the Connector only for a moment: the next read waits it out.
		if err != nil && (code(err) != "connector_busy" || time.Now().After(deadline)) {
			return refused("reading the sign-in", err)
		}
		switch text(snapshot, "remote_access", "state") {
		case "ready":
			return m.connected()
		case "error":
			return fail("the connection intent cannot be read (%s). Run: om down, then om up", text(snapshot, "remote_access", "code"))
		}
		switch status := text(snapshot, "account", "status"); status {
		case "denied", "cancelled", "expired", "signed_out":
			return fail("the sign-in was %s. Run: om up", strings.ReplaceAll(status, "_", " "))
		}
		if time.Now().After(deadline) {
			return fail("the sign-in was not approved in time. Run: om up")
		}
	}
}

// connected waits until the daemon serves the device now in use and reports
// it. A sign-in that changed the device made the daemon exit; only the shell
// kind has no supervisor to start it again.
func (m *machine) connected() error {
	for deadline := time.Now().Add(60 * time.Second); ; time.Sleep(time.Second) {
		if m.rec.Service == "shell" && m.daemonPID() == 0 {
			if err := m.start(); err != nil {
				return fail("the daemon could not be started (%v). Run: om up", err)
			}
		}
		reason := m.disconnected()
		if reason == "" {
			break
		}
		if reason == "update_required" {
			return fail("Update required. Run: om update")
		}
		if time.Now().After(deadline) {
			return fail("not connected yet (%s). Run: om status", reason)
		}
	}
	view := m.view()
	host, alias := view.self()
	if alias != "" {
		host = alias
	}
	fmt.Fprintf(m.out, "Connected: %s (user %s)%s.\n", host, m.user, view.permission(", "))
	return nil
}

// disconnected asks the daemon why this machine is not connected: "" when it is.
func (m *machine) disconnected() string {
	status := struct {
		Connected bool
		Reason    string
	}{Reason: "not_running"}
	if b, err := m.ask("status"); err == nil {
		json.Unmarshal(b, &status)
	}
	if status.Connected {
		return ""
	}
	return status.Reason
}

// login signs in: with a key Cloud approves the sign-in itself; without one it
// prints the link to open in a browser.
func (m *machine) login(connection map[string]any, key, name string) error {
	if key != "" {
		// The Connector returns a sign-in that is already pending and ignores a key given then.
		if snapshot, err := m.snapshot(); err != nil {
			return refused("reading the sign-in", err)
		} else if text(snapshot, "account", "status") == "pending" {
			if _, err := m.companion("cancel-login", map[string]any{"expected_binding": snapshot["binding"]}); err != nil {
				return refused("ending the earlier sign-in", err)
			}
		}
	}
	input := map[string]any{"expected_binding": connection["binding"]}
	if name != "" {
		input["device_name"] = name
	}
	if key != "" {
		input["key"] = key
	}
	result, err := m.companion("login", input)
	if err != nil {
		return refused("the sign-in", err)
	}
	if result["status"] == "pending" {
		fmt.Fprintf(m.out, "Open this link in a browser to connect this machine:\n  %s\n", text(result, "verification_uri"))
	}
	return nil
}

// refused is the line for an owner's refusal of what om was doing: Cloud's
// code for a key is shown as it is.
func refused(what string, err error) error {
	switch c := code(err); c {
	case "key_invalid", "key_platform", "native_device_limit", "native_login_settled":
		return fail("the key was refused (%s). Create a new key in the Console and run the command again.", c)
	case "cloud_unavailable", "cloud_busy":
		return fail("%s could not reach Cloud (%s). Check the network and run the command again.", what, c)
	case "connector_busy":
		return fail("another Open Machine command is running. Run this one again when it ends.")
	default:
		return fail("%s failed (%s). Run the command again.", what, c)
	}
}

// view is the account view the daemon serves, as far as om prints it.
type view struct {
	DeviceID string `json:"device_id"`
	Account  *struct {
		Login string `json:"login"`
	} `json:"account"`
	Policy *struct {
		Mode string `json:"mode"`
	} `json:"policy"`
	Devices []struct {
		DeviceID string  `json:"device_id"`
		Name     string  `json:"name"`
		Alias    *string `json:"alias"`
	} `json:"devices"`
}

// view reads the daemon's account view; it is empty when the daemon gives none.
func (m *machine) view() (v view) {
	if b, err := m.ask("watch"); err == nil {
		json.Unmarshal(b, &v)
	}
	return v
}

// self is this machine in the view: its name and the alias the computers that
// reach it call it by, each "" when unknown.
func (v view) self() (name, alias string) {
	for _, d := range v.Devices {
		if d.DeviceID == v.DeviceID {
			if d.Alias != nil {
				alias = *d.Alias + ".om"
			}
			return d.Name, alias
		}
	}
	return "", ""
}

// permission is the machine's permission as the Console names it, after lead, or "" when unknown.
func (v view) permission(lead string) string {
	if v.Policy == nil {
		return ""
	}
	label := map[string]string{"read_only": "Read only", "workspace": "Workspace", "full_access": "Full access"}[v.Policy.Mode]
	if label == "" {
		return ""
	}
	return lead + label
}

// status is om status: one state line, then what is known of the machine.
func (m *machine) status(asJSON bool) error {
	// snapshot learns from Cloud that the machine was removed; without the Connector's lock or Cloud, connection still answers.
	connection, err := m.snapshot()
	if err != nil {
		if connection, err = m.connection(); err != nil {
			return refused("reading the connection", err)
		}
	}
	state, line := "connecting", "Connecting"
	v := view{}
	switch remote := text(connection, "remote_access", "state"); {
	case remote == "off" || remote == "error": // an intent that cannot be read is off
		state, line = "disconnected", "Disconnected. Run: om up"
	case remote == "pairing_required" && text(connection, "remote_access", "code") == "device_not_authorized":
		state, line = "removed", "Removed from the account. Run: om up"
	case remote == "pairing_required":
		state, line = "signed_out", "Signed out. Run: om up"
	case m.daemonPID() == 0: // the daemon opens its socket only once a device exists
		state, line = "not_running", "Not running. Run: om up"
	default:
		switch m.disconnected() {
		case "":
			state, line = "connected", "Connected"
		case "update_required":
			state, line = "update_required", "Update required. Run: om update"
		}
		v = m.view()
	}
	name, alias := v.self()
	account := ""
	if v.Account != nil {
		account = v.Account.Login
	}
	if asJSON {
		mode := ""
		if v.Policy != nil {
			mode = v.Policy.Mode
		}
		b, _ := json.Marshal(map[string]any{"state": state, "name": name, "alias": alias, "user": m.user, "account": account,
			"permission": mode, "version": m.rec.Release.Version})
		fmt.Fprintf(m.out, "%s\n", b)
	} else {
		fmt.Fprintln(m.out, line)
		for _, item := range [][2]string{{"Name", name}, {"Alias", alias}, {"User", m.user}, {"Account", account},
			{"Permission", v.permission("")}, {"Version", m.rec.Release.Version}} {
			if item[1] != "" {
				fmt.Fprintf(m.out, "%s: %s\n", item[0], item[1])
			}
		}
	}
	if state != "connected" {
		return &failure{} // the state line said it
	}
	return nil
}

// coreVersion is the installed Core's version, "" when Core does not answer.
func (m *machine) coreVersion() string {
	reply, err := m.manage("", "status")
	if err != nil {
		return ""
	}
	return text(reply, "product_version")
}

// logout signs out; the device stays registered.
func (m *machine) logout() error {
	snapshot, err := m.snapshot()
	if err != nil {
		return refused("reading the sign-in", err)
	}
	if unread := text(snapshot, "errors", "account"); unread != "" { // such as Cloud out of reach: nothing says it is signed out
		return refused("reading the sign-in", &ownerError{unread})
	}
	action := map[string]string{"active": "sign-out", "pending": "cancel-login"}[text(snapshot, "account", "status")]
	if action != "" {
		if _, err := m.companion(action, map[string]any{"expected_binding": snapshot["binding"]}); err != nil {
			return refused("signing out", err)
		}
	}
	fmt.Fprintln(m.out, "Signed out.")
	return nil
}
