"""Shared disposable fixtures; no test cases or test discovery."""

import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import tasks
from openmachine.tasks import store as task_store
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import ObserverFixture, task_contract, terminal_task_fixture



class TaskObserverCaseFixture(ObserverFixture):
    def publication_crash(self, phase):
        _, started = self.owned()
        (self.work / "release").touch()
        self.wait(started)
        payload = self.root / "observer-crash.json"
        payload.write_text(json.dumps({"config": self.config.model_dump(mode="json"), "state": str(self.state)}))
        code = '''import json,os,sys,time
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.tasks import store
value=json.loads(Path(sys.argv[1]).read_text())
original=store._replace
def crash(path,raw):
 if path.name=="task.json" and any((item["result"] or {}).get("status")=="succeeded" for item in json.loads(raw)["operations"]):
  if sys.argv[2]=="before": os._exit(67)
  original(path,raw)
  os._exit(68)
 return original(path,raw)
store._replace=crash
app=create_application(Settings.model_validate(value["config"]),Path(value["state"]))
with app.task_observer.running(): time.sleep(12)
os._exit(69)
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=18)
        self.assertEqual(result.returncode, 67 if phase == "before" else 68, result.stderr)
        prior = self.raw()
        with self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
            self.until(lambda: self.app.task_observer._snapshot["state"] == "running")
        final = self.raw()
        self.assertEqual(final["handoff"]["reason"], "result_ready")
        if phase == "after":
            self.assertEqual(final, prior, "A committed result/handoff must not be republished")
            self.assertEqual(self.view()["index_status"], "stale")
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(len(final["operations"]), 1)
        self.assertEqual(self.wait(started)["created_at"], started["created_at"])
