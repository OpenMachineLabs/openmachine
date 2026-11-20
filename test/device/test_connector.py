"""Private state, device proofs, the retained bundle, Cloud transport and device removal."""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))

from cryptography.exceptions import InvalidSignature
import httpx2 as httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from openmachine_device.cli import command, parser
from openmachine_device.errors import ConnectorError
from openmachine_device.cloud import Cloud, unpair
from openmachine_device.service import install, remove, verify_bundle, upgrade, upgrade_status, inspect as inspect_service
from openmachine_device.store import Identity, ORIGIN, Store, hardware_id, key_file
from fixture_device import stored_device


def write_bundle(bundle: Path, version: str, platform: str = "darwin") -> Path:
    (bundle / "python/bin").mkdir(parents=True)
    (bundle / "python/bin/python3").write_bytes(("python-" + version).encode())
    (bundle / "entry.py").write_text("version = " + repr(version) + "\n")
    files = {item: hashlib.sha256((bundle / item).read_bytes()).hexdigest() for item in ("python/bin/python3", "entry.py")}
    (bundle / "manifest.json").write_text(json.dumps({
        "format": 1, "product": "openmachine-connector", "version": version,
        "platform": platform, "architecture": os.uname().machine, "files": files, "links": {}}))
    return bundle


class PrivateState(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="cs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state")

    def test_internal_connector_has_no_gui_command_or_implicit_window(self):
        import io
        import runpy
        import shlex
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['gui'])
        builder = runpy.run_path(str(ROOT / 'scripts/build_bundle.py'), run_name='connector_builder_test')
        app = self.root / 'internal.app'
        bundle = app / 'Contents/Resources/bundle'
        python = bundle / 'python/bin/python3'
        python.parent.mkdir(parents=True)
        marker = self.root / 'arguments'
        python.write_text('#!/bin/sh\nprintf "%s\n" "$@" > ' + shlex.quote(str(marker)) + '\n')
        python.chmod(0o755)
        builder['app_shell'](app, '0.2.0')
        executable = app / 'Contents/MacOS/OpenMachineConnector'
        subprocess.run([str(executable)], check=True)
        self.assertFalse(marker.exists())
        subprocess.run([str(executable), 'inspect'], check=True)
        self.assertEqual(marker.read_text().splitlines()[-1], 'inspect')

    def test_ed25519_proof_binds_origin_method_path_nonce_and_exact_body(self):
        identity = Identity(Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
        raw = b'{"principal_id":"principal-A"}'
        headers = identity.proof(ORIGIN, "POST", "/api/native-sessions/sssssssssssssssssssssssssssssssssssssssssss/status", "pair-A", "challenge-value", raw)
        message = "\n".join(("om-device-v1", ORIGIN, "POST", "/api/native-sessions/sssssssssssssssssssssssssssssssssssssssssss/status", "pair-A", "challenge-value", hashlib.sha256(raw).hexdigest())).encode()
        public = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(identity.public_key + "="))
        signature = base64.urlsafe_b64decode(headers["X-OM-Signature"] + "==")
        public.verify(signature, message)
        with self.assertRaises(InvalidSignature):
            public.verify(signature, message.replace(b"/status", b"/sign-out"))
        with self.assertRaises(InvalidSignature):
            public.verify(signature, message + b"\n")
        vector = json.loads((ROOT / "test/vectors/proof.json").read_text())
        timed = identity.proof(vector["origin"], vector["method"], vector["path"], vector["challenge_id"],
                               vector["challenge"], vector["body"].encode(), vector["time"])
        self.assertEqual(timed["X-OM-Signature"], vector["signature_v2"])
        self.assertEqual(timed["X-OM-Time"], str(vector["time"]))

    def test_a_key_file_is_named_by_its_fingerprint_and_unsafe_permissions_are_not_repaired(self):
        identity = Identity.create(self.store)
        self.assertEqual(Identity.load(self.store, identity.fingerprint).public_key, identity.public_key)
        key = self.store.root / key_file(identity.fingerprint)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(ConnectorError):
            self.store.write_bytes(key.name, b"\0" * 32, create=True)
        key.chmod(0o644)
        with self.assertRaises(ConnectorError):
            Identity.load(self.store, identity.fingerprint)
        self.assertEqual(key.stat().st_mode & 0o777, 0o644)

    def test_a_linux_hardware_id_is_the_state_file_else_the_machine_id_and_only_a_sign_in_creates_it(self):
        """om reads by the same rule and never creates the file."""
        machine, machine_id = self.root / "machine-id", "0123456789abcdef0123456789abcdef"

        def read(state: str, **options) -> str:
            return hardware_id(Store(self.root / state), **options)  # a new Store, as a new process: nothing is cached

        with patch("sys.platform", "linux"), patch("openmachine_device.store.MACHINE_ID", machine):
            for state, unreadable in (("missing", None), ("malformed", "uninitialized\n")):
                with self.subTest(machine_id=state):
                    machine.unlink(missing_ok=True)
                    if unreadable:
                        machine.write_text(unreadable)
                    stored = self.root / state / "hardware-id"
                    with self.assertRaises(ConnectorError) as refused:
                        read(state)
                    self.assertEqual(refused.exception.code, "hardware_unavailable")
                    self.assertFalse(stored.exists())
                    created = read(state, create=True)
                    self.assertRegex(created, r"\A[0-9a-f]{32}\Z")
                    machine.write_text(machine_id + "\n")  # a machine ID that appears later never replaces it
                    self.assertEqual((read(state), read(state, create=True)), (created, created))
            self.assertEqual((read("readable"), read("readable", create=True)), (machine_id, machine_id))
            self.assertFalse((self.root / "readable" / "hardware-id").exists())
            stored.write_text("not an ID")  # a malformed file is an error, never replaced
            for options in ({}, {"create": True}):
                with self.assertRaises(ConnectorError) as refused:
                    read(state, **options)
                self.assertEqual(refused.exception.code, "hardware_unavailable")
            self.assertEqual(stored.read_text(), "not an ID")

    def test_symlink_and_duplicate_owner_fail_closed(self):
        self.store.write("config.json", {"version": 1})
        (self.root / "outside").write_text("{}")
        (self.store.root / "linked.json").symlink_to(self.root / "outside")
        with self.assertRaises(ConnectorError):
            self.store.read("linked.json")
        with self.store.lock("operation.lock"):
            with self.assertRaises(ConnectorError):
                with self.store.lock("operation.lock"):
                    self.fail("A second owner acquired the same lock")

    def test_build_manifest_declares_only_connector_protocols(self):
        from runpy import run_path
        inventory = run_path(str(ROOT / "scripts/build_bundle.py"))["inventory"]
        bundle = self.root / "manifest-test"
        bundle.mkdir()
        (bundle / "entry.py").write_text("pass\n")
        manifest = inventory(bundle, "0.1.0", "a" * 64)
        self.assertEqual(set(manifest), {"format", "companion_protocol", "setup_protocol", "connection_protocol",
                                         "product", "version", "platform", "architecture", "python_archive_sha256",
                                         "files", "links"})

    def test_bundle_bytecode_is_reproducible_and_read_by_a_run_that_writes_none(self):
        from runpy import run_path
        compile_bytecode = run_path(str(ROOT / "scripts/build_bundle.py"))["compile_bytecode"]
        built = []
        for name in ("first", "second"):  # two staging directories, as two builds use
            bundle = self.root / name
            for source in ("app/module.py", "python/lib/python3.12/library.py", "python/lib/python3.12/site-packages/pip.py"):
                (bundle / source).parent.mkdir(parents=True, exist_ok=True)
                (bundle / source).write_text("VALUE = 1\n")
            compile_bytecode(Path(sys.executable), bundle)
            built.append({path.relative_to(bundle).parent.parent.as_posix(): path.read_bytes() for path in bundle.rglob("*.pyc")})
        self.assertEqual(built[0], built[1])
        self.assertEqual(set(built[0]), {"app", "python/lib/python3.12"})  # pip is not run
        for bytecode in built[0].values():
            self.assertEqual(int.from_bytes(bytecode[4:8], "little"), 0b11)  # checked against the source's hash
        run = subprocess.run([sys.executable, "-I", "-B", "-v", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import module",
                              str(bundle / "app")], capture_output=True, text=True, check=True)
        self.assertRegex(run.stderr, r"__pycache__/module\.[^ ]+\.pyc matches ")

    def test_bundle_takes_the_repository_version(self):
        from runpy import run_path
        version = run_path(str(ROOT / "scripts/build_bundle.py"))["release_version"]()
        self.assertEqual(version, run_path(str(ROOT / "access/openmachine_access/__init__.py"))["__version__"])

    def test_bundle_inventory_binds_every_file(self):
        bundle = write_bundle(self.root / "bundle", "0.1.0")
        verify_bundle(bundle)
        (bundle / "entry.py").write_text("changed\n")
        with self.assertRaises(ConnectorError):
            verify_bundle(bundle)

    def test_retire_generated_bytecode_without_trusting_it_as_install_input(self):
        bundle = write_bundle(self.root / "bundle", "0.1.0")
        cache = bundle / "__pycache__/entry.cpython-312.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"historical cache")
        with self.assertRaises(ConnectorError):
            verify_bundle(bundle)
        verify_bundle(bundle, retiring=True)
        cache.unlink()
        cache.symlink_to(bundle / "entry.py")
        with self.assertRaises(ConnectorError):
            verify_bundle(bundle, retiring=True)
        cache.unlink()
        (bundle / "unknown.py").write_text("unknown")
        with self.assertRaises(ConnectorError):
            verify_bundle(bundle, retiring=True)


class RetainedBundle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="upgrade-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.store = Store(self.root / "state")
        for entry in (patch("openmachine_device.service.sys", SimpleNamespace(platform="darwin")),
                      patch.object(Path, "home", return_value=self.home)):
            entry.start()
            self.addCleanup(entry.stop)
        key, stored = stored_device(self, self.store, {"device_id": "device-A"})
        self.key_name = key_file(key.fingerprint)
        self.store.write("config.json", {"version": 1, "enabled": False, "server": ORIGIN, **stored})
        self.store.write("native-session.json", {"session_id": "original-A"})
        self.first = write_bundle(self.root / "first", "0.1.0")
        self.second = write_bundle(self.root / "second", "0.2.0")
        self.assertEqual(install(self.store, self.first), {"status": "installed", "version": "0.1.0"})
        self.old = self.store.read("installation.json")
        self.next_digest = verify_bundle(self.second)[1]
        self.operation = "a" * 32

    def perform(self, *, resume=False, operation=None):
        return upgrade(self.store, None if resume else self.second,
                       self.old["bundle_sha256"], self.next_digest, operation or self.operation, resume=resume)

    def test_install_retains_and_selects_the_bundle_without_a_service(self):
        self.assertEqual(set(self.old), {"bundle", "bundle_sha256", "version", "phase"})
        self.assertEqual(self.old["phase"], "installed")
        self.assertTrue(Path(self.old["bundle"]).is_dir())
        self.assertFalse((self.home / "Library/LaunchAgents").exists())
        self.assertEqual(install(self.store, self.first)["status"], "installed")
        self.assertEqual(self.store.read("installation.json"), self.old)
        with self.assertRaises(ConnectorError) as observed:
            install(self.store, self.second)
        self.assertEqual(observed.exception.code, "connector_update_not_supported")
        self.assertEqual(self.store.read("installation.json"), self.old)

    def test_launch_agent_keys_of_an_earlier_build_are_ignored_and_never_acted_on(self):
        plist = self.home / "Library/LaunchAgents/app.openmachine.connector.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"earlier build")
        self.store.write("installation.json", {**self.old, "plist": str(plist), "plist_sha256": "0" * 64})
        with patch("subprocess.run", side_effect=AssertionError("No launchctl")):
            self.assertEqual(inspect_service(self.store)["bundle_sha256"], self.old["bundle_sha256"])
            self.assertEqual(self.perform()["status"], "verified")
        self.assertNotIn("plist", self.store.read("installation.json"))
        self.assertEqual(plist.read_bytes(), b"earlier build")

    def test_upgrade_switches_and_preserves_identity(self):
        files = {name: self.store.read_bytes(name) for name in (self.key_name, "config.json", "native-session.json")}
        result = self.perform()
        self.assertEqual(result, {"status": "verified", "operation_id": self.operation, "version": "0.2.0",
                                  "bundle_sha256": self.next_digest})
        self.assertEqual(inspect_service(self.store)["bundle_sha256"], self.next_digest)
        for name, original in files.items():
            self.assertEqual(self.store.read_bytes(name), original)
        self.assertTrue(Path(self.old["bundle"]).exists())
        self.assertEqual(self.perform(), result)
        self.assertEqual(upgrade_status(self.store, self.operation), result)

    def test_a_verified_upgrade_keeps_only_the_selected_bundle_and_the_one_before_it(self):
        self.perform()
        versions = Path(self.old["bundle"]).parent
        self.assertEqual({item.name for item in versions.iterdir()}, {self.old["bundle_sha256"], self.next_digest})
        third = write_bundle(self.root / "third", "0.3.0")
        digest = verify_bundle(third)[1]
        self.assertEqual(upgrade(self.store, third, self.next_digest, digest, "b" * 32)["status"], "verified")
        self.assertEqual({item.name for item in versions.iterdir()}, {self.next_digest, digest})

    def test_an_interrupted_retention_is_removed_and_redone(self):
        partial = Path(self.old["bundle"]).parent / (self.next_digest + ".preparing")
        (partial / "python").mkdir(parents=True)
        self.assertEqual(self.perform()["status"], "verified")
        self.assertFalse(partial.exists())
        self.assertEqual(inspect_service(self.store)["bundle_sha256"], self.next_digest)

    def test_lost_switch_recovers_same_original_without_replay(self):
        original_write = self.store.write
        def drop_installation_write(name, value, **kwargs):
            if name == "installation.json":
                raise OSError("Synthetic interrupted switch")
            return original_write(name, value, **kwargs)
        with patch.object(self.store, "write", side_effect=drop_installation_write), self.assertRaises(OSError):
            self.perform()
        partial = upgrade_status(self.store, self.operation)
        self.assertEqual((partial["status"], partial["phase"], partial["installation"]), ("prepared", "prepared", "old"))
        self.assertEqual(self.perform(), partial)
        self.assertEqual(self.perform(resume=True)["status"], "verified")
        self.assertEqual(self.store.read("installation.json")["bundle_sha256"], self.next_digest)

    def test_pending_upgrade_refuses_other_mutations(self):
        original_write = self.store.write
        def drop_installation_write(name, value, **kwargs):
            if name == "installation.json":
                raise OSError("Synthetic interrupted switch")
            return original_write(name, value, **kwargs)
        with patch.object(self.store, "write", side_effect=drop_installation_write), self.assertRaises(OSError):
            self.perform()
        with self.assertRaises(ConnectorError) as observed:
            upgrade(self.store, self.second, self.old["bundle_sha256"], self.next_digest, "b" * 32)
        self.assertEqual(observed.exception.code, "upgrade_pending")
        for effect in (lambda: install(self.store, self.second), lambda: remove(self.store),
                       lambda: upgrade(self.store, None, "0" * 64, self.next_digest, self.operation, resume=True)):
            with self.assertRaises(ConnectorError):
                effect()
        self.assertEqual(self.perform(resume=True)["status"], "verified")

    def test_app_maintenance_resumes_original_upgrade(self):
        from openmachine_device.service import maintain_bundle
        with patch("openmachine_device.service._retain_bundle", side_effect=ConnectorError("bundle_changed", "Interrupted")), \
                self.assertRaises(ConnectorError):
            maintain_bundle(self.store, self.second, self.old["bundle_sha256"], self.next_digest)
        self.assertIsNone(self.store.read("upgrade-active.json", missing=True))
        original_write = self.store.write
        def drop_installation_write(name, value, **kwargs):
            if name == "installation.json":
                raise OSError("Synthetic interrupted switch")
            return original_write(name, value, **kwargs)
        with patch.object(self.store, "write", side_effect=drop_installation_write), self.assertRaises(OSError):
            maintain_bundle(self.store, self.second, self.old["bundle_sha256"], self.next_digest)
        original = self.store.read("upgrade-active.json")["operation_id"]
        with self.assertRaises(ConnectorError):
            maintain_bundle(self.store, self.second, self.old["bundle_sha256"], "f" * 64)
        result = maintain_bundle(self.store, self.second, self.old["bundle_sha256"], self.next_digest)
        self.assertEqual((result["status"], result["operation_id"]), ("verified", original))
        self.assertEqual(maintain_bundle(self.store, self.second, self.old["bundle_sha256"], self.next_digest)["status"], "verified")
        arguments = parser().parse_args(["--state-dir", str(self.store.root), "upgrade", "--automatic",
            "--bundle", str(self.second), "--expected-current", self.old["bundle_sha256"],
            "--expected-target", self.next_digest])
        self.assertEqual(asyncio.run(command(arguments))["status"], "verified")

    def test_wrong_bundle_identity_platform_and_symlink_refuse_before_effect(self):
        with self.assertRaises(ConnectorError):
            upgrade(self.store, self.second, self.old["bundle_sha256"], "f" * 64, self.operation)
        self.assertEqual(upgrade_status(self.store, self.operation)["status"], "not_admitted")
        link = self.root / "linked"
        link.symlink_to(self.second, target_is_directory=True)
        with self.assertRaises(ConnectorError):
            upgrade(self.store, link, self.old["bundle_sha256"], self.next_digest, self.operation)
        alternative = write_bundle(self.root / "other-platform", "0.3.0")
        manifest = json.loads((alternative / "manifest.json").read_text())
        manifest["architecture"] = "definitely-not-this-architecture"
        (alternative / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ConnectorError) as observed:
            upgrade(self.store, alternative, self.old["bundle_sha256"], verify_bundle(alternative)[1], self.operation)
        self.assertEqual(observed.exception.code, "incompatible_bundle")
        self.assertEqual(self.store.read("installation.json"), self.old)

    def test_cli_inspection_and_original_status(self):
        observed = asyncio.run(command(parser().parse_args(["--state-dir", str(self.store.root), "inspect"])))
        self.assertEqual(observed["bundle_sha256"], self.old["bundle_sha256"])
        result = self.perform()
        recovered = asyncio.run(command(parser().parse_args([
            "--state-dir", str(self.store.root), "upgrade", "--operation-id", self.operation, "--status"])))
        self.assertEqual(recovered, result)

    def test_remove_deletes_the_bundle_and_sign_in_and_keeps_original_operations(self):
        self.store.write("companion-" + "c" * 32 + ".json", {"operation_id": "c" * 32})
        self.store.write("revocation-device-A.json", {"device_id": "device-A"})
        self.assertEqual(remove(self.store), {"status": "removed"})
        self.assertFalse(Path(self.old["bundle"]).exists())
        for name in (self.key_name, "config.json", "native-session.json", "installation.json", "revocation-device-A.json"):
            self.assertIsNone(self.store.read_bytes(name, missing=True))
        self.assertIsNotNone(self.store.read_bytes("companion-" + "c" * 32 + ".json", missing=True))
        self.assertEqual(install(self.store, self.first)["status"], "installed")


class LinuxBundle(unittest.TestCase):
    """A Linux bundle is retained under ~/.local/share and runs only on its own platform."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="linux-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.platform = SimpleNamespace(platform="linux")
        for entry in (patch("openmachine_device.service.sys", self.platform),
                      patch.object(Path, "home", return_value=self.root / "home")):
            entry.start()
            self.addCleanup(entry.stop)

    def test_install_inspect_and_upgrade_under_the_users_share(self):
        first, second = (write_bundle(self.root / version, version, "linux") for version in ("0.1.0", "0.2.0"))
        versions = self.root / "home/.local/share/openmachine/connector/versions"
        self.assertEqual(install(self.store, first), {"status": "installed", "version": "0.1.0"})
        old, new = verify_bundle(first)[1], verify_bundle(second)[1]
        self.assertEqual(self.store.read("installation.json")["bundle"], str(versions / old))
        self.assertEqual(versions.stat().st_mode & 0o777, 0o700)
        self.assertEqual(inspect_service(self.store)["bundle_sha256"], old)
        self.assertEqual(upgrade(self.store, second, old, new, "a" * 32)["status"], "verified")
        self.assertEqual(inspect_service(self.store)["bundle_sha256"], new)
        self.assertEqual({item.name for item in versions.iterdir()}, {old, new})

    def test_a_bundle_of_another_platform_or_architecture_is_refused(self):
        mac = write_bundle(self.root / "mac", "0.1.0")
        other = write_bundle(self.root / "other", "0.1.0", "linux")
        manifest = json.loads((other / "manifest.json").read_text())
        (other / "manifest.json").write_text(json.dumps({**manifest, "architecture": "another"}))
        linux = write_bundle(self.root / "linux", "0.1.0", "linux")
        for platform, bundle in (("linux", mac), ("linux", other), ("darwin", linux)):
            self.platform.platform = platform
            with self.assertRaises(ConnectorError) as observed:
                install(self.store, bundle)
            self.assertEqual(observed.exception.code, "incompatible_bundle")
        self.assertIsNone(self.store.read("installation.json", missing=True))


class CloudTransport(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_cloud_answers_trigger_cancellation_sign_out_or_end_a_sign_in(self):
        path = "/api/native-sessions/" + "s" * 43 + "/cancel"
        active = json.dumps({"error": {"code": "native_login_active", "message": "Sign-in completed"}}).encode()
        cases = [
            ("POST", path, 409, active, "native_login_active"),
            ("GET", path, 409, active, "cloud_conflict"),
            ("POST", path.replace("/cancel", "/sign-out"), 409, active, "cloud_conflict"),
            ("POST", "/api/devices/device-A/revoke", 409, active, "cloud_conflict"),
            ("POST", "/api/native-sessions/invalid/cancel", 409, active, "cloud_conflict"),
            ("POST", path, 403, active, "device_not_authorized"),
            ("POST", path, 409, b'{"error":{"code":"another_conflict"}}', "cloud_conflict"),
            ("POST", path, 409, b'{"error":"native_login_active"}', "cloud_conflict"),
            ("POST", path, 409, b'{"error":', "cloud_conflict"),
            # Only Cloud's own answer proves a sign-in is gone; a 404 from anything else proves nothing.
            ("POST", path, 404, b'{"error":{"code":"native_session_unavailable"}}', "native_session_unavailable"),
            ("POST", path, 404, b'<html>Not Found</html>', "cloud_unavailable"),
            # Desktop acts on Cloud's own policy write codes; elsewhere they stay generic.
            *[("POST", path.replace("/cancel", "/policy"), status, json.dumps({"error": {"code": code}}).encode(), code)
              for status, code in ((409, "policy_conflict"), (400, "invalid_policy"), (403, "device_mismatch"),
                                   (401, "native_session_inactive"))],
            ("POST", path.replace("/cancel", "/policy"), 401, b'{"error":{"code":"invalid_device_proof"}}', "device_not_authorized"),
            ("POST", path.replace("/cancel", "/device"), 409, b'{"error":{"code":"policy_conflict"}}', "cloud_conflict"),
            # A create's refused key is Cloud's own code; elsewhere it stays generic.
            ("POST", "/api/native-sessions", 403, b'{"error":{"code":"key_invalid"}}', "key_invalid"),
            ("POST", "/api/native-sessions", 400, b'{"error":{"code":"key_platform"}}', "key_platform"),
            ("POST", "/api/native-sessions", 409, b'{"error":{"code":"native_device_limit"}}', "native_device_limit"),
            ("POST", path, 403, b'{"error":{"code":"key_invalid"}}', "device_not_authorized"),
        ]
        client_type = httpx.AsyncClient
        cloud = Cloud(ORIGIN)
        for method, route, response_status, response_body, expected in cases:
            async def app(scope, receive, send):
                self.assertEqual(scope["method"], method)
                self.assertEqual(scope["path"], route)
                await send({"type": "http.response.start", "status": response_status,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": response_body})
            def client(**kwargs):
                return client_type(transport=httpx.ASGITransport(app=app), **kwargs)
            with self.subTest(method=method, route=route, status=response_status, body=response_body), \
                    patch("openmachine_device.cloud.httpx.AsyncClient", side_effect=client), \
                    self.assertRaises(ConnectorError) as observed:
                await cloud.request(method, route, body=b"{}")
            self.assertEqual(observed.exception.code, expected)

    async def test_a_connection_that_never_opened_is_tried_once_more_over_ipv4_only(self):
        """A broken IPv6 route fails before any request byte; anything after connecting is never resent."""
        client_type, transport_type = httpx.AsyncClient, httpx.AsyncHTTPTransport
        for first, expected in ((httpx.ConnectError, "verified"), (httpx.ConnectTimeout, "verified"),
                                (httpx.ReadError, "cloud_unavailable"), (httpx.WriteError, "cloud_unavailable")):
            delivered, attempts, bound = [], [], []
            async def app(scope, receive, send):
                delivered.append(scope["path"])
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"status":"verified"}'})
            def fail(request):
                raise first("unreachable", request=request)
            def transport(**kwargs):
                bound.append(kwargs)
                return "ipv4"
            def client(**kwargs):
                attempts.append(kwargs.pop("transport", None))
                return client_type(transport=httpx.ASGITransport(app=app) if attempts[-1] == "ipv4"
                                   else httpx.MockTransport(fail), **kwargs)
            with self.subTest(first=first.__name__), \
                    patch("openmachine_device.cloud.httpx.AsyncClient", side_effect=client), \
                    patch("openmachine_device.cloud.httpx.AsyncHTTPTransport", side_effect=transport):
                if expected == "verified":
                    result = await Cloud(ORIGIN).request("POST", "/api/devices/device-A/revoke", body=b"{}")
                    self.assertEqual(result, {"status": "verified"})
                    self.assertEqual(attempts, [None, "ipv4"])
                    self.assertEqual(bound, [{"local_address": "0.0.0.0"}])
                    self.assertEqual(delivered, ["/api/devices/device-A/revoke"])
                else:
                    with self.assertRaises(ConnectorError) as observed:
                        await Cloud(ORIGIN).request("POST", "/api/devices/device-A/revoke", body=b"{}")
                    self.assertEqual(observed.exception.code, expected)
                    self.assertEqual(attempts, [None])
                    self.assertEqual(delivered, [])

    async def test_cloud_is_unavailable_when_ipv4_cannot_connect_either(self):
        client_type, attempts = httpx.AsyncClient, []
        def fail(request):
            raise httpx.ConnectError("unreachable", request=request)
        def client(**kwargs):
            attempts.append(kwargs.pop("transport", None))
            return client_type(transport=httpx.MockTransport(fail), **kwargs)
        with patch("openmachine_device.cloud.httpx.AsyncClient", side_effect=client), \
                self.assertRaises(ConnectorError) as observed:
            await Cloud(ORIGIN).request("POST", "/api/devices/device-A/revoke", body=b"{}")
        self.assertEqual(observed.exception.code, "cloud_unavailable")
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(attempts[0])
        self.assertIsInstance(attempts[1], httpx.AsyncHTTPTransport)

    async def test_one_cloud_sends_its_requests_over_one_connection_and_closes_it(self):
        """A sign-in's requests pay for one connection and TLS handshake, not one each."""
        connections, closed = [], asyncio.Event()
        async def serve(reader, writer):
            connections.append(writer)
            try:
                while True:
                    head = await reader.readuntil(b"\r\n\r\n")
                    await reader.readexactly(int(re.search(rb"(?i)\r\ncontent-length: *(\d+)", head)[1]))
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
                    await writer.drain()
            except asyncio.IncompleteReadError:
                closed.set()
                writer.close()
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server:
            async with Cloud(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}") as cloud:
                for _ in range(3):
                    self.assertEqual(await cloud.request("POST", "/api/devices/device-A/challenge", body=b"{}"), {})
                self.assertEqual(len(connections), 1)
            await asyncio.wait_for(closed.wait(), 5)

    async def test_a_skewed_clock_is_corrected_once_and_never_read_as_revocation(self):
        """Cloud's time from native_clock_skew signs the one retry; a second skew stays that error."""
        client_type = httpx.AsyncClient
        cloud, key = Cloud(ORIGIN), Identity(Ed25519PrivateKey.from_private_bytes(bytes(range(32))))
        session = {"challenge_id": "s" * 43, "challenge": "challenge-value"}
        server_time = int(time.time()) + 3600
        for corrected in (True, False):
            times = []
            async def app(scope, receive, send):
                times.append(int(dict(scope["headers"])[b"x-om-time"]))
                accepted = corrected and abs(times[-1] - server_time) <= 1
                body = {"status": "pending"} if accepted else {"error": {"code": "native_clock_skew", "server_time": server_time}}
                await send({"type": "http.response.start", "status": 200 if accepted else 400,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps(body).encode()})
            def client(**kwargs):
                return client_type(transport=httpx.ASGITransport(app=app), **kwargs)
            cloud.offset = 0
            with self.subTest(corrected=corrected), patch("openmachine_device.cloud.httpx.AsyncClient", side_effect=client):
                if corrected:
                    self.assertEqual(await cloud.native("/api/native-sessions/" + "s" * 43 + "/status", session, {}, key), {"status": "pending"})
                else:
                    with self.assertRaises(ConnectorError) as skewed:
                        await cloud.native("/api/native-sessions/" + "s" * 43 + "/status", session, {}, key)
                    self.assertEqual(skewed.exception.code, "native_clock_skew")
            self.assertEqual(len(times), 2)
            self.assertLessEqual(abs(times[1] - server_time), 1)


class DeviceRevocation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="pr-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "state")
        self.store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": False})
        self.identity, self.stored = stored_device(self, self.store, {"device_id": "device-A"})

    async def test_revocation_retains_same_proof_after_reply_loss(self):
        config = self.store.read("config.json")
        config.update(enabled=True, **self.stored)
        self.store.write("config.json", config)

        class Destination(Cloud):
            challenges = 0
            requests = []

            async def challenge(inner, device_id):
                inner.challenges += 1
                return {"challenge_id": "nonce-A", "challenge": "challenge-value", "expires_at": int(time.time()) + 60}

            async def request(inner, method, path, **kwargs):
                inner.requests.append((method, path, kwargs))
                if len(inner.requests) == 1:
                    raise ConnectorError("cloud_unavailable", "Synthetic lost revocation reply")
                return {"status": "revoked", "device_id": "device-A", "revoked_at": 1234567890}

        cloud = Destination(ORIGIN)
        with self.assertRaises(ConnectorError):
            await unpair(self.store, cloud)
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertEqual(self.store.read("revocation-device-A.json")["stage"], "prepared")
        receipt = await unpair(self.store, cloud)
        self.assertEqual(receipt["status"], "revoked")
        self.assertTrue(self.store.read("config.json")["revoked"])
        self.assertEqual(cloud.challenges, 1)
        self.assertEqual(cloud.requests[0], cloud.requests[1])
        await unpair(self.store, cloud)
        self.assertEqual(len(cloud.requests), 2)


class Removal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix="dr-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state")
        self.identity, stored = stored_device(self, self.store, {"device_id": "device-A"})
        self.store.write("config.json", {"version": 1, "server": ORIGIN, "enabled": True, **stored})
        self.key_name = key_file(self.identity.fingerprint)
        self.enterContext(patch("openmachine_device.service.sys", SimpleNamespace(platform="darwin")))
        self.enterContext(patch.object(Path, "home", return_value=self.root / "home"))
        install(self.store, write_bundle(self.root / "bundle", "0.1.0"))
        self.installation = self.store.read("installation.json")

        class Destination(Cloud):
            def __init__(inner):
                super().__init__(ORIGIN)
                inner.challenges = 0
                inner.requests = []
                inner.available = True

            async def challenge(inner, device_id):
                inner.challenges += 1
                return {"challenge_id": "nonce-A", "challenge": "challenge-value", "expires_at": int(time.time()) + 60}

            async def request(inner, method, path, **kwargs):
                inner.requests.append((method, path, kwargs))
                # om is already off when Cloud is asked.
                assert self.store.read("config.json")["enabled"] is False
                if not inner.available:
                    raise ConnectorError("cloud_unavailable", "Synthetic lost revocation reply")
                return {"status": "revoked", "device_id": "device-A", "revoked_at": 1234567890}

        self.cloud = Destination()
        self.enterContext(patch("openmachine_device.cli.Cloud", return_value=self.cloud))

    async def invoke(self, *arguments):
        return await command(parser().parse_args(["--state-dir", str(self.store.root), "remove", *arguments]))

    async def test_remove_takes_code_off_then_revokes_before_local_removal(self):
        self.cloud.available = False
        failed = await self.invoke()
        self.assertEqual(failed, {"status": "incomplete", "operation": "remove", "cloud_revocation": "pending",
                                  "local_removal": "not_attempted", "errors": {"cloud_revocation": "cloud_unavailable"}})
        self.assertFalse(self.store.read("config.json")["enabled"])
        self.assertIsNotNone(self.store.read_bytes(self.key_name, missing=True))
        self.assertTrue(Path(self.installation["bundle"]).is_dir())
        self.cloud.available = True
        removed = await self.invoke()
        self.assertEqual((removed["status"], removed["cloud_revocation"], removed["local_removal"]),
                         ("removed", "verified", "verified"))
        self.assertFalse(Path(self.installation["bundle"]).exists())
        for name in (self.key_name, "config.json", "revocation-device-A.json", "installation.json"):
            self.assertIsNone(self.store.read_bytes(name, missing=True))
        self.assertEqual(self.cloud.challenges, 1)
        self.assertEqual(self.cloud.requests[0], self.cloud.requests[1])

    async def test_local_only_removal_leaves_cloud_revocation_unverified(self):
        removed = await self.invoke("--local-only")
        self.assertEqual((removed["status"], removed["cloud_revocation"]), ("removed", "unverified"))
        self.assertEqual(self.cloud.requests, [])
        self.assertIsNone(self.store.read_bytes("config.json", missing=True))


if __name__ == "__main__":
    unittest.main()
