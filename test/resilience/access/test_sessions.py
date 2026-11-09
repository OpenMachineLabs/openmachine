"""Real Code sessions: confinement, shared lifetimes, detached work, PTY loss and SFTP."""

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

from openmachine.access import macos
from openmachine.chat import projects
from openmachine.config import load
from test.support.access_sessions import HOME, SessionFixture, members

DENIED = "not permitted"
IDENTITY = ("GIT_AUTHOR_NAME=Fixture GIT_AUTHOR_EMAIL=fixture@example.invalid GIT_COMMITTER_NAME=Fixture "
            "GIT_COMMITTER_EMAIL=fixture@example.invalid")


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

    def test_the_code_host_state_is_a_control_path(self):
        """The Go Code host keeps its host key, network keys and socket beside Core's state; no session reads it."""
        default = self.root / "home/.local/state/openmachine"
        code = default.parent / "openmachine-code"
        code.mkdir(parents=True, mode=0o700)
        (code / "host").write_text("private\n")
        prelude = f"from openmachine.access import rules; rules.DEFAULT_STATE = rules.Path({str(default)!r}); "
        process, reply = self.start(self.request(self.lifetime(), "read_only", "exec", f"cat {code}/host; ls {code}"),
                                    prelude=prelude)
        out, err = process.communicate(timeout=60)
        self.assertEqual(reply["status"], "admitted", reply)
        self.assertNotIn(b"private", out)
        self.assertEqual(err.decode().count(DENIED), 2, err)

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
                                    "from openmachine.access import sessions as _s\n_s.ACKNOWLEDGE_SECONDS = 1\n")
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
        prelude = ("import time\nfrom openmachine.access import macos as _m, sessions as _s\n_s.SPAWN_SECONDS = 1\n"
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
        prelude = ("import os, time\nfrom openmachine.access import sessions as _s\n_s.SPAWN_SECONDS = 1\n"
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

    def test_restricted_sessions_cannot_create_move_or_replace_git_entries(self):
        """Unsandboxed Git later trusts any .git entry, so none is placed in any writable path of an instance."""
        full, plain, remote = self.root / "full", self.root / "plain", self.root / "plain/remote.git"
        nested, source, read_only, unselected = full / "vendor/lib", self.root / "source", self.root / "read-only", self.root / "other"
        for path in (nested, plain):
            path.mkdir(parents=True)
        git = ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null",
               "-c", "protocol.file.allow=always"]
        for repository, name in ((full, "file.txt"), (nested, "lib.txt"), (source, "file.txt"), (read_only, "file.txt"),
                                 (unselected, "file.txt")):
            repository.mkdir(exist_ok=True)
            (repository / name).write_text("x\n")
            subprocess.run([*git, "-C", str(repository), "init", "-q", "-b", "main"], check=True)
            subprocess.run([*git, "-C", str(repository), "add", name], check=True)
            subprocess.run([*git, "-C", str(repository), "commit", "-q", "-m", "Fixture"], check=True)
        # A submodule whose valid name holds a Git data directory name; Read-only and unselected ones too.
        for repository, name, path in ((full, "vendor/objects/ext", "ext"), (read_only, "sub", "sub"), (unselected, "sub", "sub")):
            subprocess.run([*git, "-C", str(repository), "submodule", "add", "-q", "--name", name, str(source), path], check=True)
            subprocess.run([*git, "-C", str(repository), "commit", "-q", "-m", "Submodule"], check=True)
        subprocess.run([*git, "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run([*git, "-C", str(full), "worktree", "add", "-q", "-b", "linked", str(self.root / "linked")], check=True)
        protected = [full / ".git/config", nested / ".git/config", full / ".git/worktrees/linked/commondir",
                     full / ".git/modules/vendor/objects/ext/config"]
        config = [path.read_bytes() for path in protected]
        settings = load(self.config)
        roots = [{"machine": "local", "workspace_name": str(path), "relative_path": ".", "mode": mode,
                  "target_id": projects.inspect(settings, "local", str(path), "inspect", ".")["project"]["target_id"]}
                 for path, mode in ((full, "full_access"), (plain, "full_access"), (read_only, "read_only"))]
        scope = {"protocol": 1, "roots": roots, "authority": {"policy_revision": 1, "grant_id": "fixture",
                                                               "grant_revision": 1, "session_generation": 0}}
        lifetime = self.lifetime()
        refused = [f"mkdir -p {full}/new/.git", f"mkdir {plain}/.git",
                   f"mkdir {full}/file && echo 'gitdir: /tmp/x' > {full}/file/.git",
                   f"mkdir {full}/renamed && echo 'gitdir: /tmp/x' > {full}/staged && mv {full}/staged {full}/renamed/.git",
                   f"mv {nested}/.git {nested}/.git-old", f"cd {full} && git worktree add -q {plain}/worktree",
                   # The persistent home or the lifetime's temp could otherwise carry one in by a directory rename.
                   'mkdir "$HOME/carried" && mkdir "$HOME/carried/.git"',
                   'mkdir "${TMPDIR%/}/carried" && mkdir "${TMPDIR%/}/carried/.git"',
                   # A repository, a selected root itself included, keeps its protected files when moved out,
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
                   # Nothing outside the Full roots changes.
                   *(f"rm {repository}/sub/.git" for repository in (read_only, unselected)),
                   f"mkdir {read_only}/.git/modules/sub/objects/zz", f"mkdir {unselected}/.git/modules/sub/refs/zz"]
        # The last command runs in a new lifetime, whose instance builds its own profile.
        for command, session_lifetime in [*((command, lifetime) for command in refused),
                                          (f'mv {full}/.git/modules/vendor/objects/ext "${{TMPDIR%/}}/moved"', self.lifetime())]:
            reply, code, _, err = self.session(session_lifetime, "selected_projects", command, machine="local", scope=scope)
            self.assertEqual(reply["status"], "admitted", reply)
            self.assertNotEqual(code, 0, command)
            self.assertIn(DENIED, err, command)
        self.assertFalse(os.path.lexists(full / ".git/modules/vendor/HEAD"))
        self.assertEqual([path.read_bytes() for path in protected], config)
        for path in (full / "new", plain, full / "file", full / "renamed", HOME / "carried", plain / "worktree"):
            self.assertFalse(os.path.lexists(path / ".git"), path)
        self.assertFalse(any(os.path.lexists(path) for path in (nested / ".git-old", read_only / ".git/modules/sub/objects/zz",
                                                                 unselected / ".git/modules/sub/refs/zz")))
        self.assertTrue(all(os.path.lexists(path / "sub/.git") for path in (read_only, unselected)))
        worktrees = subprocess.run([*git, "-C", str(full), "worktree", "list"], capture_output=True, text=True).stdout
        self.assertNotIn(str(plain / "worktree"), worktrees)
        reply, code, out, err = self.session(lifetime, "selected_projects", (
            f"cd {full} && export {IDENTITY} && echo edit >> file.txt && git commit -qam pushed && "
            f"echo edit >> vendor/lib/lib.txt && git -C vendor/lib commit -qam nested && echo edit >> ext/file.txt && "
            f"git -C ext commit -qam inside && git push -q {remote} main && git fetch -q {remote} main && "
            f"git log --oneline -1 FETCH_HEAD"), machine="local", scope=scope)
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
    def test_full_access_is_an_ordinary_shell(self):
        reply, code, out, err = self.session(self.lifetime(), "full_access", "echo HOME=$HOME; exit 3")
        self.assertEqual((reply["status"], code, out), ("admitted", 3, f"HOME={Path.home()}\n"), err)

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
