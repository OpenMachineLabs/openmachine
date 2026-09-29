"""Small real-file checks for Skills, task checkpoints and explicit text editing."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from openmachine import execution, skills, tasks, workspace
from openmachine.config import Settings, SkillSources
from openmachine.sanitize import Fault


class ContinuityTests(unittest.TestCase):
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

    def save(self, identity="task-one", revision=0, body="Completed: inspection.\nNext: run the project test.\n", **kwargs):
        return tasks.save(self.state, identity, revision, kwargs.get("title", "Repair example"), body,
                          kwargs.get("anchors", ["issue=example/project#1"]))

    def fault(self, code, operation, *args, **kwargs):
        with self.assertRaises(Fault) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def wait(self, result):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = execution.read(self.config, self.state, "desktop", result["execution_id"])
            if result["status"] in execution.TERMINAL | {"outcome_unknown"}:
                return result
            time.sleep(0.03)
        self.fail("Owned fixture command did not terminate")

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
        with execution._lease(self.state / "locks/tasks.lock"):
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
        with patch.object(tasks, "_save", side_effect=OSError("fixture interrupted index write")):
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

    def test_original_execution_link_and_legacy_receipts_do_not_replay(self):
        self.save()
        identity = uuid.uuid4().hex
        code = 'from pathlib import Path; p=Path("count.txt"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        args = (self.config, self.state, "desktop", identity, str(self.work), [sys.executable, "-c", code])
        result = self.wait(execution.start(*args, task_id="task-one"))
        self.assertEqual((result["status"], result["task_id"]), ("succeeded", "task-one"))
        self.assertEqual(execution.list_records(self.config, self.state, "desktop")["executions"][0]["task_id"], "task-one")
        (self.state / "tasks/index.json").write_text("unavailable task metadata fixture")
        self.assertEqual(execution.start(*args, task_id="task-one")["status"], "succeeded")
        receipt = self.state / "executions" / identity / "record.json"
        stored = json.loads(receipt.read_text())
        del stored["request"]["task_id"]
        receipt.write_text(json.dumps(stored))
        original = receipt.read_bytes()
        self.assertEqual(execution.start(*args)["status"], "succeeded")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual((self.work / "count.txt").read_text(), "1")

    def skill_fixture(self):
        collection = self.root / "skills"
        collection.mkdir()
        selected = collection / "format"
        selected.mkdir()
        (selected / "SKILL.md").write_text("---\nname: format\ndescription: Check a project before editing.\n---\nOriginal instructions.\n")
        (selected / ".github").mkdir()
        (selected / ".github/policy.md").write_text("Useful dotfile resource.\n")
        (collection / "same-skill").symlink_to(selected, target_is_directory=True)
        (collection / "loop").symlink_to(collection, target_is_directory=True)
        self.config.skills = SkillSources(roots=[str(collection), str(self.root / "unavailable")])
        return collection, selected

    def test_skills_are_explicit_live_and_directory_links_are_deduplicated(self):
        self.assertEqual(self.config.skills.roots, [])
        _, selected = self.skill_fixture()
        listed = skills.inspect(self.config)
        self.assertEqual(len(listed["skills"]), 2)
        self.assertTrue(listed["warnings"])
        source = str(selected / "SKILL.md")
        first = skills.inspect(self.config, "read", source)
        self.assertIn("Original instructions", first["text"])
        (selected / "SKILL.md").write_text("Updated instructions.\n")
        second = skills.inspect(self.config, "read", source)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["text"], "Updated instructions.\n")
        self.fault("source_changed", skills.inspect, self.config, "read", source, expected_snapshot=first["snapshot"])
        self.assertIn("Operate Open Machine", skills.inspect(self.config, "read")["text"])

    def test_skill_resource_files_dotfiles_names_and_escape_are_explicit(self):
        collection, selected = self.skill_fixture()
        other = collection / "other"
        other.mkdir()
        (other / "SKILL.md").write_text("---\nname: format\n---\nAnother source.\n")
        named = skills.inspect(self.config, query="format")["skills"]
        self.assertEqual(len(named), 2)
        self.assertEqual(len({item["skill"] for item in named}), 2)
        source = str(selected / "SKILL.md")
        text = skills.inspect(self.config, "read", source, ".github/policy.md")
        self.assertIn("Useful dotfile", text["text"])
        first = skills.inspect(self.config, "files", source)
        self.assertIn(".github", [item["name"] for item in first["entries"]])
        (selected / "more.md").write_text("new resource")
        self.fault("source_changed", skills.inspect, self.config, "files", source, expected_snapshot=first["snapshot"])
        (selected / "escape.md").symlink_to(self.root / "outside.md")
        (self.root / "outside.md").write_text("outside")
        for path in ("../outside.md", ".env", "escape.md"):
            with self.subTest(path=path), self.assertRaises((Fault, OSError)):
                skills.inspect(self.config, "read", source, path)

    def test_text_write_readback_preconditions_and_permissions(self):
        (self.work / "unrelated.txt").write_text("preserve")
        args = (self.config, self.state, "desktop", "project", "source.py")
        read = workspace.read_text_file(self.config, "desktop", "project", "source.py")
        self.assertFalse(read["exists"])
        first = workspace.write_text_file(*args, "value = 1\n", "", "")
        self.assertEqual(first["status"], "written")
        self.assertEqual(workspace.write_text_file(*args, "value = 1\n", "", "")["status"], "already_current")
        file = self.work / "source.py"
        file.chmod(0o755)
        workspace.write_text_file(*args, "value = 2\n", "", first["sha256"])
        self.assertEqual(file.stat().st_mode & 0o777, 0o755)
        self.fault("content_mismatch", workspace.write_text_file, *args, "stale", "", first["sha256"])
        self.fault("context_mismatch", workspace.write_text_file, *args, "stale", "0" * 40, "")
        self.assertEqual((self.work / "unrelated.txt").read_text(), "preserve")

    def test_file_write_reuses_execution_lock_and_disabled_permission(self):
        running = execution.start(self.config, self.state, "desktop", uuid.uuid4().hex, str(self.work),
                                  [sys.executable, "-c", "import time; time.sleep(0.7)"])
        args = (self.config, self.state, "desktop", "project", "new.py", "value = 1\n", "", "")
        try:
            self.fault("resource_busy", workspace.write_text_file, *args)
        finally:
            self.assertEqual(self.wait(running)["status"], "succeeded")
        self.config.machines["desktop"].execution_enabled = False
        self.fault("execution_disabled", workspace.write_text_file, *args)
        self.assertFalse((self.work / "new.py").exists())

    def test_file_paths_types_bounds_and_utf8_pages(self):
        outside = self.root / "outside.py"
        outside.write_text("preserve")
        (self.work / "link.py").symlink_to(outside)
        os.link(outside, self.work / "hard.py")
        os.mkfifo(self.work / "pipe.py")
        for name in ("../outside.py", ".git/config", ".ENV", "link.py", "hard.py", "pipe.py", "missing/child.py"):
            with self.subTest(name=name), self.assertRaises((Fault, OSError)):
                workspace.write_text_file(self.config, self.state, "desktop", "project", name, "new", "", "")
        for value in ("bad\0", "x" * (workspace.MAX_TEXT_FILE + 1)):
            self.fault("invalid_content", workspace.write_text_file, self.config, self.state, "desktop", "project", "bad.txt", value, "", "")
        content = "sample \u00e9 \u20ac\n" * 5000
        (self.work / "pages.txt").write_text(content)
        offset, parts, digest = 0, [], hashlib.sha256(content.encode()).hexdigest()
        while True:
            page = workspace.read_text_file(self.config, "desktop", "project", "pages.txt", offset, digest)
            parts.append(page["text"])
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(parts), content)
        self.assertEqual(outside.read_text(), "preserve")
