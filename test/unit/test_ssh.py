"""SSH boundary checks; live SSH and ChatGPT acceptance are recorded separately."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from integrations.ssh import MAX_MESSAGE, PROTOCOL, bundle, client, endpoint, recovery
from openmachine import __version__, execution, tasks
from openmachine.execution import api as execution_api, logs
from openmachine.config import Settings
from openmachine.bootstrap import create_application
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
        self.request = {"protocol": PROTOCOL, "version": __version__, "identity": self.identity,
                        "execution_enabled": True, "workspace_write_enabled": True, "workspaces": {"project": str(self.project)},
                        "operation": "read_workspace_file", "arguments": {"workspace": "project", "path": "file.txt"}}

    def dispatch(self, operation, **arguments):
        request = self.request | {"operation": operation, "arguments": arguments}
        with patch.object(endpoint, "_identity", return_value=(self.identity, self.root)):
            return endpoint._dispatch(request, self.archive)["result"]

    def response(self, result):
        return 0, json.dumps({"protocol": PROTOCOL, "version": __version__, "identity": self.identity,
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
        first = bundle.build_bundle(self.root / "bundle")
        self.assertEqual(first, bundle.build_bundle(self.root / "bundle"))
        artifact = Path(first["path"])
        self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), first["sha256"])
        with zipfile.ZipFile(artifact) as archive:
            self.assertEqual(set(archive.namelist()), {"__main__.py", "integrations/__init__.py", "integrations/ssh/__init__.py",
                              "integrations/ssh/endpoint.py", "integrations/ssh/bundle.py",
                              "openmachine/__init__.py", "openmachine/execution/__init__.py",
                              "openmachine/execution/__main__.py", "openmachine/execution/api.py",
                              "openmachine/execution/worker.py", "openmachine/execution/records.py", "openmachine/execution/process.py",
                              "openmachine/execution/logs.py", "openmachine/execution/control.py", "openmachine/workspace/__init__.py",
                              "openmachine/workspace/inspect.py", "openmachine/workspace/git.py",
                              "openmachine/workspace/files.py", "openmachine/workspace/changes.py", "openmachine/workspace/patch.py", "openmachine/foundation/__init__.py",
                              "openmachine/foundation/state.py", "openmachine/foundation/files.py",
                              "openmachine/foundation/sanitize.py"})
        # -S removes third-party site packages; the remote entry must still import.
        result = subprocess.run([sys.executable, "-I", "-S", str(artifact), "rpc"], input="{}",
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "protocol_mismatch")
        artifact.write_bytes(b"changed")
        with self.assertRaises(Fault) as caught:
            bundle.build_bundle(artifact.parent)
        self.assertEqual(caught.exception.code, "worker_conflict")

    def test_archive_worker_runs_without_site_packages_or_controller_task_store(self):
        artifact = Path(bundle.build_bundle(self.root / "workers")["path"])
        state = private_directory(self.root / "state")
        directory = private_directory(state / "executions" / ("c" * 32))
        locks = private_directory(state / "locks")
        record = {"format_version": execution.FORMAT, "version": __version__, "status": "accepted", "exit_code": None,
                  "created_at": 1, "log_truncated": False, "output_incomplete": False,
                  "context": {"worktree": str(self.project)},
                  "request": {"machine": self.identity, "cwd": str(self.project), "expected_head_sha": "",
                              "argv": [sys.executable, "-I", "-S", "-c",
                                       'from pathlib import Path; p=Path("count.txt"); '
                                       'p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("worker-complete")'],
                              "timeout_seconds": 10, "task_id": "controller-only-task", "termination_grace_seconds": 5,
                              "tty": False, "terminal_size": None},
                  "io_mode": "pipe", "terminal_size": None, "control": None, "control_ready": False,
                  "command": {"spawned": False, "exited": False, "started_at": None, "exited_at": None, "exit_signal": None},
                  "stop": {"reason": None, "requested_at": None, "grace_seconds": 5, "force_used": False},
                  "cleanup": {"containment": "posix_session", "status": "pending", "error": None}}
        with lease(directory / "control.lock"):
            save_json(directory / "record.json", record)
        key = hashlib.sha256(str(self.project).encode()).hexdigest()
        with lease(locks / f"workspace-{key}.lock") as owned, lease(directory / "worker.lock") as worker:
            result = subprocess.run([sys.executable, "-I", "-S", str(artifact), "worker", str(directory),
                                     str(worker.fileno()), str(owned.fileno())], cwd=self.root,
                                    pass_fds=(worker.fileno(), owned.fileno()),
                                    capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        observed = execution.read_record(directory)
        self.assertEqual((observed["status"], observed["exit_code"]), ("succeeded", 0))
        self.assertEqual(observed["created_at"], 1)
        self.assertEqual(observed["request"]["task_id"], "controller-only-task")
        self.assertEqual((self.project / "count.txt").read_text(), "1")
        self.assertIn("worker-complete", logs.read(directory, 0, 65536)["output"])
        self.assertFalse((state / "tasks").exists())

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

    def test_shared_recovery_is_passive_bounded_and_fail_closed(self):
        state = self.root / "controller"
        self.assertEqual(recovery.read(state)["targets"], {})
        self.assertFalse(state.exists())
        episode = {"episode_id": "c" * 32, "started_at_ms": 1, "deadline_ms": 3600001,
                   "attempts": 1, "next_attempt_ms": 2001, "reason_code": "ssh_transport_unavailable"}
        recovery.replace(state, self.identity, None, episode)
        before = (state / "ssh-recovery.json").read_bytes()
        self.assertEqual(recovery.read(state)["targets"][self.identity], episode)
        recovery.replace(state, self.identity, episode, episode)
        self.assertEqual((state / "ssh-recovery.json").read_bytes(), before)
        with self.assertRaises(Fault) as caught:
            recovery.replace(state, self.identity, None, None)
        self.assertEqual(caught.exception.code, "ssh_recovery_conflict")
        self.assertEqual((state / "ssh-recovery.json").read_bytes(), before)
        for data in (before.replace(b'"attempts": 1', b'"attempts": true'),
                     before.replace(b'"deadline_ms": 3600001', b'"deadline_ms": 3600002'),
                     before.replace(b'"format": 1', b'"format": 1, "format": 1'), b"x" * (recovery.MAX_BYTES + 1)):
            (state / "ssh-recovery.json").write_bytes(data)
            with self.assertRaises(Fault):
                recovery.read(state)
            self.assertEqual((state / "ssh-recovery.json").read_bytes(), data)
        (state / "ssh-recovery.json").unlink()
        with self.assertRaises(Fault) as caught:
            recovery.read(state)
        self.assertEqual(caught.exception.code, "ssh_recovery_corrupt")
        with self.assertRaises(Fault):
            recovery.replace(state, self.identity, None, None)
        self.assertFalse((state / "ssh-recovery.json").exists())

    def test_shared_recovery_lost_durability_ack_never_reinitializes(self):
        state = self.root / "controller"
        original = recovery.save_json
        def lost(path, value):
            original(path, value)
            raise OSError("Lost acknowledgement")
        with patch.object(recovery, "save_json", side_effect=lost), self.assertRaises(Fault) as caught:
            recovery.replace(state, self.identity, None, None)
        self.assertEqual(caught.exception.code, "ssh_recovery_unverified")
        self.assertEqual(recovery.read(state)["targets"], {self.identity: None})

    def test_controller_checkpoint_is_required_only_for_a_new_execution(self):
        app = create_application(self.settings, self.root / "controller")
        arguments = {"execution_id": "c" * 32, "cwd": str(self.project), "argv": ["true"], "task_id": "original-task"}
        missing = (0, b'{"error":{"code":"execution_not_found","message":"No original receipt."}}')
        with patch.object(client, "_exchange", return_value=missing) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "tasks_uninitialized")
            self.assertEqual(exchange.call_count, 1)
        app.save_task("original-task", 0, "Task", "Authorized disposable task.")
        with patch.object(client, "_exchange", side_effect=[missing, self.response({"status": "accepted"})]) as exchange:
            self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "accepted")
            self.assertEqual(exchange.call_count, 2)
        conflict = (0, b'{"error":{"code":"execution_conflict","message":"Changed original request."}}')
        for final in (self.response({"status": "succeeded"}), conflict):
            with self.subTest(final=final), \
                    patch.object(client, "_exchange", side_effect=[self.response({"status": "succeeded"}), final]) as exchange, \
                    patch.object(tasks, "read", side_effect=AssertionError("Old receipts do not need a checkpoint")):
                if final == conflict:
                    with self.assertRaises(Fault) as caught:
                        app.start_execution("build-machine", **(arguments | {"argv": ["false"]}))
                    self.assertEqual(caught.exception.code, "execution_conflict")
                else:
                    self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "succeeded")
                operations = [json.loads(call.args[1])["operation"] for call in exchange.call_args_list]
                self.assertEqual(operations, ["read_execution", "start_execution"])
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "ssh_unavailable")
            self.assertEqual(exchange.call_count, 1)
        with patch.object(client, "_exchange", side_effect=AssertionError("Invalid requests never reach SSH")):
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **(arguments | {"task_id": "invalid/task"}))
            self.assertEqual(caught.exception.code, "invalid_task_id")

    def test_structured_task_is_not_a_new_remote_execution_grant(self):
        from test_continuity import task_contract
        state = self.root / "controller"
        app = create_application(self.settings, state)
        request = task_contract()
        tasks.create(state, request.pop("task_id"), **request)
        arguments = {"execution_id": "d" * 32, "cwd": str(self.project), "argv": ["true"], "task_id": "structured-one"}
        missing = (0, b'{"error":{"code":"execution_not_found","message":"No original receipt."}}')
        with patch.object(client, "_exchange", return_value=missing) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "supervised_admission_unavailable")
            self.assertEqual([json.loads(call.args[1])["operation"] for call in exchange.call_args_list], ["read_execution"])
        (state / "tasks/structured-one/task.json").write_text("Uncertain original task")
        with patch.object(client, "_exchange", side_effect=[self.response({"status": "succeeded"}),
                self.response({"status": "succeeded"})]) as exchange:
            self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "succeeded")
            self.assertEqual([json.loads(call.args[1])["operation"] for call in exchange.call_args_list],
                             ["read_execution", "start_execution"])

    def test_application_routes_remote_files_without_resolving_controller_paths(self):
        app = create_application(self.settings, self.root / "controller")
        self.settings.workspaces["project"].root = "/remote/not-on-controller"
        operations = [
            (app.apply_workspace_patch, ("build-machine", "project", "patch", "a" * 64, "", [], False, "c" * 32), "apply_workspace_patch"),
            (app.inspect_workspace, ("build-machine", "project", "files"), "inspect_workspace"),
            (app.read_workspace_file, ("build-machine", "project", "file.txt"), "read_workspace_file"),
            (app.write_workspace_file, ("build-machine", "project", "file.txt", "new", "", ""), "write_workspace_file"),
            (app.read_execution, ("build-machine", "c" * 32), "read_execution"),
            (app.list_executions, ("build-machine",), "list_executions"),
            (app.cancel_execution, ("build-machine", "c" * 32), "cancel_execution"),
        ]
        for operation, arguments, name in operations:
            with self.subTest(operation=name), patch.object(client, "_exchange", return_value=self.response({})) as exchange:
                operation(*arguments)
                request = json.loads(exchange.call_args.args[1])
                self.assertEqual(request["operation"], name)
                self.assertEqual(request["workspaces"], {"project": "/remote/not-on-controller"})
        self.settings.machines["build-machine"].execution_enabled = False
        with patch.object(client, "_exchange", return_value=self.response({"status": "succeeded"})):
            self.assertEqual(app.cancel_execution("build-machine", "c" * 32)["status"], "succeeded")
        self.assertFalse((app.state / "executions").exists())

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

    def test_bounded_pipe_exchange_and_oversized_output(self):
        code, data = client._exchange([sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"], b"hello")
        self.assertEqual((code, data), (0, b"hello"))
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaises(Fault) as caught:
                client._exchange([sys.executable, "-c", f"import sys; sys.stdin.buffer.read(); sys.{stream}.write('x'*2097152)"], b"hello")
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
        config = endpoint._RemoteConfig(self.identity, {}, False, False)
        with patch.object(execution_api, "_scope_state", return_value="active"):
            self.assertEqual(execution.read(config, state, self.identity, directory.name)["status"], "finishing")
        with patch.object(execution_api, "_scope_state", return_value="not-found"):
            self.assertEqual(execution.read(config, state, self.identity, directory.name)["status"], "succeeded")
        with patch.object(execution_api, "_scope_state", side_effect=Fault("scope_unavailable", "No user manager")):
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
            with patch.object(execution_api, "_scope_state", side_effect=inspect_scope):
                result = execution.read(config, state, self.identity, directory.name)
            self.assertEqual((result["status"], result["cleanup"]["status"]), (status, cleanup))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual((directory / "record.json").read_bytes(), original)
        with patch.object(execution_api, "_scope_state", side_effect=Fault("scope_unavailable", "fixture")):
            result = execution.read(config, state, self.identity, directory.name)
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("outcome_unknown", "unverified"))
        self.assertEqual((directory / "record.json").read_bytes(), original)


    def test_wait_routes_once_and_observer_event_never_enters_wire(self):
        app = create_application(self.settings, self.root / "controller")
        cancelled = threading.Event()
        with patch.object(client, "_exchange", return_value=self.response({"status": "running", "wait_expired": True})) as exchange:
            result = app.read_execution("build-machine", "c" * 32, 7, 100, 0.25, cancelled=cancelled)
        self.assertTrue(result["wait_expired"])
        self.assertEqual(exchange.call_count, 1)
        self.assertIs(exchange.call_args.kwargs["cancelled"], cancelled)
        request = json.loads(exchange.call_args.args[1])
        self.assertEqual(request["arguments"], {"execution_id": "c" * 32, "offset": 7, "max_bytes": 100, "wait_seconds": 0.25})
        with patch.object(client, "_exchange", side_effect=AssertionError("Invalid waits must not contact the target")):
            with self.assertRaises(Fault) as caught:
                app.read_execution("build-machine", "c" * 32, wait_seconds=True)
        self.assertEqual(caught.exception.code, "invalid_wait")
        cancelled.set()
        with patch.object(client, "_exchange", side_effect=AssertionError("Cancelled reads must not contact the target")):
            with self.assertRaises(Fault) as caught:
                app.read_execution("build-machine", "c" * 32, wait_seconds=1, cancelled=cancelled)
        self.assertEqual(caught.exception.code, "observation_cancelled")
        with self.assertRaises(Fault) as caught:
            self.dispatch("read_execution", execution_id="c" * 32, cancelled=True)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_observation_exchange_cancellation_ends_only_its_owned_transport(self):
        for close_streams in (False, True):
            with self.subTest(close_streams=close_streams):
                cancelled = threading.Event()
                timer = threading.Timer(0.1, cancelled.set)
                timer.start()
                started = time.monotonic()
                code = "import os,sys,time; sys.stdin.buffer.read(); "
                if close_streams:
                    code += "os.close(0); os.close(1); os.close(2); "
                code += "time.sleep(10)"
                try:
                    with self.assertRaises(Fault) as caught:
                        client._exchange([sys.executable, "-c", code], b"{}", cancelled=cancelled)
                finally:
                    timer.join()
                self.assertEqual(caught.exception.code, "observation_cancelled")
                self.assertLess(time.monotonic() - started, 3)

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
        with patch.object(execution_api, "_scope_state", return_value="active"):
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

    def test_control_routes_once_and_never_replays_an_unknown_ssh_effect(self):
        app = create_application(self.settings, self.root / "controller")
        with patch.object(client, "_exchange", return_value=self.response({"status": "running"})) as exchange:
            app.control_execution("build-machine", "c" * 32, 0, "write_stdin", "literal\n")
        self.assertEqual(exchange.call_count, 1)
        frame = json.loads(exchange.call_args.args[1])
        self.assertEqual(frame["operation"], "control_execution")
        self.assertEqual(frame["arguments"], {"execution_id": "c" * 32, "expected_control_seq": 0,
                                              "action": "write_stdin", "text": "literal\n", "terminal_size": None})
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            with self.assertRaises(Fault) as caught:
                app.control_execution("build-machine", "c" * 32, 0, "interrupt")
            self.assertEqual(caught.exception.code, "outcome_unknown")
            self.assertEqual(exchange.call_count, 1)
        self.settings.machines["build-machine"].execution_enabled = False
        with patch.object(client, "_exchange", return_value=self.response({})) as exchange:
            with self.assertRaises(Fault) as caught:
                app.control_execution("build-machine", "c" * 32, 0, "write_stdin", "denied")
            self.assertEqual(caught.exception.code, "execution_disabled")
            exchange.assert_not_called()
            app.control_execution("build-machine", "c" * 32, 0, "interrupt")
            self.assertEqual(exchange.call_count, 1)
        with self.assertRaises(Fault) as caught:
            self.dispatch("control_execution", execution_id="c" * 32, expected_control_seq=0,
                          action="interrupt", unapproved=True)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_old_wire_rejects_new_controls_before_target_admission(self):
        with patch.object(endpoint, "_identity", side_effect=AssertionError("Protocol rejection precedes target access")):
            with self.assertRaises(Fault) as caught:
                endpoint._dispatch(self.request | {"protocol": 2, "operation": "control_execution"}, self.archive)
        self.assertEqual(caught.exception.code, "protocol_mismatch")
