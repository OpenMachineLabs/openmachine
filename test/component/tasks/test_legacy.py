"""Tests for tasks behavior at the component layer."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from openmachine import skills
from openmachine.chat import tasks
from openmachine.chat.tasks import database, records
from openmachine.config import SkillSources
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from test.support.continuity_cases import ContinuityCaseFixture
from test.support.tasks import task_contract, write_legacy_checkpoint, write_legacy_task


class ContinuityTests(ContinuityCaseFixture, unittest.TestCase):
    def test_private_checkpoint_lookup_does_not_duplicate_progress_in_index(self):
        self.assertEqual(tasks.find(self.state)["status"], "uninitialized")
        self.assertFalse(self.state.exists())
        self.fault("tasks_uninitialized", tasks.read, self.state, "task-one")
        saved = self.save()
        self.assertEqual(saved["revision"], 1)
        found = tasks.find(self.state, anchors=["issue=example/project#1"])
        self.assertEqual(found["status"], "matched")
        self.assertFalse(found["resume_authorized"])
        observed = tasks.read(self.state, "task-one")
        self.assertIn("Next: run", observed["body"])
        self.assertNotIn("body", found["tasks"][0])
        self.assertNotIn("Completed: inspection", json.dumps(found))
        self.assertEqual(tasks.storage_state(self.state)["kind"], "sqlite")
        self.assertFalse((self.state / "tasks").exists())
        self.assertEqual((self.state / "tasks.db").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_revision_comparison_identical_retry_and_real_lock_conflict(self):
        first = self.save()
        self.assertEqual(self.save()["status"], "unchanged")
        second = self.save(revision=1, body="Completed: tests.\nNext: review diff.\n")
        self.assertEqual(second["revision"], 2)
        self.fault("task_conflict", self.save, revision=1, body="Stale different progress.")
        self.fault("task_conflict", self.save, revision=0, body="Completed: tests.\nNext: review diff.\n")
        with lease(self.state / "locks/tasks.lock"):
            self.fault("resource_busy", self.save, revision=2, body="Do not write while busy.")
        self.assertEqual(tasks.read(self.state, "task-one")["revision"], 2)
        self.assertEqual(first["generation"], 1)

    def test_ambiguous_and_descriptive_lookup_and_changed_generation(self):
        self.save()
        self.save("task-two")
        self.assertEqual(tasks.find(self.state, anchors=["issue=example/project#1"])["status"], "ambiguous")
        self.assertEqual(tasks.find(self.state, query="Repair")["status"], "candidates")
        first = tasks.find(self.state, limit=1)
        self.assertEqual(first["next_offset"], 1)
        next_page = tasks.find(self.state, limit=1, offset=1, expected_generation=first["generation"])
        self.assertEqual(next_page["tasks"][0]["task_id"], "task-two")
        self.save("task-three", anchors=["issue=example/project#2"])
        self.fault("task_index_changed", tasks.find, self.state, offset=1, expected_generation=first["generation"])

    def test_missing_corrupt_and_disappeared_database_are_never_reinitialized(self):
        self.save()
        path = self.state / "tasks.db"
        original = path.read_bytes()
        path.write_bytes(b"broken")
        self.fault("corrupt_tasks", self.save, "new-task")
        self.assertEqual(path.read_bytes(), b"broken")
        path.write_bytes(original)
        retained = self.state / "retained-task-fixture"
        path.rename(retained)
        self.fault("corrupt_tasks", tasks.find, self.state)
        self.fault("corrupt_tasks", self.save, "new-task")
        self.assertFalse(path.exists())
        self.assertEqual(retained.read_bytes(), original)
        self.assertFalse((self.state / "tasks").exists())

    def test_unverified_checkpoint_commit_preserves_body_revision_and_lookup_together(self):
        original = self.save()
        found = tasks.find(self.state)
        with patch.object(database, "commit", side_effect=sqlite3.OperationalError("fixture interrupted commit")):
            self.fault("task_write_unverified", self.save, revision=1, body="Updated material progress.\n")
        observed = tasks.read(self.state, "task-one")
        self.assertEqual((observed["body"], observed["revision"], observed["generation"]),
                         (original["body"], original["revision"], original["generation"]))
        self.assertEqual(tasks.find(self.state), found)

    def test_task_input_bounds_and_database_links_fail_closed(self):
        self.fault("invalid_task_id", self.save, "../outside")
        self.fault("invalid_checkpoint", self.save, body="x" * (tasks.MAX_CHECKPOINT + 1))
        self.fault("invalid_checkpoint", self.save, body="value\0")
        self.assertFalse(self.state.exists())
        self.save()
        path = self.state / "tasks.db"
        retained = self.root / "retained.db"
        path.rename(retained)
        path.symlink_to(retained)
        self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.fault("corrupt_tasks", self.save, revision=1, body="Do not write through a database alias.")
        self.assertTrue(retained.is_file())

    def test_task_lock_privacy_and_unknown_database_schema_are_rejected(self):
        self.save()
        lock = self.state / "locks/tasks.lock"
        lock.chmod(0o644)
        self.fault("corrupt_tasks", self.save, revision=1, body="Do not write through a non-private lock.")
        lock.chmod(0o600)
        archive = "tasks-legacy-" + "a" * 32
        with closing(sqlite3.connect(self.state / "tasks.db")) as connection:
            for source, digest in (("../outside", "b" * 64), (str(self.root / "outside"), "b" * 64),
                                   (archive + "/nested", "b" * 64), (archive, None),
                                   (None, "b" * 64), (archive, "invalid")):
                with self.subTest(archive=source, digest=digest):
                    connection.execute("UPDATE store SET archive=?,import_digest=?", (source, digest))
                    connection.commit()
                    self.fault("corrupt_tasks", tasks.find, self.state)
                    self.fault("corrupt_tasks", tasks.migrate, self.state)
                    self.assertFalse((self.root / "outside").exists())
            connection.execute("UPDATE store SET archive=NULL,import_digest=NULL")
            connection.commit()
        with database.connect(self.state, write=True) as connection:
            connection.execute("UPDATE store SET schema=999")
        self.fault("unsupported_task_format", tasks.find, self.state)
        self.fault("unsupported_task_format", self.save, revision=1, body="Do not adopt an unknown database schema.")

    def test_progress_updates_preserve_anchors_and_skill_root_alias_is_rejected(self):
        self.save()
        self.save(revision=1, body="Next material checkpoint.", anchors=[])
        self.assertEqual(tasks.find(self.state, anchors=["issue=example/project#1"])["status"], "matched")
        self.save(revision=2, body="Next material checkpoint.", anchors=["branch=repair-fixture"])
        self.assertEqual(tasks.read(self.state, "task-one")["anchors"], ["branch=repair-fixture", "issue=example/project#1"])
        root_alias = self.root / "root-alias"
        root_alias.symlink_to(Path("/"), target_is_directory=True)
        baseline = skills.inspect(self.config)["skills"]
        self.config.skills = SkillSources(roots=[str(root_alias)])
        result = skills.inspect(self.config)
        self.assertEqual(result["skills"], baseline)
        self.assertEqual(result["warnings"][0]["code"], "invalid_skill_root")

    def test_additive_anchor_limit_rejects_before_mutating_state(self):
        anchors = [f"anchor={index}" for index in range(16)]
        saved = self.save(anchors=anchors)
        self.assertEqual(saved["revision"], 1)
        checkpoint = tasks.read(self.state, "task-one")
        found = tasks.find(self.state)
        self.assertEqual(self.save(revision=1, anchors=[anchors[0]])["status"], "unchanged")
        self.fault("invalid_task", self.save, revision=1, body="Would exceed anchor limit.\n",
                   anchors=["anchor=overflow"])
        self.assertEqual(tasks.read(self.state, "task-one"), checkpoint)
        self.assertEqual(tasks.find(self.state), found)
        self.assertEqual(tasks.read(self.state, "task-one")["anchors"], sorted(anchors))

    def test_missing_established_lock_is_never_recreated(self):
        self.save()
        lock = self.state / "locks/tasks.lock"
        before = (self.state / "tasks.db").read_bytes()
        with lease(lock):
            lock.unlink()
            self.fault("corrupt_tasks", tasks.migrate, self.state)
            self.fault("corrupt_tasks", self.save, revision=1, body="Do not replace lock.")
        self.assertEqual((self.state / "tasks.db").read_bytes(), before)
        old = self.root / "legacy-lock"
        write_legacy_checkpoint(old, "task-one", "Old note.\n")
        with lease(old / "locks/tasks.lock"):
            (old / "locks/tasks.lock").unlink()
            self.fault("corrupt_tasks", tasks.migrate, old)
        self.assertFalse((old / "tasks.db").exists())

    def test_pre_release_corruption_or_links_cannot_grant_current_writes(self):
        write_legacy_checkpoint(self.state, "task-one", "Original checkpoint.\n")
        index = self.state / "tasks/index.json"
        original = index.read_bytes()
        for raw in (b"broken", records.canonical({**json.loads(original), "unexpected": True})):
            index.write_bytes(raw)
            self.fault("corrupt_tasks", tasks.find, self.state)
            self.fault("task_migration_required", tasks.migrate, self.state)
        index.write_bytes(original)
        note = self.state / "tasks/task-one/task.md"
        retained = self.root / "saved.md"
        note.rename(retained)
        note.symlink_to(retained)
        self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.fault("task_migration_required", tasks.migrate, self.state)
        self.assertEqual(retained.read_text(), "Original checkpoint.\n")
        self.assertFalse((self.state / "tasks.db").exists())

    def test_pre_release_prefix_ids_remain_original_not_imported(self):
        for identity in ("task-one", "task-one-c1"):
            write_legacy_checkpoint(self.state, identity, "Original private note.\n")
        root = self.state / "tasks"
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.fault("task_migration_required", tasks.migrate, self.state)
        self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
        self.assertFalse((self.state / "tasks.db").exists())

    def test_pre_release_paused_unknown_history_never_becomes_writer_authority(self):
        now = time.time_ns() // 1000000
        record = records.new_record(records.normalize_create(task_contract()), now)
        operation = {"id": "a" * 32, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
                     "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                     "locator": {"kind": "execution", "machine": "desktop", "execution_id": "a" * 32,
                                 "control_seq": None}, "admitted_at_ms": now, "state": "unknown",
                     "observed_at_ms": now, "result": {"status": "unknown", "receipt_sha256": None,
                                                      "summary": "Unsettled.", "log_offset": None}}
        record["operations"].append(operation)
        record["control"].update(state="paused", stop_intent="pause")
        record["plan"]["stages"][0].update(state="blocked", evidence=[operation["id"]])
        record["handoff"] = None
        write_legacy_task(self.state, record)
        root = self.state / "tasks"
        original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.fault("task_migration_required", tasks.migrate, self.state)
        self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, original)
        self.assertFalse((self.state / "tasks.db").exists())


if __name__ == "__main__":
    unittest.main()
