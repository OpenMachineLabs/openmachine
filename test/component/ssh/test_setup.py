"""Guided setup effects, exact identities and recovery without live servers."""

import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from integrations.ssh import client, provision, setup
from openmachine.config import SshConnection, load
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
from openmachine.service import maintenance
from test.support.service_cases import ServiceCaseFixture


def key(byte=1):
    return "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + bytes([byte]) * 32).decode()


class SetupTests(ServiceCaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.operation_id = "7" * 32
        self.request = {"name": "build", "host": "server.example", "user": "operator", "port": 22}
        self.available = False
        self.remote_calls = []

    def remote(self, record, action, **fields):
        self.remote_calls.append(action)
        if action == "prepare":
            self.available = True
        return {"home": "/home/operator", "uid": 1000, "identity": "d" * 64, "os": "Linux", "architecture": "x86_64",
                "python": "3.12.9", "systemd": "255", "worker_status": "available" if self.available else "absent",
                "worker": "/home/operator/.local/share/openmachine/workers/" + record["artifact"]["sha256"] + ".pyz",
                **({"root": fields["root"]} if action == "project" else {})}

    def act(self, action, value=None):
        return maintenance.setup_ssh(self.config, self.state, action, self.operation_id, value or {})["setup"]

    def test_unknown_host_requires_review_and_lost_prepare_reads_original_once(self):
        before = self.config.read_bytes()
        with patch.object(setup, "_discover", return_value=(key(), False)), patch.object(setup, "_remote", side_effect=self.remote), \
                patch.object(client, "call", return_value={"target_identity": "d" * 64}):
            view = self.act("inspect", self.request)
            self.assertEqual(view["phase"], "host_key_review")
            self.assertEqual(self.remote_calls, [])
            self.assertEqual(self.act("prepare", {"confirmed": True})["phase"], "host_key_review")
            with self.assertRaises(Fault):
                self.act("trust", {"fingerprint": "wrong", "confirmed": True})
            view = self.act("trust", {"fingerprint": view["fingerprint"], "confirmed": True})
            self.assertEqual(view["phase"], "ready_to_prepare")
            def lost(record, action, **fields):
                result = self.remote(record, action, **fields)
                if action == "prepare":
                    raise Fault("ssh_unavailable", "Lost response")
                return result
            with patch.object(setup, "_remote", side_effect=lost):
                view = self.act("prepare", {"confirmed": True, "sha256": view["sha256"]})
            self.assertEqual(view["phase"], "preparing")
            self.assertEqual(self.act("status")["phase"], "prepared")
            self.act("prepare", {"confirmed": True, "sha256": view["sha256"]})
            self.assertEqual(self.remote_calls.count("prepare"), 1)
            view = self.act("project", {"name": "remote-project", "root": "/remote/project", "commands": False, "writes": False})
            self.assertEqual(view["phase"], "project_ready")
            self.assertEqual(view["project"]["root"], "/remote/project")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotIn("identity_file", json.dumps(view))
        self.assertEqual(setup.check_state(self.state)["ssh_setup_records"], 1)

    def test_known_worker_reused_and_remote_project_saved_through_existing_maintenance(self):
        self.policy_fixture()
        self.machine_fixture()
        self.available = True
        with patch.object(setup, "_discover", return_value=(key(), True)), patch.object(setup, "_remote", side_effect=self.remote), \
                patch.object(client, "call", return_value={"target_identity": "d" * 64}), patch.object(maintenance, "_spawn") as spawn:
            self.assertEqual(self.act("inspect", self.request)["phase"], "prepared")
            self.act("project", {"name": "remote-project", "root": "/remote/project", "commands": False, "writes": True})
            with patch.object(setup, "_remote", return_value={"root": "/different/project"}):
                changed = self.act("save", {"confirmed": True})
            self.assertEqual(changed["reason"], "remote_project_changed")
            self.assertFalse(maintenance._operation_path(self.state, self.operation_id).exists())
            result = self.act("save", {"confirmed": True})
            self.assertEqual(result["phase"], "saving", result.get("reason"))
            record = maintenance._read(self.state, self.operation_id)
            config = load(Path(record["request"]["candidate_config"]))
            self.assertEqual(config.workspaces["remote-project"].root, "/remote/project")
            target = config.machines["build"]
            self.assertEqual((target.execution_enabled, target.workspace_write_enabled), (False, True))
            self.assertEqual(target.ssh_connection.host_key, key())
            from openmachine.bootstrap import create_application
            target.ssh_connection.identity_file = "/native/selected-key"
            capabilities = create_application(config, self.state).get_capabilities()
            self.assertNotIn("ssh_connection", capabilities["machines"]["build"])
            self.assertNotIn("/native/selected-key", json.dumps(capabilities))
            self.act("save", {"confirmed": True})
            spawn.assert_called_once()
            self.assertNotIn("prepare", self.remote_calls)

    def test_candidate_must_read_retained_setup_records_and_cannot_ignore_them(self):
        import subprocess
        import sys
        from openmachine.service import checks
        with patch.object(setup, "_discover", return_value=(key(), False)):
            self.act("inspect", self.request)
        proof = checks.check_state(self.config, self.state)
        self.assertEqual(proof["ssh_setup_records"], 1)
        for candidate in (proof | {"state_contract": 21}, proof | {"ssh_setup_records": 0}):
            with patch.object(checks.processes, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(candidate), "")):
                with self.assertRaises(Fault):
                    checks._candidate_state(Path(sys.executable), self.config, self.state)

    def test_save_before_admission_recovers_without_resubmitting_or_bypassing_lock(self):
        self.available = True
        with patch.object(setup, "_discover", return_value=(key(), True)), patch.object(setup, "_remote", side_effect=self.remote), \
                patch.object(client, "call", return_value={"target_identity": "d" * 64}), \
                patch.object(maintenance, "change_machine", side_effect=OSError("fixture disk failure")) as change:
            self.act("inspect", self.request)
            self.act("project", {"name": "project", "root": "/remote/project", "commands": False, "writes": False})
            self.assertEqual(self.act("save", {"confirmed": True})["phase"], "saving")
            with lease(self.state / "service/operation.lock"):
                with self.assertRaises(Fault):
                    self.act("status")
            self.assertEqual(setup._read(self.state, self.operation_id)["phase"], "saving")
            self.assertEqual(self.act("status")["phase"], "project_ready")
            # A crashed submitter leaves the same pre-admission state on disk.
            record = setup._read(self.state, self.operation_id) | {"phase": "saving"}
            save_json(setup._path(self.state, self.operation_id), record)
            self.assertEqual(self.act("status")["phase"], "project_ready")
            change.assert_called_once()
            self.assertFalse(maintenance._operation_path(self.state, self.operation_id).exists())

    def test_wrong_credentials_unsupported_server_and_cancel_never_prepare(self):
        for code in ("ssh_authentication_required", "unsupported_target", "scope_unavailable", "worker_conflict"):
            self.operation_id = hashlib.md5(code.encode(), usedforsecurity=False).hexdigest()
            with patch.object(setup, "_discover", return_value=(key(), True)), \
                    patch.object(setup, "_remote", side_effect=Fault(code, "Fixture refusal")) as remote:
                view = self.act("inspect", self.request)
                self.assertEqual(view["reason"], code)
                self.assertEqual(remote.call_args.args[1], "inspect")
                self.assertEqual(self.act("cancel")["phase"], "cancelled")
                self.assertEqual(remote.call_count, 1)

    def test_changed_host_keys_block_and_direct_route_keeps_strict_owned_trust(self):
        connection = {"host": "server.example", "user": "operator", "port": 2200, "host_key": key()}
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh/known_hosts").write_text("fixture")
        with patch.object(Path, "home", return_value=self.root), \
                patch.object(Path, "is_file", autospec=True, side_effect=lambda path: path == self.root / ".ssh/known_hosts"), \
                patch.object(client, "_exchange", side_effect=[
                (0, f"[server.example]:2200 {key(2)}\n".encode()), (0, f"[server.example]:2200 {key()}\n".encode())]):
            with self.assertRaises(Fault) as caught:
                setup._discover(connection)
            self.assertEqual(caught.exception.code, "ssh_host_key_mismatch")
        with client.connection_args(None, SshConnection(**connection)) as argv:
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("/dev/null", argv)
            known = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("UserKnownHostsFile=")))
            self.assertEqual(known.read_text(), f"[server.example]:2200 {key()}\n")
            self.assertNotIn("StrictHostKeyChecking=no", argv)
        self.assertFalse(known.exists())
        for host in ("-option", "host,other", "host;whoami", "10.0.0.0/8"):
            with self.assertRaises(ValueError):
                SshConnection(**(connection | {"host": host}))

    def test_invalid_typed_input_returns_bounded_rejection_without_an_original(self):
        for identity, value, reason in (("invalid", self.request, "invalid_operation_id"),
                                       (self.operation_id, {"name": "build", "alias": ["wrong"]}, "invalid_machine")):
            with self.assertRaises(Fault) as caught:
                maintenance.setup_ssh(self.config, self.state, "inspect", identity, value)
            self.assertEqual(caught.exception.code, reason)
            self.assertFalse(maintenance._operation_path(self.state, self.operation_id).exists())
        with patch.object(maintenance.sys, "platform", "darwin"), patch.object(maintenance.os, "getuid", return_value=501):
            result = maintenance.dispatch(self.config, self.state, "ssh-setup", setup_action="inspect",
                                          identity="invalid", value=self.request)
        self.assertEqual(result["reason"], "invalid_operation_id")

    def test_remote_preparation_publishes_exact_bytes_without_overwrite_or_symlink(self):
        raw = b"reviewed fixture worker"
        digest = hashlib.sha256(raw).hexdigest()
        destination = self.root / ".local/share/openmachine/workers" / (digest + ".pyz")
        facts = {"home": str(self.root), "worker": str(destination), "worker_status": "absent"}
        request = {"action": "prepare", "sha256": digest, "archive": base64.b64encode(raw).decode()}
        with patch.object(provision, "inspect", return_value=facts):
            self.assertEqual(provision.act(request)["worker_status"], "available")
            self.assertEqual(destination.read_bytes(), raw)
            self.assertEqual(provision.act(request)["worker_status"], "available")
            destination.chmod(0o600)
            destination.write_bytes(b"conflict")
            with self.assertRaisesRegex(ValueError, "worker_conflict"):
                provision.act(request)
            self.assertEqual(destination.read_bytes(), b"conflict")
            destination.unlink()
            preserved = self.root / "unrelated-worker"
            preserved.write_bytes(raw)
            destination.symlink_to(preserved)
            with self.assertRaisesRegex(ValueError, "worker_conflict"):
                provision.act(request)
            self.assertEqual(preserved.read_bytes(), raw)
