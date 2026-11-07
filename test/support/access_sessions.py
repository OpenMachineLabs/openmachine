"""Drive python -I -m openmachine.access the way the Go host does: request and reply on a control socket."""

import json
import os
from pathlib import Path
import pty
import pwd
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

# Restricted homes go under the root-owned sticky /private/tmp, never the real /Users/Shared, with a name
# owned by this test process so concurrent runs never share or delete each other's home.
SHARED = Path("/private/tmp")
HOME_NAME = f"om-access-test-{os.getpid()}-{{uid}}"
HOME = SHARED / HOME_NAME.format(uid=os.getuid()) / "home"
BOOT = ("import sys; from pathlib import Path; from openmachine.access import sessions; "
        f"sessions.SHARED = Path({str(SHARED)!r}); sessions.HOME_NAME = {HOME_NAME!r}; "
        "from openmachine.access.__main__ import main; sys.exit(main(sys.argv[1:]))")
USER = pwd.getpwuid(os.getuid()).pw_name


class SessionFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-access-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version = 1\n\n[machines.local]\ntransport = "local"\nlocal_access = "full_access"\n'
                               'execution_enabled = true\nworkspace_write_enabled = true\n')
        self.lifetimes = set()
        self.addCleanup(self.end_all)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(HOME.parent, ignore_errors=True)

    def lifetime(self):
        name = "t" + uuid.uuid4().hex[:12]
        self.lifetimes.add(name)
        return name

    def end_all(self):
        for name in self.lifetimes:
            self.terminate(name)

    def command(self, *args):
        return [sys.executable, "-I", "-c", BOOT, "--config", str(self.config), "--state-dir", str(self.state), *args]

    def request(self, lifetime, policy, kind, command=None, **extra):
        value = {"protocol": 1, "user": USER, "lifetime": lifetime, "policy": policy, "kind": kind, **extra}
        if command is not None:
            value["command"] = command
        return value

    def session(self, lifetime, policy="read_only", command="true", *, kind="exec", **extra):
        """Run one channel with pipe IO; returns (reply, exit code, stdout, stderr)."""
        return self.admit(self.request(lifetime, policy, kind, command, **extra))

    def admit(self, value, stdin=b""):
        process, reply = self.start(value)
        out, err = process.communicate(stdin, timeout=60)
        return reply, process.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    def start(self, value):
        """Launch one channel and return the running launcher with its admission reply."""
        parent, child = socket.socketpair()
        process = subprocess.Popen(self.command("session", "--control-fd", str(child.fileno())),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=[child.fileno()], start_new_session=True)
        child.close()
        with parent:
            parent.sendall(json.dumps(value).encode() + b"\n")
            reply = json.loads(parent.makefile("rb").readline() or b"{}")
        return process, reply

    def wait_for(self, path, seconds=10):
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return path.exists()

    def terminal(self, lifetime, typed, until, *, policy="read_only"):
        """Run an interactive shell on a PTY, type input, and read until a computed marker appears."""
        master, slave = pty.openpty()
        parent, child = socket.socketpair()
        process = subprocess.Popen(self.command("session", "--control-fd", str(child.fileno())), stdin=slave,
                                   stdout=slave, stderr=slave, pass_fds=[child.fileno()], start_new_session=True)
        os.close(slave)
        child.close()
        with parent:
            parent.sendall(json.dumps(self.request(lifetime, policy, "shell", terminal={"term": "xterm-256color"})).encode()
                           + b"\n")
            reply = json.loads(parent.makefile("rb").readline() or b"{}")
        os.write(master, typed.encode())
        output, deadline = b"", time.monotonic() + 20
        while until.encode() not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
        return reply, process, master, output.decode(errors="replace")

    def terminate(self, lifetime, *, background=False):
        if background:
            process = subprocess.Popen(self.command("terminate"), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            process.stdin.write(json.dumps({"protocol": 1, "lifetime": lifetime}).encode())
            process.stdin.close()
            return process
        result = subprocess.run(self.command("terminate"), input=json.dumps({"protocol": 1, "lifetime": lifetime}).encode(),
                                capture_output=True, timeout=60)
        return json.loads(result.stdout or b"{}"), result.returncode

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")

    def beating(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        before = path.stat().st_mtime_ns
        time.sleep(0.8)
        return path.stat().st_mtime_ns != before
