"""Shared disposable fixtures; no test cases or test discovery."""

from contextlib import ExitStack, redirect_stdout
import io
import hashlib
import tomllib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch
import zipfile
from openmachine import __version__, app as application, service, tasks
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import checks as service_checks
from openmachine.service import install as service_install
from openmachine.service import runtime as service_runtime
from openmachine.service import state as service_state
from openmachine.service import transition as service_transition



class ServiceCaseFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-m2-service-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = private_directory(self.root / "state")
        private_directory(self.state / "locks")
        private_directory(self.state / "service")
        self.config = self.root / "config.toml"
        self.config.write_text('schema_version=1\n[machines.local]\ntransport="local"\n')
        self.a, self.b = "a" * 64, "b" * 64
        for identity in (self.a, self.b):
            directory = private_directory(self.state / "service/versions" / identity)
            save_json(directory / "installation.json", {
                "identity": identity, "version": "0.1.0a7", "protocol": 1, "state_contract": 1})
        self.record = {"format": 1, "config": str(self.config), "probe": {"workspace": "project", "path": "AGENTS.md"},
                       "current": self.a, "previous": None, "pending": None,
                       "launch_python": str(Path(sys._base_executable).resolve()), "path_environment": os.defpath}
        self.save(self.record)

    def save(self, record):
        save_json(self.state / "service/service.json", record)

    def assert_fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def machine_fixture(self):
        digest = "c" * 64
        save_json(self.state / "service/versions" / self.a / "installation.json", {
            "identity": self.a, "version": __version__, "protocol": service_state.PROTOCOL,
            "state_contract": service_state.STATE_CONTRACT, "management_contract": 1, "payload_sha256": digest})
        self.enterContext(patch.object(maintenance, "_bound"))
        self.enterContext(patch.object(maintenance.payload, "verify"))
        self.config.write_text(self.config.read_text() + '\n[skills]\nroots=["/retained/skills"]\n')
        return {"target_identity": "d" * 64, "worker_sha256": "e" * 64,
                "version": __version__, "identity_verified": True, "os": "Linux", "architecture": "x86_64"}

    def retired_events_fixture(self):
        # Original format-1 writer shape with synthetic, never-contacted values.
        import base64
        import hashlib
        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        task, principal, url = "retired-task", "native-uid:0", "https://callback.example/retired"
        identity = digest([principal, url, "task.handoff_ready", {"task_id": task}])
        handoff = "a" * 32
        budget = {"started_at_ms": 1000000, "deadline_ms": 4600000, "attempts": 1, "next_attempt_ms": 1001000}
        delivery = {"id": digest([identity, handoff]), "payload": {"task_id": task,
            "handoff_id": handoff, "task_revision": 1, "plan_revision": 1, "reason": "result_ready"},
            "occurred_at_ms": 1000000, "epoch": 1, "budget": budget,
            "accepted_at_ms": None, "read_at_ms": None, "blocker": None}
        subscription = {"id": identity, "generation": "b" * 32, "principal": principal,
            "task_id": task, "url": url, "secret": "whsec_" + base64.b64encode(b"f" * 32).decode(),
            "previous_secret": None, "rotation_until_ms": 0, "verified_at_ms": 1000000,
            "expires_at_ms": 4600000, "delivery": delivery}
        value = {"format": 1, "subscriptions": {identity: subscription},
                 "connections": {"callback.example:443": budget}}
        save_json(self.state / "events.json", value)
        with lease(self.state / "locks/events.lock"):
            pass
        return value, digest(value)

    def activation_mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        verify = stack.enter_context(patch.object(service_install, "_verify_candidate"))
        stack.enter_context(patch.object(service_checks, "_candidate_state", return_value={}))
        stop = stack.enter_context(patch.object(service_runtime, "_stop"))
        start = stack.enter_context(patch.object(service_runtime, "_start", return_value={"status": "ready"}))
        return verify, stop, start

    def managed_fixture(self):
        path = self.state / "service/versions" / self.a / "installation.json"
        save_json(path, service_state._json(path) | {"state_contract": 9})

    def supervisor_mocks(self):
        self.managed_fixture()
        (self.state / "mcp.sock").touch()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stop, clock, child = threading.Event(), [1000.0], Mock(pid=2468)
        child.poll.return_value = None
        stack.enter_context(patch.object(service_runtime.threading, "Event", return_value=stop))
        stack.enter_context(patch.object(service_runtime.time, "time_ns", side_effect=lambda: int(clock[0] * 1e9)))
        stack.enter_context(patch.object(service_runtime.time, "monotonic", side_effect=lambda: clock[0]))
        stack.enter_context(patch.object(service_runtime.random, "uniform", return_value=1))
        stack.enter_context(patch.object(service_runtime, "_verify_files"))
        stack.enter_context(patch.object(service_runtime, "_native", return_value={"loaded": True, "pid": os.getpid()}))
        stack.enter_context(patch.object(service_runtime.os, "getpgrp", return_value=os.getpid()))
        stack.enter_context(patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))))
        launch = stack.enter_context(patch.object(service_runtime, "_launch_mcp", return_value=child))
        def advance(seconds):
            clock[0] += seconds
            if clock[0] > 2000:
                self.fail("The bounded supervisor did not stop")
        stack.enter_context(patch.object(stop, "wait", side_effect=advance))
        return stop, clock, child, launch

    def policy_fixture(self):
        binding = {"identity": self.a, "launch_python": str(Path(sys._base_executable).resolve())}
        record = self.record | {"format": 2, "management": binding,
                                "policy": {"desired": "stopped", "start_at_login": True}}
        for identity in (self.a, self.b):
            path = self.state / "service/versions" / identity / "installation.json"
            save_json(path, service_state._json(path) | {"state_contract": 14, "management_contract": 1})
        self.save(record)
        return record

    def operation_fixture(self, phase="prepared"):
        import hashlib
        private_directory(self.state / "service/operations")
        digest = hashlib.sha256(self.config.read_bytes()).hexdigest()
        record = {"format": 1, "operation_id": "c" * 32, "created_at_ms": 1000, "updated_at_ms": 1000,
                  "phase": phase, "reason": "", "outcome": None,
                  "request": {"action": "update", "config": str(self.config), "source": str(self.root / "payload"),
                              "candidate_config": str(self.config), "payload_sha256": "d" * 64,
                              "config_sha256": digest, "candidate_sha256": digest,
                              "selection_sha256": maintenance._selection(self.state), "workspace": "", "path": ""},
                  "prepared": {"identity": self.b, "payload_sha256": "d" * 64,
                               "runtime": str(self.state / "service/versions" / self.b),
                               "status": "prepared", "version": "0.1.0a7", "launch_python": sys.executable,
                               "path_environment": os.defpath, "selection": "unchanged", "service": "not_started"}}
        if phase in {"accepted", "retaining", "retained", "preparing"}:
            record["prepared"] = None
        if phase == "completed":
            record["outcome"] = {"status": "ready"}
        maintenance._save(self.state, record, phase)
        with lease(self.state / "service/operation.lock"):
            pass
        return maintenance._read(self.state, record["operation_id"])
