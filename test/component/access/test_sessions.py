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

from openmachine.access import restricted, rules, sessions
from openmachine.access.linux import sandbox
from openmachine.access.macos import seatbelt as macos
from openmachine.access.macos import sessions as backend
from openmachine.foundation.sanitize import Fault

STICKY_ROOT = Path("/tmp").resolve()  # Root-owned and sticky on macOS and Linux.


class PersistentHomeTests(unittest.TestCase):
    def setUp(self):
        name = f"om-access-component-{os.getpid()}-{{uid}}"  # Owned by this process; never another run's home.
        self.home = STICKY_ROOT / name.format(uid=os.getuid())
        self.homes = sessions.keyed_homes(self.home)
        for path in (self.home, self.homes):
            self.addCleanup(shutil.rmtree, path, True)
        for target, value in (("SHARED", STICKY_ROOT), ("HOME_NAME", name)):
            patcher = patch.object(sessions, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def unsafe(self, key=None):
        with self.assertRaises(Fault) as caught:
            backend.persistent_home(key)
        self.assertEqual(caught.exception.code, "persistent_home_unsafe")

    def test_the_home_is_created_owner_only_with_only_the_git_identity(self):
        with patch.object(restricted, "_git_identity", return_value={"name": "Dev", "email": "dev@example.invalid"}):
            self.assertEqual(backend.persistent_home(None), (self.home, [self.homes]))
        for name in ("", "home", "cache", "config", "data", "state"):
            self.assertEqual((self.home / name).stat().st_mode & 0o777, 0o700, name)
        config = self.home / "home" / ".gitconfig"
        self.assertEqual(config.read_text(), "[user]\n\tname = Dev\n\temail = dev@example.invalid\n")
        config.write_text("changed by confined work\n")
        with patch.object(restricted, "_git_identity", return_value={"name": "Other"}):
            backend.persistent_home(None)
        self.assertEqual(config.read_text(), "changed by confined work\n")

    def test_a_home_of_a_key_is_made_beside_the_default_one_like_it(self):
        key = "0123456789abcdef" * 2
        with patch.object(restricted, "_git_identity", return_value={"name": "Dev"}):
            self.assertEqual(backend.persistent_home(key), (self.homes / key, [self.home, self.homes]))
            # The default home exists by now, to be hidden, and is still made whole on its own first use.
            self.assertEqual(backend.persistent_home(None), (self.home, [self.homes]))
        for path in (self.homes, self.homes / key / "home", self.home):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)
        self.assertEqual((self.home / "home/.gitconfig").read_text(), "[user]\n\tname = Dev\n")
        # What the default home's sessions made of it refuses only them.
        (self.home / "cache").chmod(0o755)
        self.home.chmod(0o755)
        self.unsafe()
        self.assertEqual(backend.persistent_home(key)[0], self.homes / key)
        for path in (self.home, self.home / "cache"):
            path.chmod(0o700)
        # Every sandbox denies the directory of the keyed homes, so no session starts while it is unsafe.
        (self.homes / key / "state").chmod(0o750)
        self.unsafe(key)
        self.assertEqual(backend.persistent_home(None), (self.home, [self.homes]))
        self.homes.rename(self.homes.with_suffix(".aside"))
        self.addCleanup(shutil.rmtree, self.homes.with_suffix(".aside"), True)
        self.homes.symlink_to(self.homes.with_suffix(".aside"))
        self.addCleanup(self.homes.unlink)
        self.unsafe(key)
        self.unsafe()

    def test_links_loose_modes_and_unprotected_parents_are_refused(self):
        backend.persistent_home(None)
        (self.home / "cache").rmdir()
        (self.home / "cache").symlink_to(Path.home())
        self.unsafe()
        (self.home / "cache").unlink()
        backend.persistent_home(None)
        self.home.chmod(0o755)
        self.unsafe()
        self.home.chmod(0o700)
        if sys.platform == "darwin":
            subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(self.home / "data")], check=True)
            self.unsafe()
            subprocess.run(["/bin/chmod", "-N", str(self.home / "data")], check=True)
        backend.persistent_home(None)
        with tempfile.TemporaryDirectory() as owned, patch.object(sessions, "SHARED", Path(owned)):
            if os.getuid() == 0:
                os.chown(owned, 65534, -1)  # Any owner but root is refused, as this user is when not root.
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
        request = sessions.parse(json.dumps({"protocol": 4, "user": "dev", "lifetime": "l", "policy": "full_access", "kind": "shell",
            "connection": "127.0.0.1 1 127.0.0.1 22",
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
        if sys.platform == "darwin":
            with patch.object(sessions, "SHARED", self.root), patch.object(sessions, "HOME_NAME", "bin"):
                self.refused(self.client)  # inside the persistent home, which restricted work may write
                keyed = sessions.keyed_homes(self.client.parent) / "key/om-code"
                keyed.parent.mkdir(parents=True, mode=0o755)
                shutil.copy(self.client, keyed)
                self.refused(keyed)  # and inside the home of a key

    @unittest.skipUnless(sys.platform == "linux", "Linux keeps the sandbox home and runtime tree under the account home")
    def test_on_linux_clients_where_restricted_work_writes_are_refused(self):
        with patch.object(rules, "account_home", return_value=self.root):
            for directory in (sandbox.home(self.root) / "home/bin", sandbox.runtime(self.root) / "abc",
                              sessions.keyed_homes(sandbox.home(self.root)) / ("a" * 32) / "home"):
                directory.mkdir(parents=True, mode=0o755)
                shutil.copy(self.client, directory / "om-code")
                self.refused(directory / "om-code")
            self.assertEqual(self.check(self.client), self.client)

    @unittest.skipUnless(sys.platform == "linux", "Linux keeps the sandbox home under the account home")
    def test_on_linux_a_home_that_cannot_be_created_refuses_before_anything_starts(self):
        from openmachine.access.linux import sessions as linux
        with patch.object(rules, "account_home", return_value=self.client), self.assertRaises(Fault) as caught:
            linux.persistent_home(None)  # The account home is a file here.
        self.assertEqual((caught.exception.code, caught.exception.effect), ("persistent_home_unsafe", "known_no_effect"))

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
            text = backend._restricted_profile(None, self.root / "state", self.root / "T/p", self.root / "home", [])
        self.assertIn(f'(allow file-read* process-exec (literal "{self.root / "T/omc/client/om-code"}"))', text)
        self.assertNotIn(str(self.client), text)

    @unittest.skipUnless(sys.platform == "darwin", "Core keeps its copy in the macOS user temporary directory")
    def test_core_keeps_one_copy_and_replaces_it_in_place_when_the_client_changes(self):
        with patch.object(macos, "user_temp", return_value=self.root / "T"):
            stable = restricted._install(self.client)
            copied = stable.stat()
            self.assertEqual((stable, stable.read_text(), stat.S_IMODE(copied.st_mode)),
                             (self.root / "T/omc/client/om-code", "#!/bin/sh\n", 0o755))
            self.assertEqual(restricted._install(self.client).stat().st_ino, copied.st_ino)  # unchanged: kept
            update = self.root / "next/om-code"  # as an update installs a new file elsewhere
            update.parent.mkdir(mode=0o755)
            update.write_text("#!/bin/sh\necho next\n")
            self.assertEqual(restricted._install(update), stable)
            self.assertEqual((stable.read_text(), [p.name for p in stable.parent.iterdir()]), ("#!/bin/sh\necho next\n", ["om-code"]))
            update.unlink()  # removed meanwhile, as by a later update
            with self.assertRaises(Fault) as caught:
                restricted._install(update)
            self.assertEqual((caught.exception.code, caught.exception.effect), ("github_client_invalid", "known_no_effect"))


@unittest.skipUnless(macos.lifetime_unavailable_reason() is None, "requires the macOS native process interfaces")
class SpawnAcknowledgmentTests(unittest.TestCase):
    """The spawner acknowledges a launch with the command's real birth, or proves that nothing ran."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-spawn-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def spawn(self, argv, birth=restricted.birth, answer=None):
        """Run one launch as the spawner. With an answer, the command vouches for itself: it sends one byte
        and executes only once it reads the launcher's, here sent ahead; an empty answer hangs up instead."""
        launcher, spawner = socket.socketpair()
        self.addCleanup(launcher.close)
        self.addCleanup(spawner.close)
        body = json.dumps({"program": argv[0], "argv": argv, "env": {}, "cwd": str(self.root), "tty": False}).encode()
        null = os.open(os.devnull, os.O_RDWR)
        socket.send_fds(launcher, [struct.pack("!I", len(body))], [null, null, null])
        os.close(null)
        launcher.sendall(body)
        if answer is not None:
            launcher.sendall(answer) if answer else launcher.shutdown(socket.SHUT_WR)
        with patch.object(restricted, "birth", birth):
            try:
                pid = restricted._spawn(spawner, answer is not None)
            except OSError:
                pid = None
        replies = launcher.makefile("rb")
        if answer is not None:
            self.assertEqual(replies.read(1), b"\0")  # The command's own byte comes first.
        frame = json.loads(replies.readline())
        if pid is not None:
            os.waitpid(pid, 0)
        return pid, frame

    def test_a_command_that_exits_at_once_still_reports_its_real_birth(self):
        real, seen = restricted.birth, []

        def slow(pid):
            time.sleep(0.3)  # Long enough for an ungated child to exec and exit.
            seen.append(real(pid))
            return seen[-1]

        pid, frame = self.spawn(["/usr/bin/true"], slow)
        self.assertIsNotNone(seen[0])
        self.assertEqual(restricted.started(frame), restricted.Birth(pid, seen[0].start))

    def test_an_unreadable_birth_withholds_the_launch(self):
        ran = self.root / "ran"
        pid, frame = self.spawn(["/bin/sh", "-c", f"echo x > {ran}"], lambda pid: None)
        self.assertIsNone(pid)
        with self.assertRaises(Fault) as caught:
            restricted.started(frame)
        self.assertEqual((caught.exception.code, caught.exception.effect), ("launch_failed", "known_no_effect"))
        time.sleep(0.3)
        self.assertFalse(ran.exists())

    def test_a_command_that_vouches_for_itself_runs_only_once_its_launcher_answers(self):
        ran = self.root / "ran"
        pid, frame = self.spawn(["/bin/sh", "-c", f"echo x > {ran}"], answer=b"\0")
        self.assertEqual(restricted.started(frame).pid, pid)
        self.assertTrue(ran.exists())
        ran.unlink()
        for answer in (b"", b"x"):  # The launcher hangs up, or answers anything else.
            with self.subTest(answer=answer):
                pid, frame = self.spawn(["/bin/sh", "-c", f"echo x > {ran}"], answer=answer)
                self.assertIsNone(pid)
                with self.assertRaises(Fault) as caught:
                    restricted.started(frame)
                self.assertEqual((caught.exception.code, caught.exception.effect), ("launch_failed", "known_no_effect"))
                self.assertFalse(ran.exists())
