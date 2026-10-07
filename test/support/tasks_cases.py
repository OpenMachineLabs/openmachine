"""Shared disposable fixtures; no test cases or test discovery."""

import hashlib
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import execution, tasks
from openmachine.tasks import store as task_store
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture, task_contract, terminal_task_fixture



class StructuredTaskCaseFixture(TaskFixture):
    def seed_canonical(self, count, record=None):
        from openmachine.tasks import records, transitions
        for number in range(count):
            request = task_contract(self.context, f"task-{number:03d}")
            value = records.new_record(records.normalize_create(request), time.time_ns() // 1000000)
            if record is not None:
                value = json.loads(json.dumps(record))
                value["task_id"] = request["task_id"]
            raw = records.canonical(value)
            self.assertEqual(records.decode(raw), value)
            directory = self.state / "tasks" / request["task_id"]
            directory.mkdir(mode=0o700)
            path = directory / "task.json"
            path.write_bytes(raw)
            path.chmod(0o600)
