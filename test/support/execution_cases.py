"""Shared disposable fixtures; no test cases or test discovery."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import signal
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from openmachine import execution
from openmachine.execution import api, logs
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from test.support.execution import ExecutionFixture



class ExecutionCaseFixture(ExecutionFixture):
    def start(self, code, *, execution_id=None, cwd=None, **options):
        identity = execution_id or uuid.uuid4().hex
        self.ids.append(identity)
        return execution.start(self.config, self.state, "local", identity, str(cwd or self.work),
                               [sys.executable, "-c", code], timeout_seconds=5, **options)
