"""Behavior checks for StructuredTaskTests."""

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
from support_tasks import TaskFixture, task_contract, terminal_task_fixture


class StructuredTaskTests(TaskFixture, unittest.TestCase):
    def test_canonical_creation_restart_and_truthful_projection(self):
        value = self.create()["record"]
        expected = (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((value["revision"], value["plan"]["revision"]), (1, 1))
        self.assertEqual(value["operations"], [])
        self.assertIsNone(value["control"]["owner"])
        self.assertEqual(value["handoff"]["reason"], "plan_ready")
        result = subprocess.run([sys.executable, "-I", "-c",
            'import json,sys; from pathlib import Path; from openmachine import tasks; print(json.dumps(tasks.read(Path(sys.argv[1]),"structured-one")))',
            str(self.state)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["record"], value)
        self.assertEqual(observed["index_status"], "current")
        self.assertEqual(observed["observations"][0]["state"], "unknown")
        self.assertTrue(observed["support"]["supervised_admission"])
        self.assertFalse(any(observed["support"][key] for key in ("runtime_recovery", "events_delivery")))
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertFalse((self.state / "executions").exists())
        index = json.loads((self.state / "tasks/index.json").read_text())
        self.assertEqual(set(index["tasks"]["structured-one"]), {"owner", "title", "anchors", "revision", "sha256"})

    def test_identical_creation_observes_without_new_grant_checks_or_handoff(self):
        first = self.create()
        raw, inode = self.path.read_bytes(), self.path.stat().st_ino
        with patch.object(self.app, "_validate_task_scope", side_effect=AssertionError("retry validated new work")), \
                patch.object(task_store.time, "time_ns", return_value=(self.request["contract"]["expires_at_ms"] + 1) * 1000000):
            second = self.create()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["record"], second["record"])
        self.fault("task_conflict", self.create, {**self.request, "title": "Different intent"})
        self.fault("task_conflict", self.create, {**self.request, "update_id": "2" * 32})
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (raw, inode))

    def test_strict_contract_and_utf8_budgets_reject_before_task_effects(self):
        import copy
        changes = [lambda r: r["stages"][0].update(state="accepted"),
                   lambda r: r["stages"][0].update(depends_on=["verify"]),
                   lambda r: r["stages"][0].update(source_sha256=hashlib.sha256(b"[]").hexdigest()),
                   lambda r: r["contract"].update(expires_at_ms=True),
                   lambda r: r["contract"].update(expires_at_ms=2**53),
                   lambda r: r["contract"].update(scope="\u00e9" * 513),
                   lambda r: r["contract"].update(acceptance=" "),
                   lambda r: r["origin"].update(objective="bad\0text"),
                   lambda r: r["contract"].update(authority_refs=[]),
                   lambda r: r["stages"].append(copy.deepcopy(r["stages"][0]))]
        for change in changes:
            request = copy.deepcopy(self.request)
            change(request)
            with self.subTest(change=change):
                self.fault("invalid_task_contract", self.create, request)
                self.assertFalse((self.state / "tasks").exists())
        self.fault("invalid_task_contract", tasks.normalize_create, {**self.request, "runtime_state": "succeeded"})
        oversized = {**self.request, "anchors": ["x" * 256 for _ in range(1000)]}
        self.fault("invalid_task_contract", self.create, oversized)
        self.assertFalse((self.state / "tasks").exists())
        self.request["title"] = "\u00e9" * 200
        self.assertEqual(self.create()["record"]["title"], self.request["title"])

    def test_canonical_corruption_never_falls_back_or_overwrites(self):
        import copy
        from openmachine.tasks import records, transitions
        first = self.create()["record"]
        original = self.path.read_bytes()
        values = [records.canonical({**first, **update}) for update in
                  ({"format": True}, {"revision": True}, {"revision": 0}, {"extra": "unknown"},
                   {"task_id": "another"}, {"updated_at_ms": 0})]
        broken = copy.deepcopy(first)
        broken["plan"]["stages"][0]["evidence"] = ["a" * 32]
        values.extend([records.canonical(broken), original.replace(b'"format":3', b'"format":3,"format":3'),
                       b"\xef\xbb\xbf" + original, original.rstrip(b"\n"), original + b" ", b"x" * 65537])
        for raw in values:
            with self.subTest(size=len(raw)):
                self.path.write_bytes(raw)
                for operation in (lambda: self.app.read_task("structured-one"), self.create, self.app.rebuild_task_index):
                    with self.assertRaises(Fault):
                        operation()
                self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_bytes(original)
        legacy = self.path.with_name("task.md")
        legacy.write_text("Not a fallback")
        legacy.chmod(0o600)
        self.fault("corrupt_tasks", self.app.read_task, "structured-one")
        self.fault("corrupt_tasks", self.create)
        self.assertEqual(self.path.read_bytes(), original)

    def test_index_write_failure_preserves_canonical_and_legacy_history(self):
        saved = self.app.save_task("legacy", 0, "Original", "Retain this history.\n", [])
        legacy = self.state / "tasks/legacy/task.md"
        original = legacy.read_bytes()
        with patch.object(task_store, "_write_index", side_effect=OSError("fixture index loss")):
            created = self.create()
        self.assertEqual((created["status"], created["index_status"]), ("created", "stale"))
        raw, inode = self.path.read_bytes(), self.path.stat().st_ino
        self.assertEqual(self.app.read_task("structured-one")["record"], created["record"])
        self.assertEqual(self.create()["status"], "unchanged")
        self.assertEqual(self.app.find_tasks()["coverage"], "incomplete")
        self.assertEqual(self.app.rebuild_task_index()["status"], "current")
        self.assertEqual(self.app.read_task("legacy")["revision"], saved["revision"])
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (raw, inode))
        self.app.save_task("legacy", 1, "Original", "Later explicit checkpoint.\n", [])
        self.assertEqual(self.app.read_task("legacy")["revision"], 2)
        self.fault("task_conflict", self.app.save_task, "structured-one", 1, "No conversion", "Do not write.", [])
        self.fault("task_conflict", self.create, {**self.request, "task_id": "legacy"})

    def test_real_process_loss_on_both_sides_of_directory_publication(self):
        code = '''import json,os,sys
from pathlib import Path
from openmachine.tasks import store
request=json.load(sys.stdin)
rename=store.os.rename
def interrupt(source,destination):
    if sys.argv[2]=="before": os._exit(91)
    rename(source,destination)
    os._exit(91)
store.os.rename=interrupt
store.create(Path(sys.argv[1]),request.pop("task_id"),**request)
'''
        for point in ("before", "after"):
            state = self.root / ("crash-" + point)
            result = subprocess.run([sys.executable, "-I", "-c", code, str(state), point],
                                    input=json.dumps(self.request), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 91, result.stderr)
            path = state / "tasks/structured-one/task.json"
            self.assertEqual(path.exists(), point == "after")
            if point == "after":
                raw = path.read_bytes()
                self.assertEqual(tasks.read(state, "structured-one")["index_status"], "stale")
            else:
                self.assertFalse(path.parent.exists())
                self.assertEqual(tasks.find(state)["tasks"], [])
            request = dict(self.request)
            observed = tasks.create(state, request.pop("task_id"), **request)
            self.assertEqual(observed["status"], "unchanged" if point == "after" else "created")
            if point == "after":
                self.assertEqual(path.read_bytes(), raw)
            self.assertFalse((state / "executions").exists())

    def test_directory_fsync_failure_is_unknown_not_a_second_creation(self):
        sync = task_store._sync_directory
        def fail_root(path):
            if path == self.state / "tasks":
                raise OSError("fixture directory fsync failure")
            sync(path)
        with patch.object(task_store, "_sync_directory", side_effect=fail_root):
            self.fault("task_write_unverified", self.create)
        raw, inode = self.path.read_bytes(), self.path.stat().st_ino
        self.assertEqual(self.app.read_task("structured-one")["index_status"], "stale")
        self.assertEqual(self.create()["status"], "unchanged")
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino), (raw, inode))

    def test_missing_index_is_rebuilt_only_from_complete_canonical_owners(self):
        self.create()
        index = self.state / "tasks/index.json"
        index.unlink()
        raw = self.path.read_bytes()
        self.assertEqual(self.app.read_task("structured-one")["index_status"], "stale")
        self.assertFalse(index.exists())
        self.assertEqual(self.app.rebuild_task_index()["coverage"], "complete")
        self.assertEqual(self.path.read_bytes(), raw)
        self.app.save_task("legacy", 0, "Legacy", "Cannot reconstruct its lost revision.", [])
        index.unlink()
        self.fault("corrupt_tasks", self.app.rebuild_task_index)
        self.assertFalse(index.exists())
        self.assertEqual(self.path.read_bytes(), raw)

    def test_terminal_history_is_readable_without_any_supervisor_or_write(self):
        from openmachine.tasks import records, transitions
        value = terminal_task_fixture(self.create()["record"])
        raw = records.canonical(value)
        self.path.write_bytes(raw)
        self.assertEqual(self.app.read_task("structured-one")["index_status"], "stale")
        self.app.rebuild_task_index()
        snapshot = {p: p.read_bytes() for p in (self.path, self.state / "tasks/index.json")}
        self.assertEqual(self.app.read_task("structured-one")["record"], value)
        self.assertEqual(self.app.find_tasks(anchors=["fixture=structured"])["tasks"][0]["revision"], 2)
        self.assertEqual(tasks.check_state(self.state)["structured_task_records"], 1)
        self.assertEqual({p: p.read_bytes() for p in snapshot}, snapshot)
        self.assertFalse((self.state / "executions").exists())

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

    def test_rebuild_pages_survive_restart_and_block_new_writes_until_complete(self):
        self.create()
        self.seed_canonical(51)
        raw = self.path.read_bytes()
        first = self.app.rebuild_task_index()
        self.assertEqual((first["records_checked"], first["coverage"]), (50, "incomplete"))
        self.assertEqual(self.app.find_tasks()["coverage"], "incomplete")
        self.fault("task_index_incomplete", self.create, {**self.request, "task_id": "new-task"})
        self.fault("task_index_incomplete", self.app.save_task, "new-legacy", 0, "No write", "No work.", [])
        self.fault("task_index_changed", self.app.rebuild_task_index)
        self.fault("task_index_changed", self.app.rebuild_task_index, first["generation"] - 1)
        configuration = self.root / "rebuild.toml"
        configuration.write_text('schema_version=1\n[machines.desktop]\ntransport="local"\n')
        result = subprocess.run([sys.executable, "-I", "-m", "openmachine.cli", "--config", str(configuration),
            "--state-dir", str(self.state), "rebuild-task-index", "--expected-generation", str(first["generation"])],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["coverage"], "complete")
        self.assertEqual(tasks.check_state(self.state)["task_records"], 52)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_rebuild_honors_two_mib_page_budget_for_large_retained_records(self):
        from openmachine.tasks import records, transitions
        value = terminal_task_fixture(self.create()["record"])
        source_hash = value["plan"]["stages"][0]["source_sha256"]
        value["operations"] = [{"id": f"{n:032x}", "stage_id": "verify", "plan_revision": 1, "epoch": 0,
            "request_sha256": "a" * 64, "source_sha256": source_hash,
            "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                        "execution_id": f"{n:032x}", "control_seq": None},
            "admitted_at_ms": value["created_at_ms"], "state": "observed", "observed_at_ms": value["updated_at_ms"],
            "result": {"status": "succeeded", "receipt_sha256": "b" * 64, "summary": "x" * 256, "log_offset": 0}}
            for n in range(48)]
        while len(records.canonical(value)) < 60000:
            value["history"].append({"at_ms": value["updated_at_ms"], "kind": "decision", "stage_id": None,
                                     "operation_id": None, "summary": "x" * 256})
        self.assertLessEqual(len(value["history"]), 64)
        self.assertLessEqual(len(records.canonical(value)), records.MAX_RECORD)
        self.seed_canonical(40, value)
        with patch.object(task_store, "read_structured", wraps=task_store.read_structured) as observed:
            first = self.app.rebuild_task_index()
        self.assertLess(first["records_checked"], 50)
        self.assertEqual(observed.call_count, first["records_checked"])
        self.assertEqual(first["coverage"], "incomplete")
        self.assertLessEqual((first["records_checked"] - 1) * len(records.canonical(value)) + self.path.stat().st_size, tasks.MAX_INDEX)
        self.assertEqual(self.app.rebuild_task_index(first["generation"])["coverage"], "complete")

    def test_scope_checks_use_current_target_and_grants_without_holding_task_lock(self):
        self.app.save_task("existing", 0, "Existing", "Preserve.", [])
        original = self.app.machines.read_workspace_file
        def unlocked(*args, **kwargs):
            with lease(self.state / "locks/tasks.lock", create=False):
                pass
            return original(*args, **kwargs)
        with patch.object(self.app.machines, "read_workspace_file", side_effect=unlocked):
            self.create()
        changed = task_contract(self.context, "changed")
        changed["contract"]["targets"][0]["workspace_identity"] = "f" * 64
        self.fault("context_mismatch", self.create, changed)
        changed = task_contract(self.context, "disabled")
        changed["contract"]["tools"] = ["start_execution"]
        self.config.machines["desktop"].execution_enabled = False
        self.fault("execution_disabled", self.create, changed)
        changed["contract"]["tools"] = ["future_supervisor"]
        self.fault("invalid_task_contract", self.create, changed)
        self.assertFalse((self.state / "tasks/changed").exists())
        self.assertFalse((self.state / "tasks/disabled").exists())

    def test_structured_association_blocks_new_launch_but_original_receipt_wins(self):
        self.create()
        identity = uuid.uuid4().hex
        code = 'from pathlib import Path; p=Path("count.txt"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        arguments = ("desktop", identity, str(self.work), [sys.executable, "-c", code])
        self.fault("supervised_admission_unavailable", self.app.start_execution, *arguments, task_id="structured-one")
        self.assertFalse((self.state / "executions" / identity).exists())
        # Seed original domain evidence, not an unguarded Application route.
        self.assertEqual(self.wait(execution.start(self.config, self.state, *arguments, task_id="structured-one"))["status"], "succeeded")
        self.path.write_text("unavailable canonical task")
        self.assertEqual(self.app.start_execution(*arguments, task_id="structured-one")["status"], "succeeded")
        self.assertEqual(self.app.read_execution("desktop", identity)["status"], "succeeded")
        self.fault("execution_conflict", self.app.start_execution, "desktop", identity, str(self.work),
                   [sys.executable, "-c", "print('changed')"], task_id="structured-one")
        self.assertEqual((self.work / "count.txt").read_text(), "1")

    def test_concurrent_creators_never_replace_another_record(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.app.save_task("existing", 0, "Existing", "Retain.", [])
        barrier = threading.Barrier(2)
        original = self.app._validate_task_scope
        def ready(request, **kwargs):
            original(request, **kwargs)
            barrier.wait(timeout=5)
        def create():
            try:
                return self.create()["status"]
            except Fault as error:
                return error.code
        with patch.object(self.app, "_validate_task_scope", side_effect=ready), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        self.assertEqual(outcomes.count("created"), 1)
        self.assertTrue(set(outcomes) <= {"created", "unchanged", "resource_busy"})
        self.assertEqual(self.app.read_task("structured-one")["record"]["revision"], 1)

    def test_source_hashes_follow_locator_order_and_preserve_legacy_anchor_rules(self):
        sources = [{"locator": "fixture:z", "sha256": "a" * 64}, {"locator": "fixture:a", "sha256": "b" * 64}]
        self.request["contract"]["sources"] = sources
        self.request["anchors"] = [" ", "fixture=structured", " "]
        # Independent client vector: compact sorted JSON source objects plus a final newline.
        self.request["stages"][0].update(source_refs=["fixture:z", "fixture:a"],
            source_sha256="1323491417f9158ef1586fd4829f003c485343775f56103760e21ca83c38c904")
        self.request["stages"].append({"id": "finish", "depends_on": ["verify"], "result": "Retained plan",
            "verification": "Read both stages", "source_refs": [], "source_sha256": hashlib.sha256(b"[]\n").hexdigest()})
        created = self.create()["record"]
        self.assertEqual(created["anchors"], [" ", "fixture=structured"])
        self.assertEqual(created["plan"]["stages"][1]["depends_on"], ["verify"])
        self.assertEqual(self.app.read_task("structured-one")["record"], created)
        self.fault("invalid_task_contract", self.create, {**self.request, "task_id": "blank-title", "title": " "})

    def test_rebuild_does_not_invent_empty_history_or_replace_unsafe_index(self):
        self.create()
        index = self.state / "tasks/index.json"
        original = index.read_bytes()
        outside = self.root / "retained-index"
        index.rename(outside)
        index.symlink_to(outside)
        self.fault("corrupt_tasks", self.app.rebuild_task_index)
        self.assertEqual(outside.read_bytes(), original)
        self.assertTrue(index.is_symlink())
        index.unlink()
        self.path.unlink()
        self.path.parent.rmdir()
        self.fault("corrupt_tasks", self.app.rebuild_task_index)
        self.assertFalse(index.exists())

    def test_initial_publication_syncs_store_parent_before_acknowledgement(self):
        original = task_store._sync_directory
        synced = []
        def observe(path):
            synced.append(path)
            original(path)
        with patch.object(task_store, "_sync_directory", side_effect=observe):
            created = self.create()
        self.assertEqual(created["status"], "created")
        self.assertIn(self.state, synced)
        self.assertIn(self.state / "locks", synced)
        self.assertIn(self.state / "tasks", synced)

    def test_missing_established_lock_is_not_recreated_by_rebuild(self):
        self.create()
        raw = self.path.read_bytes()
        lock = self.state / "locks/tasks.lock"
        with lease(lock):
            lock.unlink()
            self.fault("corrupt_tasks", self.app.rebuild_task_index)
            self.fault("corrupt_tasks", self.app.read_task, "structured-one")
            self.assertFalse(lock.exists())
        self.assertEqual(self.path.read_bytes(), raw)
