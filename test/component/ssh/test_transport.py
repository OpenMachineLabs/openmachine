"""Tests for ssh behavior at the component layer."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from integrations.ssh import MAX_MESSAGE, bundle, client, endpoint
from openmachine import __version__, execution
from openmachine.execution import observation
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from test.support.ssh_cases import SSHCaseFixture


class SSHTests(SSHCaseFixture, unittest.TestCase):
    def test_configuration_requires_explicit_alias_worker_and_identity_for_execution(self):
        target = self.settings.machines["build-machine"]
        for delta in ({"ssh_alias": "-bad"}, {"ssh_worker": "/tmp/mutable.pyz"}, {"ssh_identity": ""},
                      {"transport": "local"}, {"ssh_alias": None}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                type(target).model_validate(target.model_dump() | delta)
        disabled = type(target).model_validate(target.model_dump() | {"execution_enabled": False, "ssh_identity": ""})
        self.assertEqual(disabled.ssh_identity, "")
        self.assertEqual(target.ssh_alias, "existing-host")

    def test_monitoring_probe_reports_platform_without_grants_or_scope_setup(self):
        request = self.request | {"operation": "probe", "arguments": {}, "execution_enabled": False,
                                  "workspace_write_enabled": False, "workspaces": {}}
        with patch.object(endpoint, "_identity", return_value=(self.identity, self.root)), \
                patch.object(endpoint, "_scope_ready", side_effect=AssertionError("No execution prerequisite")):
            result = endpoint._dispatch(request, self.archive)["result"]
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["os"], endpoint.platform.system())
        self.assertEqual(result["architecture"], endpoint.platform.machine())
        self.assertIsNone(result["systemd"])
        self.assertFalse((self.root / ".local/state/openmachine").exists())

    def test_transport_preserves_arguments_as_json_and_pins_the_existing_alias(self):
        literal = ['python3', '-c', 'print("$HOME; $(touch escaped)")', '', 'two words', "a'b"]
        with patch.object(client, "_exchange", return_value=self.response({"execution_id": "c" * 32})) as exchange:
            client.call(self.settings, "build-machine", "start_execution",
                     execution_id="c" * 32, cwd=str(self.project), argv=literal)
        argv, data = exchange.call_args.args
        self.assertEqual(json.loads(data)["arguments"]["argv"], literal)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-2], "existing-host")
        self.assertNotIn("touch escaped", argv[-1])
        self.assertNotIn("state_dir", json.loads(data))

    def test_unknown_mutation_is_not_replayed_and_read_failure_is_not_empty_success(self):
        for operation, expected in (("write_workspace_file", "outcome_unknown"), ("list_executions", "ssh_unavailable")):
            with self.subTest(operation=operation), patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
                with self.assertRaises(Fault) as caught:
                    client.call(self.settings, "build-machine", operation)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(exchange.call_count, 1)

    def test_transport_classifies_bounded_failures_without_returning_diagnostics(self):
        cases = [("Connection refused", "ssh_unavailable", True),
                 ("Connection to fixture-host closed by remote host.\n", "ssh_unavailable", True),
                 ("Connection closed by 192.0.2.10 port 22\n", "ssh_unavailable", True),
                 ("Permission denied (publickey). Connection timed out", "ssh_authentication_required", False),
                 ("Host key verification failed.\nConnection closed by 192.0.2.10 port 22\n", "ssh_host_key_mismatch", False),
                 ("REMOTE HOST IDENTIFICATION HAS CHANGED! Connection refused", "ssh_host_key_mismatch", False)]
        for diagnostic, code, retryable in cases:
            with self.subTest(diagnostic=diagnostic), self.assertRaises(Fault) as caught:
                client._exchange([sys.executable, "-c", 'import os,sys;os.close(0);sys.stderr.write(sys.argv[1]);sys.exit(255)', diagnostic], b" " * MAX_MESSAGE)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual((caught.exception.evidence or {}).get("ssh_retryable", False), retryable)
            self.assertNotIn(diagnostic, str(caught.exception))
        with patch.object(client, "_exchange", side_effect=TimeoutError), self.assertRaises(Fault) as caught:
            client.call(self.settings, "build-machine", "read_execution", execution_id="c" * 32)
        self.assertTrue(caught.exception.evidence["ssh_retryable"])
        with patch.object(client, "_exchange", return_value=(255, b"")), self.assertRaises(Fault) as caught:
            client.call(self.settings, "build-machine", "read_execution", execution_id="c" * 32)
        self.assertFalse((caught.exception.evidence or {}).get("ssh_retryable", False))

    def test_identity_version_and_permission_fail_before_remote_file_changes(self):
        for delta, expected in (({"identity": "d" * 64}, "identity_mismatch"),
                                ({"version": "different"}, "protocol_mismatch"),
                                ({"identity": ""}, "identity_required")):
            with self.subTest(delta=delta), patch.object(endpoint, "_identity", return_value=(self.identity, self.root)):
                with self.assertRaises(Fault) as caught:
                    endpoint._dispatch(self.request | delta, self.archive)
                self.assertEqual(caught.exception.code, expected)
        self.request["execution_enabled"] = False
        self.request["workspace_write_enabled"] = False
        with self.assertRaises(Fault) as caught:
            self.dispatch("write_workspace_file", workspace="project", path="file.txt", content="new",
                          expected_head_sha="", expected_sha256="")
        self.assertEqual(caught.exception.code, "workspace_write_disabled")
        self.assertFalse((self.project / "file.txt").exists())
        self.assertFalse((self.root / ".local").exists())

    def test_remote_files_use_target_hashes_and_the_shared_execution_lock(self):
        result = self.dispatch("write_workspace_file", workspace="project", path="file.txt", content="old",
                               expected_head_sha="", expected_sha256="")
        read = self.dispatch("read_workspace_file", workspace="project", path="file.txt")
        self.assertEqual(read["text"], "old")
        self.assertEqual(read["sha256"], result["sha256"])
        with self.assertRaises(Fault) as caught:
            self.dispatch("write_workspace_file", workspace="project", path="file.txt", content="new",
                          expected_head_sha="", expected_sha256="")
        self.assertEqual(caught.exception.code, "content_mismatch")
        state = self.root / ".local/state/openmachine"
        lock = state / "locks" / ("workspace-" + hashlib.sha256(str(self.project).encode()).hexdigest() + ".lock")
        with lease(lock):
            self.request["workspaces"] = {"another-alias": str(self.project)}
            with self.assertRaises(Fault) as caught:
                self.dispatch("write_workspace_file", workspace="another-alias", path="file.txt", content="new",
                              expected_head_sha="", expected_sha256=read["sha256"])
        self.assertEqual(caught.exception.code, "resource_busy")
        self.assertEqual((self.project / "file.txt").read_text(), "old")

    def test_terminal_remote_receipt_waits_for_scope_cleanup(self):
        state = self.root / "state"
        directory = private_directory(state / "executions" / ("c" * 32))
        with lease(directory / "control.lock"):
            record = {"format_version": 3, "version": __version__, "status": "succeeded", "exit_code": 0,
                      "created_at": 1, "finished_at": 2, "log_truncated": False, "output_incomplete": False,
                      "scope_unit": "openmachine-" + "c" * 32 + ".scope",
                      "context": {"target_identity": self.identity, "worker_sha256": self.digest},
                      "request": {"machine": self.identity, "cwd": str(self.project), "expected_head_sha": "",
                                  "argv": ["true"], "timeout_seconds": 10}}
            save_json(directory / "record.json", record)
        config = endpoint._RemoteConfig(self.identity, {}, False, False)
        with patch.object(observation, "_scope_state", return_value="active"):
            self.assertEqual(execution.read(config, state, self.identity, directory.name)["status"], "finishing")
        with patch.object(observation, "_scope_state", return_value="not-found"):
            self.assertEqual(execution.read(config, state, self.identity, directory.name)["status"], "succeeded")
        with patch.object(observation, "_scope_state", side_effect=Fault("scope_unavailable", "No user manager")):
            self.assertEqual(execution.read(config, state, self.identity, directory.name)["status"], "outcome_unknown")
        record["scope_unit"] = "unrelated.scope"
        save_json(directory / "record.json", record)
        with self.assertRaises(Fault) as caught:
            execution.read(config, state, self.identity, directory.name)
        self.assertEqual(caught.exception.code, "corrupt_state")

    def test_worker_alias_cannot_change_the_checked_checksum_filename(self):
        artifact = Path(bundle.build_bundle(self.root / "workers")["path"])
        alias = artifact.parent / ("0" * 64 + ".pyz")
        alias.symlink_to(artifact)
        result = subprocess.run([sys.executable, "-I", "-S", str(alias), "rpc"], input="{}",
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "worker_identity_mismatch")

    def test_malformed_response_and_prelaunch_size_failures_are_explicit(self):
        code, raw = self.response({})
        response = json.loads(raw)
        response["identity"] = None
        with patch.object(client, "_exchange", return_value=(code, json.dumps(response).encode())):
            with self.assertRaises(Fault) as caught:
                client.call(self.settings, "build-machine", "list_executions")
        self.assertEqual(caught.exception.code, "ssh_protocol_error")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("Oversized input must not contact SSH")):
            with self.assertRaises(Fault) as caught:
                client.call(self.settings, "build-machine", "write_workspace_file", content="x" * MAX_MESSAGE)
        self.assertEqual(caught.exception.code, "request_limit")

    def test_v4_scope_observation_is_separate_and_never_rewrites_worker_facts(self):
        state = self.root / "state"
        directory = private_directory(state / "executions" / ("d" * 32))
        record = {"format_version": 4, "version": __version__, "status": "succeeded", "exit_code": 0,
                  "created_at": 1, "finished_at": 3, "log_truncated": False, "output_incomplete": False,
                  "scope_unit": "openmachine-" + "d" * 32 + ".scope", "io_mode": "pipe",
                  "context": {"target_identity": self.identity, "worker_sha256": self.digest},
                  "request": {"machine": self.identity, "cwd": str(self.project), "expected_head_sha": "",
                              "argv": ["true"], "timeout_seconds": 10, "termination_grace_seconds": 5},
                  "command": {"spawned": True, "exited": True, "started_at": 1, "exited_at": 2, "exit_signal": None},
                  "stop": {"reason": None, "requested_at": None, "grace_seconds": 5, "force_used": False},
                  "cleanup": {"containment": "systemd_scope", "status": "pending", "error": None}}
        with lease(directory / "control.lock"):
            save_json(directory / "record.json", record)
        original = (directory / "record.json").read_bytes()
        config = endpoint._RemoteConfig(self.identity, {}, False, False)
        for observed, status, cleanup in (("active", "finishing", "pending"), ("not-found", "succeeded", "verified")):
            def inspect_scope(*args, **kwargs):
                with lease(directory / "control.lock", create=False):
                    return observed
            with patch.object(observation, "_scope_state", side_effect=inspect_scope):
                result = execution.read(config, state, self.identity, directory.name)
            self.assertEqual((result["status"], result["cleanup"]["status"]), (status, cleanup))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual((directory / "record.json").read_bytes(), original)
        with patch.object(observation, "_scope_state", side_effect=Fault("scope_unavailable", "fixture")):
            result = execution.read(config, state, self.identity, directory.name)
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("outcome_unknown", "unverified"))
        self.assertEqual((directory / "record.json").read_bytes(), original)

    def test_endpoint_bounds_escaped_output_without_moving_raw_cursor(self):
        result = {"execution_id": "c" * 32, "output": "\ufffd" * 200000, "offset": 0,
                  "next_offset": 200000, "output_presentation_truncated": False}
        with patch.object(execution, "read", return_value=result):
            observed = self.dispatch("read_execution", execution_id="c" * 32, wait_seconds=1)
        self.assertTrue(observed["output_presentation_truncated"])
        self.assertEqual(observed["next_offset"], 200000)
        self.assertLess(len(json.dumps(observed).encode()), MAX_MESSAGE)
        self.assertFalse(result["output_presentation_truncated"])

    def test_lost_remote_owner_keeps_pending_control_unknown_even_with_live_scope(self):
        state = self.root / "state"
        directory = private_directory(state / "executions" / ("e" * 32))
        from openmachine.execution.records import control_fingerprint
        request = execution.control_request(0, "interrupt")
        record = {"format_version": 5, "version": __version__, "status": "running", "exit_code": None,
                  "created_at": 1, "log_truncated": False, "output_incomplete": False,
                  "scope_unit": "openmachine-" + "e" * 32 + ".scope", "io_mode": "pipe",
                  "context": {"target_identity": self.identity, "worker_sha256": self.digest},
                  "request": {"machine": self.identity, "cwd": str(self.project), "expected_head_sha": "",
                              "argv": ["true"], "timeout_seconds": 10, "termination_grace_seconds": 5,
                              "tty": False, "terminal_size": None},
                  "command": {"spawned": True, "exited": False, "started_at": 1, "exited_at": None, "exit_signal": None},
                  "stop": {"reason": None, "requested_at": None, "grace_seconds": 5, "force_used": False},
                  "cleanup": {"containment": "systemd_scope", "status": "pending", "error": None},
                  "terminal_size": None, "control_ready": True,
                  "control": {"protocol": 1, "seq": 1, "last": {
                      "seq": 1, "action": "interrupt", "request_sha256": control_fingerprint(request),
                      "status": "pending", "bytes_written": None, "accepted_at": 2, "completed_at": None, "error": None}}}
        with lease(directory / "control.lock"), lease(directory / "worker.lock"):
            save_json(directory / "record.json", record)
        before = (directory / "record.json").read_bytes()
        config = endpoint._RemoteConfig(self.identity, {}, False, False)
        with patch.object(observation, "_scope_state", return_value="active"):
            observed = execution.read(config, state, self.identity, directory.name)
            self.assertEqual(observed["status"], "outcome_unknown")
            self.assertEqual(observed["control"]["last"]["status"], "outcome_unknown")
            self.assertFalse(observed["control_ready"])
            duplicate = execution.control_execution(config, state, self.identity, directory.name, **request)
            self.assertEqual(duplicate["control_result"]["status"], "outcome_unknown")
            with self.assertRaises(Fault) as caught:
                execution.control_execution(config, state, self.identity, directory.name, 1, "interrupt")
            self.assertEqual(caught.exception.code, "control_unresolved")
        self.assertEqual((directory / "record.json").read_bytes(), before)

    def test_old_wire_rejects_new_controls_before_target_admission(self):
        with patch.object(endpoint, "_identity", side_effect=AssertionError("Protocol rejection precedes target access")):
            with self.assertRaises(Fault) as caught:
                endpoint._dispatch(self.request | {"protocol": 2, "operation": "control_execution"}, self.archive)
        self.assertEqual(caught.exception.code, "protocol_mismatch")


if __name__ == "__main__":
    unittest.main()
