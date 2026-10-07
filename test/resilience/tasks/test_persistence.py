"""Tests for tasks behavior at the resilience layer."""

import json
import subprocess
import sys
import unittest
from openmachine import tasks
from test.support.tasks_cases import StructuredTaskCaseFixture


class StructuredTaskTests(StructuredTaskCaseFixture, unittest.TestCase):
    def test_canonical_creation_restart_and_truthful_projection(self):
        value = self.create()["record"]
        expected = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((value["revision"], value["plan"]["revision"]), (1, 1))
        self.assertEqual(value["operations"], [])
        self.assertIsNone(value["control"]["owner"])
        self.assertEqual(value["handoff"]["reason"], "plan_ready")
        result = subprocess.run([sys.executable, "-I", "-c",
            'import json,sys; from pathlib import Path; from openmachine import tasks; print(json.dumps(tasks.read(Path(sys.argv[1]),"structured-one")))',
            str(self.state)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["record"], value)
        self.assertEqual(observed["index_status"], "current")
        self.assertEqual(observed["observations"][0]["state"], "unknown")
        self.assertTrue(observed["support"]["supervised_admission"])
        self.assertFalse(any(observed["support"][key] for key in ("runtime_recovery", "events_delivery")))
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertFalse((self.state / "executions").exists())
        index = json.loads((self.state / "tasks/index.json").read_text())
        self.assertEqual(set(index["tasks"]["structured-one"]), {"owner", "title", "anchors", "revision", "sha256"})

    def test_real_process_loss_on_both_sides_of_directory_publication(self):
        code = '''import json,os,sys
from pathlib import Path
from openmachine.tasks import store
request=json.load(sys.stdin)
rename=store.os.rename
def interrupt(source,destination):
    if sys.argv[2]=="before": os._exit(91)
    rename(source,destination)
    os._exit(91)
store.os.rename=interrupt
store.create(Path(sys.argv[1]),request.pop("task_id"),**request)
'''
        for point in ("before", "after"):
            state = self.root / ("crash-" + point)
            result = subprocess.run([sys.executable, "-I", "-c", code, str(state), point],
                                    input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 91, result.stderr)
            path = state / "tasks/structured-one/task.json"
            self.assertEqual(path.exists(), point == "after")
            if point == "after":
                raw = path.read_bytes()
                self.assertEqual(tasks.read(state, "structured-one")["index_status"], "stale")
            else:
                self.assertFalse(path.parent.exists())
                self.assertEqual(tasks.find(state)["tasks"], [])
            request = dict(self.request)
            observed = tasks.create(state, request.pop("task_id"), **request)
            self.assertEqual(observed["status"], "unchanged" if point == "after" else "created")
            if point == "after":
                self.assertEqual(path.read_bytes(), raw)
            self.assertFalse((state / "executions").exists())

    def test_rebuild_pages_survive_restart_and_block_new_writes_until_complete(self):
        self.create()
        self.seed_canonical(51)
        raw = self.path.read_bytes()
        first = self.app.rebuild_task_index()
        self.assertEqual((first["records_checked"], first["coverage"]), (50, "incomplete"))
        self.assertEqual(self.app.find_tasks()["coverage"], "incomplete")
        self.fault("task_index_incomplete", self.create, {**self.request, "task_id": "new-task"})
        self.fault("task_index_incomplete", self.app.save_task, "new-legacy", 0, "No write", "No work.", [])
        self.fault("task_index_changed", self.app.rebuild_task_index)
        self.fault("task_index_changed", self.app.rebuild_task_index, first["generation"] - 1)
        configuration = self.root / "rebuild.toml"
        configuration.write_text('schema_version=1\n[machines.desktop]\ntransport="local"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(configuration),
            "--state-dir", str(self.state), "rebuild-task-index", "--expected-generation", str(first["generation"])],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["coverage"], "complete")
        self.assertEqual(tasks.check_state(self.state)["task_records"], 52)
        self.assertEqual(self.path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
