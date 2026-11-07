"""Real Code sessions: confinement, shared lifetimes, detached work, PTY loss, SFTP and termination."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid

from openmachine.access import macos
from openmachine.chat import projects
from openmachine.config import load
from test.support.access_sessions import HOME, SessionFixture

DENIED = "not permitted"


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
            f"cat {Path.home()}/.ssh/config; echo HOME=$HOME; exit 3"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertEqual(code, 3, err)
        self.assertIn("here", out)
        self.assertIn(f"HOME={HOME}", out)
        self.assertGreaterEqual(err.count(DENIED), 3, err)
        self.assertFalse((outside / "x").exists())

    def test_detached_work_shares_its_lifetime_until_terminated(self):
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
        _, code, out, _ = self.session(other, command=detached("other"))
        survivor = int(out.split()[-1])
        self.assertEqual(self.terminate(first), ({"status": "verified"}, 0))
        self.assertFalse(self.alive(job))
        self.assertTrue(self.alive(survivor))
        self.assertTrue(self.alive(unrelated.pid))
        self.assertEqual(self.terminate(first), ({"status": "verified"}, 0))  # Replayed, never re-run.
        self.assertEqual(self.terminate(other), ({"status": "verified"}, 0))
        self.assertFalse(self.alive(survivor))

    def test_terminate_stops_work_that_keeps_forking_and_exiting(self):
        for setsid in (False, True):
            with self.subTest(setsid=setsid):
                lifetime = self.lifetime()
                beat = HOME / f"{lifetime}.hop"
                hop = ("import os\nn=0\nwhile True:\n if os.fork(): os._exit(0)\n" + (" os.setsid()\n" if setsid else "")
                       + f" n+=1\n if n%50==0: open({str(beat)!r},'w').write(str(n))\n")
                reply, code, _, err = self.session(lifetime, command=f"python3 -c {shlex.quote(hop)} </dev/null >/dev/null 2>&1 &")
                self.assertEqual((reply["status"], code), ("admitted", 0), err)
                self.assertTrue(self.beating(beat))
                self.assertEqual(self.terminate(lifetime), ({"status": "verified"}, 0))
                self.assertFalse(self.beating(beat))

    def test_an_instance_started_but_not_confirmed_is_still_found_by_terminate(self):
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
                self.assertTrue(macos.members(instance["marker"])[0], "the started spawner must be running")
                self.assertEqual(self.terminate(lifetime), ({"status": "verified"}, 0))
                self.assertEqual(macos.members(instance["marker"]), ([], False))

    def test_a_lost_spawner_starts_a_new_instance_and_terminate_covers_both(self):
        lifetime = self.lifetime()
        job = int(self.session(lifetime, command=detached("orphan"))[2].split()[-1])
        record = self.state / "code" / lifetime / "record.json"
        spawner = json.loads(record.read_text())["instances"][0]["pid"]
        # Governed work can kill its own spawner; that session's exit is then unknown (255).
        self.assertEqual(self.session(lifetime, command=f"kill -9 {spawner}")[1], 255)
        reply, code, out, _ = self.session(lifetime, command="echo again")
        self.assertEqual((reply["status"], code, out.strip()), ("admitted", 0, "again"))
        self.assertEqual(len(json.loads(record.read_text())["instances"]), 2)
        self.assertEqual(self.terminate(lifetime), ({"status": "verified"}, 0))
        self.assertFalse(self.alive(job))

    def test_a_changed_grant_is_refused_and_a_terminated_lifetime_is_retired(self):
        lifetime = self.lifetime()
        self.assertEqual(self.session(lifetime)[1], 0)
        reply, code, _, _ = self.session(lifetime, "full_access")
        self.assertEqual((reply["error"]["code"], code), ("lifetime_changed", 1))
        self.assertEqual(self.terminate(lifetime)[0], {"status": "verified"})
        for policy in ("read_only", "full_access"):
            reply, code, out, _ = self.session(lifetime, policy, "echo ran")
            self.assertEqual((reply["error"]["code"], reply["error"]["effect"], code, out),
                             ("lifetime_terminated", "known_no_effect", 1, ""))

    def test_terminate_waits_for_an_admission_in_progress_and_retires_the_id(self):
        from openmachine.foundation.state import lease, private_directory
        for lifetime, existing in ((self.lifetime(), False), (self.lifetime(), True)):
            with self.subTest(existing=existing):
                job = int(self.session(lifetime, command=detached("held"))[2].split()[-1]) if existing else None
                directory = private_directory(private_directory(self.state / "code") / lifetime)
                with lease(directory / "lock", wait=True):  # Stands in for an admission holding the lock.
                    pending = self.terminate(lifetime, background=True)
                    time.sleep(1)
                    self.assertIsNone(pending.poll(), "terminate must not answer while an admission is in progress")
                result = json.loads(pending.stdout.read())
                pending.wait(timeout=60)
                self.assertEqual(result, {"status": "verified" if existing else "not_found"})
                if existing:
                    self.assertFalse(self.alive(job))
                reply, code, out, _ = self.session(lifetime, command="echo ran")
                self.assertEqual((reply["error"]["code"], code, out), ("lifetime_terminated", 1, ""))

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
        self.assertEqual(self.terminate(lifetime)[0], {"status": "verified"})
        self.assertFalse(self.alive(kept))

    def test_selected_projects_write_only_full_roots_outside_protected_paths(self):
        full, read_only = self.root / "full", self.root / "read-only"
        for path in (full, read_only):
            path.mkdir()
            (path / "file.txt").write_text(path.name + "\n")
        subprocess.run(["git", "-C", str(full), "init", "-q"], check=True)
        settings = load(self.config)
        roots = [{"machine": "local", "workspace_name": str(path), "relative_path": ".", "mode": mode,
                  "target_id": projects.inspect(settings, "local", str(path), "inspect", ".")["project"]["target_id"]}
                 for path, mode in ((full, "full_access"), (read_only, "read_only"))]
        scope = {"protocol": 1, "roots": roots, "authority": {"policy_revision": 1, "grant_id": "fixture",
                                                               "grant_revision": 1, "session_generation": 0}}
        lifetime = self.lifetime()
        reply, code, out, err = self.session(lifetime, "selected_projects", (
            f"echo w >> {full}/file.txt && echo full-ok; echo x > {full}/.git/hooks/pre-commit; "
            f"echo x >> {read_only}/file.txt; cat {read_only}/file.txt"), machine="local", scope=scope)
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertIn("full-ok", out)
        self.assertIn("read-only", out)
        self.assertEqual(err.count(DENIED), 2, err)
        self.assertFalse((full / ".git/hooks/pre-commit").exists())
        self.assertEqual((read_only / "file.txt").read_text(), "read-only\n")
        # The live instance is reused, but a replaced directory is never rebound to the old grant.
        read_only.rename(self.root / "moved")
        read_only.mkdir()
        reply, code, out, _ = self.session(lifetime, "selected_projects", "echo ran", machine="local", scope=scope)
        self.assertEqual((reply["status"], reply["error"]["code"], reply["error"]["effect"], code, out),
                         ("denied", "scope_target_changed", "known_no_effect", 1, ""))

    def test_terminal_loss_leaves_nohup_work_running_until_terminate(self):
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
        self.assertEqual(self.terminate(lifetime)[0], {"status": "verified"})
        self.assertFalse(self.beating(beat))

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
        self.assertEqual(self.terminate(lifetime)[0], {"status": "verified"})
        self.assertFalse(self.beating(HOME / "mux.beat"))

    @unittest.skipUnless(shutil.which("tmux", path=os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"),
                         "tmux is not installed")
    def test_real_tmux_survives_sessions_and_ends_with_the_lifetime(self):
        lifetime = self.lifetime()
        reply, code, _, err = self.session(lifetime, command=f"tmux new-session -d -s om 'while :; do date > $HOME/tmux.beat; sleep 0.2; done'")
        self.assertEqual((reply["status"], code), ("admitted", 0), err)
        self.assertTrue(self.beating(HOME / "tmux.beat"))
        self.assertIn("om", self.session(lifetime, command="tmux ls")[2])
        self.assertNotEqual(self.session(self.lifetime(), command="tmux ls")[1], 0)
        self.assertEqual(self.terminate(lifetime)[0], {"status": "verified"})
        self.assertFalse(self.beating(HOME / "tmux.beat"))

    @unittest.skipUnless(Path("/usr/bin/sftp").exists(), "requires the OpenSSH sftp client")
    def test_sftp_runs_the_server_inside_the_sandbox(self):
        lifetime = self.lifetime()
        wrapper = self.root / "host.py"
        # sftp -D runs this stand-in host; it relays the session the way the Go host would.
        wrapper.write_text(
            "import json,socket,subprocess,sys\n"
            f"command = {self.command('session', '--control-fd', 'FD')!r}\n"
            "parent, child = socket.socketpair()\n"
            "process = subprocess.Popen([str(child.fileno()) if part == 'FD' else part for part in command], pass_fds=[child.fileno()])\n"
            "child.close()\n"
            f"parent.sendall(json.dumps({self.request(lifetime, 'read_only', 'sftp')!r}).encode() + b'\\n')\n"
            "reply = json.loads(parent.makefile('rb').readline())\n"
            "sys.exit(process.wait() if reply.get('status') == 'admitted' else 2)\n")
        upload, download = self.root / "upload.txt", self.root / "download.txt"
        upload.write_text("payload\n")
        batch = self.root / "batch"
        batch.write_text(f"put {upload} up.txt\nget up.txt {download}\n-put {upload} {self.root}/escape.txt\n")
        result = subprocess.run(["/usr/bin/sftp", "-b", str(batch), "-D", f"{sys.executable} {wrapper}"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(download.read_text(), "payload\n")
        self.assertIn("Permission denied", result.stdout + result.stderr)
        self.assertFalse((self.root / "escape.txt").exists())


class FullAccessSessionTests(SessionFixture, unittest.TestCase):
    def test_full_access_is_an_ordinary_shell_with_best_effort_cleanup(self):
        lifetime = self.lifetime()
        # A live session: its leader still verifies, so its foreground and background work is ended.
        process, reply = self.start(self.request(lifetime, "full_access", "exec",
                                                 "echo HOME=$HOME; sleep 60 </dev/null >/dev/null 2>&1 & echo job=$!; wait"))
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertEqual(process.stdout.readline().decode().strip(), f"HOME={Path.home()}")
        job = int(process.stdout.readline().decode().split("job=")[1])
        self.assertTrue(self.alive(job))
        result, code = self.terminate(lifetime)
        self.assertEqual((result, code), ({"status": "termination_unverified", "reason": "full_access_best_effort"}, 1))
        process.wait(timeout=20)
        self.assertFalse(self.alive(job))
        self.assertEqual(self.terminate(lifetime), (result, 1))  # Replayed for a lost reply.

    def test_full_access_never_claims_work_its_leader_no_longer_anchors(self):
        lifetime = self.lifetime()
        # The shell exits and leaves a job behind; its session id is no longer provably this session's.
        reply, code, out, _ = self.session(lifetime, "full_access", "sleep 60 </dev/null >/dev/null 2>&1 & echo job=$!")
        self.assertEqual((reply["status"], code), ("admitted", 0))
        job = int(out.split("job=")[1].split()[0])
        self.addCleanup(lambda: self.alive(job) and os.kill(job, signal.SIGKILL))
        self.assertEqual(self.terminate(lifetime)[0]["status"], "termination_unverified")
        self.assertTrue(self.alive(job))

    def test_a_request_that_does_not_validate_starts_nothing(self):
        lifetime = self.lifetime()
        for extra in ({"user": "root"}, {"command": None}, {"kind": "sftp"}, {"policy": "selected_projects"},
                      {"environment": {"PATH": "/tmp"}}, {"lifetime": "../escape"}, {"unexpected": True}):
            with self.subTest(extra=extra):
                reply, code, out, _ = self.admit(self.request(lifetime, "full_access", "exec", "echo ran") | extra)
                self.assertEqual(reply["status"], "denied")
                self.assertIn(reply["error"]["code"], {"request_invalid", "user_rejected"})
                self.assertEqual((code, out), (1, ""))
        self.assertFalse((self.state / "code" / lifetime).exists())
