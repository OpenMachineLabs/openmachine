"""Disposable owned processes; shared helpers contain no test cases."""

from pathlib import Path
import subprocess
import tempfile
import time
from unittest.mock import patch
from openmachine import execution
from openmachine.config import Settings, Workspace
from openmachine.execution import api
from openmachine.execution.records import TERMINAL
from openmachine.foundation.sanitize import Fault


class ExecutionFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-e1-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({
            "schema_version": 1, "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "local", "root": str(self.work)}},
        })
        self.ids = []
        self.addCleanup(self.stop_owned)

    def stop_owned(self):
        for identity in self.ids:
            try:
                result = execution.read(self.config, self.state, "local", identity)
                if result["status"] not in TERMINAL:
                    execution.cancel(self.config, self.state, "local", identity)
                    self.until(identity, lambda item: item["status"] in TERMINAL)
            except Fault:
                # Corruption tests retain their intentionally unreadable disposable record.
                pass

    def until(self, identity, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "local", identity)
            if predicate(result):
                return result
            time.sleep(0.02)
        self.fail(f"Original execution did not reach the expected observation: {identity}")

    def finish(self, result):
        return self.until(result["execution_id"], lambda item: item["status"] in TERMINAL)

    def output(self, identity):
        chunks, offset = [], 0
        while True:
            page = execution.read(self.config, self.state, "local", identity, offset=offset)
            chunks.append(page["output"])
            if page["next_offset"] == offset:
                return "".join(chunks)
            offset = page["next_offset"]

    def assert_owned_cleanup(self):
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["other"] = Workspace(machine="local", root=str(other))
        child = 'import os,signal,time; from pathlib import Path; os.setpgid(0,0); signal.signal(signal.SIGTERM,signal.SIG_IGN); print("child-ready",flush=True); time.sleep(1); Path("escaped").touch(); time.sleep(2)'
        code = 'import signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(4)'
        owned = self.start(code, termination_grace_seconds=0.1)
        sentinel = self.start('import time; from pathlib import Path; time.sleep(0.4); Path("sentinel").touch()', cwd=other)
        self.until(owned["execution_id"], lambda item: "child-ready" in item["output"])
        execution.cancel(self.config, self.state, "local", owned["execution_id"])
        result = self.finish(owned)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["stop"]["force_used"])
        self.assertEqual(result["cleanup"]["status"], "verified")
        self.assertEqual(self.finish(sentinel)["status"], "succeeded")
        time.sleep(1)
        self.assertFalse((self.work / "escaped").exists())
        self.assertTrue((other / "sentinel").exists())

    def injected_worker(self, setup):
        original = subprocess.Popen
        script = 'import sys; from pathlib import Path; import openmachine.execution.worker as w\n' + setup + '\nw.worker(Path(sys.argv[1]),*(int(x) for x in sys.argv[2:]))'
        def launch(argv, *args, **kwargs):
            if len(argv) > 3 and argv[1:4] == ["-I", "-m", "openmachine.execution"]:
                argv = [argv[0], "-I", "-c", script, *argv[4:]]
            return original(argv, *args, **kwargs)
        return patch.object(api.subprocess, "Popen", side_effect=launch)
