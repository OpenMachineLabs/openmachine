"""Shared disposable fixtures; no test cases or test discovery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
import uuid
from integrations import connections, credentials
from integrations.github import auth, connection
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import GitHubConfig
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import checks
from openmachine.tasks import records
from test.support.tasks import TaskFixture



class ConnectionCaseFixture(TaskFixture):
    def setUp(self):
        super().setUp()
        self.config = self.config.model_copy(update={"github": GitHubConfig(repositories=["Example/repo"])})
        self.enterContext(patch.object(auth, "CLIENT_ID", "Iv1.fixture"))
        self.enterContext(patch.object(credentials, "select", return_value="file"))
        self.app = create_application(self.config, self.state)
        self.actor = {"id": 1, "login": "operator"}
        self.calls, self.tokens, self.comment = [], {}, None
        self.owner = None
        self.device = dict(device_code="private-fixture-device-code", user_code="ABCD-EFGH",
                           verification_uri=auth.VERIFY_URL, expires_in=900, interval=0.01)
        self.start = self.enterContext(patch.object(auth, "start_device", return_value=self.device))
        self.poll = self.enterContext(patch.object(auth, "poll_device", side_effect=lambda *a: (self.pair(), a[-1])))
        self.refresh = self.enterContext(patch.object(auth, "refresh", side_effect=lambda *a: self.pair()))
        self.enterContext(patch.object(auth, "request", side_effect=self.api))
        self.enterContext(patch.object(connection, "request", side_effect=lambda m, e, p=None: self.api("legacy", m, e, p)))
        self.addCleanup(self.stop)

    def pair(self):
        value = credentials.Credential(account_id=0, phase="unverified",
            access_token="access-fixture-" + uuid.uuid4().hex, refresh_token="refresh-fixture-" + uuid.uuid4().hex,
            access_expires_at_ms=connections.now_ms() + 28800000,
            refresh_expires_at_ms=connections.now_ms() + 15897600000)
        self.tokens[value.access_token] = dict(self.actor)
        return value

    def api(self, token, method, endpoint, payload=None):
        self.calls.append((method, endpoint))
        actor = {"id": 1, "login": "operator"} if token == "legacy" else self.tokens[token]
        if endpoint == "user":
            return dict(actor)
        if endpoint == "repos/Example/repo":
            return {"full_name": "Example/repo", "private": True, "permissions": {"push": True}}
        if endpoint == "repos/Example/repo/issues/1":
            return {"number": 1}
        if method == "POST" and endpoint == "repos/Example/repo/issues/1/comments":
            self.comment = dict(id=50, body=payload["body"], user=dict(actor),
                                issue_url="https://api.github.com/repos/Example/repo/issues/1")
            return dict(self.comment)
        if endpoint == "repos/Example/repo/issues/comments/50":
            return dict(self.comment)
        if endpoint.startswith("repos/Example/repo/issues/1/comments?"):
            return [dict(self.comment)] if self.comment else []
        raise AssertionError((method, endpoint))

    def run_owner(self):
        if self.owner is None:
            self.owner = self.app.connections.running()
            self.owner.__enter__()

    def stop(self):
        if self.owner:
            self.owner.__exit__(None, None, None)
            self.owner = None

    def result(self, identity):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.app.read_integration_login(identity)
            if result["status"] != "pending":
                return result
            time.sleep(0.01)
        self.fail("The fixture login did not complete")

    def connect(self, **kwargs):
        self.run_owner()
        identity = uuid.uuid4().hex
        self.app.connect_integration(identity, **kwargs)
        result = self.result(identity)
        self.assertEqual(result["status"], "succeeded", result)
        return self.app.list_integrations(connection_id=result["connection_id"])["connections"][0]

    def stored(self, item):
        value = connections._read(self.state).connections[item["id"]]
        return value, credentials.load(self.state, value.credential_ref, value.credential_store)

    def expire_access(self, item):
        value, pair = self.stored(item)
        credentials.save(self.state, value.credential_ref, value.credential_store,
                         pair.model_copy(update={"access_expires_at_ms": connections.now_ms() + 1000}))
        return value
