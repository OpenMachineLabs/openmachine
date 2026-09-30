"""SSH boundary checks; live SSH and ChatGPT acceptance are recorded separately."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from integrations import ssh
from openmachine import __version__, execution, tasks
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-ssh-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.identity = "a" * 64
        self.digest = "b" * 64
        self.archive = self.root / (self.digest + ".pyz")
        self.settings = Settings.model_validate({
            "schema_version": 1,
            "machines": {"build-machine": {"transport": "ssh", "ssh_alias": "existing-host",
                          "ssh_worker": str(self.archive), "ssh_identity": self.identity, "execution_enabled": True}},
            "workspaces": {"project": {"machine": "build-machine", "root": str(self.project)}},
        })
        self.request = {"protocol": ssh.PROTOCOL, "version": __version__, "identity": self.identity,
                        "execution_enabled": True, "workspaces": {"project": str(self.project)},
                        "operation": "read_workspace_file", "arguments": {"workspace": "project", "path": "file.txt"}}

    def dispatch(self, operation, **arguments):
        request = self.request | {"operation": operation, "arguments": arguments}
        with patch.object(ssh, "_identity", return_value=(self.identity, self.root)):
            return ssh._dispatch(request, self.archive)["result"]

    def response(self, result):
        return 0, json.dumps({"protocol": ssh.PROTOCOL, "version": __version__, "identity": self.identity,
                              "worker_sha256": self.digest, "result": result}).encode()

    def test_configuration_requires_explicit_alias_worker_and_identity_for_execution(self):
        target = self.settings.machines["build-machine"]
        for delta in ({"ssh_alias": "-bad"}, {"ssh_worker": "/tmp/mutable.pyz"}, {"ssh_identity": ""},
                      {"transport": "local"}, {"ssh_alias": None}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                type(target).model_validate(target.model_dump() | delta)
        disabled = type(target).model_validate(target.model_dump() | {"execution_enabled": False, "ssh_identity": ""})
        self.assertEqual(disabled.ssh_identity, "")
        self.assertEqual(target.ssh_alias, "existing-host")

    def test_doctor_does_not_probe_ssh_or_resolve_remote_paths_by_default(self):
        config = self.root / "config.toml"
        config.write_text(f'schema_version=1\n[machines.build]\ntransport="ssh"\nssh_alias="existing-host"\n'
                          f'ssh_worker="/remote/{self.digest}.pyz"\n[workspaces.remote]\nmachine="build"\nroot="/remote/not-on-controller"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(config), "doctor"],
                                capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["unavailable_workspaces"], [])
        self.assertEqual(value["ssh_targets"], {"build": "not_probed"})

    def test_bundle_is_reproducible_content_addressed_and_dependency_free(self):
        first = ssh.build_bundle(self.root / "bundle")
        self.assertEqual(first, ssh.build_bundle(self.root / "bundle"))
        artifact = Path(first["path"])
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), first["sha256"])
        with zipfile.ZipFile(artifact) as archive:
            self.assertEqual(set(archive.namelist()), {"__main__.py", "integrations/__init__.py", "integrations/ssh.py",
                              "openmachine/__init__.py", "openmachine/execution.py", "openmachine/workspace.py",
                              "openmachine/logs.py", "openmachine/foundation/__init__.py",
                              "openmachine/foundation/state.py", "openmachine/foundation/files.py",
                              "openmachine/foundation/sanitize.py"})
        # -S removes third-party site packages; the remote entry must still import.
        result = subprocess.run([sys.executable, "-I", "-S", str(artifact), "rpc"], input="{}",
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "protocol_mismatch")
        artifact.write_bytes(b"changed")
        with self.assertRaises(Fault) as caught:
            ssh.build_bundle(artifact.parent)
        self.assertEqual(caught.exception.code, "worker_conflict")

    def test_transport_preserves_arguments_as_json_and_pins_the_existing_alias(self):
        literal = ['python3', '-c', 'print("$HOME; $(touch escaped)")', '', 'two words', "a'b"]
        with patch.object(ssh, "_exchange", return_value=self.response({"execution_id": "c" * 32})) as exchange:
            ssh.call(self.settings, self.root / "controller", "build-machine", "start_execution",
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
            with self.subTest(operation=operation), patch.object(ssh, "_exchange", side_effect=TimeoutError) as exchange:
                with self.assertRaises(Fault) as caught:
                    ssh.call(self.settings, None, "build-machine", operation)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(exchange.call_count, 1)

    def test_controller_checkpoint_is_required_only_for_a_new_execution(self):
        arguments = {"execution_id": "c" * 32, "cwd": str(self.project), "argv": ["true"], "task_id": "original-task"}
        missing = (0, b'{"error":{"code":"execution_not_found","message":"No original receipt."}}')
        with patch.object(ssh, "_exchange", return_value=missing) as exchange:
            with self.assertRaises(Fault):
                ssh.call(self.settings, self.root / "controller", "build-machine", "start_execution", **arguments)
            self.assertEqual(exchange.call_count, 1)
        state = self.root / "controller"
        tasks.save(state, "original-task", 0, "Task", "Authorized disposable task.", [])
        with patch.object(ssh, "_exchange", side_effect=[missing, self.response({"status": "accepted"})]) as exchange:
            self.assertEqual(ssh.call(self.settings, state, "build-machine", "start_execution", **arguments)["status"], "accepted")
            self.assertEqual(exchange.call_count, 2)
        with patch.object(ssh, "_exchange", return_value=self.response({"status": "succeeded"})), \
                patch.object(tasks, "read", side_effect=AssertionError("Old receipts do not need a checkpoint")):
            ssh.call(self.settings, state, "build-machine", "start_execution", **arguments)

    def test_identity_version_and_permission_fail_before_remote_file_changes(self):
        for delta, expected in (({"identity": "d" * 64}, "identity_mismatch"),
                                ({"version": "different"}, "protocol_mismatch"),
                                ({"identity": ""}, "identity_required")):
            with self.subTest(delta=delta), patch.object(ssh, "_identity", return_value=(self.identity, self.root)):
                with self.assertRaises(Fault) as caught:
                    ssh._dispatch(self.request | delta, self.archive)
                self.assertEqual(caught.exception.code, expected)
        self.request["execution_enabled"] = False
        with self.assertRaises(Fault) as caught:
            self.dispatch("write_workspace_file", workspace="project", path="file.txt", content="new",
                          expected_head_sha="", expected_sha256="")
        self.assertEqual(caught.exception.code, "execution_disabled")
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

    def test_bounded_pipe_exchange_and_oversized_output(self):
        code, data = ssh._exchange([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], b"hello")
        self.assertEqual((code, data), (0, b"hello"))
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaises(Fault) as caught:
                ssh._exchange([sys.executable, "-c", f"import sys; sys.stdin.buffer.read(); sys.{stream}.write('x'*2097152)"], b"hello")
            self.assertEqual(caught.exception.code, "response_limit")

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
        with patch.object(execution, "_scope_state", return_value="active"):
            self.assertEqual(execution._receipt(directory, self.identity)[1], "finishing")
        with patch.object(execution, "_scope_state", return_value="not-found"):
            self.assertEqual(execution._receipt(directory, self.identity)[1], "succeeded")
        with patch.object(execution, "_scope_state", side_effect=Fault("scope_unavailable", "No user manager")):
            self.assertEqual(execution._receipt(directory, self.identity)[1], "outcome_unknown")
        record["scope_unit"] = "unrelated.scope"
        save_json(directory / "record.json", record)
        with self.assertRaises(Fault) as caught:
            execution._receipt(directory, self.identity)
        self.assertEqual(caught.exception.code, "corrupt_state")

    def test_worker_alias_cannot_change_the_checked_checksum_filename(self):
        artifact = Path(ssh.build_bundle(self.root / "workers")["path"])
        alias = artifact.parent / ("0" * 64 + ".pyz")
        alias.symlink_to(artifact)
        result = subprocess.run([sys.executable, "-I", "-S", str(alias), "rpc"], input="{}",
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "worker_identity_mismatch")

    def test_malformed_response_and_prelaunch_size_failures_are_explicit(self):
        code, raw = self.response({})
        response = json.loads(raw)
        response["identity"] = None
        with patch.object(ssh, "_exchange", return_value=(code, json.dumps(response).encode())):
            with self.assertRaises(Fault) as caught:
                ssh.call(self.settings, None, "build-machine", "list_executions")
        self.assertEqual(caught.exception.code, "ssh_protocol_error")
        with patch.object(subprocess, "Popen", side_effect=AssertionError("Oversized input must not contact SSH")):
            with self.assertRaises(Fault) as caught:
                ssh.call(self.settings, None, "build-machine", "write_workspace_file", content="x" * ssh.MAX_MESSAGE)
        self.assertEqual(caught.exception.code, "request_limit")
