"""Payload integrity and installer ownership; subprocess mocks are not native acceptance."""

from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory
from openmachine.service import checks, install, payload


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="om-payload-", dir="/tmp")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.cleanup)
        self.source = self.root / "app-payload"
        self.source.mkdir()
        for name in ("python/bin/python3.13", "git/bin/git", "bin/git"):
            path = self.source / name
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        (self.source / "python/bin/python3").symlink_to("python3.13")
        (self.source / "wheels").mkdir()
        self.wheel = self.source / "wheels/openmachine-0.2.8-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr("openmachine-0.2.8.dist-info/METADATA", "Name: openmachine\nVersion: 0.2.8\n")
        (self.source / "requirements.lock").write_text("example==1.0\n")
        (self.source / "THIRD_PARTY_NOTICES.md").write_text("Fixture notices.\n")
        self.manifest = {"format": 1, "target": dict(payload.TARGET),
            "core": {"version": "0.2.8", "commit": "a" * 40, "wheel": self.wheel.relative_to(self.source).as_posix()},
            "python": {"version": "3.13.16", "build": "20261003", "url": "https://example.test/python", "sha256": "b" * 64},
            "git": {"version": "2.53.0", "build": "fixture", "url": "https://example.test/git", "sha256": "c" * 64}}
        self.digest = self.seal()
        self.state = private_directory(self.root / "state")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        stack = self.enterContext(ExitStack())
        stack.enter_context(patch.object(payload.sys, "platform", "darwin"))
        stack.enter_context(patch.object(payload.platform, "machine", return_value="arm64"))
        stack.enter_context(patch.object(payload.platform, "mac_ver", return_value=("13.0", (), "")))

    def cleanup(self):
        # Only this disposable fixture: retained product directories are read-only.
        for directory, directories, _ in os.walk(self.root, followlinks=False):
            Path(directory).chmod(0o700)
            for name in directories:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        self.temporary.cleanup()

    def seal(self):
        self.manifest["files"] = payload.inventory(self.source)
        raw = (json.dumps(self.manifest, sort_keys=True) + "\n").encode()
        (self.source / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def fault(self, code, function):
        with self.assertRaises(Fault) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_manifest_binds_all_bytes_paths_modes_and_links(self):
        self.assertEqual(payload.verify(self.source, self.digest), self.manifest)
        file = self.source / "requirements.lock"
        original = file.read_bytes()
        file.write_bytes(original + b"changed")
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        file.write_bytes(original)
        extra = self.source / "unexpected.py"
        extra.touch()
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        extra.unlink()
        file.chmod(0o666)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        file.chmod(0o644)
        file.unlink()
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))

    def test_manifest_and_target_rejection_before_copy_or_execution(self):
        path = self.source / "manifest.json"
        original = path.read_bytes()
        for raw in (b"null", b"[]", b'{"format":1,"format":1}', b"\xff", b"{}"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, digest))
        path.write_bytes(original)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, "0" * 64))
        with patch.object(payload, "MANIFEST_LIMIT", 8):
            self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        for target in ({"arch": "x86_64"}, {"minimum_os": "14.0"}):
            self.manifest["target"] = payload.TARGET | target
            digest = self.seal()
            self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, digest))
        self.manifest["target"] = dict(payload.TARGET)
        digest = self.seal()
        with patch.object(payload.platform, "machine", return_value="x86_64"):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))
        with patch.object(payload.platform, "mac_ver", return_value=("12.7", (), "")):
            self.fault("unsupported_payload_host", lambda: payload.verify(self.source, digest))
        with patch.object(checks, "_run") as run:
            self.fault("invalid_runtime_payload", lambda: install.prepare_payload(self.config, self.state, self.source, "0" * 64))
            run.assert_not_called()
        self.assertFalse((self.state / "service").exists())

    def test_escaping_links_special_files_and_limits_are_rejected(self):
        link = self.source / "python/bin/python3"
        link.unlink()
        link.symlink_to(self.config)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        link.unlink()
        link.symlink_to("python3.13")
        fifo = self.source / "pipe"
        os.mkfifo(fifo)
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        fifo.unlink()
        for name in ("FILE_LIMIT", "BYTE_LIMIT"):
            with self.subTest(limit=name), patch.object(payload, name, 1):
                self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.digest))
        self.manifest["core"]["wheel"] = "../outside.whl"
        self.fault("invalid_runtime_payload", lambda: payload.verify(self.source, self.seal()))

    def test_retained_payload_survives_source_replacement_and_is_not_overwritten(self):
        retained, manifest = payload.retain(self.state, self.source, self.digest)
        inode = (retained / "python/bin/python3.13").stat().st_ino
        again, _ = payload.retain(self.state, self.source, self.digest)
        self.assertEqual(again, retained)
        self.assertEqual((retained / "python/bin/python3.13").stat().st_ino, inode)
        self.assertEqual(retained.stat().st_mode & 0o777, 0o500)
        self.source.rename(self.root / "replaced-app")
        self.assertEqual(payload.verify(retained, self.digest), manifest)
        self.assertEqual(os.readlink(retained / "python/bin/python3"), "python3.13")

    def test_reuse_completes_interrupted_read_only_permissions(self):
        with patch.object(Path, "chmod", side_effect=OSError("fixture hardening interruption")), self.assertRaises(OSError):
            payload.retain(self.state, self.source, self.digest)
        retained = self.state / "service/payloads" / self.digest
        self.assertEqual(payload.verify(retained, self.digest), self.manifest)
        inode = (retained / "python/bin/python3.13").stat().st_ino
        self.assertTrue(retained.stat().st_mode & 0o200)
        with patch.object(payload.shutil, "copytree") as copy:
            again, _ = payload.retain(self.state, self.source, self.digest)
            copy.assert_not_called()
        self.assertEqual(again, retained)
        self.assertEqual((retained / "python/bin/python3.13").stat().st_ino, inode)
        for path in [retained, *retained.rglob("*")]:
            if not path.is_symlink():
                self.assertEqual(path.stat().st_mode & 0o222, 0, path)
        self.assertEqual(payload.verify(retained, self.digest), self.manifest)

    def test_interrupted_copy_and_disk_exhaustion_never_replace_prior_content(self):
        parent = private_directory(self.state / "service/payloads")
        with patch.object(payload.shutil, "disk_usage", return_value=Mock(free=0)):
            self.fault("insufficient_disk_space", lambda: payload.retain(self.state, self.source, self.digest))
        self.assertFalse((parent / self.digest).exists())
        def partial(source, destination, **kwargs):
            destination.mkdir()
            (destination / "partial").write_text("retain this failed copy")
            raise OSError("fixture interruption")
        with patch.object(payload.shutil, "copytree", side_effect=partial), self.assertRaises(OSError):
            payload.retain(self.state, self.source, self.digest)
        with patch.object(payload.shutil, "copytree") as copy:
            self.fault("invalid_runtime_payload", lambda: payload.retain(self.state, self.source, self.digest))
            copy.assert_not_called()
        self.assertEqual((parent / self.digest / "partial").read_text(), "retain this failed copy")

    def test_offline_preparation_reuses_installer_and_never_selects_service(self):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            output = "3.13.16\n" if "-c" in argv else "git version 2.53.0\n" if "--version" in argv else ""
            return subprocess.CompletedProcess(argv, 0, output, "")
        def state(python, *_args, **kwargs):
            self.assertIn(str(self.state / "service/payloads"), kwargs["env"]["PATH"])
            return {"version": "0.2.8", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 13}
        with patch.object(checks, "_run", side_effect=run), patch.object(checks, "_candidate_state", side_effect=state):
            result = install.prepare_payload(self.config, self.state, self.source, self.digest)
            again = install.prepare_payload(self.config, self.state, self.source, self.digest)
            with lease(self.state / "service/operation.lock"):
                inherited = install._prepare_payload(self.config, self.state, self.source, self.digest)
        self.assertEqual(result, again)
        self.assertEqual(result, inherited)
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["selection"], "unchanged")
        self.assertFalse((self.state / "service/service.json").exists())
        installers = [(argv, options) for argv, options in calls if "install" in argv]
        self.assertEqual(len(installers), 2)
        for argv, options in installers:
            self.assertIn("--no-index", argv)
            self.assertIn("--only-binary=:all:", argv)
            self.assertIn("--no-deps", argv)
            self.assertEqual(options["env"]["PIP_CONFIG_FILE"], os.devnull)
        venv = [argv for argv, _ in calls if "venv" in argv]
        self.assertEqual(len(venv), 1)
        self.assertTrue(venv[0][0].startswith(str(self.state / "service/payloads")))
        self.assertEqual(venv[0][-1], result["runtime"])
        record = json.loads((Path(result["runtime"]) / "installation.json").read_text())
        self.assertEqual(record["payload_sha256"], self.digest)
        candidate = self.root / "candidate.toml"
        candidate.write_bytes(self.config.read_bytes() + b"# configuration-only update\n")
        with patch.object(checks, "_run", side_effect=run), patch.object(checks, "_candidate_state", side_effect=state):
            changed = install.prepare_payload(candidate, self.state, self.source, self.digest)
            self.assertEqual(install.prepare_payload(candidate, self.state, self.source, self.digest), changed)
        self.assertEqual(changed["payload_sha256"], result["payload_sha256"])
        self.assertNotEqual(changed["identity"], result["identity"])
        self.assertFalse((self.state / "service/service.json").exists())

    def test_failed_installation_is_retained_without_an_online_fallback(self):
        with patch.object(checks, "_run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.fault("installation_failed", lambda: install._prepare(
                self.state, self.wheel, self.source / "requirements.lock", self.config,
                offline=(self.source, self.digest, self.manifest)))
        versions = list((self.state / "service/versions").iterdir())
        self.assertEqual(len(versions), 1)
        self.assertFalse((versions[0] / "installation.json").exists())
        with patch.object(checks, "_run") as run:
            self.fault("service_state_invalid", lambda: install._prepare(
                self.state, self.wheel, self.source / "requirements.lock", self.config,
                offline=(self.source, self.digest, self.manifest)))
            run.assert_not_called()
        self.assertTrue((versions[0] / "artifacts" / self.wheel.name).exists())

    def test_same_python_version_with_different_payload_has_different_identity(self):
        def state(python, *_args, **kwargs):
            return {"version": "0.2.8", "prefix": str(python.parent.parent), "protocol": 1, "state_contract": 13}
        with patch.object(checks, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(checks, "_candidate_state", side_effect=state):
            first = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                     offline=(self.source, self.digest, self.manifest))
            self.manifest["python"]["build"] = "different-build"
            second = install._prepare(self.state, self.wheel, self.source / "requirements.lock", self.config,
                                      offline=(self.source, self.seal(), self.manifest))
        self.assertNotEqual(first, second)

    def test_installer_environment_does_not_inherit_injection_or_package_indexes(self):
        with patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://example.test", "PYTHONPATH": "/untrusted", "DYLD_LIBRARY_PATH": "/untrusted"}):
            env = payload.environment(self.source)
        self.assertFalse({"PIP_EXTRA_INDEX_URL", "PYTHONPATH", "DYLD_LIBRARY_PATH"} & env.keys())
        self.assertEqual(env["PIP_NO_INDEX"], "1")
        self.assertTrue(env["PATH"].startswith(str(self.source / "bin") + os.pathsep))

    def test_git_entry_resolves_helpers_after_relocation_and_preserves_arguments(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/build_runtime_payload.py"))
        (self.source / "git/bin/git").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$GIT_EXEC_PATH" "$GIT_TEMPLATE_DIR" "$@"\n')
        builder["git_entry"](self.source)
        moved = self.root / "relocated payload 'quoted'"
        self.source.rename(moved)
        result = subprocess.run([str(moved / "bin/git"), "a b", "$(not-a-command)"],
            cwd="/", env={"PATH": "/bin:/usr/bin", "GIT_EXEC_PATH": "/foreign"},
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(moved / "git/libexec/git-core"),
            str(moved / "git/share/git-core/templates"), "a b", "$(not-a-command)"])

    def test_producer_rejects_hardlinks_and_archive_path_escape(self):
        builder = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/build_runtime_payload.py"))
        for name, kind in (("hard", tarfile.LNKTYPE), ("../escape", tarfile.REGTYPE)):
            archive = self.root / ("hard.tar" if kind == tarfile.LNKTYPE else "escape.tar")
            with tarfile.open(archive, "w") as output:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = "target" if kind == tarfile.LNKTYPE else ""
                output.addfile(member, io.BytesIO(b""))
            with self.subTest(name=name), self.assertRaises((ValueError, tarfile.FilterError)):
                builder["extract"](archive, self.root / "unpacked")
        self.assertFalse((self.root / "escape").exists())
