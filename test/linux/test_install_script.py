"""scripts/install.sh under sh and dash against a local download storage."""

import functools
import hashlib
import http.server
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/install.sh").read_text()
SHELLS = [shell for shell in ("sh", "dash") if shutil.which(shell)]
VERSION = "0.0.16"
TOOLS = ("tar", "gzip", "curl", "mkdir", "rm", "mv", "cat")


def archive(marker: str) -> bytes:
    """A release archive whose om only records that it ran, with its arguments and the key it was given."""
    om = ('#!/bin/sh\necho "' + marker + ' $*" > "$HOME/ran"\necho "${OM_KEY:-}" > "$HOME/key"\n').encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as target:
        member = tarfile.TarInfo(f"openmachine-{VERSION}-linux-x86_64/connector/om")
        member.size, member.mode = len(om), 0o755
        target.addfile(member, io.BytesIO(om))
    return buffer.getvalue()


class InstallScript(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.storage = self.root / "storage/linux" / VERSION
        self.storage.mkdir(parents=True)
        self.released = archive("released")
        self.publish(self.released)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.root / "storage"))
        handler.log_message = lambda *args: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.downloads = f"http://127.0.0.1:{server.server_address[1]}/linux"
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        # The machine the script sees: a Linux x86_64 with glibc 2.17 and the user "tester".
        self.tool("uname", 'case $1 in -s) echo Linux ;; -m) echo x86_64 ;; esac')
        self.tool("getconf", 'echo "glibc 2.17"')
        self.tool("id", "echo tester")
        self.tool("sha256sum", f'exec "{shutil.which("python3")}" -c \'import hashlib, sys; '
                               'print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])\' "$1"')

    def publish(self, content: bytes) -> None:
        (self.storage / f"openmachine-{VERSION}-linux-x86_64.tar.gz").write_bytes(content)

    def tool(self, name: str, body: str) -> None:
        (self.bin / name).write_text("#!/bin/sh\n" + body + "\n")
        (self.bin / name).chmod(0o755)

    def script(self, aarch64: str = "b" * 64) -> str:
        """The script as Cloud serves it for the released archive."""
        served = SCRIPT
        for name, value in {"VERSION": VERSION, "CHANNEL": "staging", "DOWNLOADS": self.downloads,
                            "SERVER": "https://mcp.example", "SHA256_X86_64": hashlib.sha256(self.released).hexdigest(),
                            "SHA256_AARCH64": aarch64}.items():
            served = served.replace(f"@{name}@", value)
        self.assertNotIn("@", served.replace('"$@"', ""))
        return served

    def run_script(self, shell: str, *arguments: str, script: str | None = None, env: dict | None = None):
        return subprocess.run([shutil.which(shell), "-s", "--", *arguments], input=script or self.script(), text=True,
                              capture_output=True, timeout=60, env={"HOME": str(self.home), "PATH": str(self.bin), **(env or {})})

    def ran(self) -> str | None:
        return (self.home / "ran").read_text().strip() if (self.home / "ran").exists() else None

    def refused(self, line: str, *arguments: str, **options) -> None:
        """Every shell refuses in exactly this one line, and nothing is unpacked or run."""
        for shell in SHELLS:
            with self.subTest(shell=shell, line=line):
                result = self.run_script(shell, *arguments, **options)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", "install.sh: " + line + "\n"))
                self.assertIsNone(self.ran())
                self.assertEqual(list((self.home / ".local/state/openmachine-om/releases").glob("*")), [])

    def test_it_verifies_the_archive_then_runs_its_installer_with_the_key_in_the_environment(self):
        installer = f"released install --server https://mcp.example --channel staging --downloads {self.downloads}"
        state = self.home / ".local/state/openmachine-om"
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = self.run_script(shell, "--key", "omkey_secret", "--name", "gpu 01", "--no-up")
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(self.ran(), installer + " --name gpu 01 --no-up")
                self.assertEqual((self.home / "key").read_text(), "omkey_secret\n")
                self.assertEqual(state.stat().st_mode & 0o777, 0o700)
                # Run again while that release is unpacked, as after an interruption, it downloads nothing and runs its installer.
                self.publish(archive("replaced"))
                result = self.run_script(shell, "--key-file", "/a key", env={"OM_KEY": "omkey_environment", "OM_SERVER": "https://staging.example"})
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(self.ran(), installer.replace("https://mcp.example", "https://staging.example") + " --key-file /a key")
                self.assertEqual((self.home / "key").read_text(), "omkey_environment\n")
                self.assertEqual(sorted(item.name for item in (state / "releases").iterdir()), [VERSION])
                self.publish(self.released)
                shutil.rmtree(state)
                (self.home / "ran").unlink()

    def test_a_truncated_download_runs_nothing(self):
        script = self.script()
        last = script.rindex("\n", 0, -1) + 1
        self.assertEqual(script[last:], '{ _om_install "$@"; }\n')
        cuts = [index + 1 for index, char in enumerate(script[:last]) if char == "\n"] + list(range(last, len(script) - 2))
        for shell in SHELLS:
            for cut in cuts:
                result = self.run_script(shell, script=script[:cut] or "\n")
                self.assertEqual((result.stdout, self.ran(), (self.home / ".local").exists()), ("", None, False),
                                 f"{shell} ran a script cut at byte {cut}: {result.stderr}")

    def test_a_replaced_archive_is_never_unpacked_or_run(self):
        # The storage serves another archive, as with a replaced manifest: the script holds the hash Cloud verified.
        self.publish(archive("replaced"))
        self.refused(f"the downloaded archive is not Open Machine {VERSION}; nothing was installed.")
        (self.storage / f"openmachine-{VERSION}-linux-x86_64.tar.gz").unlink()
        self.refused(f"{self.downloads}/{VERSION}/openmachine-{VERSION}-linux-x86_64.tar.gz could not be downloaded.")

    def test_it_names_what_an_unsupported_system_lacks(self):
        machine = 'case $1 in -s) echo %s ;; -m) echo %s ;; esac'
        cases = [
            ("uname", machine % ("Darwin", "x86_64"), "Open Machine installs on Linux; this system is Darwin."),
            ("uname", machine % ("Linux", "riscv64"), "Open Machine needs x86_64 or aarch64; this machine is riscv64."),
            ("uname", machine % ("Linux", "aarch64"), f"Open Machine {VERSION} has no build for aarch64."),
            ("getconf", "exit 1", "Open Machine needs glibc 2.17 or later; this system does not use glibc."),
            ("getconf", 'echo "glibc 2.12"', "Open Machine needs glibc 2.17 or later; this system has 2.12."),
            ("id", "echo user@domain", "the OS user name 'user@domain' has characters other than letters, digits, '.', '_' and '-'."),
            ("id", "echo " + "u" * 33, "the OS user name '" + "u" * 33 + "' is longer than 32 characters."),
        ]
        for name, body, line in cases:
            original = (self.bin / name).read_text()
            self.tool(name, body)
            self.refused(line, script=self.script(aarch64=""))
            (self.bin / name).write_text(original)
        for tool in ("tar", "gzip", "sha256sum", "curl"):
            saved = self.bin / (tool + ".saved")
            (self.bin / tool).rename(saved)
            self.refused("curl or wget is missing. Install one and run this again." if tool == "curl"
                         else tool + " is missing. Install it and run this again.")
            saved.rename(self.bin / tool)
        self.refused("HOME is not tester's own directory.", env={"HOME": str(self.root / "missing")})
        if os.geteuid():
            self.refused("HOME is not tester's own directory.", env={"HOME": "/"})

    def test_it_refuses_options_it_does_not_have(self):
        self.refused("unknown option. Options: --key KEY, --key-file FILE, --name NAME, --no-up.", "--key=omkey_secret")
        self.refused("--key needs a key.", "--key")
        self.refused("--name needs a value.", "--name")

    def test_a_newer_glibc_and_wget_are_enough(self):
        self.tool("getconf", 'echo "glibc 2.39"')
        (self.bin / "curl").unlink()
        self.tool("wget", f'[ "$1" = -q ] && [ "$2" = -O ] && exec "{shutil.which("curl")}" -fsSL -o "$3" "$4"')
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.assertEqual(self.run_script(shell).returncode, 0)
                self.assertTrue(self.ran().startswith("released install "))


if __name__ == "__main__":
    unittest.main()
