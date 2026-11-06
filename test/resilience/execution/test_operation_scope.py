"""Real macOS Seatbelt confinement of scoped commands, their descendants and PTYs."""

import os
from pathlib import Path
import pty
import shutil
import socket
import unittest
import uuid

from openmachine.execution import seatbelt
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

    def test_links_control_surfaces_and_other_processes_cannot_widen_scope(self):
        (self.core / "outside").symlink_to(self.drive, target_is_directory=True)
        os.link(self.drive / "drive.txt", self.core / "planted")
        for script in ("cat outside/drive.txt", f"ln {self.personal}/notes.txt copy", f"ls {self.state}",
                       "echo x > .git/hooks/pre-commit", "git config user.name changed", "echo x > .git/config.worktree",
                       "echo /tmp > .git/commondir", "mv .git .git-moved", "mkdir -p .git/modules/lib/hooks", "mkdir .vscode"):
            result = self.shell(script)
            self.assertEqual(result["status"], "failed", script)
            self.assertNotIn("SECRET", result["output"], script)
        forged = self.shell(f"mkdir .git/worktrees && mkdir .git/worktrees/forged && cd .git/worktrees/forged && "
                            f"echo {self.personal}/.git > gitdir && echo ../.. > commondir && "
                            f"echo 'ref: refs/heads/main' > HEAD && touch locked")
        self.assertEqual(forged["status"], "succeeded", forged["output"])
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


if __name__ == "__main__":
    unittest.main()
