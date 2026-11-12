"""The bubblewrap sandbox as arguments: which bubblewrap, what is hidden, and what is protected in a folder."""

import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access.linux import sandbox
from openmachine.foundation.sanitize import Fault


def pairs(arguments, flag):
    """The destination of each mount an argument list makes with flag, in order."""
    return [arguments[index + 2 if flag.endswith("bind") else index + 1]
            for index, item in enumerate(arguments) if item == flag]


class SandboxArgumentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-sandbox-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_the_first_bubblewrap_that_builds_the_sandbox_is_used(self):
        system, prefix = self.root / "system-bwrap", self.root / "payload/python"
        prefix.mkdir(parents=True)
        payload = prefix.parent / "bwrap"
        self.enterContext(patch.object(sandbox, "SYSTEM_BWRAP", system))
        self.enterContext(patch.object(sys, "base_prefix", str(prefix)))
        self.addCleanup(sandbox.bubblewrap.cache_clear)

        def probe(**programs):
            for path, text in ((system, programs.get("system")), (payload, programs.get("payload"))):
                path.unlink(missing_ok=True)
                if text:
                    path.write_text(f"#!/bin/sh\n{text}\n")
                    path.chmod(0o755)
            sandbox.bubblewrap.cache_clear()
            return sandbox.bubblewrap()

        self.assertIsNone(probe())
        self.assertIsNone(probe(system="exit 1", payload="echo 'setting up uid map: Permission denied' >&2; exit 1"))
        self.assertEqual(probe(system="exit 0", payload="exit 0"), system)
        self.assertEqual(probe(system="exit 1", payload="exit 0"), payload)
        self.assertEqual(probe(payload="exit 0"), payload)
        # The probe builds what an instance is built on: namespaces, a read-only root and the private mounts.
        arguments = self.root / "arguments"
        self.assertEqual(probe(system=f'echo "$@" > {arguments}'), system)
        probed = arguments.read_text().split()
        self.assertEqual(probed[-1], "true")
        for expected in (["--unshare-pid"], ["--ro-bind", "/", "/"], ["--proc", "/proc"], ["--dev", "/dev"], ["--tmpfs", "/tmp"],
                         ["--tmpfs", "/run"], ["--cap-drop", "ALL"] if os.getuid() == 0 else ["--unshare-user"]):
            self.assertIn(" ".join(expected), " ".join(probed))
        with patch.object(sandbox.os, "getuid", return_value=0):
            self.assertEqual(sandbox._base()[:2], ["--cap-drop", "ALL"])
            self.assertNotIn("--unshare-user", sandbox._base())

    def test_read_denials_are_physical_paths(self):
        home, elsewhere = self.root / "home", self.root / "elsewhere"
        for path in (home, elsewhere):
            path.mkdir()
        (home / ".aws").symlink_to(elsewhere)
        denied = sandbox.read_denials(home)
        self.assertIn(elsewhere, denied)
        self.assertNotIn(home / ".aws", denied)
        for name in (".ssh", ".gnupg", ".config/gh", ".netrc", ".bash_history", ".local/share/fish/fish_history",
                     ".local/share/keyrings"):
            self.assertIn(home / name, denied)

    def test_what_is_hidden_is_an_empty_read_only_directory_or_an_empty_file(self):
        state, secret, missing = self.root / "state", self.root / "secret", self.root / "missing"
        runtime = state / "service/python"
        nested = state / "code"
        for path in (runtime, nested):
            path.mkdir(parents=True)
        secret.write_text("x\n")
        found = sandbox._masks([nested, state, secret, missing, state], {runtime: "--ro-bind"})
        # A path that does not exist is not mounted over; one inside a hidden directory is hidden with it.
        self.assertEqual(found, ["--ro-bind", "/dev/null", str(secret), "--tmpfs", str(state),
                                 "--ro-bind", str(runtime), str(runtime), "--remount-ro", str(state)])
        # A path inside what stays exposed in a hidden directory is hidden again.
        inner = runtime / "private"
        inner.mkdir()
        found = sandbox._masks([state, inner], {runtime: "--ro-bind"})
        self.assertEqual(found[-4:], ["--tmpfs", str(inner), "--remount-ro", str(inner)])

    def test_the_search_for_repositories_is_bounded_and_enters_no_git_directory(self):
        folder = self.root / "folder"
        expected = []
        for path in ("", "a", "a/b/c/d"):
            (folder / path / ".git/modules/sub/.git").mkdir(parents=True)
            expected.append(folder / path / ".git")
        (folder / "worktree").mkdir()
        (folder / "worktree/.git").write_text("gitdir: /elsewhere\n")
        expected.append(folder / "worktree/.git")
        (folder / "a/b/c/d/e/.git").mkdir(parents=True)  # Five levels below the folder.
        outside = self.root / "outside"
        (outside / ".git").mkdir(parents=True)
        (folder / "link").symlink_to(outside, target_is_directory=True)
        self.assertEqual(sorted(sandbox.git_entries(folder)), sorted(expected))
        for limit in ("MAX_ENTRIES", "MAX_REPOSITORIES"):
            with self.subTest(limit=limit), patch.object(sandbox, limit, 3), self.assertRaises(sandbox.TooLarge):
                sandbox.git_entries(folder)

    def instance(self, roots):
        home, private = self.root / "sandbox-home", self.root / "runtime/lifetime"
        for path in (home, private, private.parent / "client"):
            path.mkdir(parents=True, exist_ok=True)
        return sandbox.arguments(home=home, roots=roots, hidden=[self.root / "state"], private=private,
                                 client=private.parent / "client")

    def test_an_instance_writes_only_its_home_its_folders_and_its_lifetime_directory(self):
        folder, state = self.root / "folder", self.root / "state"
        for path in (folder / "vendor/lib/.git", folder / ".git", folder / ".claude", state):
            path.mkdir(parents=True)
        (folder / ".mcp.json").write_text("{}\n")
        arguments = self.instance({folder: sandbox.git_entries(folder)})
        runtime = self.root / "runtime"
        base = len(sandbox._base())
        self.assertEqual(arguments[:base], sandbox._base())
        self.assertEqual(pairs(arguments[base:], "--tmpfs"), [str(runtime), str(state)])
        writable = list(dict.fromkeys(pairs(arguments, "--bind")))  # Bound again where the fixture is in /tmp.
        # The lifetime's directory inside the hidden runtime tree, then the home and the folder, then
        # every directory between a .git and its folder, parents first.
        self.assertEqual(writable, [str(runtime / "lifetime"), str(self.root / "sandbox-home"), str(folder),
                                    str(folder / "vendor"), str(folder / "vendor/lib")])
        readonly = pairs(arguments, "--ro-bind")
        self.assertIn(str(runtime / "client"), readonly)
        # Read-only last, over what exists: the protected names, .mcp.json and each .git.
        self.assertEqual(sorted(readonly[-4:]), sorted(str(folder / name) for name in (".claude", ".mcp.json", ".git", "vendor/lib/.git")))
        self.assertGreater(arguments.index(str(folder)), max(index for index, item in enumerate(arguments) if item == "--remount-ro"))
        # More mounts than one bubblewrap takes are refused, not passed on for it to fail.
        with patch.object(sandbox, "MAX_ARGUMENTS", len(arguments) - 1), self.assertRaises(sandbox.TooLarge):
            self.instance({folder: sandbox.git_entries(folder)})

    def test_a_protected_name_that_is_a_link_cannot_be_held_in_place(self):
        folder, target = self.root / "folder", self.root / "target"
        for path in (folder, target):
            path.mkdir()
        for name in (".vscode", ".mcp.json", ".git"):
            with self.subTest(name=name):
                (folder / name).symlink_to(target)
                with self.assertRaises(Fault) as caught:
                    self.instance({folder: sandbox.git_entries(folder)})
                self.assertEqual((caught.exception.code, caught.exception.effect), ("scope_unsupported", "known_no_effect"))
                self.assertIn(str(folder / name), str(caught.exception))
                (folder / name).unlink()

    @unittest.skipUnless(sys.platform == "linux", "credentials are a Linux socket message")
    def test_a_message_names_its_sender_by_the_kernels_credentials(self):
        credentials = struct.pack("3i", 4242, 1000, 1000)
        self.assertEqual(sandbox.sender([(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credentials)]), 4242)
        unseen = struct.pack("3i", 0, 1000, 1000)  # A sender this namespace cannot see.
        self.assertIsNone(sandbox.sender([(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, unseen)]))
        self.assertIsNone(sandbox.sender([(socket.SOL_SOCKET, socket.SCM_RIGHTS, credentials)]))
        self.assertIsNone(sandbox.sender([]))

    def test_no_bubblewrap_names_its_cause(self):
        installed = self.root / "bwrap"
        installed.write_text("")
        for restricted, uid, system, expected in (
                ("1\n", 1000, installed, "apparmor_parser"), ("1\n", 0, installed, "container"),
                ("0\n", 1000, installed, "older than 0.5"), (OSError(), 1000, installed, "container"),
                ("0\n", 1000, self.root / "missing", "None is installed")):
            with self.subTest(restricted=restricted, uid=uid), patch.object(sandbox.os, "getuid", return_value=uid), \
                    patch.object(sandbox, "SYSTEM_BWRAP", system), patch.object(sys, "base_prefix", str(self.root / "none/python")), \
                    patch.object(Path, "read_text", side_effect=[restricted]):
                fault = sandbox.unavailable()
            self.assertEqual((fault.code, fault.effect), ("scope_unsupported", "known_no_effect"))
            self.assertIn(expected, str(fault))


if __name__ == "__main__":
    unittest.main()
