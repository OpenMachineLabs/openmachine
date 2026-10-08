"""Tests for tasks behavior at the component layer."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch
from openmachine import skills, tasks
from openmachine.tasks import database, migration, records
from openmachine.config import SkillSources
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, save_json
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

    def test_missing_established_lock_is_not_recreated_while_old_inode_is_held(self):
        self.save()
        lock = self.state / "locks/tasks.lock"
        original = (self.state / "tasks.db").read_bytes()
        with lease(lock):
            lock.unlink()
            self.fault("corrupt_tasks", self.save, revision=1, body="Must not use a replacement lock inode.")
            self.fault("corrupt_tasks", tasks.migrate, self.state)
            self.assertFalse(lock.exists())
            self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.assertEqual((self.state / "tasks.db").read_bytes(), original)
        for prepared in (False, True):
            with self.subTest(prepared=prepared):
                state = self.root / ("prepared-lock" if prepared else "legacy-lock")
                write_legacy_checkpoint(state, "task-one", "Preserve the original lock binding.\n")
                if prepared:
                    with patch.object(migration, "_verify_import", side_effect=Fault("task_migration_incomplete", "Fixture interrupted import.")):
                        self.fault("task_migration_incomplete", tasks.migrate, state)
                paths = [state / "tasks/index.json", state / "tasks/task-one/task.md"]
                if prepared:
                    paths.append(state / "tasks.db")
                original = [path.read_bytes() for path in paths]
                lock = state / "locks/tasks.lock"
                with lease(lock):
                    lock.unlink()
                    self.fault("corrupt_tasks", tasks.migrate, state)
                    self.assertFalse(lock.exists())
                    self.assertTrue((state / "tasks").is_dir())
                self.assertEqual([path.read_bytes() for path in paths], original)
                self.assertEqual((state / "tasks.db").exists(), prepared)

    def test_legacy_index_loss_unknown_fields_orphans_and_links_block_import(self):
        write_legacy_checkpoint(self.state, "task-one", "Original private checkpoint.\n")
        path = self.state / "tasks/index.json"
        original = path.read_bytes()
        for raw in (b"broken", records.canonical({**json.loads(original), "unexpected": "not metadata"})):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.fault("corrupt_tasks", tasks.find, self.state)
                self.fault("corrupt_tasks", tasks.migrate, self.state)
                self.assertFalse((self.state / "tasks.db").exists())
        path.unlink()
        self.fault("corrupt_tasks", tasks.find, self.state)
        self.fault("corrupt_tasks", tasks.migrate, self.state)
        save_json(path, json.loads(original))
        directory = self.state / "tasks/orphan"
        directory.mkdir(mode=0o700)
        self.fault("corrupt_tasks", tasks.migrate, self.state)
        directory.rmdir()
        checkpoint = self.state / "tasks/task-one/task.md"
        retained = self.root / "retained.md"
        checkpoint.rename(retained)
        checkpoint.symlink_to(retained)
        self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.fault("corrupt_tasks", tasks.migrate, self.state)
        self.assertTrue(retained.is_file())
        self.assertFalse((self.state / "tasks.db").exists())

    def test_unfenced_import_reprepares_advanced_original_without_replacing_identity(self):
        now = time.time_ns() // 1000000
        record = records.new_record(records.normalize_create(task_contract()), now)
        operation = {"id": "a" * 32, "stage_id": "verify", "plan_revision": 1, "epoch": 0,
            "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
            "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                "execution_id": "a" * 32, "control_seq": None},
            "admitted_at_ms": now, "state": "unknown", "observed_at_ms": now,
            "result": {"status": "unknown", "receipt_sha256": None, "summary": "Retained unknown.", "log_offset": None}}
        record["operations"] = [operation]
        record["control"].update(state="paused", stop_intent="pause")
        record["plan"]["stages"][0].update(state="blocked", evidence=[operation["id"]])
        record["handoff"] = None
        write_legacy_task(self.state, record)
        write_legacy_checkpoint(self.state, "task-one", "Original checkpoint.\n")
        with patch.object(migration, "_verify_import", side_effect=Fault("task_migration_incomplete", "Fixture interrupted import.")):
            self.fault("task_migration_incomplete", tasks.migrate, self.state)
        prepared = tasks.storage_state(self.state)
        inode = (self.state / "tasks.db").stat().st_ino
        self.assertEqual((prepared["kind"], prepared["active"], prepared["fenced"]), ("legacy", False, False))
        body = "Progress from the resumed original controller.\n"
        with lease(self.state / "locks/tasks.lock"):
            path = self.state / "tasks/index.json"
            index = json.loads(path.read_text())
            (self.state / "tasks/task-one/task.md").write_text(body)
            index["tasks"]["task-one"].update(revision=2, sha256=hashlib.sha256(body.encode()).hexdigest())
            index["generation"] += 1
            save_json(path, index)
        raw = (self.state / "tasks/structured-one/task.json").read_bytes()
        active = tasks.migrate(self.state)
        self.assertEqual((active["identity"], (self.state / "tasks.db").stat().st_ino), (prepared["identity"], inode))
        self.assertEqual((active["kind"], active["active"], active["fenced"]), ("sqlite", True, True))
        observed = tasks.read(self.state, "task-one")
        self.assertEqual((observed["body"], observed["revision"]), (body, 2))
        self.assertFalse(observed["resume_authorized"])
        self.assertEqual(tasks.operation(self.state, "structured-one", operation["id"]), operation)
        observed = tasks.read(self.state, "structured-one")
        self.assertEqual((observed["record"]["revision"], observed["record"]["control"]["state"]), (1, "paused"))
        self.assertEqual(observed["facts"]["history_count"], len(record["history"]))
        archive = next(self.state.glob("tasks-legacy-*"))
        self.assertEqual((archive / "structured-one/task.json").read_bytes(), raw)
        self.assertEqual((archive / "task-one/task.md").read_text(), body)
        self.assertEqual(tasks.check_state(self.state)["task_records"], 2)

    def test_fenced_import_never_reprepares_changed_archive(self):
        write_legacy_checkpoint(self.state, "task-one", "Original checkpoint.\n")
        commit = database.commit
        def interrupted(connection):
            if not (self.state / "tasks").exists():
                raise sqlite3.OperationalError("Fixture interrupted ownership commit")
            commit(connection)
        with patch.object(database, "commit", side_effect=interrupted):
            self.fault("task_write_unverified", tasks.migrate, self.state)
        prepared = tasks.storage_state(self.state)
        self.assertEqual((prepared["kind"], prepared["active"], prepared["fenced"]), ("sqlite", False, True))
        archive = next(self.state.glob("tasks-legacy-*"))
        body = "Unverified change to a fenced archive.\n"
        (archive / "task-one/task.md").write_text(body)
        path = archive / "index.json"
        index = json.loads(path.read_text())
        index["tasks"]["task-one"].update(revision=2, sha256=hashlib.sha256(body.encode()).hexdigest())
        index["generation"] += 1
        save_json(path, index)
        self.fault("task_migration_conflict", tasks.migrate, self.state)
        self.assertEqual(tasks.storage_state(self.state), prepared)
        self.assertFalse((self.state / "tasks").exists())
        with database.connect(self.state, maintenance=True) as connection:
            row = connection.execute("SELECT current,sha256 FROM tasks WHERE task_id='task-one'").fetchone()
            self.assertEqual(database.decode(row["current"], row["sha256"])["body"], "Original checkpoint.\n")

    def test_migration_preserves_tasks_with_prefix_related_ids(self):
        bodies = {"task-one": "Original checkpoint.\n", "task-one-c1": "Related checkpoint.\n"}
        for identity, body in bodies.items():
            write_legacy_checkpoint(self.state, identity, body)
        root = self.state / "tasks"
        original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

        active = tasks.migrate(self.state)

        self.assertEqual((active["kind"], active["active"], active["fenced"]), ("sqlite", True, True))
        archive = next(self.state.glob("tasks-legacy-*"))
        self.assertEqual({path.relative_to(archive): path.read_bytes() for path in archive.rglob("*") if path.is_file()}, original)
        for identity, body in bodies.items():
            self.assertEqual(tasks.read(self.state, identity)["body"], body)
        self.assertEqual(tasks.check_state(self.state)["task_records"], 2)
        self.assertEqual(tasks.migrate(self.state), active)

    def test_migration_preserves_all_semantic_formats_and_large_paused_history(self):
        now = time.time_ns() // 1000000
        originals = {}
        for version in (1, 2, 3, 4):
            request = task_contract(identity=f"format-{version}")
            if version >= 2:
                request["contract"].update(repositories=["example/project"], github_connections={
                    "example/project": {"connection_id": "c" * 32, "account_id": 7, "authorization_revision": 2}})
            if version == 4:
                request["stages"].insert(0, {**request["stages"][0], "id": "prepare"})
                request["stages"][1]["depends_on"] = ["prepare"]
            record = records.new_record(records.normalize_create(request), now)
            record["format"] = version
            if version < 4:
                record.pop("control_revision")
                record.pop("effect_scopes")
            if version < 3:
                record["contract"].pop("recovery_policy")
            if version == 4:
                record.update(revision=217, control_revision=217, handoff=None)
                record["control"].update(state="paused", stop_intent="pause", current_stage_id="verify")
                for index in range(49):
                    identity = f"{index + 1:032x}"
                    unknown = index == 48
                    record["operations"].append({"id": identity, "stage_id": "prepare" if index < 3 else "verify",
                        "plan_revision": 1, "epoch": 0, "request_sha256": "b" * 64,
                        "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                        "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                            "execution_id": identity, "control_seq": None},
                        "admitted_at_ms": now, "state": "unknown" if unknown else "observed", "observed_at_ms": now,
                        "result": {"status": "unknown" if unknown else "succeeded", "receipt_sha256": None if unknown else "d" * 64,
                            "summary": "Retained original outcome.", "log_offset": None}})
                record["plan"]["stages"][0].update(state="accepted", evidence=[op["id"] for op in record["operations"][:3]])
                record["plan"]["stages"][1].update(state="blocked", evidence=[record["operations"][-1]["id"]])
                record["history"] = [{"at_ms": now, "kind": "decision", "stage_id": op["stage_id"],
                    "operation_id": op["id"], "summary": f"Original decision {index}."}
                    for index, op in enumerate(record["operations"][:39])]
            originals[record["task_id"]] = write_legacy_task(self.state, record)
        write_legacy_checkpoint(self.state, "markdown", "Retain this original unsupervised checkpoint.\n")
        root = self.state / "tasks"
        raw = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        lock_inode = (self.state / "locks/tasks.lock").stat().st_ino
        tasks.migrate(self.state)
        archive = next(self.state.glob("tasks-legacy-*"))
        self.assertEqual({path.relative_to(archive): path.read_bytes() for path in archive.rglob("*") if path.is_file()}, raw)
        self.assertFalse(root.exists())
        self.assertEqual((self.state / "locks/tasks.lock").stat().st_ino, lock_inode)
        for identity, original in originals.items():
            with self.subTest(format=original["format"]):
                observed = tasks.read(self.state, identity)
                ignored = {"operations", "history", "effect_scopes", "_facts", "_decisions"}
                self.assertEqual({key: value for key, value in observed["record"].items() if key not in ignored},
                                 {key: value for key, value in original.items() if key not in ignored})
                for section in ("operations", "history"):
                    page = tasks.public_read(self.state, identity, section=section, limit=50)
                    self.assertTrue(page["complete"])
                    self.assertEqual([{key: value for key, value in item.items() if key != "sequence"}
                                      for item in page["items"]], original[section])
        observed = tasks.read(self.state, "format-4")
        self.assertEqual((observed["record"]["revision"], observed["facts"]["operation_count"],
                          observed["facts"]["history_count"], observed["facts"]["unknown_count"]), (217, 49, 39, 1))
        self.assertEqual(observed["record"]["control"]["state"], "paused")
        self.assertFalse(tasks.read(self.state, "markdown")["resume_authorized"])
        proof = tasks.check_state(self.state)
        self.assertEqual((proof["structured_task_formats_present"], proof["task_records"]), ([1, 2, 3, 4], 5))


if __name__ == "__main__":
    unittest.main()
