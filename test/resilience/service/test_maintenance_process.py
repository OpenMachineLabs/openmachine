"""Tests for service behavior at the resilience layer."""

import json
import os
from pathlib import Path
import shutil
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


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("OM_NATIVE_FIRST_USE") == "1",
                     "opt-in macOS first-use test with an explicitly trusted payload")
class NativeFirstUseTests(unittest.TestCase):
    def test_isolated_install_start_ready_and_stop(self):
        source = Path(os.environ["OM_NATIVE_PAYLOAD"]).resolve(strict=True)
        digest = os.environ["OM_NATIVE_PAYLOAD_SHA256"]
        payload.verify(source, digest)
        root = Path(tempfile.mkdtemp(prefix="om-first-", dir="/tmp")).resolve()
        config, state, project = root / "config.toml", root / "state", root / "project"
        project.mkdir()
        (project / "hello.txt").write_text("Isolated first-use readiness check.\n")
        operation = uuid.uuid4().hex
        command = [str(source / "python/bin/python3"), "-I", "-B", "-m", "openmachine.cli",
                   "--config", str(config), "--state-dir", str(state), "manage", "--contract", "1"]
        evidence = {"fixture": str(root), "operation_id": operation, "steps": []}
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
            self.assertIn(result.returncode, (0, 1), result.stderr)
            self.assertEqual(observed.get("management_contract"), 1, observed)
            return observed

        try:
            self.assertEqual(call("status")["status"], "not_installed")
            self.assertEqual(call("configure", "--workspace", str(project))["status"], "configured")
            accepted = call("install", "--operation-id", operation, "--payload", str(source),
                            "--sha256", digest, "--probe-workspace", "project", "--probe-path", "hello.txt")
            deadline = time.monotonic() + 120
            while accepted["status"] in {"accepted", "busy"} and time.monotonic() < deadline:
                time.sleep(0.5)
                accepted = call("operation", "--operation-id", operation)
            self.assertEqual(accepted["status"], "completed", accepted)
            self.assertEqual(accepted["operation"]["operation_id"], operation)
            stopped = call("status")
            self.assertEqual(stopped["status"], "stopped", stopped)
            self.assertFalse(stopped["startup"]["enabled"])
            ready = call("start")
            self.assertEqual(ready["status"], "ready", ready)
            observed = call("status")
            self.assertEqual(observed["status"], "ready", observed)
            self.assertEqual(observed["current"], stopped["current"])
            stopped = call("stop")
            self.assertEqual(stopped["status"], "stopped", stopped)
            self.assertFalse(stopped["loaded"])
            self.assertFalse(runtime.socket_alive(state / "mcp.sock"))
            # Remove only this unique test's definition after verified unload.
            evidence["stage"] = "cleanup"
            runtime._plist_path(state).unlink()
            # Retained payload directories are sealed read-only during install.
            for directory, _, _ in os.walk(root, followlinks=False):
                path = Path(directory)
                path.chmod(path.stat().st_mode | 0o200)
            shutil.rmtree(root)
            print(json.dumps({"native_first_use": "passed", "seconds": round(time.monotonic() - started, 2),
                              "steps": [{k: v for k, v in step.items() if k not in {"reply", "stdout", "stderr"}}
                                        for step in evidence["steps"]]}), flush=True)
        except BaseException as error:
            # Keep the exact state and operation after failure; never replay it.
            evidence["seconds"] = round(time.monotonic() - started, 2)
            evidence["error"] = {"type": type(error).__name__, "detail": str(error)}
            (root / "test-result.json").write_text(json.dumps(evidence, indent=2))
            print(f"Native first-use failed; original evidence retained at {root / 'test-result.json'}", flush=True)
            raise


if __name__ == "__main__":
    unittest.main()
