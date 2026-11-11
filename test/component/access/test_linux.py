"""A Linux session's system environment, from fixture system files and runtime directories."""

import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from openmachine.access.linux import environment


class LinuxEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-linux-env-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.system = self.root / "environment"
        self.locales = (self.root / "default-locale", self.root / "locale.conf")
        (self.root / "run").mkdir()
        for name, value in (("SYSTEM", self.system), ("LOCALES", self.locales), ("RUNTIME", self.root / "run")):
            self.enterContext(patch.object(environment, name, value))

    def test_system_files_give_plain_variables_and_path(self):
        self.system.write_text('# comment\nPATH="/usr/local/bin:/usr/bin"\nexport EDITOR=vim\n'
                               "QUOTED='a b'\nnot a variable\n=x\n1BAD=x\n  SPACED = x\nEMPTY=\nMIXED=\"x'\n")
        self.locales[1].write_text("LANG=en_GB.UTF-8\nLC_TIME=C\n")
        self.assertEqual(environment.system(os.getuid()), {
            "PATH": "/usr/local/bin:/usr/bin", "EDITOR": "vim", "QUOTED": "a b", "EMPTY": "", "MIXED": "\"x'",
            "LANG": "en_GB.UTF-8", "LC_TIME": "C"})
        # /etc/default/locale wins over /etc/locale.conf when both exist.
        self.locales[0].write_text('LANG="C.UTF-8"\n')
        self.assertEqual(environment.system(os.getuid())["LANG"], "C.UTF-8")

    def test_missing_or_oversized_files_leave_the_default_path(self):
        self.assertEqual(environment.system(os.getuid()), {"PATH": environment.DEFAULT_PATH})
        self.system.write_text("LANG=C\n")
        self.assertEqual(environment.system(os.getuid()), {"LANG": "C", "PATH": environment.DEFAULT_PATH})
        self.system.write_text("PATH=/x\n" + "#" * environment.LIMIT)
        self.assertEqual(environment.system(os.getuid()), {"PATH": environment.DEFAULT_PATH})

    def test_the_users_runtime_directory_and_bus_when_they_exist(self):
        runtime = self.root / "run" / str(os.getuid())
        self.assertNotIn("XDG_RUNTIME_DIR", environment.system(os.getuid()))
        runtime.mkdir(mode=0o700)
        self.assertEqual(environment.system(os.getuid())["XDG_RUNTIME_DIR"], str(runtime))
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", environment.system(os.getuid()))
        (runtime / "bus").write_text("")  # Not a socket.
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", environment.system(os.getuid()))
        (runtime / "bus").unlink()
        with socket.socket(socket.AF_UNIX) as bus:
            bus.bind(str(runtime / "bus"))
            self.assertEqual(environment.system(os.getuid())["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={runtime}/bus")
        # Another user's directory is not this user's runtime directory.
        (self.root / "run" / "4242").mkdir()
        self.assertNotIn("XDG_RUNTIME_DIR", environment.system(4242))


if __name__ == "__main__":
    unittest.main()
