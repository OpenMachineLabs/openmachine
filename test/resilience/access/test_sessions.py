"""Real Code sessions: confinement, shared lifetimes, detached work, PTY loss and SFTP."""

import json
import os
from pathlib import Path
import pwd
import pty
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest

from openmachine.access import sessions
from openmachine.access.linux import sandbox
from openmachine.access.macos import seatbelt as macos
from test.support.access_sessions import CLIENT_NAME, CONNECTION, HOME, HOMES, USER, SessionFixture, members

DENIED = "not permitted"
IDENTITY = ("GIT_AUTHOR_NAME=Fixture GIT_AUTHOR_EMAIL=fixture@example.invalid GIT_COMMITTER_NAME=Fixture "
            "GIT_COMMITTER_EMAIL=fixture@example.invalid")

# Sessions take the account's home from the password database, whatever $HOME the caller has.
ACCOUNT_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)


def detached(name):
    """fork, setsid, fork: a job reparented to launchd in a new session, writing a pid file and a heartbeat."""
    script = ("import os,sys,time\nif os.fork(): sys.exit(0)\nos.setsid()\nif os.fork(): sys.exit(0)\n"
              f"h=os.environ['HOME']\nopen(h+'/{name}.pid','w').write(str(os.getpid()))\n"
              f"while True:\n open(h+'/{name}.beat','w').write('x'); time.sleep(0.2)\n")
    return f"python3 -c {shlex.quote(script)} </dev/null >/dev/null 2>&1; sleep 0.5; cat $HOME/{name}.pid"


@unittest.skipUnless(macos.lifetime_unavailable_reason() is None, "requires the macOS Seatbelt lifetime backend")
class RestrictedSessionTests(SessionFixture, unittest.TestCase):
    def test_commands_are_confined_and_report_their_exit(self):
        outside = self.root / "outside"
        outside.mkdir()
        lifetime = self.lifetime()
        reply, code, out, err = self.session(lifetime, command=(
            f"cat <<EOF > $HOME/heredoc\nhere\nEOF\ncat $HOME/heredoc; echo w > {outside}/x; /usr/bin/sudo -n true; "
            f"cat {ACCOUNT_HOME}/.ssh/config; echo HOME=$HOME; exit 3"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertEqual(code, 3, err)
        self.assertIn("here", out)
        self.assertIn(f"HOME={HOME}", out)
        self.assertGreaterEqual(err.count(DENIED), 3, err)
        self.assertFalse((outside / "x").exists())

    def routing(self, client):
        broker = "https://127.0.0.1:41234"
        return {"client": str(client), "environment": {"OM_CODE_BROKER": broker, "OM_CODE_CAPABILITY": "c" * 43,
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"credential.{broker}.helper",
                "GIT_CONFIG_VALUE_0": f"!'{client}' git-credential"}}

    def clients(self):
        """Two om-code clients, each beside another program, in a directory the profile denies."""
        denied, clients = self.root / "denied", []
        for name in ("a", "b"):
            (denied / name).mkdir(parents=True, mode=0o755)
            for program in ("om-code", "other"):
                path = denied / name / program
                path.write_text(f"#!/bin/sh\necho {program}-{name} \"$@\"\n")
                path.chmod(0o755)
            clients.append(denied / name / "om-code")
        prelude = f"from openmachine.access.macos import seatbelt; seatbelt.SYSTEM_DENIED = seatbelt.SYSTEM_DENIED + ({str(denied)!r},); "
        return clients, prelude

    def admitted(self, lifetime, command, prelude, client=None):
        """Run one Read Only exec, routed to client when one is named; returns its stdout and stderr."""
        extra = {"github": self.routing(client)} if client else {}
        process, reply = self.start(self.request(lifetime, "read_only", "exec", command, **extra), prelude=prelude)
        out, err = process.communicate(timeout=60)
        self.assertEqual(reply["status"], "admitted", reply)
        return out.decode(), err.decode()

    def instances(self, lifetime):
        return [item.get("client") for item in json.loads((self.state / "code" / lifetime / "record.json").read_text())["instances"]]

    def stable(self):
        return macos.user_temp() / "omc" / CLIENT_NAME / "om-code"

    def test_github_routing_runs_cores_copy_of_the_client_in_the_lifetimes_one_instance(self):
        (first, second), prelude = self.clients()
        lifetime, stable = self.lifetime(), self.stable()
        # The session runs Core's copy by name, never the host's file, and cannot replace the copy.
        out, err = self.admitted(lifetime, f"env | grep -E '^(OM_CODE|GIT_CONFIG)' | sort; echo PATH=$PATH; om-code hi; {first}; "
                                 f"{first.parent}/other; ln {stable} $HOME/linked; echo x >> {stable}", prelude, first)
        self.assertIn("OM_CODE_BROKER=https://127.0.0.1:41234\nOM_CODE_CAPABILITY=" + "c" * 43, out)
        self.assertIn(f"GIT_CONFIG_VALUE_0=!'{first}' git-credential", out)
        self.assertRegex(out, rf"PATH=\S+:{stable.parent}\n")
        self.assertIn("om-code-a hi", out)
        self.assertNotIn("other-a", out)
        self.assertEqual(err.count(DENIED), 4, err)
        self.assertFalse((HOME / "linked").exists())
        self.assertNotIn("x", stable.read_text().split())
        # After the host's client changes, as after an om-code update, sessions run the new copy in the
        # same instance, where a job started before the change can still be signaled.
        job = self.admitted(lifetime, detached("routed"), prelude, first)[0].strip()
        out, err = self.admitted(lifetime, f"om-code; kill -0 {job} && echo signaled", prelude, second)
        self.assertEqual(out.split(), ["om-code-b", "signaled"], err)
        # A session without routing (as SFTP) shares that instance and sees none of the variables.
        self.assertEqual(self.admitted(lifetime, "env | grep -c OM_CODE", prelude)[0], "0\n")
        self.assertEqual(self.instances(lifetime), [str(stable)])

    def test_only_the_newest_instance_admits(self):
        """Every instance binds the lifetime's one spawner socket, so an older live one is unreachable."""
        (first, _), prelude = self.clients()
        lifetime, stable = self.lifetime(), str(self.stable())
        self.admitted(lifetime, "true", prelude)
        self.assertIn("om-code-a", self.admitted(lifetime, "om-code", prelude, first)[0])
        self.assertEqual(self.instances(lifetime), [stable])
        # An instance from before Core kept its copy cannot run it: unrouted sessions still use it, and
        # the first routed one starts a new instance.
        record = self.state / "code" / lifetime / "record.json"
        value = json.loads(record.read_text())
        del value["instances"][-1]["client"]
        record.write_text(json.dumps(value))
        self.admitted(lifetime, "true", prelude)
        self.assertIn("om-code-a", self.admitted(lifetime, "om-code", prelude, first)[0])
        self.assertEqual(self.instances(lifetime), [None, stable])
        # Once the newest spawner is lost, the next session starts another instance, though an older one lives.
        spawner = json.loads(record.read_text())["instances"][-1]["pid"]
        self.admitted(lifetime, f"kill -9 {spawner}", prelude)
        self.assertEqual(self.admitted(lifetime, "echo again", prelude)[0], "again\n")
        self.assertEqual(self.instances(lifetime), [None, stable, stable])

    def test_an_admission_connects_before_a_newer_instance_rebinds_the_socket(self):
        (first, _), prelude = self.clients()
        lifetime = self.lifetime()
        self.admitted(lifetime, "true", prelude)
        record = self.state / "code" / lifetime / "record.json"
        value = json.loads(record.read_text())
        del value["instances"][-1]["client"]  # an instance from before Core kept its copy
        record.write_text(json.dumps(value))
        # This admission pauses under the lifetime lock, just before it connects; meanwhile a routed
        # session waits for the lock, then starts a newer instance that rebinds the socket.
        holding = self.root / "holding"
        pause = ("import time\nfrom openmachine.access import restricted as _s\n_connect = _s._connect\n"
                 f"def _paused(lifetime, instance):\n open({str(holding)!r}, 'w').close()\n time.sleep(3)\n"
                 " return _connect(lifetime, instance)\n_s._connect = _paused\n")
        paused, control = self.launch(self.request(lifetime, "read_only", "exec", "echo paused"), prelude + pause)
        self.assertTrue(self.wait_for(holding))  # it holds the lifetime lock
        self.assertIn("om-code-a y", self.admitted(lifetime, "om-code y", prelude, first)[0])
        with control:
            reply = json.loads(control.makefile("rb").readline() or b"{}")
        out, _ = paused.communicate(timeout=60)
        self.assertEqual((reply.get("status"), out), ("admitted", b"paused\n"), reply)

    def test_a_client_removed_before_core_copies_it_has_no_effect(self):
        """As when an update removes an old client: it passed its check, then vanished before the copy."""
        (first, second), prelude = self.clients()
        lifetime = self.lifetime()
        self.admitted(lifetime, "true", prelude, second)  # Core holds a copy of another client
        remove = ("from openmachine.access import restricted as _s\n_install = _s._install\n"
                  "def _removed(client):\n client.unlink()\n return _install(client)\n_s._install = _removed\n")
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran", github=self.routing(first)),
                                    prelude + remove)
        out, _ = process.communicate(timeout=60)
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"], out), ("github_client_invalid", "known_no_effect", b""))
        self.assertEqual(self.instances(lifetime), [str(self.stable())])

    def test_a_client_restricted_work_could_replace_is_refused(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[0]["status"], "admitted")  # Core creates the persistent home
        client = HOME / "om-code"
        client.write_text("#!/bin/sh\n")
        client.chmod(0o755)
        reply, _, _, _ = self.session(lifetime, command="true", github=self.routing(client))
        self.assertEqual((reply["status"], reply["error"]["code"]), ("denied", "github_client_invalid"), reply)

    def test_the_code_host_and_connector_state_are_control_paths(self):
        """The Code host and the Connector keep keys and sign-in beside Core's state; no session reads them."""
        default = self.root / "home/.local/state/openmachine"
        hosts = [default.parent / "openmachine-code", default.parent / "openmachine-connector"]
        for path in hosts:
            path.mkdir(parents=True, mode=0o700)
            (path / "host").write_text("private\n")
        prelude = f"from openmachine.access import rules; rules.DEFAULT_STATE = rules.Path({str(default)!r}); "
        command = "; ".join(f"cat {path}/host; ls {path}" for path in hosts)
        process, reply = self.start(self.request(self.lifetime(), "read_only", "exec", command), prelude=prelude)
        out, err = process.communicate(timeout=60)
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertNotIn(b"private", out)
        self.assertEqual(err.decode().count(DENIED), 4, err)

    def test_detached_work_shares_its_lifetime(self):
        first, other = self.lifetime(), self.lifetime()
        reply, code, out, err = self.session(first, command=detached("job"))
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        job = int(out.split()[-1])
        unrelated = subprocess.Popen(["/bin/sleep", "60"])
        self.addCleanup(unrelated.kill)
        self.assertTrue(self.alive(job))
        self.assertEqual(self.session(first, command=f"kill -0 {job}")[1], 0)
        self.assertNotEqual(self.session(other, command=f"kill -0 {job}")[1], 0)
        self.assertNotEqual(self.session(first, command=f"kill -0 {unrelated.pid}")[1], 0)

    def test_an_instance_started_but_not_confirmed_stays_recorded(self):
        # The first save that records a started spawner fails, or the launcher dies right there.
        for name, failure in (("ENOSPC", "raise OSError(28, 'No space left on device')"), ("lost launcher", "os._exit(9)")):
            with self.subTest(failure=name):
                lifetime = self.lifetime()
                prelude = ("import os\nfrom openmachine.access import sessions as _s\n_save = _s.Lifetime.save\n"
                           "def _fail(self, record):\n"
                           " if any(item.get('pid') for item in record.get('instances', [])):\n"
                           f"  {failure}\n _save(self, record)\n_s.Lifetime.save = _fail\n")
                process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"), prelude)
                out, _ = process.communicate(timeout=60)
                self.assertEqual(out, b"")
                if name == "ENOSPC":
                    self.assertEqual((reply["error"]["code"], reply["error"]["effect"]), ("launch_unverified", "effect_unknown"))
                else:
                    self.assertEqual((reply, process.returncode), ({}, 9))
                [instance] = json.loads((self.state / "code" / lifetime / "record.json").read_text())["instances"]
                self.assertIsNone(instance["pid"])
                self.assertTrue(members(instance["marker"]), "the started spawner must be running")

    def test_a_lost_spawner_starts_a_new_instance(self):
        lifetime = self.lifetime()
        record = self.state / "code" / lifetime / "record.json"
        self.assertEqual(self.session(lifetime)[1], 0)
        spawner = json.loads(record.read_text())["instances"][0]["pid"]
        # Governed work can kill its own spawner; that session's exit is then unknown (255).
        self.assertEqual(self.session(lifetime, command=f"kill -9 {spawner}")[1], 255)
        reply, code, out, _ = self.session(lifetime, command="echo again")
        self.assertEqual((reply["status"], code, out.strip()), ("admitted", 0, "again"))
        self.assertEqual(len(json.loads(record.read_text())["instances"]), 2)

    def test_a_stopped_spawner_bounds_admission(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[1], 0)
        record = self.state / "code" / lifetime / "record.json"
        [instance] = json.loads(record.read_text())["instances"]
        # Governed work may signal its own instance, including its spawner.
        reply, code, _, err = self.session(lifetime, command=f"(sleep 0.3; kill -STOP {instance['pid']}) </dev/null >/dev/null 2>&1 &")
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        deadline = time.monotonic() + 5
        while not self.stopped(instance["pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.stopped(instance["pid"]))
        started = time.monotonic()
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"),
                                    "from openmachine.access import restricted as _s\n_s.ACKNOWLEDGE_SECONDS = 1\n")
        self.assertEqual(process.wait(timeout=60), 1)
        self.assertLess(time.monotonic() - started, 30)
        # The request may already be queued at the spawner: a retry is not known to be safe.
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"]), ("launch_unverified", "effect_unknown"))
        # The queued launch holds the channel IO; the cleanup that kills the spawner releases it.
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def test_a_spawner_that_never_reports_ready_releases_the_lifetime(self):
        lifetime = self.lifetime()
        # The spawner enters its sandbox and then stalls before it reports ready.
        prelude = ("import time\nfrom openmachine.access import restricted as _s\nfrom openmachine.access.macos import seatbelt as _m\n_s.SPAWN_SECONDS = 1\n"
                   "_apply = _m.apply\ndef _stall(text):\n _apply(text)\n time.sleep(120)\n_m.apply = _stall\n")
        started = time.monotonic()
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"), prelude)
        self.assertEqual(process.wait(timeout=60), 1)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"]), ("lifetime_unverifiable", "known_no_effect"))
        [instance] = json.loads((self.state / "code" / lifetime / "record.json").read_text())["instances"]
        self.assertTrue(members(instance["marker"]), "the stalled spawner is a member")
        process.communicate(timeout=10)
        self.assertEqual(self.session(lifetime, command="echo again")[1:3], (0, "again\n"))

    def test_a_stalled_intermediate_does_not_hold_the_lifetime(self):
        lifetime = self.lifetime()
        noted = self.state / "intermediate"
        # The intermediate process stalls before it can fork the spawner.
        prelude = ("import os, time\nfrom openmachine.access import restricted as _s\n_s.SPAWN_SECONDS = 1\n"
                   f"def _stall():\n open({str(noted)!r}, 'w').write(str(os.getpid()))\n time.sleep(120)\n"
                   "os.setsid = _stall\n")
        started = time.monotonic()
        process, reply = self.start(self.request(lifetime, "read_only", "exec", "echo ran"), prelude)
        self.assertEqual(process.wait(timeout=60), 1)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((reply["error"]["code"], reply["error"]["effect"]), ("lifetime_unverifiable", "known_no_effect"))
        self.assertFalse(self.alive(int(noted.read_text())), "the stalled intermediate is ended")
        process.communicate(timeout=10)
        self.assertEqual(self.session(lifetime, command="echo again")[1:3], (0, "again\n"))

    def test_a_changed_policy_is_refused_in_either_direction(self):
        for first, then in (("read_only", "full_access"), ("full_access", "read_only")):
            with self.subTest(first=first):
                lifetime = self.lifetime()
                self.assertEqual(self.session(lifetime, first)[1], 0)
                reply, code, out, _ = self.session(lifetime, then, "echo ran")
                self.assertEqual((reply["error"]["code"], reply["error"]["effect"], code, out),
                                 ("lifetime_changed", "known_no_effect", 1, ""))

    def test_a_changed_home_is_refused_in_either_direction(self):
        self.a_changed_home_is_refused(HOME.parent)

    def test_each_sandbox_home_is_closed_to_the_sessions_of_every_other(self):
        self.homes_are_closed(HOME.parent, as_folders=True)

    def test_forwarded_signals_reach_the_command_group_but_not_detached_work(self):
        lifetime = self.lifetime()
        kept = int(self.session(lifetime, command=detached("kept"))[2].split()[-1])
        marker = HOME / f"{lifetime}.fg"
        for signum, expected in ((signal.SIGINT, -signal.SIGINT), (signal.SIGTERM, -signal.SIGTERM)):
            with self.subTest(signal=signum.name):
                marker.unlink(missing_ok=True)
                # A shell waiting on a foreground child: the child is not the session's recorded process.
                process, reply = self.start(self.request(lifetime, "read_only", "exec",
                                                         f"sh -c 'echo $$ > {marker}; exec sleep 60'; echo after"))
                self.assertEqual(reply["status"], "admitted", reply)
                self.assertTrue(self.wait_for(marker))
                child = int(marker.read_text())
                process.send_signal(signum)
                out, _ = process.communicate(timeout=20)
                self.assertEqual(process.returncode, expected)
                self.assertNotIn(b"after", out)
                deadline = time.monotonic() + 5
                while self.alive(child) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(self.alive(child))
        self.assertTrue(self.alive(kept))

    def test_workspace_writes_only_its_folders_and_starts_in_the_first(self):
        code, notes, outside = self.root / "code", self.root / "notes", self.root / "outside"
        for path in (code, notes, outside):
            path.mkdir()
            (path / "file.txt").write_text(path.name + "\n")
        git = ["git", "-C", str(code), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        for args in (["init", "-q", "-b", "main"], ["add", "file.txt"], ["commit", "-q", "-m", "Fixture"]):
            subprocess.run(git + args, check=True, capture_output=True)
        # A registered worktree of a folder's repository is writable although no folder holds it.
        linked = self.root / "code-linked"
        subprocess.run(git + ["worktree", "add", "-q", "-b", "linked", str(linked)], check=True, capture_output=True)
        (code / ".claude").mkdir()
        (code / ".claude/settings.json").write_text("{}\n")
        folders = [str(code), str(notes)]
        lifetime = self.lifetime()
        reply, code_, out, err = self.session(lifetime, "workspace", (
            f"pwd; echo w >> {code}/file.txt && echo w >> {notes}/file.txt && echo w > {linked}/new.txt && echo wrote; "
            f"cat {outside}/file.txt; mkdir -p $HOME/.claude && echo .mcp > $HOME/.mcp.json && echo home-ok"),
            folders=folders)
        self.assertEqual((reply["status"], code_), ("admitted", 0), err)
        self.assertEqual(out.split(), [str(code), "wrote", "outside", "home-ok"])
        # Outside the folders, and on names the owner's tools later run, every write is refused, in any case.
        refused = [f"echo x >> {outside}/file.txt", f"echo x > {code}/.git/hooks/pre-commit", f"mkdir {code}/.vscode",
                   f"mkdir -p {notes}/a/.idea", f"echo x >> {code}/.claude/settings.json", f"mkdir {notes}/.Claude",
                   f"mkdir {notes}/.CODEX", f"mkdir {notes}/.agents", f"mkdir -p {notes}/sub/.Cursor", f"mkdir {notes}/.gemini",
                   f"mkdir {code}/.Husky", f"echo {{}} > {notes}/.MCP.json", f"mkdir {linked}/.claude", f"mkdir {notes}/.GIT",
                   f"rm -r {code}/.claude"]
        for command in refused:
            reply, code_, _, err = self.session(lifetime, "workspace", command, folders=folders)
            self.assertEqual(reply["status"], "admitted", reply)
            self.assertNotEqual(code_, 0, command)
            self.assertIn(DENIED, err, command)
        self.assertEqual((outside / "file.txt").read_text(), "outside\n")
        self.assertEqual((code / ".claude/settings.json").read_text(), "{}\n")
        for path in (code / ".vscode", notes / "a/.idea", notes / ".claude", notes / ".codex", notes / ".agents",
                     notes / "sub/.cursor", notes / ".gemini", code / ".husky", notes / ".mcp.json", linked / ".claude",
                     notes / ".git", code / ".git/hooks/pre-commit"):
            self.assertFalse(os.path.lexists(path), path)
        # A lifetime keeps the folders it started with.
        reply, code_, out, _ = self.session(lifetime, "workspace", "echo ran", folders=[str(code)])
        self.assertEqual((reply["error"]["code"], code_, out), ("lifetime_changed", 1, ""))
        # Each admission re-checks the folders: a renamed one is refused before anything runs.
        notes.rename(self.root / "moved")
        reply, code_, out, _ = self.session(lifetime, "workspace", "echo ran", folders=folders)
        self.assertEqual((reply["status"], reply["error"]["code"], reply["error"]["effect"], code_, out),
                         ("denied", "folder_missing", "known_no_effect", 1, ""))
        self.assertIn(str(notes), reply["error"]["message"])

    def test_restricted_sessions_reach_no_sockets_terminals_descriptors_or_denied_areas(self):
        project, personal, denied = self.root / "project", self.root / "personal", self.root / "denied"
        for path in (project, personal, denied):
            path.mkdir()
            (path / ".env").write_text(f"ENV-{path.name.upper()}\n")
        (denied / "drive.txt").write_text("DRIVE-SECRET\n")
        shutil.copy("/usr/bin/true", denied / "tool")
        (project / "outside").symlink_to(denied, target_is_directory=True)
        prelude = f"from openmachine.access.macos import seatbelt; seatbelt.SYSTEM_DENIED = seatbelt.SYSTEM_DENIED + ({str(denied)!r},); "
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.root / "c.sock"))
        listener.listen(1)
        listener.setblocking(False)
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        probes = "\n".join([
            "import ctypes, os, socket, sys",
            "s = socket.socket(socket.AF_UNIX)",
            "try:\n    s.connect(sys.argv[1]); print('unix: connected')\nexcept OSError as e:\n    print('unix:', type(e).__name__)",
            "l = ctypes.CDLL(None); p = ctypes.c_uint()",
            "print('appleevents:', l.bootstrap_look_up(ctypes.c_uint.in_dll(l, 'bootstrap_port'), "
            "b'com.apple.coreservices.appleevents', ctypes.byref(p)))",
            "for fd in range(3, 256):\n    try:\n        os.write(fd, b'x')\n    except OSError:\n        continue\n    print('WRITABLE', fd)"])
        command = (f"{shlex.quote(sys.executable)} -I -c {shlex.quote(probes)} {self.root / 'c.sock'}; "
                   f"echo injected > {os.ttyname(slave)}; cat {project}/.env; cat {personal}/.env; "
                   f"cat {project}/outside/drive.txt; {denied}/tool && echo tool-ran; true")
        process, reply = self.start(self.request(self.lifetime(), "workspace", "exec", command, folders=[str(project)]),
                                    prelude=prelude)
        out, err = (stream.decode(errors="replace") for stream in process.communicate(timeout=60))
        self.assertEqual((reply["status"], process.returncode), ("admitted", 0), err)
        self.assertIn("unix: PermissionError", out)
        self.assertNotIn("appleevents: 0\n", out)
        self.assertNotIn("WRITABLE", out)
        self.assertIn("ENV-PROJECT", out)  # A folder reads its own secret-named files.
        self.assertNotIn("ENV-PERSONAL", out)
        self.assertNotIn("DRIVE-SECRET", out)
        self.assertNotIn("tool-ran", out)
        self.assertGreaterEqual(err.count(DENIED), 4, err)
        with self.assertRaises(BlockingIOError):
            listener.accept()

    def test_restricted_sessions_cannot_create_move_or_replace_git_entries(self):
        """Unsandboxed Git later trusts any .git entry, so none is placed in any writable path of an instance."""
        full, plain, remote = self.root / "full", self.root / "plain", self.root / "plain/remote.git"
        nested, source, unselected = full / "vendor/lib", self.root / "source", self.root / "other"
        for path in (nested, plain):
            path.mkdir(parents=True)
        git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
               "-c", "protocol.file.allow=always"]
        for repository, name in ((full, "file.txt"), (nested, "lib.txt"), (source, "file.txt"), (unselected, "file.txt")):
            repository.mkdir(exist_ok=True)
            (repository / name).write_text("x\n")
            subprocess.run([*git, "-C", str(repository), "init", "-q", "-b", "main"], check=True)
            subprocess.run([*git, "-C", str(repository), "add", name], check=True)
            subprocess.run([*git, "-C", str(repository), "commit", "-q", "-m", "Fixture"], check=True)
        # A submodule whose valid name holds a Git data directory name; one outside the folders too.
        for repository, name, path in ((full, "vendor/objects/ext", "ext"), (unselected, "sub", "sub")):
            subprocess.run([*git, "-C", str(repository), "submodule", "add", "-q", "--name", name, str(source), path], check=True)
            subprocess.run([*git, "-C", str(repository), "commit", "-q", "-m", "Submodule"], check=True)
        subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run([*git, "-C", str(full), "worktree", "add", "-q", "-b", "linked", str(self.root / "linked")], check=True)
        protected = [full / ".git/config", nested / ".git/config", full / ".git/worktrees/linked/commondir",
                     full / ".git/modules/vendor/objects/ext/config"]
        config = [path.read_bytes() for path in protected]
        folders = [str(full), str(plain)]
        lifetime = self.lifetime()
        refused = [f"mkdir -p {full}/new/.git", f"mkdir {plain}/.git", f"mkdir {plain}/.GIT",
                   f"git init -q {plain}/init", f"git -c protocol.file.allow=always clone -q {source} {plain}/clone",
                   f"mkdir {full}/file && echo 'gitdir: /tmp/x' > {full}/file/.git",
                   f"mkdir {full}/renamed && echo 'gitdir: /tmp/x' > {full}/staged && mv {full}/staged {full}/renamed/.git",
                   f"mv {nested}/.git {nested}/.git-old", f"cd {full} && git worktree add -q {plain}/worktree",
                   # The persistent home or the lifetime's temp could otherwise carry one in by a directory rename.
                   'mkdir "$HOME/carried" && mkdir "$HOME/carried/.git"',
                   'mkdir "${TMPDIR%/}/carried" && mkdir "${TMPDIR%/}/carried/.git"',
                   # A repository, a folder itself included, keeps its protected files when moved out,
                   # and a worktree registration stays in place.
                   *(f't="${{TMPDIR%/}}/moved"; mv {path} "$t" && {{ echo "[core]" >> "$t/.git/config"; s=$?; '
                     f'mv "$t" {path}; exit $s; }}' for path in (full, nested)),
                   f'mv {full}/.git/worktrees/linked "${{TMPDIR%/}}/moved"',
                   f'mv {full}/.git/modules/vendor/objects/ext "${{TMPDIR%/}}/moved"',
                   # A HEAD in a name directory would make a later instance treat it as a Git dir.
                   f'echo "ref: refs/heads/main" > {full}/.git/modules/vendor/HEAD',
                   # Protected files are not replaced by unlinking and creating them or by renaming over them.
                   f"cd {full}/.git/modules/vendor/objects/ext && rm config && echo '[core]' > config",
                   f"cd {full}/.git/worktrees/linked && echo /tmp > staged && mv staged commondir",
                   # Nothing outside the folders changes.
                   f"rm {unselected}/sub/.git", f"mkdir {unselected}/.git/modules/sub/refs/zz"]
        # The last command runs in a new lifetime, whose instance builds its own profile.
        for command, session_lifetime in [*((command, lifetime) for command in refused),
                                          (f'mv {full}/.git/modules/vendor/objects/ext "${{TMPDIR%/}}/moved"', self.lifetime())]:
            reply, code, _, err = self.session(session_lifetime, "workspace", command, folders=folders)
            self.assertEqual(reply["status"], "admitted", reply)
            self.assertNotEqual(code, 0, command)
            self.assertIn(DENIED, err, command)
        self.assertFalse(os.path.lexists(full / ".git/modules/vendor/HEAD"))
        self.assertEqual([path.read_bytes() for path in protected], config)
        for path in (full / "new", plain, full / "file", full / "renamed", HOME / "carried", plain / "worktree",
                     plain / "init", plain / "clone"):
            self.assertFalse(os.path.lexists(path / ".git"), path)
        self.assertFalse(any(os.path.lexists(path) for path in (nested / ".git-old", unselected / ".git/modules/sub/refs/zz")))
        self.assertTrue(os.path.lexists(unselected / "sub/.git"))
        worktrees = subprocess.run([*git, "-C", str(full), "worktree", "list"], capture_output=True, text=True).stdout
        self.assertNotIn(str(plain / "worktree"), worktrees)
        reply, code, out, err = self.session(lifetime, "workspace", (
            f"cd {full} && export {IDENTITY} && echo edit >> file.txt && git commit -qam pushed && "
            f"echo edit >> vendor/lib/lib.txt && git -C vendor/lib commit -qam nested && echo edit >> ext/file.txt && "
            f"git -C ext commit -qam inside && git push -q {remote} main && git fetch -q {remote} main && "
            f"git log --oneline -1 FETCH_HEAD"), folders=folders)
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertIn("pushed", out)

    def test_terminal_loss_leaves_nohup_work_running(self):
        lifetime = self.lifetime()
        beat = HOME / f"pty-{lifetime}.beat"
        reply, process, master, output = self.terminal(lifetime, (
            f"tty; nohup sh -c 'while :; do date > {beat}; sleep 0.2; done' >/dev/null 2>&1 & "
            "sleep 1; echo MARK-$((40+2))\n"), "MARK-42")
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertIn("/dev/ttys", output)
        os.close(master)  # PTY loss: the shell gets SIGHUP, the nohup job keeps running.
        process.wait(timeout=20)
        self.assertTrue(self.beating(beat))

    def test_a_tmux_style_server_lives_in_the_lifetime_namespace(self):
        lifetime = self.lifetime()
        server = ("import os,pty,socket,time\nif os.fork(): raise SystemExit\nos.setsid()\n"
                  "s=socket.socket(socket.AF_UNIX); s.bind(os.environ['TMUX_TMPDIR']+'/server'); s.listen(1)\n"
                  "pid,fd=pty.fork()\nif pid==0:\n while True: open(os.environ['HOME']+'/mux.beat','w').write('x'); time.sleep(0.2)\n"
                  "while True: s.accept()[0].close()\n")
        reply, code, _, err = self.session(lifetime, command=f"python3 -c {shlex.quote(server)} </dev/null >/dev/null 2>&1; sleep 1")
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertTrue(self.beating(HOME / "mux.beat"))
        client = "import os,socket; socket.socket(socket.AF_UNIX).connect(os.environ['TMUX_TMPDIR']+'/server'); print('reached')"
        self.assertIn("reached", self.session(lifetime, command=f"python3 -c {shlex.quote(client)}")[2])
        self.assertNotIn("reached", self.session(self.lifetime(), command=f"python3 -c {shlex.quote(client)}")[2])

    @unittest.skipUnless(shutil.which("tmux", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"),
                         "tmux is not installed")
    def test_real_tmux_survives_sessions_in_its_lifetime(self):
        lifetime = self.lifetime()
        reply, code, _, err = self.session(lifetime, command=f"tmux new-session -d -s om 'while :; do date > $HOME/tmux.beat; sleep 0.2; done'")
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertTrue(self.beating(HOME / "tmux.beat"))
        self.assertIn("om", self.session(lifetime, command="tmux ls")[2])
        self.assertNotEqual(self.session(self.lifetime(), command="tmux ls")[1], 0)

    @unittest.skipUnless(Path("/usr/bin/sftp").exists(), "requires the OpenSSH sftp client")
    def test_sftp_runs_the_server_inside_the_sandbox(self):
        upload, download = self.root / "upload.txt", self.root / "download.txt"
        upload.write_text("payload\n")
        result = self.sftp(self.request(self.lifetime(), "read_only", "sftp"),
                           f"put {upload} up.txt\nget up.txt {download}\n-put {upload} {self.root}/escape.txt\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(download.read_text(), "payload\n")
        self.assertIn("Permission denied", result.stdout + result.stderr)
        self.assertFalse((self.root / "escape.txt").exists())


class FullAccessSessionTests(SessionFixture, unittest.TestCase):
    def test_github_routing_reaches_a_full_session(self):
        client = self.root / "bin/om-code"
        client.parent.mkdir(mode=0o755)
        client.write_text("#!/bin/sh\necho client \"$@\"\n")
        client.chmod(0o755)
        broker = "https://127.0.0.1:41234"
        github = {"client": str(client), "environment": {"OM_CODE_BROKER": broker, "OM_CODE_CAPABILITY": "c" * 43,
                  "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{broker}.postBuffer", "GIT_CONFIG_VALUE_0": "1048576"}}
        reply, code, out, err = self.session(self.lifetime(), "full_access", "echo $OM_CODE_BROKER $GIT_CONFIG_KEY_0; om-code ok",
                                             github=github)
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertEqual(out.splitlines(), [f"{broker} http.{broker}.postBuffer", "client ok"])

    def test_a_link_at_the_persistent_home_name_never_refuses_a_full_session(self):
        """/Users/Shared is world-writable and sticky: another local user may create this user's home name
        as a link, here to an ancestor of the client, before any restricted session creates the home."""
        client = self.root / "bin/om-code"
        client.parent.mkdir(mode=0o755)
        client.write_text("#!/bin/sh\necho client\n")
        client.chmod(0o755)
        self.assertFalse(HOME.parent.exists() or HOME.parent.is_symlink())
        HOME.parent.symlink_to(self.root)
        self.addCleanup(HOME.parent.unlink)
        broker = "https://127.0.0.1:41234"
        github = {"client": str(client), "environment": {"OM_CODE_BROKER": broker, "OM_CODE_CAPABILITY": "c" * 43,
                  "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{broker}.postBuffer", "GIT_CONFIG_VALUE_0": "1"}}
        reply, code, out, err = self.session(self.lifetime(), "full_access", "om-code", github=github)
        self.assertEqual((reply["status"], code, out), ("admitted", 0, "client\n"), err)

    def test_full_access_is_an_ordinary_shell(self):
        reply, code, out, err = self.session(self.lifetime(), "full_access", "echo HOME=$HOME; exit 3")
        self.assertEqual((reply["status"], code, out), ("admitted", 3, f"HOME={ACCOUNT_HOME}\n"), err)

    def test_full_access_takes_a_sandbox_home_and_uses_none(self):
        lifetime, key = self.lifetime(), "f" * 32
        for extra in ({"home": key}, {}):  # Nor does its lifetime keep one.
            reply, code, out, err = self.session(lifetime, "full_access", "echo HOME=$HOME", **extra)
            self.assertEqual((reply["status"], code, out), ("admitted", 0, f"HOME={ACCOUNT_HOME}\n"), err)
        homes = sessions.keyed_homes(sandbox.home(ACCOUNT_HOME)) if sys.platform == "linux" else HOMES
        self.assertFalse((homes / key).exists())

    def test_a_command_starts_with_default_signal_actions(self):
        """A POSIX shell sets no trap for a signal ignored when it started, as Python's SIGPIPE and nohup's SIGHUP are."""
        process, reply = self.start(self.request(self.lifetime(), "full_access", "exec", "/bin/sh -c 'trap : HUP PIPE; trap'"),
                                    prelude="import signal; signal.signal(signal.SIGHUP, signal.SIG_IGN)\n")
        out, err = process.communicate(timeout=60)
        self.assertEqual(reply.get("status"), "admitted", err)
        self.assertEqual(sum(name in out.decode() for name in ("HUP", "PIPE")), 2, out)

    def test_full_access_names_the_connection_in_exec_and_shell_sessions(self):
        reply, code, out, err = self.session(self.lifetime(), "full_access", 'echo "$SSH_CONNECTION|$SSH_CLIENT|$USER"')
        self.assertEqual((reply["status"], code, out), ("admitted", 0, f"{CONNECTION}|100.64.0.2 52144 22|{USER}\n"), err)
        reply, process, master, output = self.terminal(self.lifetime(), "echo SHELL-$((40+2)) $SSH_CLIENT; exit\n",
                                                       "SHELL-42 100.64.0.2 52144 22", policy="full_access")
        os.close(master)
        process.wait(timeout=20)
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertIn("SHELL-42 100.64.0.2 52144 22", output)

    @unittest.skipUnless(shutil.which("sftp") and any(os.access(path, os.X_OK) for path in sessions.SFTP_SERVERS),
                         "requires the OpenSSH sftp client and server")
    def test_full_access_sftp_reads_and_writes_as_the_user(self):
        upload, download, written = self.root / "upload.txt", self.root / "download.txt", self.root / "written.txt"
        upload.write_text("payload\n")
        result = self.sftp(self.request(self.lifetime(), "full_access", "sftp"),
                           f"put {upload} {written}\nget {written} {download}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((written.read_text(), download.read_text()), ("payload\n", "payload\n"))

    def test_a_request_that_does_not_validate_starts_nothing(self):
        lifetime = self.lifetime()
        for extra in ({"protocol": 3}, {"home": "A" * 32}, {"connection": "100.64.0.2 52144"}, {"user": "openmachine-nobody"}, {"command": None}, {"kind": "sftp"}, {"policy": "admin"},
                      {"policy": "workspace"}, {"folders": ["/tmp"]},
                      {"environment": {"PATH": "/tmp"}}, {"lifetime": "../escape"}, {"unexpected": True}):
            with self.subTest(extra=extra):
                reply, code, out, _ = self.admit(self.request(lifetime, "full_access", "exec", "echo ran") | extra)
                self.assertEqual(reply["status"], "denied")
                self.assertIn(reply["error"]["code"], {"request_invalid", "user_rejected"})
                self.assertEqual((code, out), (1, ""))
        self.assertFalse((self.state / "code" / lifetime).exists())
