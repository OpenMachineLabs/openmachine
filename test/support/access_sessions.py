"""Drive python -I -m openmachine.access the way the Go host does: request and reply on a control socket."""

import json
import os
from pathlib import Path
import pty
import pwd
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from openmachine.access import restricted, sessions
from openmachine.access.macos import seatbelt as macos

# Restricted homes go under the root-owned sticky temp directory, never the real /Users/Shared, with a name
# owned by this test process so concurrent runs never share or delete each other's home.
SHARED = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
HOME_NAME = f"om-access-test-{os.getpid()}-{{uid}}"
HOME = SHARED / HOME_NAME.format(uid=os.getuid()) / "home"
HOMES = sessions.keyed_homes(HOME.parent)  # Where the homes that requests name by key are.
# Core's copy of om-code is per user too; each test process keeps its own.
CLIENT_NAME = f"client-{os.getpid()}"
BOOT = ("import sys; from pathlib import Path; from openmachine.access import sessions; "
        "from openmachine.access import restricted; "
        f"sessions.SHARED = Path({str(SHARED)!r}); sessions.HOME_NAME = {HOME_NAME!r}; restricted.CLIENT_NAME = {CLIENT_NAME!r}; "
        "from openmachine.access.__main__ import main; sys.exit(main(sys.argv[1:]))")
USER = pwd.getpwuid(os.getuid()).pw_name
CONNECTION = "100.64.0.2 52144 100.64.0.1 22"


class SessionFixture(object):
    BASE = "/tmp"  # Where each test's files live.
    prelude = ""  # Runs first in every launcher of this test.
    python = sys.executable  # Runs every launcher of this test.

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-access-", dir=self.BASE)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.config = self.root / "config.toml"
        self.config.write_text('[machines.local]\ntransport = "local"\n')
        self.lifetimes = set()
        self.addCleanup(self.end_all)

    @classmethod
    def tearDownClass(cls):
        for path in (HOME.parent, HOMES):
            shutil.rmtree(path, ignore_errors=True)
        if sys.platform == "darwin":
            shutil.rmtree(macos.user_temp() / "omc" / CLIENT_NAME, ignore_errors=True)

    def lifetime(self):
        name = "t" + uuid.uuid4().hex[:12]
        self.lifetimes.add(name)
        return name

    def end_all(self):
        """Kill what each lifetime left running and remove its runtime tree; Core never ends a lifetime."""
        for name in self.lifetimes:
            record = self.state / "code" / name / "record.json"
            if record.exists():
                for instance in json.loads(record.read_text()).get("instances", []):
                    end(instance["marker"])
            if sys.platform == "darwin":
                shutil.rmtree(macos.user_temp() / "omc" / restricted.digest(name)[:12], ignore_errors=True)

    def command(self, *args, prelude=""):
        """prelude runs first in the launcher process, for fault injection."""
        return [self.python, "-I", "-c", self.prelude + prelude + BOOT, "--config", str(self.config), "--state-dir",
                str(self.state), *args]

    def request(self, lifetime, policy, kind, command=None, **extra):
        value = {"protocol": 4, "user": USER, "lifetime": lifetime, "policy": policy, "kind": kind, "connection": CONNECTION,
                 **extra}
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

    def start(self, value, prelude=""):
        """Launch one channel and return the running launcher with its admission reply."""
        process, control = self.launch(value, prelude)
        with control:
            reply = json.loads(control.makefile("rb").readline() or b"{}")
        return process, reply

    def launch(self, value, prelude=""):
        """Send one channel's request without waiting; returns the launcher and its control socket."""
        parent, child = socket.socketpair()
        process = subprocess.Popen(self.command("session", "--control-fd", str(child.fileno()), prelude=prelude),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=[child.fileno()], start_new_session=True)
        child.close()
        parent.sendall(json.dumps(value).encode() + b"\n")
        return process, parent

    def sftp(self, request, batch):
        """Run OpenSSH's sftp client against one channel: sftp -D runs a stand-in host that relays it as the Go host would."""
        wrapper = self.root / "host.py"
        wrapper.write_text(
            "import json,socket,subprocess,sys\n"
            f"command = {self.command('session', '--control-fd', 'FD')!r}\n"
            "parent, child = socket.socketpair()\n"
            "process = subprocess.Popen([str(child.fileno()) if part == 'FD' else part for part in command], pass_fds=[child.fileno()])\n"
            "child.close()\n"
            f"parent.sendall(json.dumps({request!r}).encode() + b'\\n')\n"
            "reply = json.loads(parent.makefile('rb').readline())\n"
            "sys.exit(process.wait() if reply.get('status') == 'admitted' else 2)\n")
        commands = self.root / "batch"
        commands.write_text(batch)
        return subprocess.run(["sftp", "-b", str(commands), "-D", f"{sys.executable} {wrapper}"],
                              capture_output=True, text=True, timeout=60)

    def homes_are_closed(self, default, folders):
        """Leave a secret and shell startup files in the default sandbox home and in two keyed ones. Then, from
        a login shell of each home under each policy and each of folders, try to read and change the others:
        it runs its own startup file and nothing else comes of it."""
        keyed = sessions.keyed_homes(default)
        homes = {None: default / "home", "a" * 32: keyed / ("a" * 32) / "home", "b" * 32: keyed / ("b" * 32) / "home"}
        startup = (".profile", ".zprofile", ".bash_profile")  # A login sh, zsh or bash reads exactly one.
        for key, home in homes.items():
            reply, code, out, err = self.session(self.lifetime(), command=(
                f"echo HOME=$HOME; echo SECRET-{key} > secret; for name in {' '.join(startup)}; do "
                f"echo echo STARTUP-{key} > $name; done"), **({"home": key} if key else {}))
            self.assertEqual((reply["status"], code, out), ("admitted", 0, f"HOME={home}\n"), err)
        for key, home in homes.items():
            others = [other for other in homes.values() if other != home]
            typed = "".join(f"cat {other}/secret {str(other).swapcase()}/secret; ls -A {other.parent}; "
                            f"echo x > {other}/planted; echo echo PLANTED >> {other}/{name}\n"
                            for other in others for name in startup)
            if key is None:
                typed += f"ls -A {keyed}\n"  # The keys are not listed either.
            for policy, extra in (("read_only", {}), *(("workspace", {"folders": item}) for item in folders(others))):
                with self.subTest(home=key, policy=policy, **extra):
                    reply, code, out, err = self.admit(self.request(self.lifetime(), policy, "shell", **extra,
                                                                    **({"home": key} if key else {})), typed.encode())
                    self.assertEqual((reply["status"], out), ("admitted", f"STARTUP-{key}\n"), err)
        for key, home in homes.items():
            self.assertEqual([(home / name).read_text() for name in startup], [f"echo STARTUP-{key}\n"] * 3)
            self.assertFalse((home / "planted").exists())

    def a_changed_home_is_refused(self):
        """A lifetime keeps the home it started with: the default one, or the one of a key."""
        for first, then in (({}, {"home": "a" * 32}), ({"home": "a" * 32}, {}), ({"home": "a" * 32}, {"home": "b" * 32})):
            with self.subTest(first=first, then=then):
                lifetime = self.lifetime()
                self.assertEqual(self.session(lifetime, **first)[1], 0)
                reply, code, out, _ = self.session(lifetime, command="echo ran", **then)
                self.assertEqual((reply["error"]["code"], reply["error"]["effect"], code, out),
                                 ("lifetime_changed", "known_no_effect", 1, ""))
                self.assertEqual(self.session(lifetime, command="echo again", **first)[1:3], (0, "again\n"))

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

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(state) and not state.startswith("Z")

    def stopped(self, pid):
        return subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip().startswith("T")

    def beating(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        before = path.stat().st_mtime_ns
        time.sleep(0.8)
        return path.stat().st_mtime_ns != before


def members(marker):
    """PIDs of every process in the lifetime instance with this marker."""
    listed = subprocess.run(["/bin/ps", "-A", "-o", "pid="], capture_output=True, text=True, check=True).stdout
    return [pid for pid in map(int, listed.split()) if pid > 1 and restricted.backend.member(pid, marker) is True]


def end(marker, seconds=10):
    """Kill an instance's members until a scan finds none; each kill re-verifies its target's identity."""
    deadline = time.monotonic() + seconds
    while found := members(marker):
        if time.monotonic() > deadline:
            raise AssertionError(f"lifetime members remain: {found}")
        for pid in found:
            if (current := restricted.birth(pid)) is not None:
                restricted.signal_member(current, marker, signal.SIGKILL)
        time.sleep(0.05)
