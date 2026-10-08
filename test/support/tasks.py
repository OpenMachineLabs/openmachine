"""Disposable task fixtures shared without importing test cases."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import uuid
from openmachine import execution, tasks
from openmachine.execution.records import TERMINAL
from openmachine.config import Settings
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault


def task_contract(context=None, identity="structured-one"):
    """Disposable declared work, not production authority or worker evidence."""
    targets = [] if context is None else [{"machine": "desktop", "workspace": "project",
        "workspace_identity": context["workspace_identity"], "head_sha": context.get("head_sha") or None}]
    return {"task_id": identity, "update_id": "1" * 32, "title": "Retain fixture work", "anchors": ["fixture=structured"],
        "origin": {"request_ref": "fixture:request", "request_sha256": "a" * 64, "objective": "Verify retained work."},
        "contract": {"objective": "Verify retained work.", "scope": "Disposable fixture only.", "exclusions": "No production effects.",
            "acceptance": "Read the same canonical record.", "stop_condition": "Stop after verification.",
            "authority_refs": ["fixture:consent"], "targets": targets, "repositories": [], "tools": ["read_task"],
            "sources": [],
            "expires_at_ms": time.time_ns() // 1000000 + 3600000},
        "stages": [{"id": "verify", "depends_on": [], "result": "Retained record", "verification": "Exact readback",
                    "source_refs": [], "source_sha256": hashlib.sha256(b"[]\n").hexdigest()}]}


def terminal_task_fixture(record):
    """Synthetic retained terminal shape for readers; no terminal writer is exposed."""
    value = json.loads(json.dumps(record))
    value.pop("_facts", None)
    value.pop("_decisions", None)
    value["revision"] += 1
    value["updated_at_ms"] += 1
    value["control"].update(state="completed", current_stage_id=None)
    value["plan"]["stages"][0]["state"] = "accepted"
    value["handoff"] = None
    value["terminal"] = {"outcome": "completed", "finished_at_ms": value["updated_at_ms"], "summary": "Fixture decision.",
        "acceptance": [{"stage_id": "verify", "operation_id": None}], "limitations": "Synthetic reader fixture."}
    value["history"].append({"at_ms": value["updated_at_ms"], "kind": "terminal", "stage_id": None,
                             "operation_id": None, "summary": "Fixture terminal evidence."})
    value["last_update"] = {"id": "f" * 32, "request_sha256": "f" * 64}
    return value


class ContinuityFixture(object):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-continuity-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.config = Settings.model_validate({"schema_version": 1,
            "machines": {"desktop": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "desktop", "root": str(self.work)}}})

    def fault(self, code, operation, *args, **kwargs):
        with self.assertRaises(Fault) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "desktop", result["execution_id"])
            if result["status"] in TERMINAL | {"outcome_unknown"}:
                return result
            time.sleep(0.03)
        self.fail("Owned fixture command did not terminate")


class TaskFixture(ContinuityFixture):
    def setUp(self):
        ContinuityFixture.setUp(self)
        self.app = create_application(self.config, self.state)
        self.context = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.request = task_contract(self.context)
        self.path = self.state / "tasks.db"

    def create(self, request=None):
        result = self.app.create_task(**(self.request if request is None else request))
        return {**result, **tasks.read(self.state, result["task_id"])}

    def view(self):
        view = self.app._task_view("structured-one")
        return view

    def task_bytes(self, task_id="structured-one"):
        return tasks.records.canonical(tasks.read(self.state, task_id)["record"])

    def control_arguments(self, action, **values):
        record = self.view()["record"]
        return dict(task_id=record["task_id"], expected_revision=record["revision"],
            expected_plan_revision=record["plan"]["revision"], expected_epoch=record["control"]["epoch"],
            update_id=uuid.uuid4().hex, transition=dict(action=action, **values))

    def control(self, action, **values):
        result = self.app.control_task(**self.control_arguments(action, **values))
        return {**result, **self.view()}

    def context_for(self, identity):
        record = self.view()["record"]
        return dict(task_id=record["task_id"], task_revision=record["revision"], plan_revision=record["plan"]["revision"],
                    continuation_epoch=record["control"]["epoch"], operation_id=identity)

    def accept_plan(self, *, tools=None, claim=True):
        self.request["contract"]["tools"] = tools or ["read_task", "start_execution", "control_execution", "cancel_execution",
            "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "control_task"]
        result = self.create()
        if claim:
            result = self.control("claim", handoff_id=result["record"]["handoff"]["id"])
        return result

    def write_arguments(self, identity=None, *, content="original\n", path="result.txt"):
        identity = identity or uuid.uuid4().hex
        return dict(machine="desktop", workspace_name="project", path=path, content=content,
            expected_head_sha="", expected_sha256="", change_id=identity,
            expected_workspace_identity=self.context["workspace_identity"], task_context=self.context_for(identity))

    def start_arguments(self, code="print('fixture')", **values):
        identity = uuid.uuid4().hex
        return dict(machine="desktop", execution_id=identity, cwd=str(self.work), argv=[sys.executable, "-c", code],
                    task_context=self.context_for(identity), **values)


class ObserverFixture(TaskFixture):
    def setUp(self):
        TaskFixture.setUp(self)
        from openmachine.app import task_observation
        self.observer_module = task_observation
        # Only unit scheduling is accelerated. Real process/MCP tests use S0's 2s.
        self.interval = patch.object(task_observation, "INTERVAL_MS", 100)
        self.interval.start()
        self.addCleanup(self.interval.stop)
        self.addCleanup(self.app.task_observer.stop)

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except Fault as error:
                if error.code != "resource_busy":
                    raise
            time.sleep(0.02)
        self.fail("The bounded fixture did not reach the required observable state")

    def raw(self, identity="structured-one"):
        return tasks.read(self.state, identity)["record"]

    def owned(self):
        self.accept_plan()
        code = ('from pathlib import Path\nimport time\n'
                'p=Path("counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
                'while not Path("release").exists(): time.sleep(0.02)\n')
        args = self.start_arguments(code, timeout_seconds=15)
        started = self.app.start_execution(**args)
        def finish():
            (self.work / "release").touch()
            self.wait(started)
        self.addCleanup(finish)
        self.until(lambda: (self.work / "counter").exists())
        return args, started

    def seed(self, count=1, configure=None):
        """Synthetic pending receipts for bounded scheduling, not effect proof."""
        identities = []
        for index in range(count):
            identity = f"observer-{index:03}"
            self.app.create_task(**task_contract(self.context, identity))
            def pending(record, now):
                operation_id = f"{index + 1:032x}"
                record["operations"] = [{"id": operation_id, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
                    "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                    "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                        "execution_id": operation_id, "control_seq": None},
                    "admitted_at_ms": now, "state": "admitted", "observed_at_ms": None, "result": None}]
                if configure is not None:
                    configure(record, index)
                record["control"]["state"] = record["plan"]["stages"][0]["state"] = "running"
                record["handoff"] = None
            tasks.update(self.state, identity, pending)
            identities.append(identity)
        return identities

    def fixture_result(self, operation):
        return {"execution_id": operation["id"], "status": "succeeded", "exit_code": 0,
                "cleanup": {"status": "verified"}, "created_at": 1.0}


def write_legacy_task(state, record):
    """Author explicit historical bytes in a disposable fixture, never a runtime writer."""
    from openmachine.foundation.state import private_directory, save_json
    from openmachine.tasks import records, index
    if (state / "tasks.db").exists():
        raise ValueError("Seed legacy bytes before creating a SQLite fixture")
    state = private_directory(state)
    root = private_directory(state / "tasks")
    private_directory(state / "locks")
    (state / "locks/tasks.lock").touch(mode=0o600, exist_ok=True)
    value = records.Record.model_validate({key: item for key, item in record.items() if key not in {"_facts", "_decisions"}}).model_dump()
    raw = records.canonical(value)
    private_directory(root / value["task_id"])
    path = root / value["task_id"] / "task.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    catalog = index.load_index(root) if (root / "index.json").exists() else {
        "format_version": 2, "generation": 0, "coverage": "complete", "next_cursor": None, "tasks": {}}
    catalog["tasks"][value["task_id"]] = index.structured_metadata(value, raw)
    catalog["generation"] += 1
    save_json(root / "index.json", catalog)
    return value


def replace_task_record(state, record):
    """Seed one explicit historical shape inside a disposable SQLite fixture."""
    from openmachine.tasks import database, records, store
    value = {key: item for key, item in record.items() if key not in {"_facts", "_decisions"}}
    value = records.Record.model_validate(value).model_dump()
    with database.connect(state, write=True) as connection:
        identity = value["task_id"]
        for table in ("operation_scopes", "task_events", "task_updates", "operations", "stage_decisions", "tasks"):
            connection.execute(f"DELETE FROM {table} WHERE task_id=?", (identity,))
        store._insert_record(connection, value)
    return value


def write_legacy_checkpoint(state, identity, body, title="Legacy fixture", anchors=None):
    from openmachine.foundation.state import private_directory, save_json
    from openmachine.tasks import index
    if (state / "tasks.db").exists():
        raise ValueError("Seed legacy bytes before creating a SQLite fixture")
    state = private_directory(state)
    root = private_directory(state / "tasks")
    private_directory(state / "locks")
    (state / "locks/tasks.lock").touch(mode=0o600, exist_ok=True)
    private_directory(root / identity)
    raw = body.encode()
    (root / identity / "task.md").write_bytes(raw)
    (root / identity / "task.md").chmod(0o600)
    catalog = index.load_index(root) if (root / "index.json").exists() else {
        "format_version": 2, "generation": 0, "coverage": "complete", "next_cursor": None, "tasks": {}}
    catalog["tasks"][identity] = {"owner": "legacy", "title": title, "anchors": sorted(anchors or []),
        "revision": 1, "sha256": hashlib.sha256(raw).hexdigest()}
    catalog["generation"] += 1
    save_json(root / "index.json", catalog)
