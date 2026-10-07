"""Shared disposable fixtures; no test cases or test discovery."""

import ast
from graphlib import CycleError, TopologicalSorter
from importlib.resources import files
from importlib.util import resolve_name
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from openmachine import execution, workspace
from openmachine.execution import logs
from openmachine.execution.records import TERMINAL
from openmachine.workspace.git import MAX_READ
from openmachine.config import Settings, Workspace, load
from openmachine.foundation.sanitize import Fault, redact



class RuntimeCaseFixture(object):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({
            "schema_version": 1, "machines": {"my-laptop": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "my-laptop", "root": str(self.work)}},
        })

    def start(self, code, *, execution_id=None, cwd=None, **kwargs):
        return execution.start(self.config, self.state, "my-laptop", execution_id or uuid.uuid4().hex,
                               str(cwd or self.work), [sys.executable, "-c", code], **kwargs)

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "my-laptop", result["execution_id"])
            if result["status"] in TERMINAL | {"outcome_unknown"}:
                return result
            time.sleep(0.03)
        self.fail(f"Execution did not finish: {result['execution_id']}")

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.work), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def _repository(self):
        self._git("init", "-q")
        (self.work / "code.py").write_text("value = 1\n")
        self._git("add", "code.py")
        self._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
