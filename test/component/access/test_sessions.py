"""The persistent restricted-mode home, and the spawner's launch acknowledgment."""

import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from openmachine.access import macos, sessions
from openmachine.foundation.sanitize import Fault

STICKY_ROOT = Path("/tmp").resolve()  # Root-owned and sticky on macOS and Linux.


class PersistentHomeTests(unittest.TestCase):
    def setUp(self):
        name = f"om-access-component-{os.getpid()}-{{uid}}"  # Owned by this process; never another run's home.
        self.home = STICKY_ROOT / name.format(uid=os.getuid())
        self.addCleanup(shutil.rmtree, self.home, True)
        for target, value in (("SHARED", STICKY_ROOT), ("HOME_NAME", name)):
            patcher = patch.object(sessions, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def unsafe(self):
        with self.assertRaises(Fault) as caught:
            sessions.persistent_home()
        self.assertEqual(caught.exception.code, "persistent_home_unsafe")

    def test_the_home_is_created_owner_only_with_only_the_git_identity(self):
        with patch.object(sessions, "_git_identity", return_value={"name": "Dev", "email": "dev@example.invalid"}):
            self.assertEqual(sessions.persistent_home(), self.home)
        for name in ("", "home", "cache", "config", "data", "state"):
            self.assertEqual((self.home / name).stat().st_mode & 0o777, 0o700, name)
        config = self.home / "home" / ".gitconfig"
        self.assertEqual(config.read_text(), "[user]\n\tname = Dev\n\temail = dev@example.invalid\n")
        config.write_text("changed by confined work\n")
        with patch.object(sessions, "_git_identity", return_value={"name": "Other"}):
            sessions.persistent_home()
        self.assertEqual(config.read_text(), "changed by confined work\n")

    def test_links_loose_modes_and_unprotected_parents_are_refused(self):
        sessions.persistent_home()
        (self.home / "cache").rmdir()
        (self.home / "cache").symlink_to(Path.home())
        self.unsafe()
        (self.home / "cache").unlink()
        sessions.persistent_home()
        self.home.chmod(0o755)
        self.unsafe()
        self.home.chmod(0o700)
        if sys.platform == "darwin":
            subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(self.home / "data")], check=True)
            self.unsafe()
            subprocess.run(["/bin/chmod", "-N", str(self.home / "data")], check=True)
        sessions.persistent_home()
        with tempfile.TemporaryDirectory() as owned, patch.object(sessions, "SHARED", Path(owned)):
            self.unsafe()
        with patch.object(sessions, "SHARED", STICKY_ROOT / "missing-om-root"):
            self.unsafe()


class GitHubClientTests(unittest.TestCase):
    """The om-code client a session may run is one file that only root or this user can replace."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-client-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.client = self.root / "bin/om-code"
        self.client.parent.mkdir(mode=0o755)
        self.client.write_text("#!/bin/sh\n")
        self.client.chmod(0o755)

    def check(self, client):
        request = sessions.parse(json.dumps({"protocol": 1, "user": "dev", "lifetime": "l", "policy": "full_access", "kind": "shell",
            "github": {"client": str(client), "environment": {"OM_CODE_BROKER": "https://127.0.0.1:1", "OM_CODE_CAPABILITY": "c" * 43,
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://127.0.0.1:1.postBuffer", "GIT_CONFIG_VALUE_0": "1"}}}).encode())
        return sessions._client(request)

    def refused(self, client):
        with self.assertRaises(Fault) as caught:
            self.check(client)
        self.assertEqual((caught.exception.code, caught.exception.effect), ("github_client_invalid", "known_no_effect"))

    def test_an_owned_executable_at_its_physical_path_is_accepted(self):
        self.assertEqual(self.check(self.client), self.client)

    def test_writable_linked_missing_or_shared_clients_are_refused(self):
        link = self.root / "link"
        link.symlink_to(self.client)
        self.refused(link)
        self.refused(self.root / "missing")
        self.client.chmod(0o775)
        self.refused(self.client)
        self.client.chmod(0o644)
        self.refused(self.client)
        self.client.chmod(0o755)
        self.client.parent.chmod(0o777)
        self.refused(self.client)
        self.client.parent.chmod(0o755)
        with patch.object(sessions, "SHARED", self.root), patch.object(sessions, "HOME_NAME", "bin"):
            self.refused(self.client)  # inside the persistent home, which restricted work may write

    @unittest.skipUnless(sys.platform == "darwin", "macOS keeps restricted runtime trees and ACLs")
    def test_clients_where_restricted_work_writes_are_refused_by_physical_identity(self):
        runtime = self.root / "omc/abc/om-code"
        runtime.parent.mkdir(parents=True, mode=0o755)
        shutil.copy(self.client, runtime)
        with patch.object(macos, "user_temp", return_value=self.root):
            self.refused(runtime)  # inside a restricted runtime tree
            self.assertEqual(self.check(self.client), self.client)
        self.addCleanup(subprocess.run, ["/bin/chmod", "-N", str(self.client)], check=False)
        subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(self.client)], check=True)
        self.refused(self.client)
        subprocess.run(["/bin/chmod", "-N", str(self.client)], check=True)
        if (self.root / "BIN").exists():  # a case-insensitive volume, as macOS uses by default
            with patch.object(sessions, "SHARED", self.root), patch.object(sessions, "HOME_NAME", "BIN"):
                self.refused(self.client)
        # A restricted profile runs only Core's copy, never the host's file.
        with patch.object(macos, "user_temp", return_value=self.root / "T"):
            text = sessions._restricted_profile(None, self.root / "state", self.root / "T/p", self.root / "home")
        self.assertIn(f'(allow file-read* process-exec (literal "{self.root / "T/omc/client/om-code"}"))', text)
        self.assertNotIn(str(self.client), text)

    @unittest.skipUnless(sys.platform == "darwin", "Core keeps its copy in the macOS user temporary directory")
    def test_core_keeps_one_copy_and_replaces_it_in_place_when_the_client_changes(self):
        with patch.object(macos, "user_temp", return_value=self.root / "T"):
            stable = sessions._install(self.client)
            copied = stable.stat()
            self.assertEqual((stable, stable.read_text(), stat.S_IMODE(copied.st_mode)),
                             (self.root / "T/omc/client/om-code", "#!/bin/sh\n", 0o755))
            self.assertEqual(sessions._install(self.client).stat().st_ino, copied.st_ino)  # unchanged: kept
            update = self.root / "next/om-code"  # as an update installs a new file elsewhere
            update.parent.mkdir(mode=0o755)
            update.write_text("#!/bin/sh\necho next\n")
            self.assertEqual(sessions._install(update), stable)
            self.assertEqual((stable.read_text(), [p.name for p in stable.parent.iterdir()]), ("#!/bin/sh\necho next\n", ["om-code"]))
            update.unlink()  # removed meanwhile, as by a later update
            with self.assertRaises(Fault) as caught:
                sessions._install(update)
            self.assertEqual((caught.exception.code, caught.exception.effect), ("github_client_invalid", "known_no_effect"))


@unittest.skipUnless(macos.lifetime_unavailable_reason() is None, "requires the macOS native process interfaces")
class SpawnAcknowledgmentTests(unittest.TestCase):
    """The spawner acknowledges a launch with the command's real birth, or proves that nothing ran."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-spawn-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def spawn(self, argv, birth):
        launcher, spawner = socket.socketpair()
        self.addCleanup(launcher.close)
        self.addCleanup(spawner.close)
        body = json.dumps({"program": argv[0], "argv": argv, "env": {}, "cwd": str(self.root), "tty": False}).encode()
        null = os.open(os.devnull, os.O_RDWR)
        socket.send_fds(launcher, [struct.pack("!I", len(body))], [null, null, null])
        os.close(null)
        launcher.sendall(body)
        with patch.object(macos, "birth", birth):
            try:
                pid = sessions._spawn(spawner)
            except OSError:
                pid = None
        frame = json.loads(launcher.makefile("rb").readline())
        if pid is not None:
            os.waitpid(pid, 0)
        return pid, frame

    def test_a_command_that_exits_at_once_still_reports_its_real_birth(self):
        real, seen = macos.birth, []

        def slow(pid):
            time.sleep(0.3)  # Long enough for an ungated child to exec and exit.
            seen.append(real(pid))
            return seen[-1]

        pid, frame = self.spawn(["/usr/bin/true"], slow)
        self.assertIsNotNone(seen[0])
        self.assertEqual(sessions.started(frame), macos.Birth(pid, seen[0].start))

    def test_an_unreadable_birth_withholds_the_launch(self):
        ran = self.root / "ran"
        pid, frame = self.spawn(["/bin/sh", "-c", f"echo x > {ran}"], lambda pid: None)
        self.assertIsNone(pid)
        with self.assertRaises(Fault) as caught:
            sessions.started(frame)
        self.assertEqual((caught.exception.code, caught.exception.effect), ("launch_failed", "known_no_effect"))
        time.sleep(0.3)
        self.assertFalse(ran.exists())
