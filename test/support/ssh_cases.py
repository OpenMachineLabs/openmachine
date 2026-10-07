"""Shared disposable fixtures; no test cases or test discovery."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile
from integrations.ssh import MAX_MESSAGE, PROTOCOL, bundle, client, endpoint, recovery
from openmachine import __version__, execution, tasks
from openmachine.execution import api as execution_api, logs
from openmachine.config import Settings
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json



class SSHCaseFixture(object):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-ssh-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.identity = "a" * 64
        self.digest = "b" * 64
        self.archive = self.root / (self.digest + ".pyz")
        self.settings = Settings.model_validate({
            "schema_version": 1,
            "machines": {"build-machine": {"transport": "ssh", "ssh_alias": "existing-host",
                          "ssh_worker": str(self.archive), "ssh_identity": self.identity, "execution_enabled": True}},
            "workspaces": {"project": {"machine": "build-machine", "root": str(self.project)}},
        })
        self.request = {"protocol": PROTOCOL, "version": __version__, "identity": self.identity,
                        "execution_enabled": True, "workspace_write_enabled": True, "workspaces": {"project": str(self.project)},
                        "operation": "read_workspace_file", "arguments": {"workspace": "project", "path": "file.txt"}}

    def dispatch(self, operation, **arguments):
        request = self.request | {"operation": operation, "arguments": arguments}
        with patch.object(endpoint, "_identity", return_value=(self.identity, self.root)):
            return endpoint._dispatch(request, self.archive)["result"]

    def response(self, result):
        return 0, json.dumps({"protocol": PROTOCOL, "version": __version__, "identity": self.identity,
                              "worker_sha256": self.digest, "result": result}).encode()
