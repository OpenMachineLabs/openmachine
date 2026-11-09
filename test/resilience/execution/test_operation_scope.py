"""Real macOS Seatbelt confinement of scoped commands, their descendants and PTYs."""

import os
from pathlib import Path
import pty
import shutil
import socket
import unittest
import uuid

from openmachine.access import macos as seatbelt
from test.support.operation_scope import AUTHORITY, ScopeFixture

DENIED = "Operation not permitted"


@unittest.skipUnless(seatbelt.unavailable_reason() is None, "requires the macOS Seatbelt backend")
class NativeScopeTests(ScopeFixture, unittest.TestCase):
    def shell(self, script, *, cwd=None, tty=False):
        return self.run_scoped(["/bin/sh", "-c", script], cwd, tty=tty)

    def test_commands_and_descendants_read_broadly_but_write_only_full_projects(self):
        for tty in (False, True):
            with self.subTest(tty=tty):
                allowed = self.shell(f"cat ../desktop/file.txt && ls .. && cat {self.personal}/notes.txt && "
                                     f"cat {self.developer}/dev.txt && echo w > new.txt && echo w > ../cloud/new.txt", tty=tty)
                self.assertEqual((allowed["status"], allowed["exit_code"]), ("succeeded", 0), allowed["output"])
                self.assertIn("personal notes", allowed["output"])
                for script in (f"cat {self.home}/.ssh/id_test", f"ls {self.home}/Library/CloudStorage",
                               f"python3 -c \"open('{self.drive}/drive.txt').read()\"",
                               f"sh -c 'sh -c \"cat {self.drive}/drive.txt\"'", "echo x > ../desktop/file.txt",
                               "touch ../created", "mkdir ../made", "mv ../desktop/file.txt moved",
                               f"echo x > {self.personal}/new", "echo x > /private/tmp/om-scope-escape"):
                    denied = self.shell(script, tty=tty)
                    self.assertEqual(denied["status"], "failed", script)
                    self.assertIn(DENIED, denied["output"], script)
                    self.assertNotIn("SECRET", denied["output"], script)
                found = self.shell(f"find {self.root} -name '*.txt' 2>/dev/null; true", tty=tty)
                self.assertIn("notes.txt", found["output"])
                self.assertNotIn("drive.txt", found["output"])
                self.shell(f"(nohup sh -c 'sleep 0.3; echo x > {self.dev}/escaped' >/dev/null 2>&1 &); sleep 1", tty=tty)
                self.assertFalse((self.dev / "escaped").exists())
                self.assertEqual((self.desktop / "file.txt").read_text(), "desktop\n")
                self.assertFalse((self.dev / "created").exists() or (self.personal / "new").exists())
        sibling = self.shell("git log --oneline && echo x > new.txt", cwd=self.desktop)
        self.assertIn("Fixture", sibling["output"])
        self.assertIn(DENIED, sibling["output"])
        self.assertFalse((self.desktop / "new.txt").exists())

    def test_secret_files_and_denied_areas_open_only_inside_selected_roots(self):
        own = self.shell("cat .env && cat ../desktop/.env && ls -a ../../personal")
        self.assertEqual(own["status"], "succeeded", own["output"])
        self.assertIn("ENV-CORE", own["output"])
        self.assertIn("ENV-DESKTOP", own["output"])
        self.assertIn(".env", own["output"])  # Names stay visible; contents do not.
        for script in (f"cat {self.personal}/.env", f"grep -r ENV- {self.personal}"):
            denied = self.shell(script)
            self.assertNotIn("ENV-PERSONAL", denied["output"], script)
            self.assertIn(DENIED, denied["output"], script)
        tls = self.shell("curl -sS -o /dev/null -w '%{http_code}' https://example.com")
        self.assertEqual(tls["output"].strip()[-3:], "200", tls["output"])
        drive = self.scope(self.binding(self.drive, "read_only"), self.binding(self.core, "full_access"))
        self.assertIn("DRIVE-SECRET", self.run_scoped(["cat", str(self.drive / "drive.txt")], scope=drive)["output"])

    def test_links_control_surfaces_and_other_processes_cannot_widen_scope(self):
        (self.core / "outside").symlink_to(self.drive, target_is_directory=True)
        os.link(self.drive / "drive.txt", self.core / "planted")
        for script in ("cat outside/drive.txt", f"ln {self.personal}/notes.txt copy", f"ls {self.state}",
                       "echo x > .git/hooks/pre-commit", "git config user.name changed", "echo x > .git/config.worktree",
                       "echo /tmp > .git/commondir", "mv .git .git-moved", "mkdir -p .git/modules/lib/hooks", "mkdir .vscode"):
            result = self.shell(script)
            self.assertEqual(result["status"], "failed", script)
            self.assertNotIn("SECRET", result["output"], script)
        # Scoped work cannot register a worktree, so it cannot point one at another directory either.
        forged = self.shell(f"mkdir .git/worktrees && mkdir .git/worktrees/forged && cd .git/worktrees/forged && "
                            f"echo {self.personal}/.git > gitdir")
        self.assertIn(DENIED, forged["output"])
        widened = self.shell(f"echo x > {self.personal}/owned")
        self.assertIn(DENIED, widened["output"])
        self.assertNotIn(str(self.personal), {item["path"] for item in widened["scope"]["worktrees"]})
        # Path-based enforcement: a hardlink placed inside a granted root before launch is in scope.
        self.assertIn("DRIVE-SECRET", self.shell("cat planted")["output"])

        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.root / "c.sock"))
        listener.listen(1)
        listener.setblocking(False)
        probe = self.run_scoped(["python3", "-c", "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])",
                                 str(self.root / "c.sock")])
        self.assertIn("PermissionError", probe["output"])
        with self.assertRaises(BlockingIOError):
            listener.accept()

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        terminal = self.shell(f"echo injected > {os.ttyname(slave)}")
        self.assertIn(DENIED, terminal["output"])
        apple = self.run_scoped(["python3", "-c", "import ctypes; l=ctypes.CDLL(None); p=ctypes.c_uint(); "
                                 "print(l.bootstrap_look_up(ctypes.c_uint.in_dll(l, 'bootstrap_port'), "
                                 "b'com.apple.coreservices.appleevents', ctypes.byref(p)))"])
        self.assertNotEqual(apple["output"].strip(), "0")
        descriptors = self.run_scoped(["python3", "-c", "import os\nfor fd in range(3, 256):\n    try:\n        os.write(fd, b'x')\n"
                                       "    except OSError:\n        continue\n    print('WRITABLE', fd)"])
        self.assertEqual(descriptors["status"], "succeeded")
        self.assertNotIn("WRITABLE", descriptors["output"])

        tool = self.drive / "tool"
        shutil.copy("/usr/bin/true", tool)
        refused = self.run_scoped([str(tool)])
        self.assertEqual((refused["status"], refused["command"]["spawned"], refused["error"]), ("failed", False, "PermissionError"))

    def test_git_entries_cannot_be_created_moved_or_replaced_wherever_commands_write(self):
        """Any .git entry, and every Git dir nested in one, selects configuration and hooks that unsandboxed Git runs."""
        plain, nested, linked = self.dev / "plain", self.core / "vendor" / "lib", self.dev / "core-linked"
        plain.mkdir()
        nested.mkdir(parents=True)
        (nested / "lib.txt").write_text("lib\n")
        self.git(nested, "init", "-q", "-b", "main")
        self.git(nested, "add", "lib.txt")
        self.git(nested, "commit", "-q", "-m", "Nested")
        # A submodule, and a linked worktree with that submodule: Git dirs nested in .git/modules and .git/worktrees.
        self.git(self.core, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.cloud), "mod")
        self.git(self.core, "commit", "-q", "-m", "Submodule")
        self.git(self.core, "worktree", "add", "-q", "-b", "linked", str(linked))
        self.git(linked, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
        configs = [nested / ".git/config", self.core / ".git/modules/mod/config",
                   self.core / ".git/worktrees/core-linked/commondir", self.core / ".git/worktrees/core-linked/modules/mod/config"]
        before = [path.read_bytes() for path in configs]
        scope = self.scope(self.binding(self.dev, "read_only"), self.binding(self.core, "full_access"),
                           self.binding(plain, "full_access"))
        clone = "import ctypes, os, sys; c = ctypes.CDLL(None, use_errno=True); " \
                "sys.exit(c.clonefile(b'vendor/lib', b'vendor/copy', 0) and os.strerror(ctypes.get_errno()))"
        refused = [
            # New repositories and links: nested, at a Full root without one, linked, renamed or cloned into place.
            (self.core, "mkdir -p new/.git"), (plain, "mkdir .git"),
            (self.core, "mkdir file && echo 'gitdir: /tmp/x' > file/.git"),
            (self.core, "mkdir symlink && ln -s /tmp/x symlink/.git"),
            (self.core, "mkdir renamed && echo 'gitdir: /tmp/x' > staged && mv staged renamed/.git"),
            (self.core, "git worktree add -q ../plain/worktree"), (self.core, f"python3 -c \"{clone}\""),
            # Replacing one, or preparing one in the private temp to carry in by renaming its parent.
            (self.core, "mv vendor/lib/.git vendor/lib/.git-old"),
            (self.core, 'mkdir "${TMPDIR%/}/carried" && mkdir "${TMPDIR%/}/carried/.git"'),
            # A repository moved into the private temp keeps its protected files there.
            (self.core, 't="${TMPDIR%/}"; mv vendor/lib "$t/lib" && { echo "[core]" >> "$t/lib/.git/config"; s=$?; '
                        'mv "$t/lib" vendor/lib; exit $s; }'),
            # Nested Git dirs stay in place, and their own configuration is protected too.
            (self.core, "mv .git/modules/mod moved"), (self.core, "mv .git/worktrees/core-linked moved"),
            (self.core, "mkdir .git/worktrees/forged"), (self.core, "ln -s /tmp .git/worktrees/forged"),
            (self.core, 'echo "[core]" >> .git/worktrees/core-linked/modules/mod/config')]
        if os.path.exists(str(self.root).upper()):  # A case-insensitive volume resolves .GIT as .git.
            refused.append((self.core, "mkdir upper && mkdir upper/.GIT"))
        for cwd, script in refused:
            result = self.run_scoped(["/bin/sh", "-c", script], cwd, scope=scope)
            self.assertEqual(result["status"], "failed", script)
            self.assertIn(DENIED, result["output"], script)
        for path in (*(self.core / name for name in ("new", "file", "symlink", "renamed", "upper", "vendor/copy")), plain):
            self.assertFalse(os.path.lexists(path / ".git"), path)
        self.assertFalse(any(os.path.lexists(path) for path in (nested / ".git-old", self.core / "moved",
                                                                 self.core / ".git/worktrees/forged")))
        self.assertEqual([path.read_bytes() for path in configs], before)
        self.assertNotIn(str(plain / "worktree"), self.git(self.core, "worktree", "list"))
        self.git(self.root, "init", "-q", "--bare", str(plain / "remote.git"))
        allowed = self.run_scoped(["/usr/bin/env", "GIT_AUTHOR_NAME=Fixture", "GIT_AUTHOR_EMAIL=fixture@example.invalid",
                                   "GIT_COMMITTER_NAME=Fixture", "GIT_COMMITTER_EMAIL=fixture@example.invalid", "/bin/sh", "-c",
                                   "echo edit >> file.txt && git commit -qam pushed && echo edit >> vendor/lib/lib.txt && "
                                   "git -C vendor/lib commit -qam nested && echo edit >> mod/file.txt && "
                                   "git -C mod commit -qam inside && git push -q ../plain/remote.git main && "
                                   "git fetch -q ../plain/remote.git main && git log --oneline -1 FETCH_HEAD && "
                                   "git rm -qf mod && git commit -qm removed"], scope=scope)
        self.assertEqual(allowed["status"], "succeeded", allowed["output"])
        self.assertIn("pushed", allowed["output"])
        self.assertIn("pushed", self.git(plain / "remote.git", "log", "--oneline", "-1", "main"))
        self.assertIn("nested", self.git(nested, "log", "--oneline", "-1"))
        self.assertEqual(self.git(self.core, "ls-files", "mod"), "")

    def test_a_failed_scoped_worktree_creation_leaves_no_registration(self):
        # A base that tracks IDE settings cannot be checked out under scope; Git then removes what it created.
        (self.core / ".vscode").mkdir()
        (self.core / ".vscode/settings.json").write_text("{}\n")
        self.git(self.core, "add", ".vscode/settings.json")
        self.git(self.core, "commit", "-q", "-m", "Settings")
        context = self.app.read_workspace_file("local", str(self.core), "file.txt")["context"]
        destination = self.dev / "core-failed"
        created = self.app.create_worktree("local", str(self.core), str(destination), "main", uuid.uuid4().hex,
                                           context["workspace_identity"], "failed-wt", operation_scope=self.scope())
        created = self.app.read_execution("local", created["execution_id"], wait_seconds=5)
        while created["status"] not in {"succeeded", "failed", "outcome_unknown"}:
            created = self.app.read_execution("local", created["execution_id"], wait_seconds=1)
        self.assertEqual(created["status"], "failed", created["output"])
        self.assertIn(DENIED, created["output"])
        self.assertFalse(os.path.lexists(destination))
        self.assertEqual(list((self.core / ".git/worktrees").glob("*")), [])
        self.assertNotIn(str(destination), self.git(self.core, "worktree", "list"))

    def test_development_tools_use_private_runtime_paths(self):
        (self.cloud / "test_fixture.py").write_text(
            "import tempfile, unittest\nclass T(unittest.TestCase):\n    def test_tmp(self):\n"
            "        with tempfile.NamedTemporaryFile() as f:\n            f.write(b'x')\n")
        commit = self.shell("echo c >> file.txt && git -c user.name=Fixture -c user.email=fixture@example.invalid "
                            "commit -qam scoped && git status --short && git log --oneline | head -1")
        self.assertEqual(commit["status"], "succeeded", commit["output"])
        self.assertIn("scoped", commit["output"])
        tests = self.run_scoped(["python3", "-m", "unittest", "-q", "test_fixture"], self.cloud)
        self.assertEqual(tests["status"], "succeeded", tests["output"])
        environment = self.run_scoped(["/usr/bin/env"])
        variables = dict(line.split("=", 1) for line in environment["output"].splitlines() if "=" in line)
        private = Path(environment["scope"]["private"])
        self.assertEqual(variables["HOME"], str(private / "home"))
        self.assertTrue(variables["TMPDIR"].startswith(str(private)))
        self.assertFalse({"SSH_AUTH_SOCK", "GIT_DIR"} & set(variables))
        self.assertFalse(private.exists())
        scope = environment["scope"]
        self.assertEqual((scope["backend"], scope["authority"], scope["denials"]), ("macos_seatbelt", AUTHORITY, "not_observable"))
        self.assertEqual({(root["path"], root["mode"]) for root in scope["roots"]},
                         {(str(self.dev), "read_only"), (str(self.core), "full_access"), (str(self.cloud), "full_access")})
        record = (self.state / "executions" / environment["execution_id"] / "record.json").read_text()
        self.assertIn('"private_cleanup": "verified"', record)

    def test_scoped_worktree_is_created_under_a_read_only_parent_and_inherits_full(self):
        context = self.app.read_workspace_file("local", str(self.core), "file.txt")["context"]
        destination = self.dev / "core-wt"
        created = self.app.create_worktree("local", str(self.core), str(destination), "main", uuid.uuid4().hex,
                                           context["workspace_identity"], "scoped-wt", operation_scope=self.scope())
        created = self.app.read_execution("local", created["execution_id"], wait_seconds=5)
        while created["status"] not in {"succeeded", "failed", "outcome_unknown"}:
            created = self.app.read_execution("local", created["execution_id"], wait_seconds=1)
        self.assertEqual(created["status"], "succeeded", created["output"])
        self.assertEqual(created["scope"]["destination"], str(destination))
        inside = self.shell("echo w >> file.txt && git -c user.name=Fixture -c user.email=fixture@example.invalid "
                            "commit -qam inherited && cat ../desktop/file.txt", cwd=destination)
        self.assertEqual(inside["status"], "succeeded", inside["output"])
        self.assertIn(DENIED, self.shell("echo 'gitdir: /tmp/x' > .git", cwd=destination)["output"])
        self.assertIn(DENIED, self.shell(f"echo /tmp > {self.core}/.git/worktrees/core-wt/commondir")["output"])
        self.fault("path_out_of_scope", lambda: self.app.create_worktree(
            "local", str(self.core), str(self.personal / "wt"), "main", uuid.uuid4().hex,
            context["workspace_identity"], "denied-wt", operation_scope=self.scope()))
        # Beside the repository needs no selection of the parent.
        beside = self.app.create_worktree("local", str(self.core), str(self.dev / "core-beside"), "main", uuid.uuid4().hex,
                                          context["workspace_identity"], "beside-wt",
                                          operation_scope=self.scope(self.binding(self.core, "full_access")))
        beside = self.app.read_execution("local", beside["execution_id"], wait_seconds=5)
        while beside["status"] not in {"succeeded", "failed", "outcome_unknown"}:
            beside = self.app.read_execution("local", beside["execution_id"], wait_seconds=1)
        self.assertEqual(beside["status"], "succeeded", beside["output"])
        self.assertTrue((self.dev / "core-beside" / "file.txt").is_file())


if __name__ == "__main__":
    unittest.main()
