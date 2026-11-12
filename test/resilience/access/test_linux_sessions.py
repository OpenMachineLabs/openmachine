"""Real Read only and Workspace sessions on Linux, each in a bubblewrap instance."""

import ctypes
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest

from openmachine.access.linux import sandbox
from openmachine.access import restricted, sessions
from test.support.access_sessions import CLIENT_NAME, SessionFixture

READ_ONLY = "Read-only file system"
MISSING = "No such file or directory"
GIT = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always"]


def detached(token):
    """fork, setsid, fork: a job in a new session whose command line holds token, writing a heartbeat."""
    script = ("import os,sys,time\nif os.fork(): sys.exit(0)\nos.setsid()\nif os.fork(): sys.exit(0)\n"
              "while True:\n open(os.environ['HOME']+'/'+sys.argv[1]+'.beat','w').write('x'); time.sleep(0.2)\n")
    return f"python3 -c {shlex.quote(script)} {token} </dev/null >/dev/null 2>&1; sleep 0.5"


def running(token):
    """A command that succeeds while a process whose command line holds token is visible to it."""
    return f"pgrep -f '{token[:-1]}[{token[-1]}]' >/dev/null"


def landlock_scopes():
    """Whether this kernel's Landlock scopes signals and abstract sockets (ABI 6)."""
    return ctypes.CDLL(None).syscall(ctypes.c_long(444), None, ctypes.c_size_t(0), ctypes.c_uint32(1)) >= 6


@unittest.skipUnless(sys.platform == "linux" and sandbox.bubblewrap(), "requires a bubblewrap that builds the sandbox")
class BubblewrapSessionTests(SessionFixture, unittest.TestCase):
    # The sandbox hides /tmp, and no Workspace folder lies there.
    BASE = pwd.getpwuid(os.getuid()).pw_dir

    def setUp(self):
        super().setUp()
        # The account home every launcher of this test sees: its denied areas, sandbox home and runtime tree.
        self.account = self.root / "account"
        self.account.mkdir()
        self.home = sandbox.home(self.account) / "home"
        self.prelude = ("from pathlib import Path as _P\nfrom openmachine.access import rules as _r\n"
                        f"_r.account_home = lambda: _P({str(self.account)!r})\n")

    def instances(self, lifetime):
        return json.loads((self.state / "code" / lifetime / "record.json").read_text())["instances"]

    def kill(self, lifetime):
        """End a lifetime's newest instance from outside, as a reboot or the OOM killer does."""
        pid = self.instances(lifetime)[-1]["pid"]
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while sandbox.start(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(sandbox.start(pid))

    def repository(self, path, name="file.txt"):
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("x\n")
        for args in (["init", "-q", "-b", "main"], ["add", name], ["commit", "-q", "-m", "Fixture"]):
            subprocess.run([*GIT, "-C", str(path), *args], check=True, capture_output=True)

    def refused(self, lifetime, commands, folders, expected=(READ_ONLY, "Device or resource busy")):
        for command in commands:
            reply, code, _, err = self.session(lifetime, "workspace", command, folders=folders)
            self.assertEqual(reply["status"], "admitted", reply)
            self.assertNotEqual(code, 0, command)
            self.assertTrue(any(text in err for text in expected), (command, err))

    def test_commands_are_confined_and_report_their_exit(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.account / ".ssh").mkdir()
        (self.account / ".ssh/config").write_text("SECRET\n")
        reply, code, out, err = self.session(self.lifetime(), command=(
            f"cat <<EOF > $HOME/heredoc\nhere\nEOF\ncat $HOME/heredoc; echo w > {outside}/x; cat {self.account}/.ssh/config; "
            "echo HOME=$HOME PWD=$PWD TMPDIR=$TMPDIR; grep -E 'CapPrm|CapEff|NoNewPrivs' /proc/self/status; "
            "sudo -n true; echo sudo=$?; exit 3"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertEqual(code, 3, err)
        self.assertIn("here", out)
        self.assertIn(f"HOME={self.home} PWD={self.home} TMPDIR=/tmp\n", out)
        self.assertIn(READ_ONLY, err)
        self.assertIn(MISSING, err)
        self.assertFalse((outside / "x").exists())
        # No capability, as root too, and none to gain from a program that would grant some.
        self.assertEqual(out.count("\t0000000000000000\n"), 2, out)
        self.assertIn("NoNewPrivs:\t1", out)
        if os.getuid():
            self.assertNotIn("sudo=0", out)

    def test_cores_own_python_inside_its_hidden_state_still_runs_the_spawner(self):
        """As installed: Core's environment lies in its state, of which a session sees nothing else."""
        environment = self.state / "service/versions/installed"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True)
        site = next((environment / "lib").glob("python*/site-packages"))
        (site / "source.pth").write_text("".join(path + "\n" for path in sys.path if path))
        self.python = str(environment / "bin/python")
        lifetime = self.lifetime()
        reply, code, out, err = self.session(lifetime, command=(
            f"ls -A {self.state} {self.state}/service {self.state}/service/versions | grep -v -e : -e '^$'; "
            f"cat {self.state}/code/{lifetime}/record.json; touch {environment}/x"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertEqual(out.split(), ["service", "versions", "installed"])
        self.assertIn(MISSING, err)
        self.assertIn(READ_ONLY, err)

    @unittest.skipUnless(os.getuid() == 0, "requires root")
    def test_root_has_no_capabilities_and_reads_no_system_secrets(self):
        reply, code, out, err = self.session(self.lifetime(), command=(
            "id -u; touch $HOME/mine && chown 1 $HOME/mine; echo chown=$?; mount -t tmpfs none /mnt; echo mount=$?; "
            "cat /etc/shadow /etc/gshadow /etc/sudoers; ls -A /etc/sudoers.d | wc -l; echo x >> /etc/passwd; true"))
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertEqual(out.split()[0], "0")
        self.assertNotIn("chown=0", out)
        self.assertNotIn("mount=0", out)
        self.assertEqual(out.split()[3:], ["0"], out + err)  # Nothing of the secrets, and an empty directory.
        self.assertIn(READ_ONLY, err)

    def test_the_unreadable_areas_and_the_control_paths_are_empty(self):
        for item in sandbox.HOME_DENIED:
            path = self.account / item
            if "history" in item or item in (".netrc", ".git-credentials", ".npmrc", ".pypirc"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"SECRET {item}\n")
            else:
                path.mkdir(parents=True)
                (path / "secret").write_text(f"SECRET {item}\n")
        # A denied area that is a link is hidden where it leads.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret").write_text("SECRET elsewhere\n")
        shutil.rmtree(self.account / ".aws")
        (self.account / ".aws").symlink_to(elsewhere)
        project = self.root / "project"
        project.mkdir()
        (project / ".env").write_text("ENV-PROJECT\n")
        default = self.root / "default/.local/state/openmachine"
        hosts = [default.parent / "openmachine-code", default.parent / "openmachine-connector"]
        for path in hosts:
            path.mkdir(parents=True, mode=0o700)
            (path / "host").write_text("SECRET host\n")
        self.prelude += f"_r.DEFAULT_STATE = _P({str(default)!r})\n"
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime, command="true")[1], 0)  # The lifetime's record exists.
        command = (f"cat {self.account}/.[a-z]* {self.account}/.*/* {self.account}/.*/*/* {self.account}/.*/*/*/* {elsewhere}/* "
                   f"{self.state}/* {self.state}/*/* {self.state}/*/*/* {' '.join(f'{path}/*' for path in hosts)}; "
                   f"ls -A {self.account}/.ssh {self.state} {hosts[0]} {elsewhere}; cat {project}/.env")
        reply, _, out, _ = self.session(lifetime, command=command)
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("instances", out)
        # Each is an empty directory, and files are not hidden by name: only the listed areas are.
        self.assertEqual([line for line in out.splitlines() if line and not line.endswith(":")], ["ENV-PROJECT"])

    def test_tmp_and_run_are_the_instances_own(self):
        """The default places of the sockets through which a process of this user acts outside a sandbox."""
        uid = os.getuid()
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        path = Path(f"/tmp/om-access-{os.getpid()}.sock")
        listener.bind(str(path))
        self.addCleanup(path.unlink)
        listener.listen(1)
        listener.setblocking(False)
        abstract = socket.socket(socket.AF_UNIX)
        self.addCleanup(abstract.close)
        abstract.bind(f"\0om-access-{os.getpid()}")
        abstract.listen(1)
        abstract.setblocking(False)
        probes = "\n".join([
            "import os, socket, sys",
            "for name in sys.argv[1:]:\n    s = socket.socket(socket.AF_UNIX)\n"
            "    try:\n        s.connect(name.replace('@', chr(0))); print('connected')\n"
            "    except OSError as e:\n        print(type(e).__name__)",
            "for fd in range(3, 256):\n    try:\n        os.write(fd, b'x')\n    except OSError:\n        continue\n    print('WRITABLE', fd)"])
        lifetime = self.lifetime()
        reply, code, out, err = self.session(lifetime, command=(
            f"python3 -c {shlex.quote(probes)} {path} @om-access-{os.getpid()}; ls -A /tmp /dev/shm /run/user/{uid} | grep -v -e : -e '^$'; "
            f"ls /run/user; echo bus=$DBUS_SESSION_BUS_ADDRESS runtime=$XDG_RUNTIME_DIR; systemd-run --user --wait true; "
            f"echo escape=$?; echo kept > /tmp/kept && echo kept > /run/user/{uid}/kept && echo kept > /dev/shm/kept; "
            "echo x > /run/x; true"))
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "FileNotFoundError")
        self.assertEqual(lines[1], "PermissionError" if landlock_scopes() else "connected")
        self.assertEqual(lines[2:], [str(uid), f"bus= runtime=/run/user/{uid}", lines[-1]], out)
        self.assertNotEqual(lines[-1], "escape=0")
        with self.assertRaises(BlockingIOError):
            listener.accept()
        self.assertFalse(Path("/tmp/kept").exists() or Path("/dev/shm/kept").exists() or Path("/run/x").exists())
        # A later session of the lifetime finds them; another lifetime's instance has its own.
        self.assertEqual(self.session(lifetime, command=f"cat /tmp/kept /run/user/{uid}/kept /dev/shm/kept")[2], "kept\n" * 3)
        self.assertEqual(self.session(self.lifetime(), command=f"ls -A /tmp /run/user/{uid} /dev/shm | grep -v :")[2].strip(), "")

    def routing(self, client):
        broker = "https://127.0.0.1:41234"
        return {"client": str(client), "environment": {"OM_CODE_BROKER": broker, "OM_CODE_CAPABILITY": "c" * 43,
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"credential.{broker}.helper",
                "GIT_CONFIG_VALUE_0": f"!'{client}' git-credential"}}

    def test_github_routing_runs_cores_copy_of_the_client_in_the_lifetimes_one_instance(self):
        clients = []
        for name in ("a", "b"):  # Each in a denied area, so only Core's copy can run.
            (self.account / ".ssh" / name).mkdir(parents=True, mode=0o755)
            clients.append(self.account / ".ssh" / name / "om-code")
            clients[-1].write_text(f"#!/bin/sh\necho om-code-{name} \"$@\"\n")
            clients[-1].chmod(0o755)
        first, second = clients
        lifetime, stable = self.lifetime(), sandbox.runtime(self.account) / CLIENT_NAME / "om-code"
        reply, code, out, err = self.session(lifetime, command=(
            f"env | grep -E '^(OM_CODE|GIT_CONFIG)' | sort; echo PATH=$PATH; om-code hi; {first}; echo x >> {stable}; "
            f"rm {stable}; ls {stable.parent.parent}; {detached('routed-job')}"), github=self.routing(first))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertIn("OM_CODE_BROKER=https://127.0.0.1:41234\nOM_CODE_CAPABILITY=" + "c" * 43, out)
        self.assertRegex(out, rf"PATH=\S+:{stable.parent}\n")
        self.assertIn("om-code-a hi", out)
        self.assertEqual((err.count(MISSING), err.count(READ_ONLY)), (1, 2), err)
        # Of the runtime tree, an instance sees the client and its own lifetime's directory.
        self.assertEqual(sorted(out.splitlines()[-2:]), sorted([CLIENT_NAME, self.instance_directory(lifetime).name]))
        # After the host's client changes, as after an om-code update, sessions run the new copy in the
        # same instance, where a job started before the change is still running.
        reply, code, out, err = self.session(lifetime, command=f"om-code && {running('routed-job')} && echo same",
                                             github=self.routing(second))
        self.assertEqual(out.split(), ["om-code-b", "same"], err)
        self.assertEqual(self.session(lifetime, command="env | grep -c OM_CODE")[2], "0\n")
        self.assertEqual(len(self.instances(lifetime)), 1)

    def instance_directory(self, lifetime):
        return sandbox.runtime(self.account) / restricted.digest(lifetime)[:12]

    def test_a_client_restricted_work_could_replace_is_refused(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[0]["status"], "admitted")  # Core creates the persistent home
        for client in (self.home / "om-code", self.instance_directory(lifetime) / "om-code"):
            client.write_text("#!/bin/sh\n")
            client.chmod(0o755)
            reply, _, _, _ = self.session(lifetime, command="true", github=self.routing(client))
            self.assertEqual((reply["status"], reply["error"]["code"]), ("denied", "github_client_invalid"), reply)

    def test_a_second_session_sees_the_first_ones_job_and_no_other_lifetime_does(self):
        first, other = self.lifetime(), self.lifetime()
        reply, code, _, err = self.session(first, command=detached("shared-job"))
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        unrelated = subprocess.Popen(["/bin/sleep", "61.25"])
        self.addCleanup(unrelated.kill)
        self.assertTrue(self.beating(self.home / "shared-job.beat"))
        self.assertEqual(self.session(first, command=running("shared-job"))[1], 0)
        self.assertNotEqual(self.session(other, command=running("shared-job"))[1], 0)
        self.assertNotEqual(self.session(first, command=running("sleep 61.25"))[1], 0)
        self.assertEqual(len(self.instances(first)), 1)

    def test_forwarded_signals_reach_the_command_group_but_neither_detached_work_nor_the_spawner(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime, command=detached("kept-job"))[1], 0)
        marker = self.home / "foreground"
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum.name):
                marker.unlink(missing_ok=True)
                # A shell waiting on a foreground child: the child is not the session's recorded process.
                process, reply = self.start(self.request(lifetime, "read_only", "exec",
                                                         f"sh -c 'echo $$ > {marker}; exec sleep 61.5'; echo after"))
                self.assertEqual(reply["status"], "admitted", reply)
                self.assertTrue(self.wait_for(marker))
                process.send_signal(signum)
                out, _ = process.communicate(timeout=20)
                self.assertEqual(process.returncode, -signum)
                self.assertNotIn(b"after", out)
                self.assertNotEqual(self.session(lifetime, command="sleep 0.3; " + running("sleep 61.5"))[1], 0)
        # Inside its PID namespace the spawner is process 1, which no signal of its own work ends.
        self.assertEqual(self.session(lifetime, command="kill -9 1; kill -TERM 1; kill -STOP 1; sleep 0.3")[0]["status"], "admitted")
        self.assertEqual(self.session(lifetime, command=running("kept-job"))[1], 0)
        self.assertTrue(self.beating(self.home / "kept-job.beat"))
        self.assertEqual(len(self.instances(lifetime)), 1)

    def test_a_lost_spawner_ends_its_instance_and_the_next_session_starts_another(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime, command=detached("lost-job"))[1], 0)
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "sleep 61.75"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.kill(lifetime)
        self.assertEqual(process.wait(timeout=20), 255)  # That session's exit is unknown.
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        reply, code, out, _ = self.session(lifetime, command=f"{running('lost-job')}; echo job=$?")
        self.assertEqual((reply["status"], out), ("admitted", "job=1\n"))  # The instance ended with its spawner.
        self.assertEqual(len(self.instances(lifetime)), 2)

    def test_a_stopped_spawner_bounds_admission(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[1], 0)
        os.kill(self.instances(lifetime)[0]["pid"], signal.SIGSTOP)
        started = time.monotonic()
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"),
                                    "from openmachine.access import restricted as _s\n_s.ACKNOWLEDGE_SECONDS = 1\n")
        self.assertEqual(process.wait(timeout=60), 1)
        self.assertLess(time.monotonic() - started, 30)
        # The request may already be queued at the spawner: a retry is not known to be safe.
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"]), ("launch_unverified", "effect_unknown"))
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def leftover(self):
        """Processes whose command line names this test's account home, as every spawner's does."""
        return subprocess.run(["pgrep", "-f", str(self.account)], capture_output=True, text=True).stdout.split()

    def test_an_instance_that_does_not_verify_is_ended_and_nothing_runs(self):
        cases = {
            # The spawner never reports that it listens.
            "never ready": ("from openmachine.access.linux import sessions as _l\n_l.SPAWNER = 'import time; time.sleep(120)'\n",
                            "lifetime_unverifiable"),
            # The self-test classifies this supervisor, which is outside, as a member.
            "self-test": ("from openmachine.access.linux import sandbox as _b\n_b.member = lambda pid, marker: True\n",
                          "lifetime_unverifiable"),
            # The spawner is found in this supervisor's own PID namespace.
            "own namespace": ("import os\nfrom openmachine.access.linux import sandbox as _b\n_namespace = _b.namespace\n"
                              "_b.namespace = lambda pid: _namespace(os.getpid())\n", "lifetime_unverifiable"),
        }
        for name, (prelude, code) in cases.items():
            with self.subTest(case=name):
                lifetime = self.lifetime()
                started = time.monotonic()
                process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"),
                                            "from openmachine.access import restricted as _s\n_s.ACKNOWLEDGE_SECONDS = 1\n" + prelude)
                out, _ = process.communicate(timeout=60)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual((reply["error"]["code"], reply["error"]["effect"], out), (code, "known_no_effect", b""))
                self.assertEqual(self.leftover(), [])
                self.assertFalse((self.state / "code" / lifetime / "record.json").exists())
                self.assertEqual(self.session(lifetime, command="echo again")[1:3], (0, "again\n"))
                self.kill(lifetime)

    def test_a_command_runs_only_once_the_supervisor_has_checked_its_own_credentials(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[1], 0)
        # The credentials the kernel attached name a process outside the instance: this launcher.
        prelude = "import os\nfrom openmachine.access.linux import sandbox as _b\n_b.sender = lambda ancillary: os.getpid()\n"
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "touch $HOME/ran"), prelude)
        out, _ = process.communicate(timeout=60)
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"], process.returncode),
                         ("lifetime_unverifiable", "known_no_effect", 1))
        self.assertEqual(self.session(lifetime, command="sleep 0.5; ls $HOME/ran")[1], 2)
        self.assertEqual(len(self.instances(lifetime)), 1)

    def test_a_changed_policy_is_refused_in_either_direction(self):
        for first, then in (("read_only", "full_access"), ("full_access", "read_only")):
            with self.subTest(first=first):
                lifetime = self.lifetime()
                self.assertEqual(self.session(lifetime, first)[1], 0)
                reply, code, out, _ = self.session(lifetime, then, "echo ran")
                self.assertEqual((reply["error"]["code"], reply["error"]["effect"], code, out),
                                 ("lifetime_changed", "known_no_effect", 1, ""))

    def test_workspace_writes_only_its_folders_and_starts_in_the_first(self):
        code, notes, outside = self.root / "code", self.root / "notes", self.root / "outside"
        for path in (notes, outside):
            path.mkdir()
            (path / "file.txt").write_text(path.name + "\n")
        self.repository(code)
        # A registered worktree of a folder's repository is writable although no folder holds it; one in
        # a place the folder rule refuses is not.
        linked, hidden = self.root / "code-linked", self.account / ".worktrees/linked"
        hidden.parent.mkdir()
        for name, path in (("linked", linked), ("hidden", hidden)):
            subprocess.run([*GIT, "-C", str(code), "worktree", "add", "-q", "-b", name, str(path)], check=True, capture_output=True)
        (code / ".claude").mkdir()
        (code / ".claude/settings.json").write_text("{}\n")
        (notes / ".mcp.json").write_text("{}\n")
        folders = [str(code), str(notes)]
        lifetime = self.lifetime()
        reply, code_, out, err = self.session(lifetime, "workspace", (
            f"pwd; echo w >> {code}/file.txt && echo w >> {notes}/file.txt && echo w > {linked}/new.txt && echo wrote; "
            f"cat {outside}/file.txt; mkdir -p $HOME/.claude && echo .mcp > $HOME/.mcp.json && echo home-ok; "
            f"cd {code} && git status --short && git log --oneline | wc -l"), folders=folders)
        self.assertEqual((reply["status"], code_), ("admitted", 0), err)
        self.assertEqual(out.split(), [str(code), "wrote", "outside", "home-ok", "M", "file.txt", "??", ".claude/", "1"])
        # Outside the folders, and on the names the owner's tools later run, existing or not, every write is refused.
        self.refused(lifetime, [
            f"echo x >> {outside}/file.txt", f"echo x > {hidden}/new.txt", f"echo x >> {code}/.claude/settings.json",
            f"rm -r {code}/.claude", f"mv {code}/.claude {code}/claude", f"mkdir {code}/.vscode/x", f"rmdir {code}/.vscode",
            f"mv {code}/.vscode {code}/vscode", f"echo x > {notes}/.idea/x", f"mkdir {notes}/.codex/x",
            f"touch {notes}/.agents/x", f"touch {notes}/.cursor/x", f"touch {notes}/.gemini/x", f"touch {code}/.husky/pre-commit",
            f"echo x > {notes}/.mcp.json", f"rm {notes}/.mcp.json", f"mv {notes}/file.txt {notes}/.mcp.json",
            f"touch {linked}/.claude/x", f"mv {code} {self.root}/moved"], folders)
        self.assertEqual((outside / "file.txt").read_text(), "outside\n")
        self.assertEqual((code / ".claude/settings.json").read_text(), "{}\n")
        self.assertEqual((notes / ".mcp.json").read_text(), "{}\n")
        for root in (code, notes, linked):
            for name in sandbox.PROTECTED:
                self.assertEqual(os.listdir(root / name), ["settings.json"] if (root, name) == (code, ".claude") else [])
        # A lifetime keeps the folders it started with.
        reply, code_, out, _ = self.session(lifetime, "workspace", "echo ran", folders=[str(code)])
        self.assertEqual((reply["error"]["code"], code_, out), ("lifetime_changed", 1, ""))
        # Each admission re-checks the folders: a renamed one is refused before anything runs.
        notes.rename(self.root / "moved")
        reply, code_, out, _ = self.session(lifetime, "workspace", "echo ran", folders=folders)
        self.assertEqual((reply["status"], reply["error"]["code"], reply["error"]["effect"], code_, out),
                         ("denied", "folder_missing", "known_no_effect", 1, ""))
        self.assertIn(str(notes), reply["error"]["message"])
        self.assertIn("Console", reply["error"]["message"])

    def snapshot(self, *paths):
        return [(str(path), path.is_symlink(), path.is_file() and path.read_bytes())
                for root in paths for path in sorted(root.rglob("*"))]

    def test_git_directories_are_read_only_and_stay_where_they_are(self):
        """Unsandboxed Git later trusts every .git entry, so none in a writable path changes, moves or is replaced."""
        full, plain, source = self.root / "full", self.root / "plain", self.root / "source"
        nested, inner, deep = full / "vendor/lib", plain / "a/inner", plain / "1/2/3/deep"
        for path, name in ((full, "file.txt"), (nested, "lib.txt"), (inner, "file.txt"), (deep, "file.txt"), (source, "file.txt")):
            self.repository(path, name)
        subprocess.run([*GIT, "-C", str(full), "submodule", "add", "-q", str(source), "ext"], check=True, capture_output=True)
        subprocess.run([*GIT, "-C", str(full), "commit", "-q", "-m", "Submodule"], check=True)
        linked = self.root / "linked"
        subprocess.run([*GIT, "-C", str(full), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True, capture_output=True)
        entries = [full / ".git", nested / ".git", inner / ".git", deep / ".git", full / "ext/.git", linked / ".git"]
        before = self.snapshot(*(path for path in entries if path.is_dir())) + [path.read_bytes() for path in entries if path.is_file()]
        folders = [str(full), str(plain)]
        lifetime = self.lifetime()
        self.refused(lifetime, [
            # Nothing in a Git directory is written, a repository's, a nested one's or one in a folder that is none.
            f"echo x > {full}/.git/hooks/pre-commit", f"echo '[core]' >> {full}/.git/config", f"rm {full}/.git/HEAD",
            f"echo x > {full}/.git/config.worktree", f"echo /tmp > {full}/.git/worktrees/linked/commondir",
            f"echo '[core]' >> {full}/.git/modules/ext/config", f"echo x > {nested}/.git/hooks/pre-commit",
            f"echo x > {inner}/.git/hooks/pre-commit", f"echo x > {deep}/.git/hooks/pre-commit",
            # A .git that is a file, a submodule's or a worktree's, is not rewritten or removed.
            f"echo 'gitdir: /tmp/x' > {full}/ext/.git", f"rm {full}/ext/.git", f"echo 'gitdir: /tmp/x' > {linked}/.git",
            # No .git, nor any directory between it and its folder, is renamed, removed or replaced.
            f"mv {full}/.git {full}/.git-old", f"mv {nested}/.git {nested}/.git-old", f"rm -rf {nested}/.git",
            f"mv {nested} {full}/vendor/moved", f"mv {full}/vendor {full}/moved", f"mv {full}/ext {full}/moved",
            f"mv {inner} {plain}/a/moved", f"mv {plain}/a {plain}/moved", f"mv {plain}/1/2 {plain}/1/moved",
            f"rm -rf {plain}/a", f"mv {full} {self.root}/moved",
            # So Git records nothing: no commit, staging, configuration or worktree.
            f"cd {full} && git add file.txt", f"cd {full} && git commit -q --allow-empty -m refused",
            f"cd {full} && git config user.name refused", f"cd {full} && git worktree add -q {plain}/worktree",
            f"cd {linked} && git commit -q --allow-empty -m refused", f"cd {nested} && git commit -q --allow-empty -m refused",
        ], folders, expected=(READ_ONLY, "Device or resource busy", "Invalid cross-device link"))
        self.assertEqual(self.snapshot(*(path for path in entries if path.is_dir()))
                         + [path.read_bytes() for path in entries if path.is_file()], before)
        self.assertFalse(any(os.path.lexists(path) for path in (
            full / ".git-old", nested / ".git-old", full / "moved", full / "vendor/moved", plain / "moved", plain / "a/moved",
            plain / "1/moved", plain / "worktree", self.root / "moved")))
        # Working trees stay writable and Git still reads.
        reply, code, out, err = self.session(lifetime, "workspace", (
            f"cd {full} && echo edit >> file.txt && echo edit >> vendor/lib/lib.txt && echo edit >> ext/file.txt && "
            f"echo edit >> {linked}/file.txt && git status --short && git diff --stat | tail -1 && git log --oneline | wc -l && "
            f"git -C {linked} status --short"), folders=folders)
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertIn(" M file.txt", out)
        self.assertIn("2 files changed", out)
        subprocess.run([*GIT, "-C", str(full), "fsck", "--strict"], check=True, capture_output=True)

    def placeholders(self):
        return sorted(json.loads((self.state / "code/sandbox.json").read_text())["placeholders"])

    def test_placeholders_exist_while_an_instance_lives_and_only_inert_ones_are_removed(self):
        folder = self.root / "folder"
        folder.mkdir()
        (folder / ".idea").mkdir()  # The owner's own, and empty.
        made = sorted(str(folder / name) for name in sandbox.PROTECTED if name != ".idea")
        first, second, third = self.lifetime(), self.lifetime(), self.lifetime()
        self.assertEqual(self.session(first, "workspace", "true", folders=[str(folder)])[1], 0)
        self.assertEqual(self.placeholders(), made)
        self.assertTrue(all(os.listdir(path) == [] for path in made))
        # Another lifetime's instance mounts over the same directories, so none goes while it lives.
        self.assertEqual(self.session(second, "workspace", "true", folders=[str(folder)])[1], 0)
        self.kill(first)
        self.assertEqual(self.session(third, command="true")[1], 0)
        self.assertEqual(self.placeholders(), made)
        self.assertTrue(all(os.path.isdir(path) for path in made))
        self.refused(second, [f"mkdir {folder}/.vscode/x", f"rmdir {folder}/.husky"], [str(folder)])
        # Once no instance lives, the next one to start removes those still empty.
        (folder / ".vscode/settings.json").write_text("{}\n")
        self.kill(second)
        self.kill(third)
        self.assertEqual(self.session(self.lifetime(), command="true")[1], 0)
        self.assertEqual(self.placeholders(), [])
        self.assertEqual(sorted(os.listdir(folder)), [".idea", ".vscode"])

    def test_a_folder_that_cannot_be_protected_is_refused(self):
        large, linked, target = self.root / "large", self.root / "linked", self.root / "target"
        for path in (large, linked, target):
            path.mkdir()
        for index in range(6):
            (large / f"file-{index}").write_text("x\n")
        (linked / ".claude").symlink_to(target)
        small = "from openmachine.access.linux import sandbox as _b\n_b.MAX_ENTRIES = 5\n"
        for folder, prelude, code in ((large, small, "folder_too_large"), (linked, "", "scope_unsupported")):
            with self.subTest(code=code):
                process, reply = self.start(self.request(self.lifetime(), "workspace", "exec", "echo ran", folders=[str(folder)]),
                                            prelude)
                out, _ = process.communicate(timeout=60)
                self.assertEqual((reply["status"], reply["error"]["code"], reply["error"]["effect"], out),
                                 ("denied", code, "known_no_effect", b""))
                self.assertIn(str(folder), reply["error"]["message"])
                self.assertIn("Console", reply["error"]["message"])
                self.assertEqual(self.leftover(), [])
        self.assertEqual(os.listdir(target), [])
        # A worktree that needs a longer search than a folder may is not granted; its folder still is.
        code, wide = self.root / "code", self.root / "wide"
        self.repository(code)
        subprocess.run([*GIT, "-C", str(code), "worktree", "add", "-q", "-b", "wide", str(wide)], check=True, capture_output=True)
        for index in range(6):
            (wide / f"file-{index}").write_text("x\n")
        process, reply = self.start(self.request(self.lifetime(), "workspace", "exec", f"echo x > {code}/new && echo x > {wide}/new",
                                                 folders=[str(code)]), small)
        _, err = process.communicate(timeout=60)
        self.assertEqual((reply["status"], process.returncode), ("admitted", 1), err)
        self.assertTrue((code / "new").exists())
        self.assertFalse((wide / "new").exists())

    def test_terminal_loss_leaves_nohup_work_running(self):
        lifetime = self.lifetime()
        beat = self.home / "pty.beat"
        reply, process, master, output = self.terminal(lifetime, (
            f"test -t 0 && echo TERMINAL; nohup sh -c 'while :; do date > {beat}; sleep 0.2; done' >/dev/null 2>&1 & "
            "sleep 1; echo MARK-$((40+2))\n"), "MARK-42")
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertIn("TERMINAL", output)
        os.close(master)  # PTY loss: the shell gets SIGHUP, the nohup job keeps running.
        process.wait(timeout=20)
        self.assertTrue(self.beating(beat))

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_real_tmux_survives_sessions_in_its_lifetime(self):
        lifetime = self.lifetime()
        reply, code, _, err = self.session(lifetime, command="tmux new-session -d -s om 'while :; do date > $HOME/tmux.beat; sleep 0.2; done'")
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertTrue(self.beating(self.home / "tmux.beat"))
        self.assertIn("om", self.session(lifetime, command="tmux ls")[2])
        self.assertNotEqual(self.session(self.lifetime(), command="tmux ls")[1], 0)

    @unittest.skipUnless(shutil.which("sftp") and any(os.access(path, os.X_OK) for path in sessions.SFTP_SERVERS),
                         "requires the OpenSSH sftp client and server")
    def test_sftp_runs_the_server_inside_the_sandbox(self):
        upload, download = self.root / "upload.txt", self.root / "download.txt"
        upload.write_text("payload\n")
        result = self.sftp(self.request(self.lifetime(), "read_only", "sftp"),
                           f"put {upload} up.txt\nget up.txt {download}\n-put {upload} {self.root}/escape.txt\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(download.read_text(), "payload\n")
        self.assertFalse((self.root / "escape.txt").exists())


@unittest.skipUnless(sys.platform == "linux", "the bubblewrap probe runs on Linux")
class UnavailableSandboxTests(SessionFixture, unittest.TestCase):
    def test_a_machine_where_no_bubblewrap_builds_the_sandbox_refuses_restricted_sessions(self):
        # Neither the system's bubblewrap nor a payload's copy exists.
        prelude = ("import sys\nfrom pathlib import Path as _P\nfrom openmachine.access.linux import sandbox as _b\n"
                   f"_b.SYSTEM_BWRAP = _P('/bin/false')\nsys.base_prefix = {str(self.root / 'python')!r}\n")
        reported = json.loads(subprocess.run(self.command("capabilities", prelude=prelude), capture_output=True, check=True).stdout)
        self.assertEqual(reported["policies"], {"full_access": "available", "read_only": "sandbox_unavailable",
                                                "workspace": "sandbox_unavailable"})
        self.assertNotIn("github_client", reported)
        for policy, extra in (("read_only", {}), ("workspace", {"folders": [str(self.root)]})):
            process, reply = self.start(self.request(self.lifetime(), policy, "exec", "echo ran", **extra), prelude)
            out, _ = process.communicate(timeout=60)
            self.assertEqual((reply["status"], reply["error"]["code"], reply["error"]["effect"], out),
                             ("denied", "scope_unsupported", "known_no_effect", b""))
            self.assertIn("bubblewrap", reply["error"]["message"])
        self.assertEqual(self.session(self.lifetime(), "full_access", "echo ran")[2], "ran\n")
