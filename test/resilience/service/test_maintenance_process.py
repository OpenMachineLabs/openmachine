"""The real maintenance process: inherited lock handoff, and an opt-in native install."""

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
from openmachine.service import maintenance, payload
from test.support.service_cases import FIRST, ServiceCaseFixture


class MaintenanceProcessTests(ServiceCaseFixture, unittest.TestCase):
    def test_detached_process_keeps_the_inherited_lock_and_the_original_record(self):
        self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)
        maintenance._save(self.state, self.record(FIRST), "preparing")
        # This interrupted phase admits no effect: the real module validates the inherited lock and exits.
        original = maintenance._operation_path(self.state, FIRST).read_bytes()
        with lease(self.state / "service/operation.lock") as lock:
            process = subprocess.Popen([sys.executable, "-I", "-m", "openmachine.service.maintenance", str(self.state),
                                        FIRST, str(lock.fileno())], cwd="/", stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(lock.fileno(),),
                                       start_new_session=True)
            self.assertEqual(process.communicate(timeout=10), (b"", b""))
            self.assertEqual(process.returncode, 0)
            self.assertEqual(self.manage("continue", identity=FIRST)["status"], "busy")
        self.assertEqual(maintenance._operation_path(self.state, FIRST).read_bytes(), original)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("OM_NATIVE_FIRST_USE") == "1",
                     "opt-in macOS first-use test with an explicitly trusted payload")
class NativeFirstUseTests(unittest.TestCase):
    def test_isolated_configure_install_and_access_capabilities(self):
        source = Path(os.environ["OM_NATIVE_PAYLOAD"]).resolve(strict=True)
        digest = os.environ["OM_NATIVE_PAYLOAD_SHA256"]
        manifest = payload.verify(source, digest)
        root = Path(tempfile.mkdtemp(prefix="om-first-", dir="/tmp")).resolve()
        config, state, operation = root / "config.toml", root / "state", uuid.uuid4().hex
        command = [str(source / "python/bin/python3"), "-I", "-B", "-m", "openmachine.cli",
                   "--config", str(config), "--state-dir", str(state), "manage", "--contract", "1"]
        evidence = {"fixture": str(root), "operation_id": operation, "steps": []}

        def call(*arguments):
            result = subprocess.run(command + list(arguments), cwd="/", capture_output=True, text=True, timeout=60)
            evidence["steps"].append({"action": arguments[0], "exit_code": result.returncode,
                                      "stdout": result.stdout, "stderr": result.stderr})
            observed = json.loads(result.stdout)
            self.assertEqual(observed.get("management_contract"), 1, observed)
            return observed

        try:
            self.assertEqual(call("status")["status"], "not_installed")
            self.assertEqual(call("configure")["status"], "configured")
            observed = call("install", "--operation-id", operation, "--payload", str(source), "--sha256", digest)
            deadline = time.monotonic() + 180
            while observed["status"] in {"accepted", "busy"} and time.monotonic() < deadline:
                time.sleep(0.5)
                observed = call("operation", "--operation-id", operation)
            self.assertEqual(observed["status"], "completed", observed)
            status = call("status")
            self.assertEqual((status["status"], status["product_version"]), ("installed", manifest["core"]["version"]))
            capabilities = subprocess.run([status["entry"]["access_python"], "-I", "-m", "openmachine.access", "capabilities"],
                                          cwd="/", capture_output=True, text=True, timeout=30)
            self.assertEqual(json.loads(capabilities.stdout)["version"], manifest["core"]["version"])
            # Retained payload directories are sealed read-only during install.
            for directory, _, _ in os.walk(root, followlinks=False):
                Path(directory).chmod(Path(directory).stat().st_mode | 0o200)
            shutil.rmtree(root)
        except BaseException as error:
            # Keep the exact state and operation after failure; never replay it.
            evidence["error"] = {"type": type(error).__name__, "detail": str(error)}
            (root / "test-result.json").write_text(json.dumps(evidence, indent=2))
            print(f"Native first-use failed; original evidence retained at {root / 'test-result.json'}", flush=True)
            raise


if __name__ == "__main__":
    unittest.main()
