"""Fresh-process import boundaries and unchanged CLI behavior."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import openmachine


LIGHTWEIGHT = ("pydantic", "openmachine.config", "openmachine.execution", "openmachine.tasks",
               "openmachine.workspace", "openmachine.service.checks", "openmachine.service.install",
               "openmachine.service.transition", "integrations.ssh", "mcp", "httpx2")
CLI = '''
import contextlib
import io
from openmachine.cli import main
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        code = main()
    except SystemExit as error:
        code = error.code
print(json.dumps({"code": code, "stdout": out.getvalue(), "stderr": err.getvalue(),
                  "modules": sorted(sys.modules)}))
'''


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-startup-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config, self.state = self.root / "config.toml", self.root / "state"

    def run_child(self, body, *, blocked=(), arguments=()):
        # The installed runner supplies the installed package here, never the checkout.
        product_root = str(Path(openmachine.__file__).resolve().parent.parent)
        script = '''
import json
import sys
root, blocked, arguments, body = sys.argv[1:]
sys.path.insert(0, root)
blocked = tuple(json.loads(blocked))
class ImportBoundary:
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Unexpected startup import: " + fullname)
sys.meta_path.insert(0, ImportBoundary())
def audit(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
        raise AssertionError("Unexpected external activity: " + event)
sys.addaudithook(audit)
sys.argv = ["openmachine", *json.loads(arguments)]
exec(body)
'''
        result = subprocess.run([sys.executable, "-I", "-c", script, product_root,
                                 json.dumps(blocked), json.dumps(arguments), body],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_version_help_and_parser_errors_need_no_runtime_dependencies(self):
        for arguments, code in [(["--version"], 0), (["--help"], 0),
                                (["manage", "--help"], 0), (["manage", "status"], 2)]:
            with self.subTest(arguments=arguments):
                result = self.run_child(CLI, blocked=LIGHTWEIGHT, arguments=arguments)
                self.assertEqual(result["code"], code)
                if arguments == ["--version"]:
                    self.assertEqual(result["stdout"], f"{openmachine.__version__} (unknown)\n")
                elif code == 0:
                    self.assertIn("usage: openmachine", result["stdout"])
                else:
                    self.assertIn("manage requires explicit --config and --state-dir", result["stderr"])
        self.assertFalse(self.state.exists())

    def test_management_status_keeps_validation_without_loading_lifecycle(self):
        arguments = ["--config", str(self.config), "--state-dir", str(self.state), "manage"]
        # Only the absent/corrupt-state paths are simulated on non-macOS CI;
        # no native job, socket, service process or installed state is consulted.
        host = 'import os\nsys.platform = "darwin"\nos.getuid = lambda: 501\n'
        cases = [(host, arguments + ["status"], 0, "not_installed", None),
                 (host, arguments + ["--contract", "99", "status"], 1, "incompatible", "incompatible_management"),
                 (host, ["--config", "relative.toml", "--state-dir", str(self.state), "manage", "status"],
                  1, "unknown", "invalid_service_path"),
                 ('sys.platform = "linux"\n', arguments + ["status"], 1, "unknown", "unsupported_service_host")]
        for setup, argv, code, status, reason in cases:
            with self.subTest(status=status, reason=reason):
                result = self.run_child(setup + CLI, blocked=LIGHTWEIGHT, arguments=argv)
                value = json.loads(result["stdout"])
                self.assertEqual((result["code"], value["status"], value.get("reason")), (code, status, reason))
                self.assertEqual(value["management_contract"], 1)
                self.assertEqual(result["stderr"], "")
        self.assertFalse(self.state.exists())
        directory = self.state / "service"
        directory.mkdir(parents=True)
        record = directory / "service.json"
        record.write_text("invalid retained selection")
        record.chmod(0o600)
        result = self.run_child(host + CLI, blocked=LIGHTWEIGHT, arguments=arguments + ["status"])
        self.assertEqual(result["code"], 1)
        self.assertEqual(json.loads(result["stdout"])["reason"], "service_state_invalid")
        self.assertEqual(record.read_text(), "invalid retained selection")

    def test_configuration_commands_still_load_and_validate(self):
        args = ["--config", str(self.config), "--state-dir", str(self.state)]
        result = self.run_child(CLI, arguments=args + ["init", "--workspace", str(self.root)])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertIn("pydantic", result["modules"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        original = self.config.read_bytes()
        result = self.run_child(CLI, arguments=args + ["init", "--workspace", str(self.root)])
        self.assertEqual(result["code"], 1)
        self.assertEqual(self.config.read_bytes(), original)
        result = self.run_child(CLI, arguments=args + ["doctor"])
        self.assertEqual(result["code"], 0, result["stderr"])
        self.assertEqual(json.loads(result["stdout"])["configuration"], "valid")
        self.config.write_text("not valid TOML")
        result = self.run_child(CLI, arguments=args + ["doctor"])
        self.assertEqual(result["code"], 1)
        self.assertIn("error", json.loads(result["stderr"]))
        self.assertFalse(self.state.exists())

    def test_service_exports_keep_original_callable_identity(self):
        result = self.run_child('''
from importlib import import_module
from openmachine import service
owners = {"check_state": "checks", "probe_service": "checks", "prepare_payload": "install",
          "socket_alive": "runtime", "status": "runtime", "supervise": "runtime", "operate": "transition",
          "PROTOCOL": "state", "STATE_CONTRACT": "state", "guard_writes": "state", "running_commit": "state"}
assert set(owners) <= set(dir(service))
for name, owner in owners.items():
    value = getattr(service, name)
    assert value is getattr(import_module("openmachine.service." + owner), name), name
    assert service.__dict__[name] is value, name
assert not hasattr(service, "unknown_export")
print(json.dumps({"exports": len(owners)}))
''')
        self.assertEqual(result["exports"], 11)

    def test_check_state_still_uses_actual_retained_state_readers(self):
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        result = self.run_child('''
from pathlib import Path
from openmachine.service import check_state
value = check_state(Path(sys.argv[1]), Path(sys.argv[2]))
assert all(name in sys.modules for name in ("openmachine.execution", "openmachine.tasks", "openmachine.workspace"))
print(json.dumps(value))
''', arguments=[str(self.config), str(self.state)])
        self.assertEqual(result["protocol"], 1)
        self.assertEqual(result["execution_records"], 0)
        self.assertTrue(result["sqlite_runtime"]["supported"])

    def test_readiness_loads_transport_but_not_installation_or_history(self):
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        result = self.run_child('''
from pathlib import Path
from openmachine.foundation.sanitize import Fault
from openmachine.service import probe_service
try:
    probe_service(Path(sys.argv[2]), Path(sys.argv[1]), {"workspace": "missing", "path": "file"}, "0.2.23")
except Fault as error:
    assert error.code == "invalid_service_probe", error.code
else:
    raise AssertionError("The invalid configured probe was accepted")
assert "mcp" in sys.modules and "httpx2" in sys.modules
print(json.dumps({"validated": True}))
''', arguments=[str(self.config), str(self.state)], blocked=("openmachine.tasks", "openmachine.execution",
                "openmachine.workspace", "openmachine.service.install", "openmachine.service.transition", "integrations.ssh"))
        self.assertTrue(result["validated"])
