package linux

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"os/user"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/OpenMachineLabs/openmachine/linux"
	"golang.org/x/sys/unix"
)

// The owners om calls are stubs: scripts that run this test binary, which
// answers as Core's manage, the Connector, systemctl, loginctl or the om
// daemon would, from one world file. They keep the owners' contracts that om
// relies on: an operation ID is admitted once and read back after.

// world is the stub owners' state and the record of every call to them.
type world struct {
	Calls     []string          // each owner call, in order
	Faults    map[string]string // one-shot faults by call, such as "core install": "lost"
	Core      fakeCore
	Connector fakeConnector
	Reason    string // why the daemon is not connected, when it is not: joining or update_required
	Team      string // the slug of the team whose machine this is; "" for a person's
}

type fakeCore struct {
	Current string             // the installed payload's manifest SHA-256
	Ops     map[string]*coreOp // by operation ID
}

type coreOp struct{ Action, SHA, Selection, Phase, Reason string }

type fakeConnector struct {
	Server     string
	Bundle     string            // the selected bundle's manifest SHA-256
	Intent     string            // the bound intent's path
	Session    string            // "", pending, active, cancelled, signed_out or denied
	Device     int               // the device in use; a sign-in registers the next one
	Revoked    bool              // Cloud removed the device
	Stopped    bool              // the bound intent was not applied: enabled is not set
	Name       string            // the device name the last sign-in gave
	Key        string            // the key the last sign-in gave
	KeyRefusal string            // Cloud's refusal of a key
	Approve    int               // snapshots until a pending sign-in is approved
	Ops        map[string]record // companion operations by ID
	Setups     map[string]string // setup operations: preparing or verified
	Upgrades   map[string]string // upgrade operations: prepared or verified
}

type record map[string]any

// edit changes the world file under its lock.
func edit(path string, change func(w *world)) {
	lock, err := os.OpenFile(path+".lock", os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		panic(err)
	}
	defer lock.Close()
	unix.Flock(int(lock.Fd()), unix.LOCK_EX)
	w := world{Faults: map[string]string{}}
	if b, err := os.ReadFile(path); err == nil {
		json.Unmarshal(b, &w)
	}
	change(&w)
	// Replaced whole: a test ends the daemon while it answers.
	b, _ := json.MarshalIndent(w, "", " ")
	if err := os.WriteFile(path+".new", b, 0o600); err == nil {
		err = os.Rename(path+".new", path)
	} else {
		panic(err)
	}
}

// fault takes the fault of a call, "" without one: a one-shot, unless it is "broken".
func (w *world) fault(call string) string {
	f := w.Faults[call]
	if f == "broken" {
		return "fail"
	}
	delete(w.Faults, call)
	return f
}

// stub answers one owner call and exits. kind is the owner; self is the stub script.
func stub(kind, self string, args []string) {
	path := os.Getenv("OM_STUB_WORLD")
	home, _ := os.UserHomeDir()
	var out any
	code := 0
	switch kind {
	case "om":
		if len(args) > 0 && args[0] == "daemon" {
			edit(path, func(w *world) {
				w.Calls = append(w.Calls, "daemon "+strings.Join(append([]string{self}, args[1:]...), " "))
			})
			daemon(path, home)
		}
		// A release's own install, as om update runs it, on the same machine.
		linux.Executable = func() (string, error) { return self, nil }
		linux.User = func() (*user.User, error) {
			return &user.User{Uid: os.Getenv("OM_STUB_UID"), Username: os.Getenv("OM_STUB_USER"), HomeDir: os.Getenv("OM_STUB_ACCOUNT_HOME")}, nil
		}
		linux.Root = os.Getenv("OM_STUB_ROOT")
		os.Exit(linux.Main(args, os.Stdout, os.Stderr))
	case "core":
		edit(path, func(w *world) { out, code = w.core(home, self, args) })
	case "connector":
		stdin, _ := io.ReadAll(os.Stdin)
		var value record
		json.Unmarshal(stdin, &value)
		edit(path, func(w *world) { out, code = w.connector(home, self, args[slices.Index(args, "-B")+2:], value) })
		if refusal, ok := out.(error); ok {
			fmt.Fprintf(os.Stderr, `{"error":{"code":%q,"message":"refused"}}`+"\n", refusal.Error())
			os.Exit(1)
		}
	case "systemctl":
		var start []string
		dead := false // systemd took the job and cannot run the program
		edit(path, func(w *world) {
			w.Calls = append(w.Calls, "systemctl "+strings.Join(args, " "))
			fault := w.fault("systemctl " + args[len(args)-1])
			if len(args) > 1 && fault == "" {
				fault = w.fault("systemctl " + args[len(args)-2])
			}
			if dead = fault == "dead"; fault == "fail" {
				code = 1
			}
		})
		if dead {
			os.Exit(0)
		}
		units := filepath.Join(os.Getenv("OM_STUB_ROOT"), "etc/systemd/system")
		if args[0] == "--user" {
			units, args = filepath.Join(home, ".config/systemd/user"), args[1:]
		}
		if code == 0 && (args[0] == "restart" || args[0] == "disable" && slices.Contains(args, "--now")) {
			stop(home)
		}
		if code == 0 && (args[0] == "start" || args[0] == "restart") && holder(home) == 0 {
			unit, _ := os.ReadFile(filepath.Join(units, "openmachine.service"))
			for _, line := range strings.Split(string(unit), "\n") {
				if command, ok := strings.CutPrefix(line, "ExecStart="); ok {
					start = strings.Split(strings.Trim(command, `"`), `" "`)
				}
			}
			cmd := exec.Command(start[0], start[1:]...)
			cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
			if err := cmd.Start(); err != nil {
				code = 1
			}
		}
	case "loginctl":
		edit(path, func(w *world) { w.Calls = append(w.Calls, "loginctl "+strings.Join(args, " ")) })
		if args[0] == "show-user" {
			fmt.Println("no")
		}
	}
	if out != nil {
		json.NewEncoder(os.Stdout).Encode(out)
	}
	os.Exit(code)
}

// core answers Core's manage: status, configure, install, update, operation and continue.
func (w *world) core(home, self string, args []string) (any, int) {
	args = args[slices.Index(args, "--contract")+2:]
	option := func(name string) string {
		if i := slices.Index(args, name); i >= 0 {
			return args[i+1]
		}
		return ""
	}
	w.Calls = append(w.Calls, "core "+strings.Join(args, " "))
	state := filepath.Join(home, ".local/state/openmachine")
	read := func(id string) record {
		op, ok := w.Core.Ops[id]
		if !ok {
			return record{"status": "unknown", "reason": "service_state_invalid"}
		}
		status := map[string]string{"interrupted": "continuation_required"}[op.Phase]
		if status == "" {
			status = op.Phase
		}
		return record{"status": status, "operation": record{"operation_id": id, "phase": op.Phase, "reason": op.Reason}}
	}
	complete := func(op *coreOp) {
		// As Core retains a payload: its interpreter is the entry's from then on.
		python := filepath.Join(state, "service/payloads", op.SHA, "python/bin/python3")
		os.MkdirAll(filepath.Dir(python), 0o755)
		b, _ := os.ReadFile(self)
		os.WriteFile(python, b, 0o755)
		w.Core.Current, op.Phase = op.SHA, "completed"
	}
	switch id := option("--operation-id"); args[0] {
	case "status":
		if w.Core.Current == "" {
			return record{"status": "not_installed", "current": nil}, 0
		}
		python := filepath.Join(state, "service/payloads", w.Core.Current, "python/bin/python3")
		return record{"status": "installed", "current": w.Core.Current, "selection_sha256": "selection-of-" + w.Core.Current,
			"product_version": "0.2.29", "entry": record{"python": python, "path": filepath.Join(state, "service/manage.py"),
				"config": filepath.Join(home, ".config/openmachine/config.toml"), "state": state, "access_python": python}}, 0
	case "configure":
		config := filepath.Join(home, ".config/openmachine/config.toml")
		if _, err := os.Stat(config); (err == nil) != slices.Contains(args, "--verify-existing") {
			return record{"status": "unknown", "reason": "configuration_mismatch"}, 1
		}
		os.MkdirAll(filepath.Dir(config), 0o700)
		os.WriteFile(config, nil, 0o600)
		return record{"status": "configured"}, 0
	case "install", "update":
		if _, admitted := w.Core.Ops[id]; admitted {
			return read(id), 0
		}
		for _, other := range w.Core.Ops {
			if other.Phase != "completed" && other.Phase != "rejected" {
				return record{"status": "busy", "reason": "maintenance_busy"}, 1
			}
		}
		if _, err := os.Stat(filepath.Join(option("--payload"), "manifest.json")); err != nil {
			return record{"status": "unknown", "reason": "payload_invalid"}, 1
		}
		if args[0] == "update" && option("--expected-selection-sha256") != "selection-of-"+w.Core.Current {
			return record{"status": "rejected", "admitted": false, "reason": "selection_changed"}, 1
		}
		op := &coreOp{Action: args[0], SHA: option("--sha256"), Phase: "accepted"}
		if w.Core.Ops == nil {
			w.Core.Ops = map[string]*coreOp{}
		}
		w.Core.Ops[id] = op
		switch w.fault("core " + args[0]) {
		case "died": // its process died before activation, and no reply came
			op.Phase = "interrupted"
			return nil, 1
		case "recovery":
			op.Phase, op.Reason = "recovery_required", "maintenance_effect_unverified"
		case "lost": // completed, and the reply never arrived
			complete(op)
			return nil, 1
		default:
			complete(op)
		}
		return record{"status": "accepted", "operation": record{"operation_id": id, "phase": "accepted", "reason": ""}}, 0
	case "operation":
		return read(id), 0
	case "continue":
		if op := w.Core.Ops[id]; w.fault("core continue") == "reject" { // stopped before activation: ended, nothing selected
			op.Phase, op.Reason = "rejected", "maintenance_interrupted"
		} else {
			complete(op)
		}
		return record{"status": "accepted"}, 0
	}
	return record{"status": "unknown", "reason": "unsupported"}, 1
}

// connection is the Connector's connection: its binding and what the bound intent says.
func (w *world) connection() record {
	c := &w.Connector
	remote := record{"managed": false, "enabled": true, "revision": nil, "state": "ready", "code": nil}
	if c.Intent != "" {
		var intent struct {
			Enabled  bool
			Revision int
		}
		b, _ := os.ReadFile(c.Intent)
		json.Unmarshal(b, &intent)
		remote["managed"], remote["enabled"], remote["revision"] = true, intent.Enabled, intent.Revision
	}
	switch {
	case remote["enabled"] == false:
		remote["state"] = "off"
	case c.Revoked:
		remote["state"], remote["code"] = "pairing_required", "device_not_authorized"
	case c.Session != "active":
		remote["state"] = "pairing_required"
	case c.Stopped:
		remote["state"] = "starting"
	}
	return record{"binding": fmt.Sprint("binding-", c.Device), "server": c.Server, "paired": c.Device > 0, "remote_access": remote}
}

// connector answers the Connector's inspect, configure, setup, upgrade, remove and companion.
func (w *world) connector(home, self string, args []string, value record) (any, int) {
	c := &w.Connector
	call := "connector " + strings.Join(args, " ")
	if args[0] == "companion" || args[0] == "setup" {
		input, _ := json.Marshal(value)
		call += " " + string(input)
	}
	w.Calls = append(w.Calls, call)
	bundle := filepath.Dir(filepath.Dir(filepath.Dir(self)))
	versions := filepath.Join(home, ".local/share/openmachine/connector/versions")
	option := func(name string) string {
		if i := slices.Index(args, name); i >= 0 {
			return args[i+1]
		}
		return ""
	}
	retain := func(source string) string {
		manifest, _ := os.ReadFile(filepath.Join(source, "manifest.json"))
		sum := sha256.Sum256(manifest)
		target := filepath.Join(versions, hex.EncodeToString(sum[:]))
		if _, err := os.Stat(target); err != nil {
			os.MkdirAll(versions, 0o700)
			if out, err := exec.Command("cp", "-R", source, target).CombinedOutput(); err != nil {
				panic(string(out))
			}
		}
		return filepath.Base(target)
	}
	id, _ := value["operation_id"].(string)
	switch args[0] {
	case "inspect":
		if c.Bundle == "" {
			return record{"status": "not_installed"}, 0
		}
		return record{"status": "installed", "bundle_sha256": c.Bundle, "phase": "installed"}, 0
	case "configure":
		if c.Server == "" {
			c.Server = option("--server")
		}
		return record{"status": "configured", "server": c.Server}, 0
	case "setup":
		if c.Setups == nil {
			c.Setups = map[string]string{}
		}
		switch phase := c.Setups[id]; {
		case args[1] == "status" && phase == "":
			return record{"operation_id": id, "status": "rejected", "can_continue": false}, 0
		case args[1] == "status" && phase == "preparing":
			return record{"operation_id": id, "status": "unknown", "can_continue": true}, 0
		case args[1] == "start" && phase == "" && w.fault("setup start") == "interrupt":
			c.Setups[id] = "preparing"
			return nil, 1
		}
		c.Setups[id], c.Bundle = "verified", retain(bundle)
		return record{"operation_id": id, "status": "verified", "can_continue": false}, 0
	case "upgrade":
		id = option("--operation-id")
		if c.Upgrades == nil {
			c.Upgrades = map[string]string{}
		}
		switch phase := c.Upgrades[id]; {
		case slices.Contains(args, "--status") && phase == "":
			return record{"status": "not_admitted", "operation_id": id}, 0
		case slices.Contains(args, "--status"):
			return record{"status": phase, "operation_id": id}, 0
		case phase == "" && option("--expected-current") != c.Bundle:
			return fmt.Errorf("installation_changed"), 1
		case phase == "" && w.fault("upgrade") == "interrupt": // retained, not yet selected
			retain(option("--bundle"))
			c.Upgrades[id] = "prepared"
			return nil, 1
		case phase == "":
			retain(option("--bundle"))
		}
		previous := c.Bundle
		c.Upgrades[id], c.Bundle = "verified", option("--expected-target")
		retained, _ := os.ReadDir(versions)
		for _, version := range retained {
			if version.Name() != c.Bundle && version.Name() != previous {
				os.RemoveAll(filepath.Join(versions, version.Name()))
			}
		}
		return record{"status": "verified", "operation_id": id, "bundle_sha256": c.Bundle}, 0
	case "remove":
		if w.fault("remove") == "offline" {
			return record{"status": "incomplete", "errors": record{"cloud_revocation": "cloud_unavailable"}}, 1
		}
		os.RemoveAll(filepath.Join(versions, c.Bundle))
		os.RemoveAll(filepath.Join(home, ".local/state/openmachine-connector"))
		*c = fakeConnector{}
		return record{"status": "removed"}, 0
	}
	// companion
	action := args[1]
	switch action {
	case "connection":
		return w.connection(), 0
	case "snapshot":
		fault := w.fault("snapshot")
		if fault == "busy" { // another command holds the Connector
			return fmt.Errorf("connector_busy"), 1
		}
		if c.Session == "pending" {
			if c.Approve == 0 { // approved in a browser: the sign-in registers a device
				c.Session, c.Revoked = "active", false
				c.Device++
			}
			c.Approve--
		}
		snapshot := w.connection()
		snapshot["account"], snapshot["errors"] = nil, record{}
		if fault == "unreachable" {
			snapshot["errors"] = record{"account": "cloud_unavailable"}
		} else if c.Session != "" {
			snapshot["account"] = record{"status": c.Session}
		}
		return snapshot, 0
	case "operation":
		if op, ok := c.Ops[id]; ok {
			if op["status"] == "unknown" { // read back once Cloud answers
				op["status"], op["result"] = "verified", w.perform(op["action"].(string), op["input"].(map[string]any))
				delete(op, "error")
			}
			return op, 0
		}
		return record{"operation_id": id, "action": "not-admitted", "status": "rejected", "result": record{"status": "not_admitted"}}, 0
	}
	if value["expected_binding"] != w.connection()["binding"] {
		return fmt.Errorf("connector_binding_changed"), 1
	}
	if op, ok := c.Ops[id]; ok {
		return op, 0
	}
	if c.Ops == nil {
		c.Ops = map[string]record{}
	}
	op := record{"operation_id": id, "action": action, "input": value, "status": "verified", "can_continue": false}
	c.Ops[id] = op
	switch w.fault(action) {
	case "unknown": // Cloud could not be reached: nothing happened yet
		op["status"], op["error"] = "unknown", "cloud_unavailable"
		return op, 0
	case "lost": // done, and the reply never arrived
		op["result"] = w.perform(action, value)
		return nil, 1
	}
	if (action == "login" || action == "move") && value["key"] != nil && c.Session != "pending" && c.KeyRefusal != "" {
		op["status"], op["error"] = "rejected", c.KeyRefusal
		return op, 0
	}
	op["result"] = w.perform(action, value)
	delete(op["input"].(record), "key") // the Connector keeps a key out of its record
	return op, 0
}

// perform applies a companion action and returns its result.
func (w *world) perform(action string, value map[string]any) record {
	c := &w.Connector
	switch action {
	case "remote-access":
		c.Intent, c.Stopped = value["intent_path"].(string), false
		return record{"status": w.connection()["remote_access"].(record)["state"]}
	case "login":
		if c.Session == "pending" { // the Connector returns a pending sign-in and ignores a key given then
			return record{"status": "pending", "verification_uri": c.Server + "/native-login?id=session"}
		}
		c.Name, _ = value["device_name"].(string)
		if key, ok := value["key"].(string); ok {
			c.Key, c.Session, c.Revoked = key, "active", false
			c.Device++
			return record{"status": "active"}
		}
		c.Session = "pending"
		return record{"status": "pending", "verification_uri": c.Server + "/native-login?id=session"}
	case "move": // the machine registers again in the account it was moved to
		c.Key, _ = value["key"].(string)
		c.Device++
		return record{"status": "active"}
	case "cancel-login":
		c.Session = "cancelled"
	case "sign-out":
		c.Session = "signed_out"
	}
	return record{"status": c.Session}
}

// holder is the process that holds the daemon's lock, 0 when none does.
func holder(home string) int {
	f, err := os.Open(filepath.Join(home, ".local/state/openmachine-om/daemon.lock"))
	if err != nil {
		return 0
	}
	defer f.Close()
	if unix.Flock(int(f.Fd()), unix.LOCK_SH|unix.LOCK_NB) == nil {
		return 0
	}
	var pid int
	fmt.Fscan(f, &pid)
	return pid
}

// stop ends the daemon and waits until it released its lock.
func stop(home string) {
	if pid := holder(home); pid > 0 {
		syscall.Kill(pid, syscall.SIGTERM)
	}
	for deadline := time.Now().Add(10 * time.Second); holder(home) != 0 && time.Now().Before(deadline); {
		time.Sleep(10 * time.Millisecond)
	}
}

// daemon is the om daemon as om sees it: it holds daemon.lock, with its
// process ID, and answers status and watch on daemon.sock until it is ended.
func daemon(path, home string) {
	state := filepath.Join(home, ".local/state/openmachine-om")
	lock, err := os.OpenFile(filepath.Join(state, "daemon.lock"), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil || unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB) != nil {
		os.Exit(1)
	}
	defer lock.Close() // held, and so referenced, for as long as the daemon runs
	lock.Truncate(0)
	lock.WriteAt([]byte(fmt.Sprint(os.Getpid())), 0)
	os.Remove(filepath.Join(state, "daemon.sock"))
	ln, err := net.Listen("unix", filepath.Join(state, "daemon.sock"))
	if err != nil {
		os.Exit(1)
	}
	ended := make(chan os.Signal, 1)
	signal.Notify(ended, syscall.SIGTERM)
	go func() { <-ended; os.Exit(0) }()
	for {
		c, err := ln.Accept()
		if err != nil {
			os.Exit(1)
		}
		line, _ := bufio.NewReader(c).ReadString('\n')
		edit(path, func(w *world) {
			remote := w.connection()["remote_access"].(record)
			switch strings.TrimSpace(line) {
			case "status":
				if reason := w.Reason; reason != "" {
					fmt.Fprintf(c, `{"connected":false,"reason":%q}`+"\n", reason)
				} else if remote["state"] != "ready" {
					fmt.Fprintln(c, `{"connected":false,"reason":"offline"}`)
				} else {
					fmt.Fprintln(c, `{"connected":true}`)
				}
			case "watch":
				if w.Team != "" { // the view of a team's machine: the team, and itself under the name its members type
					fmt.Fprintf(c, `{"live":true,"device_id":"dev_1","account":{"login":%q,"provider":"team"},"policy":{"mode":"full_access"},`+
						`"devices":[{"device_id":"dev_1","name":%q,"alias":"gpu-01.%s"}]}`+"\n", w.Team, w.Connector.Name, w.Team)
					break
				}
				fmt.Fprintf(c, `{"live":true,"device_id":"dev_1","account":{"login":"alice"},"policy":{"mode":"full_access"},`+
					`"devices":[{"device_id":"dev_0","name":"Laptop","alias":"laptop"},{"device_id":"dev_1","name":%q,"alias":"gpu-01"}]}`+"\n", w.Connector.Name)
			}
		})
		c.Close()
	}
}
