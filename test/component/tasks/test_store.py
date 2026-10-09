"""Transactional Task ownership, bounded reads and original evidence."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from openmachine import tasks
from openmachine.tasks import database, records, store as task_store
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.tasks import TaskFixture, task_contract, terminal_task_fixture, replace_task_record

_OFFLINE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts/retire_chat_recovery_once.py"
_OFFLINE_SPEC = importlib.util.spec_from_file_location("one_time_task_retirement", _OFFLINE_SCRIPT)
_OFFLINE_MODULE = importlib.util.module_from_spec(_OFFLINE_SPEC)
_OFFLINE_SPEC.loader.exec_module(_OFFLINE_MODULE)
stage_clean_break = _OFFLINE_MODULE.stage


class StructuredTaskTests(TaskFixture, unittest.TestCase):
    def test_worktree_target_requires_bounded_absolute_path_and_distinct_selection(self):
        from copy import deepcopy
        for path in ("relative/tree", "/project/../tree", "/line\nbreak", "/" + "x" * 1024,
                     "/" + "\u00e9" * 512, None):
            with self.subTest(path=path):
                request = deepcopy(self.request)
                request["contract"]["targets"][0]["worktree"] = path
                self.fault("invalid_task_contract", tasks.normalize_create, request)
        request = deepcopy(self.request)
        target = request["contract"]["targets"][0]
        request["contract"]["targets"].append(dict(target, worktree="/fixture/linked"))
        normalized = tasks.normalize_create(request)
        self.assertEqual(len(normalized["contract"]["targets"]), 2)
        request["contract"]["targets"].append(dict(target, worktree=""))
        self.fault("invalid_task_contract", tasks.normalize_create, request)

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
        self.assertTrue(set(outcomes) <= {"created", "unchanged", "task_conflict", "task_store_busy", "resource_busy"})
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
        self.assertEqual(self.view()["record"], created)
        self.fault("invalid_task_contract", self.create, {**self.request, "task_id": "blank-title", "title": " "})

    def test_revised_plan_counts_new_stages_before_their_first_operation(self):
        self.request = task_contract()
        created = self.create()["record"]
        self.control("claim", handoff_id=created["handoff"]["id"])
        def retain(record, now):
            record["operations"].append(self.operation_fixture(record, now, 1))
        retained = tasks.update(self.state, "structured-one", retain)["record"]
        definition = deepcopy(self.request["stages"][0])
        revised = self.control("revise_plan", contract=retained["contract"],
            stages=[definition, dict(definition, id="review", depends_on=["verify"])],
            invalidated_stage_ids=[], reason="Add an explicitly planned review stage.")["record"]
        self.assertEqual(tasks.check_state(self.state)["structured_task_records"], 1)
        self.assertEqual(revised["plan"]["revision"], 2)
        self.assertEqual(revised["_facts"]["stage_operation_counts"], {"verify": 1, "review": 0})
        self.assertEqual(tasks.operation(self.state, "structured-one", f"{1:032x}"), retained["operations"][0])

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

    def test_empty_worktree_target_preserves_original_creation_and_private_database(self):
        first = self.create()
        original = self.task_bytes()
        self.assertNotIn("worktree", first["record"]["contract"]["targets"][0])
        repeated = deepcopy(self.request)
        repeated["contract"]["targets"][0]["worktree"] = ""
        self.assertEqual(self.create(repeated)["record"], first["record"])
        self.assertEqual(self.task_bytes(), original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / "tasks").exists())

    def test_first_format_three_admission_retains_original_identity_and_result_budget(self):
        value = self.create()["record"]
        value["format"] = 3
        value.pop("control_revision")
        value.pop("effect_scopes")
        replace_task_record(self.state, value)
        def admit(record, now):
            record["operations"].append(self.operation_fixture(record, now, 1))
        saved = tasks.update(self.state, "structured-one", admit, upgrade=True)
        self.assertEqual(saved["record"]["format"], 4)
        self.assertEqual(saved["record"]["origin"], value["origin"])
        self.assertEqual(tasks.operation(self.state, "structured-one", f"{1:032x}")["result"]["status"], "succeeded")

    def test_retired_recovery_fields_are_not_valid_task_authority(self):
        self.create()
        with database.connect(self.state) as connection:
            original = connection.execute("SELECT current FROM tasks").fetchone()[0]
        baseline = json.loads(original)
        baseline["contract"]["recovery_policy"] = "disabled"
        baseline["chat_registration"] = None
        invalid = []
        for change in ("valid_old_pair", "task_chat_old_pair", "invalid_policy",
                       "missing_registration", "missing_policy", "invalid_registration", "old_format"):
            value = deepcopy(baseline)
            if change == "task_chat_old_pair":
                value["contract"]["recovery_policy"] = "task_chat"
                value["chat_registration"] = "a" * 32
            elif change == "invalid_policy":
                value["contract"]["recovery_policy"] = "unknown"
            elif change == "missing_registration":
                value.pop("chat_registration")
            elif change == "missing_policy":
                value["contract"].pop("recovery_policy")
            elif change == "invalid_registration":
                value["chat_registration"] = "not-an-id"
            else:
                value["format"] = 2
                value.pop("control_revision")
            invalid.append(value)
        for broken in invalid:
            with self.subTest(registration=broken.get("chat_registration"), policy=broken["contract"].get("recovery_policy")):
                raw = records.canonical(broken).decode()
                with sqlite3.connect(self.path) as connection:
                    connection.execute("UPDATE tasks SET current=?,sha256=?",
                                       (raw, hashlib.sha256(raw.encode()).hexdigest()))
                self.fault("corrupt_tasks", tasks.read, self.state, "structured-one")
                with sqlite3.connect(self.path) as connection:
                    self.assertEqual(connection.execute("SELECT current FROM tasks").fetchone()[0], raw)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE tasks SET current=?,sha256=?",
                               (original, hashlib.sha256(original.encode()).hexdigest()))
        self.assertEqual(tasks.read(self.state, "structured-one")["record"]["contract"], self.request["contract"])
        self.fault("invalid_task_contract", tasks.normalize_create,
                   {**self.request, "contract": {**self.request["contract"], "recovery_policy": "disabled"}})
        self.fault("invalid_task_contract", tasks.normalize_create,
                   {**self.request, "chat_registration": None})

    def test_offline_retirement_stages_verified_copy_and_keeps_original_evidence(self):
        self.create()
        self.app.save_task("legacy-checkpoint", 0, "Checkpoint", "Preserve unrelated work.", [])
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT current FROM tasks WHERE task_id='structured-one'").fetchone()
            old = json.loads(row[0])
            old["contract"]["recovery_policy"] = "task_chat"
            old["chat_registration"] = "a" * 32
            old_raw = records.canonical(old).decode()
            connection.execute("UPDATE tasks SET current=?,sha256=? WHERE task_id='structured-one'",
                               (old_raw, hashlib.sha256(old_raw.encode()).hexdigest()))
            before_events = list(connection.execute("SELECT * FROM task_events"))
            before_operations = list(connection.execute("SELECT * FROM operations"))
        self.fault("corrupt_tasks", tasks.read, self.state, "structured-one")
        staged = self.root / "converted-tasks.db"
        result = stage_clean_break(self.path, staged)
        self.assertEqual(result["status"], "staged_not_applied")
        self.assertEqual(result["converted_tasks"], 1)
        self.assertEqual(result["structured_tasks"], 1)
        with sqlite3.connect(self.path) as source, sqlite3.connect(staged) as output:
            original = source.execute("SELECT current FROM tasks WHERE task_id='structured-one'").fetchone()[0]
            converted = output.execute("SELECT current FROM tasks WHERE task_id='structured-one'").fetchone()[0]
            self.assertEqual(original, old_raw)
            self.assertNotIn("recovery_policy", converted)
            self.assertNotIn("chat_registration", converted)
            self.assertEqual(before_events, list(output.execute("SELECT * FROM task_events")))
            self.assertEqual(before_operations, list(output.execute("SELECT * FROM operations")))
            self.assertEqual(list(source.execute("SELECT * FROM tasks WHERE task_id='legacy-checkpoint'")),
                             list(output.execute("SELECT * FROM tasks WHERE task_id='legacy-checkpoint'")))
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            stage_clean_break(self.path, staged)  # Never overwrite a previously staged destination.

    def test_offline_retirement_refuses_invalid_history_without_publishing(self):
        self.create()
        with sqlite3.connect(self.path) as connection:
            original = connection.execute("SELECT current FROM tasks").fetchone()[0]
        for case in ("missing_registration", "invalid_policy", "bad_sha"):
            value = json.loads(original)
            value["contract"]["recovery_policy"] = "disabled" if case != "invalid_policy" else "unknown"
            if case != "missing_registration":
                value["chat_registration"] = None
            raw = records.canonical(value).decode()
            digest = "f" * 64 if case == "bad_sha" else hashlib.sha256(raw.encode()).hexdigest()
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE tasks SET current=?,sha256=?", (raw, digest))
            destination = self.root / f"invalid-{case}.db"
            with self.subTest(case=case), self.assertRaises(ValueError):
                stage_clean_break(self.path, destination)
            self.assertFalse(destination.exists())
            with sqlite3.connect(self.path) as connection:
                self.assertEqual(connection.execute("SELECT current,sha256 FROM tasks").fetchone(), (raw, digest))

    def test_corrupt_authority_is_never_skipped_rebuilt_or_replaced(self):
        self.create()
        with database.connect(self.state) as connection:
            row = connection.execute("SELECT current,sha256 FROM tasks").fetchone()
            original = row["current"]
        value = json.loads(original)
        variants = [{**value, **change} for change in ({"format": True}, {"revision": 0},
            {"extra": "unknown"}, {"task_id": "another"}, {"updated_at_ms": 0})]
        missing = deepcopy(value)
        missing["plan"]["stages"][0]["decision_version"] = 999
        variants.append(missing)
        for broken in variants:
            raw = records.canonical(broken).decode()
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE tasks SET current=?,sha256=?", (raw, hashlib.sha256(raw.encode()).hexdigest()))
            for action in (lambda: tasks.read(self.state, "structured-one"), self.create, self.app.rebuild_task_index):
                with self.assertRaises(Fault):
                    action()
            with sqlite3.connect(self.path) as connection:
                self.assertEqual(connection.execute("SELECT current FROM tasks").fetchone()[0], raw)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE tasks SET current=?,sha256=?", (original, hashlib.sha256(original.encode()).hexdigest()))
        (self.state / "tasks").mkdir(mode=0o700)
        self.fault("corrupt_tasks", self.create)
        self.fault("corrupt_tasks", tasks.read, self.state, "structured-one")

    def test_transaction_failure_preserves_all_owners_and_has_no_partial_index(self):
        saved = self.app.save_task("legacy", 0, "Original", "Retain this history.\n", [])
        with patch.object(database, "commit", side_effect=sqlite3.OperationalError("fixture interrupted commit")):
            self.fault("task_write_unverified", self.create)
        self.fault("task_not_found", tasks.read, self.state, "structured-one")
        self.assertEqual(tasks.read(self.state, "legacy")["body"], saved["body"])
        created = self.create()
        self.assertEqual(created["status"], "created")
        self.assertEqual(self.app.find_tasks()["coverage"], "complete")
        self.assertEqual(self.app.rebuild_task_index()["status"], "current")
        self.app.save_task("legacy", 1, "Original", "Later explicit checkpoint.\n", [])
        self.fault("task_conflict", self.app.save_task, "structured-one", 1, "No conversion", "No work.", [])
        self.fault("task_conflict", self.create, {**self.request, "task_id": "legacy"})

    def test_lost_commit_acknowledgement_is_reconciled_by_original_creation_id(self):
        self.app.save_task("existing", 0, "Existing", "Retain.", [])
        commit = database.commit
        def lost(connection):
            commit(connection)
            raise sqlite3.OperationalError("fixture lost acknowledgement")
        with patch.object(database, "commit", side_effect=lost):
            self.fault("task_write_unverified", self.create)
        observed = tasks.read(self.state, "structured-one")
        self.assertEqual(observed["record"]["revision"], 1)
        self.assertEqual(self.create()["status"], "unchanged")
        self.assertEqual(tasks.check_state(self.state)["task_records"], 2)

    def test_missing_or_unsafe_database_retains_the_original_writer_fence(self):
        self.create()
        original = self.path.read_bytes()
        outside = self.root / "retained.db"
        self.path.rename(outside)
        self.path.symlink_to(outside)
        self.fault("corrupt_tasks", self.app.rebuild_task_index)
        self.assertEqual(outside.read_bytes(), original)
        self.path.unlink()
        self.fault("corrupt_tasks", self.create)
        self.fault("corrupt_tasks", self.app.rebuild_task_index)
        self.assertFalse(self.path.exists())

    def test_terminal_and_original_decision_remain_passive_and_explicitly_readable(self):
        value = terminal_task_fixture(self.create()["record"])
        replace_task_record(self.state, value)
        before = self.task_bytes()
        result = self.app.read_task("structured-one")
        self.assertEqual(result["record"]["terminal"], value["terminal"])
        self.assertFalse(result["record_complete"])
        decision = self.app.read_task(**result["record"]["plan"]["stages"][0]["evidence_ref"])
        self.assertEqual(decision["items"][0]["state"], "accepted")
        self.assertEqual(self.app.find_tasks(anchors=["fixture=structured"])["tasks"][0]["revision"], 2)
        self.assertEqual(tasks.check_state(self.state)["structured_task_records"], 1)
        self.assertEqual(self.task_bytes(), before)
        self.assertFalse((self.state / "executions").exists())

    @staticmethod
    def operation_fixture(record, now, number):
        identity = f"{number:032x}"
        return {"id": identity, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
            "request_sha256": "a" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
            "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                        "execution_id": identity, "control_seq": None},
            "admitted_at_ms": now, "state": "observed", "observed_at_ms": now,
            "result": {"status": "succeeded", "receipt_sha256": "b" * 64, "summary": "Synthetic retained result.", "log_offset": 0}}

    def test_history_exceeds_old_limits_without_expanding_current_reads(self):
        self.create()
        def seed(record, now):
            record["operations"].extend(self.operation_fixture(record, now, n) for n in range(1, 1001))
        tasks.update(self.state, "structured-one", seed)
        def append(record, now):
            self.assertLessEqual(len(record["operations"]), 1)
            record["operations"].append(self.operation_fixture(record, now, 1001))
        tasks.update(self.state, "structured-one", append)
        summary = self.app.read_task("structured-one")
        self.assertEqual(summary["counts"]["operation_count"], 1001)
        self.assertGreater(summary["counts"]["history_count"], 1000)
        self.assertNotIn("operations", summary["record"])
        self.assertLess(len(records.canonical(summary["record"])), 8192)
        original = self.app.read_task("structured-one", section="operations", operation_id=f"{1:032x}")
        self.assertEqual(original["items"][0]["id"], f"{1:032x}")
        cursor, seen = "", []
        while True:
            page = self.app.read_task("structured-one", section="operations", cursor=cursor, limit=50)
            self.assertLessEqual(len(page["items"]), 50)
            seen.extend(op["id"] for op in page["items"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(seen, [f"{n:032x}" for n in range(1, 1002)])
        self.assertEqual(tasks.check_state(self.state)["task_records"], 1)

    def test_large_catalog_has_no_serialized_index_ceiling(self):
        self.create()
        with database.connect(self.state, write=True) as connection:
            for number in range(10001):
                task_store._put_legacy(connection, f"archive-{number:05}", {"title": "x" * 400,
                    "anchors": ["fixture=archive"], "revision": 1, "body": "Retained checkpoint.",
                    "sha256": hashlib.sha256(b"Retained checkpoint.").hexdigest(), "content_redacted": False})
        self.assertEqual(tasks.find(self.state, limit=1)["total"], 10002)
        self.assertEqual(self.app.save_task("after-cap", 0, "New checkpoint", "New explicit work.", [])["revision"], 1)
        first = tasks.find(self.state, anchors=["fixture=archive"], limit=50)
        self.assertEqual((len(first["tasks"]), first["total"]), (50, 10001))
        self.assertEqual(tasks.read(self.state, "archive-10000")["body"], "Retained checkpoint.")
        self.assertFalse((self.state / "tasks/index.json").exists())

    def test_original_event_cursor_keeps_its_watermark_and_operation_cursor_rejects_change(self):
        self.create()
        tasks.update(self.state, "structured-one", lambda record, now: record["operations"].extend(
            self.operation_fixture(record, now, n) for n in range(1, 4)))
        events = self.app.read_task("structured-one", section="history", limit=1)
        operations = self.app.read_task("structured-one", section="operations", limit=1)
        tasks.update(self.state, "structured-one", lambda record, now: record["control"].update(state="waiting_for_user"))
        continuation = self.app.read_task("structured-one", section="history", cursor=events["next_cursor"])
        self.assertEqual(continuation["watermark"], events["watermark"])
        self.assertLessEqual(max(event["sequence"] for event in continuation["items"]), events["watermark"])
        self.fault("task_conflict", self.app.read_task, "structured-one", section="operations", cursor=operations["next_cursor"])
        self.fault("invalid_task_read", self.app.read_task, "structured-one", section="operations", cursor=events["next_cursor"])

    def test_initial_publication_syncs_private_store_and_lock_before_acknowledgement(self):
        original = database.sync_directory
        synced = []
        def observe(path):
            synced.append(path)
            original(path)
        with patch.object(database, "sync_directory", side_effect=observe):
            created = self.create()
        self.assertEqual(created["status"], "created")
        self.assertIn(self.state, synced)
        self.assertIn(self.state / "locks", synced)

    def test_backup_is_consistent_private_and_fenced_from_new_effects(self):
        self.create()
        destination = self.root / "backup.db"
        backup = tasks.backup(self.state, destination)
        self.assertTrue(backup["restore_required"])
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(destination) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT restore_required FROM store").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT revision FROM tasks").fetchone()[0], 1)
        restored = self.root / "restore"
        restored.mkdir(mode=0o700)
        (restored / "locks").mkdir(mode=0o700)
        (restored / "locks/tasks.lock").touch(mode=0o600)
        destination.rename(restored / "tasks.db")
        self.assertEqual(tasks.read(restored, "structured-one")["record"]["revision"], 1)
        self.fault("task_restore_unreconciled", tasks.update, restored, "structured-one", lambda record, now: None)
        self.fault("task_restore_unreconciled", tasks.migrate, restored)


if __name__ == "__main__":
    unittest.main()
