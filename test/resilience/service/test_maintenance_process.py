"""Tests for service behavior at the resilience layer."""

import json
import asyncio
import errno
import hashlib
import platform
import socket
import stat
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from openmachine.foundation.state import lease
from openmachine.service import maintenance, payload, runtime
from test.support.service_cases import ServiceCaseFixture


class ServiceTests(ServiceCaseFixture, unittest.TestCase):
    def test_real_detached_maintenance_process_keeps_inherited_lock_and_original_record(self):
        record = self.operation_fixture("preparing")
        # This interrupted phase admits no effects. The real module startup
        # validates the inherited lock and exits without touching native jobs.
        original = maintenance._operation_path(self.state, record["operation_id"]).read_bytes()
        with lease(self.state / "service/operation.lock") as lock:
            process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.service.maintenance",
                str(self.state), record["operation_id"], str(lock.fileno())], cwd="/",
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(lock.fileno(),), start_new_session=True)
            self.assertEqual(process.communicate(timeout=10), (b"", b""))
            self.assertEqual(process.returncode, 0)
            self.assert_fault("resource_busy", lambda: maintenance.continue_operation(self.config, self.state, record["operation_id"]))
        self.assertEqual(maintenance._operation_path(self.state, record["operation_id"]).read_bytes(), original)


@unittest.skipUnless(sys.platform in {"darwin", "linux"} and os.environ.get("OM_NATIVE_FIRST_USE") == "1",
                     "opt-in native first-use test with an explicitly trusted payload")
class NativeFirstUseTests(unittest.TestCase):
    def save_evidence(self):
        self.evidence_path.write_text(json.dumps(self.evidence, indent=2) + "\n")
        self.evidence_path.chmod(0o600)

    def tool(self, state, name, arguments):
        import httpx2 as httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def invoke():
            async with httpx.AsyncClient(trust_env=False, timeout=10,
                    transport=httpx.AsyncHTTPTransport(uds=str(state / "mcp.sock"))) as client:
                async with streamable_http_client("http://localhost/mcp", http_client=client) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        return await session.call_tool(name, arguments)
        result = asyncio.run(invoke())
        self.evidence["tools"].append({"name": name, "arguments": arguments,
                                       "result": result.model_dump(mode="json", by_alias=True)})
        self.save_evidence()
        return result

    def test_isolated_install_start_ready_and_stop(self):
        source = Path(os.environ["OM_NATIVE_PAYLOAD"]).resolve(strict=True)
        digest = os.environ["OM_NATIVE_PAYLOAD_SHA256"]
        manifest = payload.verify(source, digest)
        if sys.platform == "linux":
            self.assertEqual(os.environ.get("OM_NATIVE_DISPOSABLE_USER"), str(os.getuid()),
                             "Linux acceptance requires an explicitly owned disposable account")
            self.assertNotEqual(os.getuid(), 0)
        root = Path(tempfile.mkdtemp(prefix="om-first-", dir="/tmp")).resolve()
        config, state, project = root / "config.toml", root / "state", root / "project"
        project.mkdir()
        (project / "hello.txt").write_text("Isolated first-use readiness check.\n")
        operation = uuid.uuid4().hex
        command = [str(source / "python/bin/python3"), "-I", "-B", "-m", "openmachine.cli",
                   "--config", str(config), "--state-dir", str(state), "manage", "--contract", "1"]
        evidence = {"fixture": str(root), "operation_id": operation, "steps": [], "tools": [],
                    "uid": os.getuid(), "platform": platform.platform(), "python": sys.version,
                    "manifest_sha256": digest, "core": manifest["core"], "target": manifest["target"],
                    "host_tools": {name: shutil.which(name) for name in
                                   ("python3", "git", "pip", "cc", "node", "npm", "rustc", "cargo")},
                    "tool_boundary": "host tooling may still be installed; not absence proof"}
        self.evidence = evidence
        self.evidence_path = Path(os.environ.get("OM_NATIVE_EVIDENCE", str(root / "test-result.json")))
        self.save_evidence()
        started = time.monotonic()

        def call(*arguments):
            evidence["stage"] = arguments[0]
            began = time.monotonic()
            result = subprocess.run(command + list(arguments), cwd="/", capture_output=True,
                                    text=True, timeout=60)
            # A non-ready status has a nonzero CLI exit but remains a valid reply.
            step = {"action": arguments[0], "exit_code": result.returncode,
                    "seconds": round(time.monotonic() - began, 2),
                    "stdout": result.stdout, "stderr": result.stderr}
            evidence["steps"].append(step)
            observed = json.loads(result.stdout)
            step["reply"] = observed
            self.save_evidence()
            self.assertIn(result.returncode, (0, 1), result.stderr)
            self.assertEqual(observed.get("management_contract"), 1, observed)
            return observed

        try:
            if sys.platform == "linux":
                import openmachine
                self.assertTrue(Path(openmachine.__file__).resolve().is_relative_to(source / "python"))
                evidence["os_release"] = platform.freedesktop_os_release()
                if os.environ.get("OM_NATIVE_SANDBOX") == "1":
                    self.assertTrue(all(value is None for value in evidence["host_tools"].values()), evidence)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                        with self.assertRaises(OSError) as unavailable:
                            probe.connect(("198.18.0.1", 9))
                    self.assertEqual(unavailable.exception.errno, errno.ENETUNREACH)
                    evidence["tool_boundary"] = "host developer tools inaccessible; no external IPv4 route"
                    evidence["network_errno"] = unavailable.exception.errno
                    self.save_evidence()
            self.assertEqual(call("status")["status"], "not_installed")
            self.assertEqual(call("configure", "--workspace", str(project))["status"], "configured")
            # Disable optional release observation in this offline fixture.
            config.write_text("check_for_updates = false\n" + config.read_text())
            from openmachine.config import load
            settings = load(config)
            self.assertEqual(settings.machine("local").local_access, "read_only")
            self.assertFalse(settings.machine("local").execution_enabled)
            self.assertFalse(settings.machine("local").workspace_write_enabled)
            original_config = config.read_bytes()
            rejected = call("install", "--operation-id", uuid.uuid4().hex, "--payload", str(source),
                            "--sha256", "0" * 64, "--probe-workspace", "project", "--probe-path", "hello.txt")
            # Admission may run asynchronously; read the original rejected request.
            reject_id = rejected.get("operation_id")
            deadline = time.monotonic() + 30
            while rejected["status"] in {"accepted", "busy"} and time.monotonic() < deadline:
                time.sleep(0.2)
                rejected = call("operation", "--operation-id", reject_id)
            self.assertEqual(rejected.get("reason"), "invalid_runtime_payload", rejected)
            self.assertEqual(call("status")["status"], "not_installed")
            accepted = call("install", "--operation-id", operation, "--payload", str(source),
                            "--sha256", digest, "--probe-workspace", "project", "--probe-path", "hello.txt")
            deadline = time.monotonic() + 120
            while accepted["status"] in {"accepted", "busy"} and time.monotonic() < deadline:
                time.sleep(0.5)
                accepted = call("operation", "--operation-id", operation)
            self.assertEqual(accepted["status"], "completed", accepted)
            self.assertEqual(accepted["operation"]["operation_id"], operation)
            # The exact original ID reads its receipt; a new install never adopts it.
            repeated = call("install", "--operation-id", operation, "--payload", str(source),
                            "--sha256", digest, "--probe-workspace", "project", "--probe-path", "hello.txt")
            self.assertEqual(repeated["operation"], accepted["operation"])
            extra_id = uuid.uuid4().hex
            refused = call("install", "--operation-id", extra_id, "--payload", str(source),
                           "--sha256", digest, "--probe-workspace", "project", "--probe-path", "hello.txt")
            self.assertIn(refused.get("reason"), {"service_already_installed", "service_adoption_required"}, refused)
            self.assertFalse((state / "service/operations" / (extra_id + ".json")).exists())
            self.assertEqual(config.read_bytes(), original_config)
            stopped = call("status")
            self.assertEqual(stopped["status"], "stopped", stopped)
            self.assertFalse(stopped["startup"]["enabled"])
            ready = call("start")
            self.assertEqual(ready["status"], "ready", ready)
            observed = call("status")
            self.assertEqual(observed["status"], "ready", observed)
            self.assertEqual(observed["current"], stopped["current"])
            socket_info = (state / "mcp.sock").lstat()
            self.assertTrue(stat.S_ISSOCK(socket_info.st_mode))
            self.assertEqual(socket_info.st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(socket_info.st_mode), 0o600)
            unit = runtime._plist_path(state)
            evidence["native_identity"] = {"socket_uid": socket_info.st_uid,
                "socket_mode": stat.S_IMODE(socket_info.st_mode), "unit_uid": unit.stat().st_uid,
                "unit_mode": stat.S_IMODE(unit.stat().st_mode), "unit": unit.read_text(),
                "unit_sha256": hashlib.sha256(unit.read_bytes()).hexdigest()}
            self.assertEqual(unit.stat().st_uid, os.getuid())
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
            self.save_evidence()
            denied = self.tool(state, "start_execution", {"machine": "local", "execution_id": uuid.uuid4().hex,
                "cwd": str(project), "argv": [str(source / "python/bin/python3"), "-c", "pass"]})
            self.assertTrue(denied.is_error, denied)
            self.assertEqual(denied.structured_content["error"]["code"], "execution_disabled")
            if sys.platform == "linux":
                # Explicit Full with commands only: the packaged service must
                # preserve this native worker through supervisor loss/restart.
                candidate = root / "full.toml"
                candidate.write_text(original_config.decode().replace('local_access = "read_only"',
                    'local_access = "full_access"').replace('execution_enabled = false', 'execution_enabled = true'))
                candidate.chmod(0o600)
                update_id = uuid.uuid4().hex
                update = call("update", "--operation-id", update_id, "--payload", str(source), "--sha256", digest,
                    "--candidate-config", str(candidate), "--config-sha256", hashlib.sha256(config.read_bytes()).hexdigest(),
                    "--candidate-sha256", hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "--expected-selection-sha256", observed["selection_sha256"])
                deadline = time.monotonic() + 120
                while update["status"] in {"accepted", "busy"} and time.monotonic() < deadline:
                    time.sleep(0.5)
                    update = call("operation", "--operation-id", update_id)
                self.assertEqual(update["status"], "completed", update)
                original_config = candidate.read_bytes()
                settings = load(config)
                self.assertEqual(settings.machine("local").local_access, "full_access")
                self.assertTrue(settings.machine("local").execution_enabled)
                self.assertFalse(settings.machine("local").workspace_write_enabled)
                observed = call("status")
                self.assertEqual(observed["status"], "ready", observed)
                execution_id = uuid.uuid4().hex
                marker = project / "worker-starts.txt"
                worker = self.tool(state, "start_execution", {"machine": "local", "execution_id": execution_id,
                    "cwd": str(project), "argv": [observed["entry"]["python"], "-c",
                    "from pathlib import Path; import time; "
                    "p=Path('worker-starts.txt'); p.write_text(p.read_text()+'start\\n' if p.exists() else 'start\\n'); "
                    "time.sleep(180)"], "timeout_seconds": 240})
                self.assertFalse(worker.is_error, worker)
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(marker.read_text(), "start\n")
                # SIGKILL prevents supervisor cleanup: the MCP child must still exit.
                supervisor, child = observed["supervisor_pid"], observed["pid"]
                runtime._native(state)  # Verify owned PID before this test-only crash.
                os.kill(supervisor, signal.SIGKILL)
                deadline = time.monotonic() + 8
                while runtime.socket_alive(state / "mcp.sock") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertFalse(runtime.socket_alive(state / "mcp.sock"), child)
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    observed = call("status")
                    if observed["status"] == "ready":
                        break
                    time.sleep(0.5)
                self.assertEqual(observed["status"], "ready", observed)
                self.assertNotEqual(observed["supervisor_pid"], supervisor)
            restarted = subprocess.run(command[:-3] + ["service", "restart"], cwd="/",
                                       capture_output=True, text=True, timeout=60)
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(call("status")["status"], "ready")
            if sys.platform == "linux":
                worker = self.tool(state, "read_execution", {"machine": "local", "execution_id": execution_id})
                self.assertFalse(worker.is_error, worker)
                self.assertEqual(worker.structured_content["status"], "running", worker)
                self.assertEqual(marker.read_text(), "start\n")
            stopped = call("stop")
            self.assertEqual(stopped["status"], "stopped", stopped)
            self.assertFalse(stopped["loaded"])
            self.assertFalse(runtime.socket_alive(state / "mcp.sock"))
            self.assertEqual(call("resume")["status"], "stopped")
            stopped_restart = subprocess.run(command[:-3] + ["service", "restart"], cwd="/",
                                             capture_output=True, text=True, timeout=60)
            evidence["stopped_restart"] = {"exit_code": stopped_restart.returncode,
                "stdout": stopped_restart.stdout, "stderr": stopped_restart.stderr}
            self.assertEqual(stopped_restart.returncode, 0, stopped_restart.stderr)
            self.assertEqual(call("status")["status"], "stopped")
            if sys.platform == "linux":
                # A native start must not override the durable Stop intent.
                native_start = runtime.systemd.command("start", runtime._plist_path(state).name)
                self.assertEqual(native_start.returncode, 0, native_start.stderr)
                self.assertEqual(call("status")["status"], "stopped")
                self.assertFalse(runtime.socket_alive(state / "mcp.sock"))
            self.assertEqual(call("operation", "--operation-id", operation)["operation"], accepted["operation"])
            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual((state / "service/service.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(call("start")["status"], "ready")
            if sys.platform == "linux":
                worker = self.tool(state, "read_execution", {"machine": "local", "execution_id": execution_id})
                self.assertFalse(worker.is_error, worker)
                self.assertEqual(worker.structured_content["status"], "running", worker)
                self.assertEqual(marker.read_text(), "start\n")
                cancelled = self.tool(state, "cancel_execution", {"machine": "local", "execution_id": execution_id})
                self.assertFalse(cancelled.is_error, cancelled)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    worker = self.tool(state, "read_execution", {"machine": "local", "execution_id": execution_id,
                                                                 "wait_seconds": 0.25})
                    self.assertFalse(worker.is_error, worker)
                    if worker.structured_content["status"] == "cancelled":
                        break
                self.assertEqual(worker.structured_content["status"], "cancelled", worker)
            self.assertEqual(call("stop")["status"], "stopped")
            # Remove only this unique test's definition after verified unload.
            evidence["stage"] = "cleanup"
            runtime._plist_path(state).unlink()
            if sys.platform == "linux":
                self.assertEqual(runtime.systemd.command("daemon-reload").returncode, 0)
            # Retained payload directories are sealed read-only during install.
            for directory, _, _ in os.walk(root, followlinks=False):
                path = Path(directory)
                path.chmod(path.stat().st_mode | 0o200)
            evidence["result"] = "PASS"
            evidence["seconds"] = round(time.monotonic() - started, 2)
            self.save_evidence()
            shutil.rmtree(root)
            print(json.dumps({"native_first_use": "passed", "seconds": round(time.monotonic() - started, 2),
                              "steps": [{k: v for k, v in step.items() if k not in {"reply", "stdout", "stderr"}}
                                        for step in evidence["steps"]]}), flush=True)
        except BaseException as error:
            # Keep the exact state and operation after failure; never replay it.
            evidence["seconds"] = round(time.monotonic() - started, 2)
            evidence["error"] = {"type": type(error).__name__, "detail": str(error)}
            evidence["result"] = "FAIL"
            self.save_evidence()
            (root / "test-result.json").write_text(json.dumps(evidence, indent=2))
            print(f"Native first-use failed; original evidence retained at {self.evidence_path}", flush=True)
            raise


if __name__ == "__main__":
    unittest.main()
