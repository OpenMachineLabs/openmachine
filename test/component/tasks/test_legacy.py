"""Tests for tasks behavior at the component layer."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch
from openmachine import skills, tasks
from openmachine.tasks import store as task_store
from openmachine.config import SkillSources
from openmachine.foundation.state import lease
from test.support.continuity_cases import ContinuityCaseFixture


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
        index = json.loads((self.state / "tasks/index.json").read_text())
        self.assertEqual(set(index["tasks"]["task-one"]), {"title", "anchors", "revision", "sha256"})
        self.assertNotIn("Completed: inspection", json.dumps(index))
        self.assertEqual((self.state / "tasks/task-one/task.md").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "tasks/task-one").stat().st_mode & 0o777, 0o700)

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

    def test_missing_corrupt_and_disappeared_index_are_never_reinitialized(self):
        self.save()
        path = self.state / "tasks/index.json"
        original = path.read_bytes()
        path.write_text("broken")
        self.fault("corrupt_tasks", self.save, "new-task")
        self.assertEqual(path.read_text(), "broken")
        path.write_bytes(original)
        path.unlink()
        self.fault("corrupt_tasks", tasks.find, self.state)
        self.fault("corrupt_tasks", self.save, "new-task")
        self.assertFalse(path.exists())
        (self.state / "tasks").rename(self.state / "retained-task-fixture")
        self.fault("corrupt_tasks", tasks.find, self.state)
        self.fault("corrupt_tasks", self.save, "new-task")
        self.assertFalse((self.state / "tasks").exists())

    def test_torn_checkpoint_index_save_is_reported_and_preserved(self):
        self.save()
        index = (self.state / "tasks/index.json").read_bytes()
        with patch.object(task_store, "save_json", side_effect=OSError("fixture interrupted index write")):
            self.fault("task_write_unverified", self.save, revision=1, body="Updated material progress.\n")
        self.assertEqual((self.state / "tasks/index.json").read_bytes(), index)
        self.assertEqual((self.state / "tasks/task-one/task.md").read_text(), "Updated material progress.\n")
        self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.fault("corrupt_tasks", self.save, revision=1, body="Do not overwrite ambiguous state.")

    def test_task_input_bounds_links_and_orphans_fail_closed(self):
        self.fault("invalid_task_id", self.save, "../outside")
        self.fault("invalid_checkpoint", self.save, body="x" * (tasks.MAX_CHECKPOINT + 1))
        self.fault("invalid_checkpoint", self.save, body="value\0")
        self.assertFalse(self.state.exists())
        self.save()
        directory = self.state / "tasks/orphan"
        directory.mkdir(mode=0o700)
        self.fault("corrupt_tasks", tasks.find, self.state)
        directory.rmdir()
        checkpoint = self.state / "tasks/task-one/task.md"
        retained = self.root / "retained.md"
        checkpoint.rename(retained)
        checkpoint.symlink_to(retained)
        self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.assertTrue(retained.is_file())

    def test_task_lock_privacy_and_unknown_index_fields_are_rejected(self):
        self.save()
        lock = self.state / "locks/tasks.lock"
        lock.chmod(0o644)
        self.fault("corrupt_tasks", self.save, revision=1, body="Do not write through a non-private lock.")
        lock.chmod(0o600)
        path = self.state / "tasks/index.json"
        data = json.loads(path.read_text())
        data["unexpected"] = "not lookup metadata"
        path.write_text(json.dumps(data))
        self.fault("corrupt_tasks", tasks.find, self.state)
        self.fault("corrupt_tasks", self.save, revision=1, body="Do not adopt an unknown index shape.")

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
        checkpoint = (self.state / "tasks/task-one/task.md").read_bytes()
        index = (self.state / "tasks/index.json").read_bytes()
        self.assertEqual(self.save(revision=1, anchors=[anchors[0]])["status"], "unchanged")
        self.fault("invalid_task", self.save, revision=1, body="Would exceed anchor limit.\n",
                   anchors=["anchor=overflow"])
        self.assertEqual((self.state / "tasks/task-one/task.md").read_bytes(), checkpoint)
        self.assertEqual((self.state / "tasks/index.json").read_bytes(), index)
        self.assertEqual(tasks.read(self.state, "task-one")["anchors"], sorted(anchors))

    def test_missing_established_lock_is_not_recreated_while_old_inode_is_held(self):
        self.save()
        lock = self.state / "locks/tasks.lock"
        original = (self.state / "tasks/task-one/task.md").read_bytes()
        with lease(lock):
            lock.unlink()
            self.fault("corrupt_tasks", self.save, revision=1, body="Must not use a replacement lock inode.")
            self.assertFalse(lock.exists())
            self.fault("corrupt_tasks", tasks.read, self.state, "task-one")
        self.assertEqual((self.state / "tasks/task-one/task.md").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
