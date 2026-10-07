"""Tests for app behavior at the resilience layer."""

import json
import sys
import unittest
import uuid
from openmachine import execution
from openmachine.execution.records import TERMINAL
from openmachine.bootstrap import create_application
from openmachine.foundation.state import lease
from test.support.continuity_cases import ContinuityCaseFixture


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_original_execution_link_and_legacy_receipts_do_not_replay(self):
        self.save()
        app = create_application(self.config, self.state)
        identity = uuid.uuid4().hex
        code = 'from pathlib import Path; p=Path("count.txt"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        args = ("desktop", identity, str(self.work), [sys.executable, "-c", code])
        result = self.wait(app.start_execution(*args, task_id="task-one"))
        self.assertEqual((result["status"], result["task_id"]), ("succeeded", "task-one"))
        self.assertEqual(execution.list_records(self.config, self.state, "desktop")["executions"][0]["task_id"], "task-one")
        (self.state / "tasks/index.json").write_text("unavailable task metadata fixture")
        self.assertEqual(app.start_execution(*args, task_id="task-one")["status"], "succeeded")
        self.fault("execution_conflict", app.start_execution, "desktop", identity, str(self.work),
                   [sys.executable, "-c", "print('changed')"], task_id="task-one")
        new_id = uuid.uuid4().hex
        self.fault("corrupt_tasks", app.start_execution, "desktop", new_id, str(self.work),
                   [sys.executable, "-c", code], task_id="task-one")
        self.assertFalse((self.state / "executions" / new_id).exists())
        receipt = self.state / "executions" / identity / "record.json"
        stored = json.loads(receipt.read_text())
        del stored["request"]["task_id"]
        receipt.write_text(json.dumps(stored))
        original = receipt.read_bytes()
        self.assertEqual(app.start_execution(*args)["status"], "succeeded")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual((self.work / "count.txt").read_text(), "1")

    def test_application_releases_admission_before_background_work_finishes(self):
        app = create_application(self.config, self.state)
        identity = uuid.uuid4().hex
        code = ('from pathlib import Path\nimport time\n'
                'print("waiting", flush=True)\n'
                'while not Path("release").exists(): time.sleep(0.02)\n')
        started = app.start_execution("desktop", identity, str(self.work), [sys.executable, "-c", code], 10)
        try:
            with lease(self.state / "locks/lifecycle.lock"):
                current = app.read_execution("desktop", identity)
                self.assertNotIn(current["status"], TERMINAL | {"outcome_unknown"})
                self.assertEqual(current["created_at"], started["created_at"])
        finally:
            (self.work / "release").touch()
            final = self.wait(started)
        self.assertEqual(final["status"], "succeeded")
        self.config.machines["desktop"].execution_enabled = False
        self.assertEqual(app.cancel_execution("desktop", identity)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
