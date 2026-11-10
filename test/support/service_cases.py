"""Shared disposable fixtures; no test cases or test discovery."""

import json
from unittest.mock import patch

from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import maintenance
from openmachine.service import state as service_state
from test.support.payload_cases import PayloadCaseFixture

FIRST, SECOND = "1" * 32, "2" * 32


class Handoff(Exception):
    """Stands in for the exec into the incoming Core, which continues the operation in a new process."""


class ServiceCaseFixture(PayloadCaseFixture):
    """One real retained payload per operation; venv and pip work is replaced by a recorded installation."""

    def setUp(self):
        super().setUp()
        self.spawned = []
        self.enterContext(patch.object(maintenance, "_spawn", side_effect=lambda state, identity, lock, python:
                                       self.spawned.append((identity, python))))

    def manage(self, action, **inputs):
        return maintenance.dispatch(self.config, self.state, action, **inputs)

    def prepare(self, state, retained, sha256):
        directory = private_directory(state / "service/versions" / sha256)
        # What the manage entry executes: here it reports its arguments.
        (directory / "bin").mkdir()
        (directory / "bin/python").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        (directory / "bin/python").chmod(0o700)
        save_json(directory / "installation.json", {
            "identity": sha256, "version": "0.2.8", "protocol": service_state.PROTOCOL,
            "state_contract": service_state.STATE_CONTRACT, "management_contract": service_state.MANAGEMENT_CONTRACT,
            "payload_sha256": sha256, "commit": "a" * 40})
        return {"identity": sha256, "version": "0.2.8"}

    def complete(self, identity, prepare=None):
        """Run an admitted operation the way its process and the incoming Core's process would."""
        with lease(self.state / "service/operation.lock") as lock:
            with patch.object(maintenance.os, "execve", side_effect=Handoff), self.assertRaises(Handoff):
                maintenance.run_operation(self.state, identity, lock.fileno())
            with patch.object(maintenance.install, "prepare", side_effect=prepare or self.prepare):
                maintenance.run_operation(self.state, identity, lock.fileno())
        return maintenance._read(self.state, identity)

    def install(self):
        self.assertEqual(self.manage("install", identity=FIRST, source=self.source, sha256=self.digest)["status"], "accepted")
        return self.complete(FIRST)

    def next_payload(self):
        (self.source / "THIRD_PARTY_NOTICES.md").write_text("Next fixture notices.\n")
        return self.seal()

    def record(self, identity):
        return json.loads(maintenance._operation_path(self.state, identity).read_text())
