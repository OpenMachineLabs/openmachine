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
from openmachine.execution.records import TERMINAL
from openmachine.foundation.files import MAX_TEXT_FILE
from openmachine.tasks import store as task_store
from openmachine.config import Settings, SkillSources
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease


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
            if result["status"] in TERMINAL | {"outcome_unknown"}:
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
        self.config.skills = SkillSources(roots=[str(root_alias)])
        result = skills.inspect(self.config)
        self.assertEqual(len(result["skills"]), 1)
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

    def test_original_execution_link_and_legacy_receipts_do_not_replay(self):
        self.save()
        app = create_application(self.config, self.state)
        identity = uuid.uuid4().hex
        code = 'from pathlib import Path; p=Path("count.txt"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        args = ("desktop", identity, str(self.work), [sys.executable, "-c", code])
        result = self.wait(app.start_execution(*args, task_id="task-one"))
        self.assertEqual((result["status"], result["task_id"]), ("succeeded", "task-one"))
        self.assertEqual(execution.list_records(self.config, self.state, "desktop")["executions"][0]["task_id"], "task-one")
        (self.state / "tasks/index.json").write_text("unavailable task metadata fixture")
        self.assertEqual(app.start_execution(*args, task_id="task-one")["status"], "succeeded")
        self.fault("execution_conflict", app.start_execution, "desktop", identity, str(self.work),
                   [sys.executable, "-c", "print('changed')"], task_id="task-one")
        new_id = uuid.uuid4().hex
        self.fault("corrupt_tasks", app.start_execution, "desktop", new_id, str(self.work),
                   [sys.executable, "-c", code], task_id="task-one")
        self.assertFalse((self.state / "executions" / new_id).exists())
        receipt = self.state / "executions" / identity / "record.json"
        stored = json.loads(receipt.read_text())
        del stored["request"]["task_id"]
        receipt.write_text(json.dumps(stored))
        original = receipt.read_bytes()
        self.assertEqual(app.start_execution(*args)["status"], "succeeded")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual((self.work / "count.txt").read_text(), "1")

    def test_application_initializes_private_state_before_admission_locks(self):
        app = create_application(self.config, self.state)
        previous_umask = os.umask(0o022)
        try:
            saved = app.save_task("private-root", 0, "Private state", "First direct application write.")
        finally:
            os.umask(previous_umask)
        self.assertEqual(saved["revision"], 1)
        for path in (self.state, self.state / "locks", self.state / "tasks"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "locks/lifecycle.lock").stat().st_mode & 0o777, 0o600)

        unsafe = self.root / "unsafe-state"
        unsafe.mkdir()
        unsafe.chmod(0o755)
        denied = create_application(self.config, unsafe)
        self.fault("unsafe_state", denied.save_task, "not-saved", 0, "Denied", "Keep the original root.")
        self.assertEqual(unsafe.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(unsafe.iterdir()), [])

    def test_application_releases_admission_before_background_work_finishes(self):
        app = create_application(self.config, self.state)
        identity = uuid.uuid4().hex
        code = ('from pathlib import Path\nimport time\n'
                'print("waiting", flush=True)\n'
                'while not Path("release").exists(): time.sleep(0.02)\n')
        started = app.start_execution("desktop", identity, str(self.work), [sys.executable, "-c", code], 10)
        try:
            with lease(self.state / "locks/lifecycle.lock"):
                current = app.read_execution("desktop", identity)
                self.assertNotIn(current["status"], TERMINAL | {"outcome_unknown"})
                self.assertEqual(current["created_at"], started["created_at"])
        finally:
            (self.work / "release").touch()
            final = self.wait(started)
        self.assertEqual(final["status"], "succeeded")
        self.config.machines["desktop"].execution_enabled = False
        self.assertEqual(app.cancel_execution("desktop", identity)["status"], "succeeded")

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
        for value in ("bad\0", "x" * (MAX_TEXT_FILE + 1)):
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


def task_contract(context=None, identity="structured-one"):
    """Disposable declared work, not production authority or worker evidence."""
    targets = [] if context is None else [{"machine": "desktop", "workspace": "project",
        "workspace_identity": context["workspace_identity"], "head_sha": context.get("head_sha") or None}]
    return {"task_id": identity, "update_id": "1" * 32, "title": "Retain fixture work", "anchors": ["fixture=structured"],
        "origin": {"request_ref": "fixture:request", "request_sha256": "a" * 64, "objective": "Verify retained work."},
        "contract": {"objective": "Verify retained work.", "scope": "Disposable fixture only.", "exclusions": "No production effects.",
            "acceptance": "Read the same canonical record.", "stop_condition": "Stop after verification.",
            "authority_refs": ["fixture:consent"], "targets": targets, "repositories": [], "tools": ["read_task"],
            "sources": [], "expires_at_ms": time.time_ns() // 1000000 + 3600000},
        "stages": [{"id": "verify", "depends_on": [], "result": "Retained record", "verification": "Exact readback",
                    "source_refs": [], "source_sha256": hashlib.sha256(b"[]\n").hexdigest()}]}


def terminal_task_fixture(record):
    """Synthetic retained terminal shape for readers; no terminal writer is exposed."""
    value = json.loads(json.dumps(record))
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


class StructuredTaskTests(unittest.TestCase):
    fault = ContinuityTests.fault
    wait = ContinuityTests.wait

    def setUp(self):
        ContinuityTests.setUp(self)
        self.app = create_application(self.config, self.state)
        self.context = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.request = task_contract(self.context)
        self.path = self.state / "tasks/structured-one/task.json"

    def create(self, request=None):
        return self.app.create_task(**(self.request if request is None else request))

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
        from openmachine.tasks import records
        first = self.create()["record"]
        original = self.path.read_bytes()
        values = [records.canonical({**first, **update}) for update in
                  ({"format": True}, {"revision": True}, {"revision": 0}, {"extra": "unknown"},
                   {"task_id": "another"}, {"updated_at_ms": 0})]
        broken = copy.deepcopy(first)
        broken["plan"]["stages"][0]["evidence"] = ["a" * 32]
        values.extend([records.canonical(broken), original.replace(b'"format":1', b'"format":1,"format":1'),
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
        from openmachine.tasks import records
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
        from openmachine.tasks import records
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
        from openmachine.tasks import records
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
        def ready(request):
            original(request)
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


class TaskAdmissionTests(unittest.TestCase):
    setUp = StructuredTaskTests.setUp
    create = StructuredTaskTests.create
    fault = ContinuityTests.fault
    wait = ContinuityTests.wait

    def view(self):
        return self.app.read_task("structured-one")

    def control_arguments(self, action, **values):
        record = self.view()["record"]
        return dict(task_id=record["task_id"], expected_revision=record["revision"],
            expected_plan_revision=record["plan"]["revision"], expected_epoch=record["control"]["epoch"],
            update_id=uuid.uuid4().hex, transition=dict(action=action, **values))

    def control(self, action, **values):
        return self.app.control_task(**self.control_arguments(action, **values))

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

    def test_control_claim_retry_deadline_and_reads_are_passive(self):
        created = self.accept_plan(claim=False)
        raw = self.path.read_bytes()
        self.view()
        self.app.find_tasks()
        self.assertEqual(self.path.read_bytes(), raw)
        args = self.control_arguments("claim", handoff_id=created["record"]["handoff"]["id"])
        claimed = self.app.control_task(**args)
        owner = claimed["record"]["control"]["owner"]
        self.assertEqual(owner["principal_ref"], f"native-uid:{os.getuid()}")
        self.assertLessEqual(owner["expires_at_ms"], claimed["record"]["updated_at_ms"] + 300000)
        with patch.object(task_store.time, "time_ns", return_value=(owner["expires_at_ms"] + 1) * 1000000):
            same = self.app.control_task(**args)
            self.assertEqual(same["status"], "unchanged")
            self.assertEqual(same["record"], claimed["record"])
            next_claim = self.control("claim", handoff_id=created["record"]["handoff"]["id"])
        self.assertEqual(next_claim["record"]["control"]["epoch"], 2)
        self.assertEqual(next_claim["record"]["handoff"]["id"], created["record"]["handoff"]["id"])
        self.fault("task_conflict", self.app.control_task, **args)

    def test_strict_controls_and_context_reject_forged_runtime_facts(self):
        self.accept_plan(claim=False)
        raw = self.path.read_bytes()
        original = self.control_arguments("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        invalid = [dict(original, expected_revision=True), dict(original, expected_epoch=-1),
            dict(original, transition={**original["transition"], "principal_ref": "forged"}),
            dict(original, transition={"action": "unknown"}), dict(original, transition={"action": "pause", "reason": "x" * 513})]
        for args in invalid:
            self.fault("invalid_task_control", self.app.control_task, **args)
        args = self.write_arguments()
        for context in ({**args["task_context"], "principal_ref": "forged"}, {**args["task_context"], "task_revision": True}):
            self.fault("invalid_task_context", self.app.write_workspace_file, **dict(args, task_context=context))
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse((self.work / "result.txt").exists())

    def test_file_admission_original_retry_conflict_and_real_completion(self):
        self.accept_plan()
        args = self.write_arguments()
        first = self.app.write_workspace_file(**args)
        self.assertEqual(first["status"], "applied")
        record = self.view()["record"]
        self.assertEqual(len(record["operations"]), 1)
        self.assertEqual(record["operations"][0]["result"]["status"], "succeeded")
        self.assertEqual(record["handoff"]["reason"], "result_ready")
        same = self.app.write_workspace_file(**args)
        self.assertFalse(same["task_operation_replayed"])
        self.assertEqual(self.view()["record"], record)
        self.fault("task_operation_conflict", self.app.write_workspace_file, **dict(args, content="different"))
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["change_id"]], reason="Verified the exact file result.")
        finished = self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["change_id"]}], limitations="Fixture only.")
        self.assertEqual(finished["record"]["terminal"]["outcome"], "completed")
        snapshot = self.path.read_bytes()
        self.fault("task_terminal", self.control, "resume", reason="Cannot reactivate history.")
        self.view()
        self.assertEqual(self.path.read_bytes(), snapshot)
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_original_execution_finishes_once_without_read_mutations(self):
        self.accept_plan()
        args = self.start_arguments('from pathlib import Path; p=Path("count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")')
        result = self.app.start_execution(**args)
        terminal = self.wait(result)
        self.assertEqual(terminal["status"], "succeeded")
        before = self.path.read_bytes()
        self.app.read_execution("desktop", args["execution_id"])
        self.view()
        self.assertEqual(self.path.read_bytes(), before)
        recovered = self.app.start_execution(**args)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual((self.work / "count").read_text(), "1")
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["execution_id"]], reason="Read the original terminal receipt and counter.")
        self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["execution_id"]}], limitations="No observer was involved.")

    def test_expired_claim_reclaims_final_handoff_without_repeating_accepted_work(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        accepted = self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["change_id"]],
                                reason="Verified original file and receipt.")["record"]
        expired = accepted["control"]["owner"]["expires_at_ms"] + 1
        finish = dict(outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["change_id"]}],
                      limitations="Local evidence; clock advanced only to the original claim deadline.")
        self.app = create_application(self.config, self.state)
        with patch.object(task_store.time, "time_ns", return_value=expired * 1000000):
            self.assertEqual(self.view()["record"], accepted)
            self.fault("task_owner_conflict", self.control, "finish", **finish)
            reclaimed = self.control("claim", handoff_id=accepted["handoff"]["id"])["record"]
            self.assertEqual(reclaimed["plan"], accepted["plan"])
            self.assertEqual(reclaimed["operations"], accepted["operations"])
            self.assertEqual(reclaimed["handoff"], accepted["handoff"])
            self.assertEqual(reclaimed["control"]["epoch"], accepted["control"]["epoch"] + 1)
            done = self.control("finish", **finish)["record"]
        self.assertEqual(done["terminal"]["outcome"], "completed")
        self.assertIsNone(done["handoff"])
        self.assertEqual(done["operations"], accepted["operations"])
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_stale_revision_plan_epoch_and_pause_reject_before_effect(self):
        self.accept_plan()
        args = self.write_arguments()
        for key in ("task_revision", "plan_revision", "continuation_epoch"):
            context = dict(args["task_context"])
            context[key] += 1
            self.fault("task_conflict", self.app.write_workspace_file, **dict(args, task_context=context))
        self.control("pause", reason="Explicit fixture pause.")
        self.fault("task_conflict", self.app.write_workspace_file, **args)
        self.fault("task_stopped", self.app.write_workspace_file, **dict(args, task_context=self.context_for(args["change_id"])))
        self.assertFalse((self.work / "result.txt").exists())
        restarted = create_application(self.config, self.state)
        self.assertEqual(restarted.read_task("structured-one")["record"]["control"]["stop_intent"], "pause")
        resumed = self.control("resume", reason="Explicitly resume the fixture.")
        self.assertIsNone(resumed["record"]["control"]["owner"])
        self.fault("task_owner_conflict", self.app.write_workspace_file, **dict(args, task_context=self.context_for(args["change_id"])))

    def test_pause_can_commit_while_admitted_domain_call_is_waiting(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.accept_plan()
        entered, proceed = threading.Event(), threading.Event()
        args = self.write_arguments()
        original = self.app.machines.write_workspace_file
        def delayed(**values):
            with lease(self.state / "locks/tasks.lock", create=False):
                pass
            entered.set()
            if not proceed.wait(5):
                raise AssertionError("Pause failed to release the domain fixture.")
            return original(**values)
        with patch.object(self.app.machines, "write_workspace_file", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.app.write_workspace_file, **args)
            self.assertTrue(entered.wait(5))
            try:
                paused = self.control("pause", reason="Pause after intent admission, before the existing file effect.")
                self.assertEqual(paused["record"]["control"]["state"], "paused")
            finally:
                proceed.set()
            self.assertEqual(future.result(10)["status"], "applied")
        record = self.view()["record"]
        self.assertEqual(record["control"]["state"], "paused")
        self.assertEqual(record["operations"][0]["result"]["status"], "succeeded")
        self.assertIsNone(record["handoff"])
        self.fault("task_stopped", self.app.write_workspace_file, **self.write_arguments(path="later.txt"))
        self.assertFalse((self.work / "later.txt").exists())

    def test_two_claimants_and_two_intents_have_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        self.accept_plan(claim=False)
        args = self.control_arguments("claim", handoff_id=self.view()["record"]["handoff"]["id"])
        other = dict(args, update_id=uuid.uuid4().hex)
        barrier = threading.Barrier(2)
        def claim(values):
            barrier.wait(5)
            try:
                return self.app.control_task(**values)["status"]
            except Fault as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result(10) for future in [pool.submit(claim, args), pool.submit(claim, other)]]
        self.assertEqual(outcomes.count("saved"), 1)
        self.assertTrue(set(outcomes) <= {"saved", "resource_busy", "task_conflict"})
        first, second = self.write_arguments(path="one.txt"), self.write_arguments(path="two.txt")
        barrier = threading.Barrier(2)
        def write(values):
            barrier.wait(5)
            try:
                return self.app.write_workspace_file(**values)["status"]
            except Fault as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [future.result(10) for future in [pool.submit(write, first), pool.submit(write, second)]]
        self.assertEqual(outcomes.count("applied"), 1)
        self.assertEqual(sum((self.work / name).exists() for name in ("one.txt", "two.txt")), 1)
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_uncertain_admission_fsync_never_dispatches_or_replays(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._sync_directory
        def fail(directory):
            if directory == self.path.parent:
                raise OSError("injected directory sync uncertainty")
            return original(directory)
        with patch.object(task_store, "_sync_directory", side_effect=fail), patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        record = self.view()["record"]
        self.assertEqual(len(record["operations"]), 1)
        self.assertEqual(record["operations"][0]["state"], "admitted")
        self.fault("change_not_found", self.app.write_workspace_file, **args)
        self.assertFalse((self.work / "result.txt").exists())
        self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_lost_file_result_publication_recovers_original_change(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._replace
        def fail(path, raw):
            value = json.loads(raw)
            if path == self.path and value["operations"] and value["operations"][0]["result"] is not None:
                raise OSError("injected result publication failure")
            return original(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail):
            self.fault("task_result_unverified", self.app.write_workspace_file, **args)
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        domain = self.app.read_workspace_change("desktop", args["change_id"])
        self.assertEqual(domain["status"], "applied")
        self.app = create_application(self.config, self.state)
        result = self.app.write_workspace_file(**args)
        self.assertFalse(result["task_operation_replayed"])
        self.assertEqual(self.app.read_workspace_change("desktop", args["change_id"])["created_at"], domain["created_at"])
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")

    def test_stale_index_preserves_pause_and_original_evidence(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(task_store, "_write_index", side_effect=OSError("index unavailable")), \
                patch.object(self.app.machines, "write_workspace_file") as dispatch:
            self.fault("task_index_incomplete", self.app.write_workspace_file, **args)
            dispatch.assert_not_called()
        self.assertEqual(self.view()["index_status"], "stale")
        paused = self.control("pause", reason="Stop remains writable with stale derived metadata.")
        self.assertEqual(paused["record"]["control"]["stop_intent"], "pause")
        self.assertEqual(paused["record"]["operations"][0]["result"]["status"], "failed")
        self.assertFalse((self.work / "result.txt").exists())
        self.app.rebuild_task_index()
        self.assertEqual(self.view()["index_status"], "current")

    def test_current_file_drift_blocks_stage_acceptance(self):
        self.accept_plan()
        args = self.write_arguments()
        self.app.write_workspace_file(**args)
        (self.work / "result.txt").write_text("independent later modification\n")
        raw = self.path.read_bytes()
        self.fault("task_evidence_changed", self.control, "record_decision", stage_id="verify", outcome="accepted",
                   evidence=[args["change_id"]], reason="Stale evidence cannot pass.")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_plan_change_invalidates_dependents_not_independent_acceptance(self):
        from openmachine.tasks import records
        sources = [{"locator": "fixture:a", "sha256": "a" * 64}, {"locator": "fixture:b", "sha256": "b" * 64}]
        self.request["contract"]["sources"] = sources
        base = self.request["stages"][0]
        self.request["stages"] = [dict(base, id="independent", source_refs=["fixture:b"], source_sha256=records.digest([sources[1]])),
            dict(base, id="changed", source_refs=["fixture:a"], source_sha256=records.digest([sources[0]])),
            dict(base, id="dependent", depends_on=["changed"])]
        self.accept_plan()
        self.control("record_decision", stage_id="independent", outcome="accepted", evidence=[], reason="Explicit non-command design decision.")
        changed = json.loads(json.dumps(self.request))
        changed["contract"]["sources"][0]["sha256"] = "c" * 64
        changed["stages"][1]["source_sha256"] = records.digest([changed["contract"]["sources"][0]])
        values = dict(contract=changed["contract"], stages=changed["stages"], invalidated_stage_ids=["changed"], reason="Source changed.")
        self.fault("task_plan_conflict", self.control, "revise_plan", **values)
        values["invalidated_stage_ids"].append("dependent")
        revised = self.control("revise_plan", **values)["record"]
        self.assertEqual([stage["state"] for stage in revised["plan"]["stages"]], ["accepted", "invalidated", "invalidated"])
        self.assertEqual(revised["plan"]["revision"], 2)
        self.assertIsNone(revised["control"]["owner"])
        self.control("claim", handoff_id=revised["handoff"]["id"])
        values["stages"] = values["stages"][1:]
        self.fault("task_plan_conflict", self.control, "revise_plan", **values)

    def test_finish_requires_accepted_stages_not_zero_exit_or_claim(self):
        self.accept_plan()
        self.fault("task_evidence_invalid", self.control, "finish", outcome="completed", acceptance=[], limitations="No evidence.")
        self.fault("task_evidence_invalid", self.control, "record_decision", stage_id="verify", outcome="accepted",
                   evidence=["a" * 32], reason="Cannot invent an operation.")
        self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[], reason="Explicit non-command decision.")
        done = self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": None}], limitations="No command was required.")
        self.assertEqual(done["record"]["control"]["state"], "completed")

    def test_control_same_update_id_different_intent_is_rejected(self):
        self.accept_plan()
        args = self.control_arguments("pause", reason="First pause.")
        self.app.control_task(**args)
        raw = self.path.read_bytes()
        changed = {**args, "transition": {"action": "cancel", "reason": "Changed intent."}}
        self.fault("task_conflict", self.app.control_task, **changed)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.app.control_task(**args)["status"], "unchanged")

    def test_capacity_reserves_stop_without_deleting_history(self):
        from openmachine.tasks import records
        self.accept_plan()
        def fill(value, now):
            while len(value["history"]) < 60:
                records.material(value, now, "decision", "Retained material decision.")
        tasks.update(self.state, "structured-one", fill)
        raw = self.path.read_bytes()
        self.fault("task_capacity", self.control, "release", reason="Cannot consume terminal reserve.")
        self.assertEqual(self.path.read_bytes(), raw)
        self.control("pause", reason="Capacity must not prevent stop.")
        done = self.control("cancel", reason="No operations remain to cancel.")
        self.assertEqual(done["record"]["control"]["state"], "cancelled")
        self.assertLessEqual(len(done["record"]["history"]), 64)
        self.assertEqual(done["record"]["history"][:60], json.loads(raw)["history"])

    def test_oversized_record_binding_lookup_is_bounded_and_nonmutating(self):
        self.accept_plan()
        self.path.write_bytes(b"x" * (tasks.MAX_INDEX + 1))
        before = self.path.stat()
        locator = {"kind": "change", "target_identity": None, "change_id": "a" * 32}
        self.fault("corrupt_tasks", tasks.operation_binding, self.state, tasks.operation_key(locator))
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.stat().st_size, tasks.MAX_INDEX + 1)

    def test_supervised_known_identity_cannot_be_stripped_or_rebound(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(task_store, "_sync_directory", side_effect=OSError("uncertain admission")):
            self.fault("task_write_unverified", self.app.write_workspace_file, **args)
        self.app.rebuild_task_index()
        bare = {key: value for key, value in args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.write_workspace_file, **bare)
        other = task_contract(self.context, "other")
        other["contract"]["tools"] = self.request["contract"]["tools"]
        created = self.app.create_task(**other)["record"]
        claimed = self.app.control_task("other", 1, 1, 0, uuid.uuid4().hex,
            {"action": "claim", "handoff_id": created["handoff"]["id"]})["record"]
        context = dict(task_id="other", task_revision=claimed["revision"], plan_revision=1,
                       continuation_epoch=claimed["control"]["epoch"], operation_id=args["change_id"])
        self.fault("task_operation_conflict", self.app.write_workspace_file, **dict(args, task_context=context))
        self.assertFalse((self.work / "result.txt").exists())

    def test_scope_grant_revocation_and_cwd_escape_fail_before_intent(self):
        self.accept_plan()
        args = self.write_arguments()
        self.config.machines["desktop"].workspace_write_enabled = False
        self.fault("workspace_write_disabled", self.app.write_workspace_file, **args)
        self.config.machines["desktop"].workspace_write_enabled = True
        outside = self.root / "outside"
        outside.mkdir()
        (self.work / "escape").symlink_to(outside, target_is_directory=True)
        start = self.start_arguments()
        self.fault("path_denied", self.app.start_execution, **dict(start, cwd=str(self.work / "escape")))
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_live_command_cancel_and_pause_do_not_relaunch(self):
        self.accept_plan()
        args = self.start_arguments('import time; from pathlib import Path; Path("started").write_text("once"); time.sleep(30)')
        result = self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        self.control("pause", reason="Keep the original command while blocking new work.")
        self.assertNotIn(self.app.read_execution("desktop", args["execution_id"])["status"], TERMINAL)
        cancelled = self.control("cancel", reason="Explicitly terminate only the owned fixture.")
        terminal = self.wait(result)
        self.assertEqual(terminal["status"], "cancelled")
        if cancelled["record"]["terminal"] is None:
            cancelled = self.control("cancel", reason="Read the original cancellation outcome.")
        self.assertEqual(cancelled["record"]["terminal"]["outcome"], "cancelled")
        self.assertEqual(len([item for item in cancelled["record"]["operations"] if item["id"] == args["execution_id"]]), 1)

    def test_all_three_file_mutations_use_original_domain_receipts(self):
        self.accept_plan()
        first = self.write_arguments()
        self.app.write_workspace_file(**first)
        second = uuid.uuid4().hex
        result = self.app.edit_workspace_file("desktop", "project", "result.txt", [{"old_text": "original", "new_text": "edited"}],
            self.context["workspace_identity"], "", hashlib.sha256(b"original\n").hexdigest(), second,
            task_context=self.context_for(second))
        self.assertEqual(result["status"], "applied")
        third = uuid.uuid4().hex
        result = self.app.apply_workspace_patch("desktop", "project", "*** Begin Patch\n*** Add File: other.txt\n+patched\n*** End Patch\n",
            self.context["workspace_identity"], "", [{"path": "other.txt", "expected_sha256": ""}], change_id=third,
            task_context=self.context_for(third))
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.work / "result.txt").read_text(), "edited\n")
        self.assertEqual((self.work / "other.txt").read_text(), "patched\n")
        self.assertEqual([item["result"]["status"] for item in self.view()["record"]["operations"]], ["succeeded"] * 3)

    def test_ambiguous_busy_after_dispatch_is_not_proof_of_no_effect(self):
        self.accept_plan()
        args = self.write_arguments()
        with patch.object(self.app.machines, "write_workspace_file", side_effect=Fault("resource_busy", "Uncertain domain observation.")):
            self.fault("resource_busy", self.app.write_workspace_file, **args)
        record = self.view()["record"]
        self.assertEqual(record["operations"][0]["state"], "unknown")
        self.assertEqual(record["operations"][0]["result"]["status"], "unknown")
        self.fault("task_effect_unresolved", self.app.write_workspace_file, **self.write_arguments(path="second.txt"))
        self.assertFalse((self.work / "second.txt").exists())

    def test_original_cancel_remains_available_with_busy_task_identity(self):
        from openmachine.tasks import records
        self.accept_plan()
        args = self.start_arguments("import time; time.sleep(30)")
        original = self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        key = ("execution", "local", args["execution_id"])
        with lease(self.state / "locks" / ("task-effect-" + records.digest(list(key)) + ".lock")):
            self.app.cancel_execution("desktop", args["execution_id"])
        self.assertEqual(self.wait(original)["status"], "cancelled")

    def test_provider_unknown_write_keeps_original_identity_and_never_resubmits(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        provider = SimpleNamespace(read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            issue=Mock(return_value={"status": "write_outcome_unknown"}), reconcile=Mock())
        self.app.github = provider
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "read_task", "control_task"])
        identity = uuid.uuid4().hex
        args = dict(action="create", repository="Owner/repo", title="Publication fixture", body="Do not copy this body into task history.",
                    task_context=self.context_for(identity))
        result = self.app.github_issue(**args)
        self.assertEqual(result["status"], "write_outcome_unknown")
        self.assertIn("<!-- om-task:structured-one:" + identity + " -->", provider.issue.call_args.kwargs["body"])
        self.assertNotIn(args["body"], self.path.read_text())
        provider.reconcile.return_value = {"status": "verified", "receipt": {"id": 71, "number": 7, "updated_at": "fixture"}}
        recovered = self.app.github_issue(**args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(provider.issue.call_count, 1)
        self.assertEqual(self.view()["record"]["operations"][0]["locator"]["number"], 7)
        self.assertEqual(self.view()["record"]["operations"][0]["result"]["status"], "succeeded")
        self.fault("task_operation_conflict", self.app.github_issue, **dict(args, title="Different request"))
        self.assertEqual(provider.issue.call_count, 1)

    def test_bodyless_provider_update_cannot_strip_known_pending_context(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        provider = SimpleNamespace(read=Mock(return_value={"configured_write": True, "data": {"permissions": {"push": True}}}),
            issue=Mock(return_value={"status": "write_outcome_unknown"}), reconcile=Mock(return_value={"status": "write_outcome_unknown"}))
        self.app.github = provider
        self.request["contract"]["repositories"] = ["Owner/repo"]
        self.accept_plan(tools=["github_issue", "read_task", "control_task"])
        identity = uuid.uuid4().hex
        args = dict(action="update", repository="Owner/repo", title="New title", number=7,
                    task_context=self.context_for(identity))
        self.app.github_issue(**args)
        self.assertEqual(self.view()["record"]["operations"][0]["state"], "unknown")
        bare = {key: value for key, value in args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.github_issue, **bare)
        self.assertEqual(provider.issue.call_count, 1)
        recovered = self.app.github_issue(**args)
        self.assertEqual(recovered["status"], "write_outcome_unknown")
        self.assertEqual(provider.issue.call_count, 1)

    def test_all_provider_entrypoints_reject_stale_context_before_provider_access(self):
        self.accept_plan()
        context = self.context_for(uuid.uuid4().hex)
        self.control("pause", reason="Reject all old provider continuations.")
        cases = [(self.app.github_issue, dict(action="create", repository="Owner/repo", title="fixture", body="fixture")),
            (self.app.github_pull_request, dict(action="create", repository="Owner/repo", title="fixture", body="fixture", head="topic", base="main")),
            (self.app.github_comment, dict(action="create", repository="Owner/repo", body="fixture", number=1)),
            (self.app.github_review, dict(action="create", repository="Owner/repo", body="fixture", number=1, expected_head_sha="a" * 40)),
            (self.app.github_merge, dict(repository="Owner/repo", number=1, expected_head_sha="a" * 40))]
        with patch.object(self.app, "_provider", side_effect=AssertionError("Stale request reached provider.")) as provider:
            for method, arguments in cases:
                self.fault("task_conflict", method, **arguments, task_context=context)
            provider.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_pty_input_lost_task_reply_recovers_without_sending_again(self):
        self.accept_plan()
        code = 'from pathlib import Path; import sys; print("READY",flush=True)\nfor line in sys.stdin:\n p=Path("input-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("ACK",flush=True)'
        args = self.start_arguments(code, tty=True)
        self.app.start_execution(**args)
        self.addCleanup(lambda: self.app.cancel_execution("desktop", args["execution_id"]))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            original = self.app.read_execution("desktop", args["execution_id"])
            if original["control_ready"] and "READY" in original["output"]:
                break
            time.sleep(0.02)
        self.assertTrue(original["control_ready"])
        self.assertIn("READY", original["output"])
        self.app.start_execution(**args)
        identity = uuid.uuid4().hex
        input_args = dict(machine="desktop", execution_id=args["execution_id"], expected_control_seq=0,
            action="write_stdin", text="only-once-fixture-input\n", task_context=self.context_for(identity))
        original_replace = task_store._replace
        def fail(path, raw):
            value = json.loads(raw)
            if path == self.path and len(value["operations"]) == 2 and value["operations"][1]["result"] is not None:
                raise OSError("Lost task publication after terminal input.")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail):
            self.fault("task_result_unverified", self.app.control_execution, **input_args)
        deadline = time.monotonic() + 5
        counter = self.work / "input-count"
        while time.monotonic() < deadline and not counter.exists():
            time.sleep(0.02)
        self.assertEqual(counter.read_text(), "1")
        recovered = self.app.control_execution(**input_args)
        self.assertFalse(recovered["task_operation_replayed"])
        self.assertEqual(recovered["control_result"]["seq"], 1)
        self.assertEqual(counter.read_text(), "1")
        self.assertNotIn("only-once-fixture-input", self.path.read_text())
        self.fault("task_operation_conflict", self.app.control_execution,
            **dict(input_args, task_context=self.context_for(uuid.uuid4().hex)))
        bare = {key: value for key, value in input_args.items() if key != "task_context"}
        self.fault("task_context_required", self.app.control_execution, **dict(bare, expected_control_seq=1))
        self.app.cancel_execution("desktop", args["execution_id"])
        self.assertEqual(self.wait(original)["status"], "cancelled")
        self.assertEqual(counter.read_text(), "1")

    def test_actual_process_loss_before_and_after_control_publication(self):
        for phase, exit_code in (("before", 17), ("after", 18)):
            state = self.root / ("control-crash-" + phase)
            app = create_application(self.config, state)
            request = task_contract(self.context, "crash-" + phase)
            created = app.create_task(**request)["record"]
            args = dict(task_id=request["task_id"], expected_revision=1, expected_plan_revision=1, expected_epoch=0,
                update_id=uuid.uuid4().hex, transition={"action": "claim", "handoff_id": created["handoff"]["id"]})
            payload = self.root / ("control-crash-" + phase + ".json")
            payload.write_text(json.dumps({"config": self.config.model_dump(mode="json"), "state": str(state), "request": args}))
            code = '''import json,os,sys
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.tasks import store
value=json.loads(Path(sys.argv[1]).read_text())
original=store._replace
def stop(path,raw):
 if path.name=="task.json" and sys.argv[2]=="before": os._exit(17)
 original(path,raw)
 if path.name=="task.json": os._exit(18)
store._replace=stop
create_application(Settings.model_validate(value["config"]),Path(value["state"])).control_task(**value["request"])
'''
            result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            observed = app.read_task(args["task_id"])
            self.assertEqual(observed["record"]["revision"], 1 if phase == "before" else 2)
            if phase == "after":
                self.assertEqual(observed["index_status"], "stale")
            original_record = observed["record"]
            recovered = app.control_task(**args)
            self.assertEqual(recovered["record"]["revision"], 2)
            if phase == "after":
                self.assertEqual(recovered["record"], original_record)
                self.assertEqual(recovered["status"], "unchanged")
            self.assertEqual(app.control_task(**args)["record"], recovered["record"])
            self.assertFalse((state / "executions").exists())


class TaskObserverTests(unittest.TestCase):
    create = StructuredTaskTests.create
    fault = ContinuityTests.fault
    wait = ContinuityTests.wait
    view = TaskAdmissionTests.view
    control_arguments = TaskAdmissionTests.control_arguments
    control = TaskAdmissionTests.control
    context_for = TaskAdmissionTests.context_for
    accept_plan = TaskAdmissionTests.accept_plan
    start_arguments = TaskAdmissionTests.start_arguments
    write_arguments = TaskAdmissionTests.write_arguments

    def setUp(self):
        StructuredTaskTests.setUp(self)
        from openmachine.app import task_observer
        self.observer_module = task_observer
        # Only unit scheduling is accelerated. Real process/MCP tests use S0's 2s.
        self.interval = patch.object(task_observer, "INTERVAL_MS", 100)
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
        return json.loads((self.state / "tasks" / identity / "task.json").read_bytes())

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

    def seed(self, count=1):
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
                record["control"]["state"] = record["plan"]["stages"][0]["state"] = "running"
                record["handoff"] = None
            tasks.update(self.state, identity, pending)
            identities.append(identity)
        return identities

    def fixture_result(self, operation):
        return {"execution_id": operation["id"], "status": "succeeded", "exit_code": 0,
                "cleanup": {"status": "verified"}, "created_at": 1.0}

    def test_construction_reads_and_discovery_never_start_or_initialize_an_observer(self):
        self.assertFalse(self.state.exists())
        self.assertEqual(tasks.observation_page(self.state, {})["entries"], [])
        self.assertFalse(self.state.exists())
        self.assertIsNone(self.app.task_observer._thread)
        self.accept_plan()
        before = self.path.read_bytes()
        for _ in range(3):
            result = self.view()
            self.app.find_tasks()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(result["support"]["local_result_observation"])
        self.assertEqual(result["observations"][0]["state"], "unknown")
        self.assertFalse((self.state / "locks/task-observer.lock").exists())
        self.interval.stop()
        self.assertEqual((self.observer_module.INTERVAL_MS, self.observer_module.READ_SECONDS,
            self.observer_module.MAX_READERS, self.observer_module.MAX_ACTIVE,
            self.observer_module.EPISODE_MS, self.observer_module.MAX_ATTEMPTS), (2000, 2.0, 4, 32, 3600000, 120))

    def test_absent_caller_result_and_handoff_publish_without_replaying_or_accepting(self):
        args, started = self.owned()
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No background effects")), observer.running():
            self.until(lambda: observer._snapshot["last"].get("structured-one"))
            before = self.raw()
            progress = before["operations"][0]["observed_at_ms"]
            observed = observer._snapshot["last"]["structured-one"][args["execution_id"]]
            self.until(lambda: observer._snapshot["last"].get("structured-one", {}).get(args["execution_id"], 0) > observed)
            self.assertEqual(self.raw(), before, "Quiet observations must not revise canonical history")
            self.assertEqual(self.raw()["operations"][0]["observed_at_ms"], progress)
            (self.work / "release").touch()
            # Only read durable bytes after the caller leaves; no Application
            # reconciliation, task control, execution retry or result-save call.
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
            final = self.raw()
            self.assertEqual(final["handoff"]["reason"], "result_ready")
            self.assertEqual(final["control"]["state"], "waiting_for_ai")
            self.assertEqual(final["control"]["owner"], before["control"]["owner"])
            self.assertEqual(final["plan"]["stages"][0]["state"], "running")
            self.assertIsNone(final["terminal"])
            self.assertEqual(final["operations"][0]["id"], args["execution_id"])
            self.until(lambda: "structured-one" not in observer._snapshot["active"])
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(self.wait(started)["created_at"], started["created_at"])

    def test_observer_restart_collects_a_result_completed_while_it_was_stopped(self):
        args, started = self.owned()
        with self.app.task_observer.running():
            self.until(lambda: self.app.task_observer._snapshot["last"].get("structured-one"))
        before = self.path.read_bytes()
        (self.work / "release").touch()
        original = self.wait(started)
        self.assertEqual(original["status"], "succeeded")
        self.assertEqual(self.path.read_bytes(), before)
        other = create_application(self.config, self.state)
        with other.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual(len(self.raw()["operations"]), 1)
        self.assertEqual(self.raw()["operations"][0]["id"], args["execution_id"])
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_lost_file_result_is_collected_from_the_original_change_only(self):
        self.accept_plan()
        args = self.write_arguments()
        original = task_store._replace
        def fail_result(path, raw):
            if path.name == "task.json" and any(item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost result publication")
            return original(path, raw)
        with patch.object(task_store, "_replace", side_effect=fail_result):
            self.fault("task_result_unverified", self.app.write_workspace_file, **args)
        receipt = self.app.read_workspace_change("desktop", args["change_id"])
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No repeated change")), self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")
        self.assertEqual(self.app.read_workspace_change("desktop", args["change_id"])["request_sha256"], receipt["request_sha256"])
        self.assertEqual(self.raw()["handoff"]["reason"], "result_ready")

    def test_pause_wins_over_a_late_read_and_original_results_still_arrive(self):
        import threading
        _, started = self.owned()
        entered, release = threading.Event(), threading.Event()
        original = self.app.task_admission._read
        def delayed(operation):
            result = original(operation)
            if not entered.is_set():
                entered.set()
                release.wait(5)
            return result
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=delayed), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                paused = self.control("pause", reason="Pause during an original read.")["record"]
                release.set()
                (self.work / "release").touch()
                self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
                record = self.raw()
                self.assertEqual(record["control"], paused["control"])
                self.assertIsNone(record["handoff"])
                self.assertIsNone(record["terminal"])
        finally:
            release.set()
        self.assertEqual(self.wait(started)["status"], "succeeded")
        self.assertEqual((self.work / "counter").read_text(), "1")

    def test_late_background_result_cannot_change_finished_history(self):
        import threading
        args, started = self.owned()
        (self.work / "release").touch()
        self.wait(started)
        entered, release = threading.Event(), threading.Event()
        original = self.app.task_admission._read
        def delayed(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if threading.current_thread().name == "om-task-read":
                entered.set()
                release.wait(5)
            return result
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=delayed), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                self.app.task_admission.reconcile(self.view())
                self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[args["execution_id"]], reason="Read the original result.")
                self.control("finish", outcome="completed", acceptance=[{"stage_id": "verify", "operation_id": args["execution_id"]}], limitations="Local fixture.")
                final = self.path.read_bytes()
                release.set()
                self.until(lambda: not self.app.task_observer._reads)
                self.view()
                self.assertEqual(self.path.read_bytes(), final)
                self.assertEqual(self.view()["observations"][1]["state"], "retired")
        finally:
            release.set()

    def test_failure_episode_pause_restart_and_cooldown_preserve_attempts(self):
        identity = self.seed()[0]
        observer = self.app.task_observer
        attempts = []
        def unavailable(operation):
            recovery = self.raw(identity)["recovery"]
            attempts.append(None if recovery is None else recovery["attempts"])
            raise Fault("source_unavailable", "Disposable observation failure")
        with patch.object(self.app.task_admission, "_read", side_effect=unavailable), patch.object(self.observer_module.random, "uniform", return_value=1):
            with observer.running():
                self.until(lambda: self.raw(identity)["recovery"])
            first = self.raw(identity)["recovery"]
            record = self.app.read_task(identity)["record"]
            paused = self.app.control_task(task_id=identity, expected_revision=record["revision"], expected_plan_revision=1,
                expected_epoch=0, update_id=uuid.uuid4().hex, transition={"action": "pause", "reason": "Keep the observation budget."})["record"]
            self.assertEqual(paused["recovery"], first)
            with observer.running():
                self.until(lambda: self.raw(identity)["recovery"]["attempts"] == 3)
            record = self.raw(identity)
            self.assertEqual(attempts, [None, 2, 3], "Each retry must reserve its attempt before reading")
            self.assertEqual(record["recovery"]["episode_id"], first["episode_id"])
            self.assertEqual(record["recovery"]["deadline_ms"], first["deadline_ms"])
            self.assertGreater(record["recovery"]["next_attempt_ms"] - record["updated_at_ms"], 29000)
            self.assertEqual(record["control"]["state"], "paused")
            self.assertIsNone(record["handoff"])
            self.assertEqual(sum(item["kind"] == "recovery" for item in record["history"]), 1)
            snapshot = self.path.parent.parent / identity / "task.json"
            before = snapshot.read_bytes()
            with observer.running():
                time.sleep(0.15)
            self.assertEqual(snapshot.read_bytes(), before)

    def test_exhausted_and_permission_episodes_do_not_restart_reads(self):
        identity = self.seed()[0]
        def exhausted(record, now):
            self.observer_module.fail_observation(record, now, now, "source_unavailable")
            record["recovery"]["attempts"] = 120
        tasks.update(self.state, identity, exhausted)
        before = self.raw(identity)
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("Budget exhausted")) as read:
            for _ in range(2):
                with self.app.task_observer.running():
                    self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity))
                read.assert_not_called()
        self.assertEqual(self.raw(identity), before)
        self.assertEqual(self.app.read_task(identity)["observations"][1]["blocker"], "observation_budget_exhausted")
        tasks.update(self.state, identity, lambda record, now: record.update(recovery=None))
        with patch.object(self.app.task_admission, "_read", side_effect=Fault("path_denied", "Never retry permission")) as read:
            with self.app.task_observer.running():
                self.until(lambda: self.raw(identity)["recovery"])
                time.sleep(0.2)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.raw(identity)["recovery"]["reason_code"], "blocked_path_denied")

    def test_timed_out_threads_keep_all_four_slots_and_owner_until_actual_exit(self):
        import threading
        identities = self.seed(5)
        release = threading.Event()
        entered, lock = [], threading.Lock()
        def slow(operation):
            with lock:
                entered.append(operation["id"])
            release.wait(10)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow):
                observer.start()
                self.until(lambda: len(entered) == 4)
                with lease(self.state / "locks/lifecycle.lock"), lease(self.state / "locks/tasks.lock"):
                    pass
                self.until(lambda: all(self.raw(identity)["recovery"] for identity in identities[:4]))
                self.assertEqual(len(entered), 4)
                self.assertEqual(len(observer._reads), 4)
                self.assertIsNone(self.raw(identities[4])["recovery"])
                self.until(lambda: self.app.read_task(identities[0]))
                observer.stop()
                with self.assertRaises(Fault):
                    with lease(self.state / "locks/task-observer.lock"):
                        pass
                self.fault("task_observer_busy", observer.start)
                release.set()
                self.until(lambda: not observer._thread.is_alive())
                self.assertEqual(len(entered), 4)
                with lease(self.state / "locks/task-observer.lock"):
                    pass
        finally:
            release.set()
            observer.stop()

    def test_coordinator_failure_does_not_release_a_live_reader_owner(self):
        import threading
        self.seed()
        release, entered = threading.Event(), threading.Event()
        def slow(operation):
            entered.set()
            release.wait(10)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow):
                observer.start()
                self.assertTrue(entered.wait(3))
                with patch.object(tasks, "observation_page", side_effect=RuntimeError("Disposable coordinator failure")):
                    observer.notify()
                    self.until(lambda: observer._snapshot["state"] == "blocked")
                    with self.assertRaises(Fault):
                        with lease(self.state / "locks/task-observer.lock"):
                            pass
                release.set()
                observer.stop()
        finally:
            release.set()
            observer.stop()

    def test_no_remote_or_provider_observation_and_changed_local_route_is_blocked(self):
        identities = self.seed(3)
        def remote(record, now):
            record["operations"][0]["locator"]["target_identity"] = "a" * 64
        def provider(record, now):
            record["operations"][0]["locator"] = {"kind": "github", "repository": "Example/repo", "object_type": "issue",
                "number": 1, "parent_number": None, "anchor": "fixture-observation"}
        tasks.update(self.state, identities[0], remote)
        tasks.update(self.state, identities[1], provider)
        del self.config.machines["desktop"]
        with patch.object(self.app.task_admission, "_read", side_effect=AssertionError("No alternate route")) as read, self.app.task_observer.running():
            self.until(lambda: self.raw(identities[2])["recovery"])
            time.sleep(0.2)
            read.assert_not_called()
            self.assertEqual(set(self.app.task_observer._known), set(identities))
        self.assertIsNone(self.raw(identities[0])["recovery"])
        self.assertIsNone(self.raw(identities[1])["recovery"])
        self.assertTrue(self.raw(identities[2])["recovery"]["reason_code"].startswith("blocked_"))

    def test_discovery_pages_do_not_reread_unchanged_history_and_report_stale_index(self):
        for index in range(55):
            self.app.create_task(**task_contract(self.context, f"history-{index:03}"))
        first = tasks.observation_page(self.state, {})
        self.assertEqual(len(first["entries"]), 50)
        self.assertTrue(first["has_more"])
        known = {item["task_id"]: item["metadata_sha256"] for item in first["entries"]}
        second = tasks.observation_page(self.state, known)
        self.assertEqual(len(second["entries"]), 5)
        known.update({item["task_id"]: item["metadata_sha256"] for item in second["entries"]})
        with patch.object(task_store, "read_structured", side_effect=AssertionError("Do not poll history")):
            self.assertEqual(tasks.observation_page(self.state, known)["entries"], [])
        with patch.object(task_store, "_write_index", side_effect=OSError("Stale derived index")):
            tasks.update(self.state, "history-000", lambda record, now: record["control"].update(pending_decision="Fixture pending decision."))
        observed = tasks.observation_page(self.state, {})
        self.assertFalse(observed["complete"])
        self.assertEqual(observed["entries"][0]["record"]["control"]["pending_decision"], "Fixture pending decision.")
        self.assertEqual(tasks.read(self.state, "history-000")["index_status"], "stale")

    def test_service_admission_blocks_publication_but_not_original_reads(self):
        import threading
        self.seed()
        entered, release = threading.Event(), threading.Event()
        def slow(operation):
            entered.set()
            release.wait(5)
            return self.fixture_result(operation)
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow), self.app.task_observer.running():
                self.assertTrue(entered.wait(3))
                with lease(self.state / "locks/lifecycle.lock"):
                    release.set()
                    time.sleep(0.2)
                    self.assertIsNone(self.raw("observer-000")["operations"][0]["result"])
                    self.app.read_task("observer-000")
                self.until(lambda: self.raw("observer-000")["operations"][0]["result"])
        finally:
            release.set()

    def publication_crash(self, phase):
        _, started = self.owned()
        (self.work / "release").touch()
        self.wait(started)
        payload = self.root / "observer-crash.json"
        payload.write_text(json.dumps({"config": self.config.model_dump(mode="json"), "state": str(self.state)}))
        code = '''import json,os,sys,time
from pathlib import Path
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.tasks import store
value=json.loads(Path(sys.argv[1]).read_text())
original=store._replace
def crash(path,raw):
 if path.name=="task.json" and any((item["result"] or {}).get("status")=="succeeded" for item in json.loads(raw)["operations"]):
  if sys.argv[2]=="before": os._exit(67)
  original(path,raw)
  os._exit(68)
 return original(path,raw)
store._replace=crash
app=create_application(Settings.model_validate(value["config"]),Path(value["state"]))
with app.task_observer.running(): time.sleep(12)
os._exit(69)
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(payload), phase], capture_output=True, text=True, timeout=18)
        self.assertEqual(result.returncode, 67 if phase == "before" else 68, result.stderr)
        prior = self.raw()
        with self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
            self.until(lambda: self.app.task_observer._snapshot["state"] == "running")
        final = self.raw()
        self.assertEqual(final["handoff"]["reason"], "result_ready")
        if phase == "after":
            self.assertEqual(final, prior, "A committed result/handoff must not be republished")
            self.assertEqual(self.view()["index_status"], "stale")
        self.assertEqual((self.work / "counter").read_text(), "1")
        self.assertEqual(len(final["operations"]), 1)
        self.assertEqual(self.wait(started)["created_at"], started["created_at"])

    def test_process_loss_before_result_publication_retains_original_work(self):
        self.publication_crash("before")

    def test_process_loss_after_result_publication_retains_the_same_handoff(self):
        self.publication_crash("after")

    def test_start_during_lifecycle_activation_waits_without_losing_original_work(self):
        identity = self.seed()[0]
        observer = self.app.task_observer
        with patch.object(self.app.task_admission, "_read", side_effect=self.fixture_result) as read:
            with lease(self.state / "locks/lifecycle.lock"):
                observer.start()
                time.sleep(0.15)
                read.assert_not_called()
                self.assertIsNone(self.raw(identity)["operations"][0]["result"])
            self.until(lambda: (self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded")
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.raw(identity)["handoff"]["reason"], "result_ready")

    def test_active_capacity_is_visible_and_overflow_is_collected_after_retirement(self):
        import threading
        identities = self.seed(35)
        release = threading.Event()
        observed = []
        def slow(operation):
            observed.append(operation["id"])
            release.wait(8)
            return self.fixture_result(operation)
        observer = self.app.task_observer
        try:
            with patch.object(self.app.task_admission, "_read", side_effect=slow), observer.running():
                self.until(lambda: len(observer._snapshot["active"]) == 32)
                self.until(lambda: observer._snapshot["errors"].get(identities[-1]) == "task_observer_capacity")
                self.assertLessEqual(len(observer._reads), 4)
                self.assertEqual(self.app.read_task(identities[-1])["observations"][1]["blocker"], "task_observer_capacity")
                release.set()
                self.until(lambda: all((self.raw(identity)["operations"][0]["result"] or {}).get("status") == "succeeded" for identity in identities))
                self.until(lambda: not observer._snapshot["active"])
                count = len(observed)
                time.sleep(0.2)
                self.assertEqual(len(observed), count)
                self.assertEqual(count, 35)
        finally:
            release.set()
            observer.stop()

    def test_corrupt_discovery_reads_still_charge_the_aggregate_byte_budget(self):
        for index in range(35):
            identity = f"corrupt-{index:03}"
            self.app.create_task(**task_contract(self.context, identity))
        for path in (self.state / "tasks").glob("corrupt-*/task.json"):
            path.write_bytes(b"x" * tasks.MAX_RECORD)
        # Invalid retained bytes are intentional fixture corruption. They must
        # not bypass the 2 MiB budget, be repaired, or be treated as missing work.
        page = tasks.observation_page(self.state, {})
        self.assertEqual(len(page["entries"]), 32)
        self.assertTrue(page["has_more"])
        self.assertFalse(page["complete"])
        self.assertTrue(all(item["error"] is not None for item in page["entries"]))
        self.assertEqual((self.state / "tasks/corrupt-000/task.json").read_bytes(), b"x" * tasks.MAX_RECORD)

    def test_stopped_observer_does_not_advertise_a_live_retry_timer(self):
        identity = self.seed()[0]
        with patch.object(self.app.task_admission, "_read", side_effect=Fault("source_unavailable", "Fixture outage")), self.app.task_observer.running():
            self.until(lambda: self.raw(identity)["recovery"])
        before = self.raw(identity)
        view = self.app.read_task(identity)
        runtime = view["observations"][1]
        self.assertEqual(runtime["state"], "stopped")
        self.assertIsNone(runtime["next_attempt_at_ms"])
        self.assertIsNotNone(view["record"]["recovery"]["next_attempt_ms"])
        self.assertEqual(self.raw(identity), before)

    def test_original_pty_input_result_is_collected_without_sending_input_again(self):
        self.accept_plan()
        code = ('from pathlib import Path\nimport sys,time\nprint("READY",flush=True)\n'
            'line=sys.stdin.readline()\np=Path("input-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
            'print("ACK",flush=True)\nwhile not Path("input-release").exists(): time.sleep(0.02)\n')
        args = self.start_arguments(code, tty=True, timeout_seconds=15)
        started = self.app.start_execution(**args)
        self.addCleanup(lambda: self.wait(self.app.cancel_execution("desktop", args["execution_id"])))
        def ready():
            original = self.app.read_execution("desktop", args["execution_id"])
            return original if original["control_ready"] and "READY" in original["output"] else None
        self.until(ready)
        self.app.task_admission.reconcile(self.view())
        operation_id = uuid.uuid4().hex
        input_args = dict(machine="desktop", execution_id=args["execution_id"], expected_control_seq=0,
            action="write_stdin", text="single-observed-pty-input\n", task_context=self.context_for(operation_id))
        original_replace = task_store._replace
        def lose_result(path, raw):
            if path == self.path and any(item["id"] == operation_id and item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost input-result publication")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=lose_result):
            self.fault("task_result_unverified", self.app.control_execution, **input_args)
        self.until(lambda: (self.work / "input-count").exists())
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("Never resend terminal input")), self.app.task_observer.running():
            self.until(lambda: (self.raw()["operations"][1]["result"] or {}).get("status") == "succeeded")
            self.assertEqual(self.raw()["operations"][1]["locator"]["control_seq"], 1)
            self.assertNotIn("single-observed-pty-input", self.path.read_text())
            self.assertEqual((self.work / "input-count").read_text(), "1")
            (self.work / "input-release").touch()
            self.until(lambda: (self.raw()["operations"][0]["result"] or {}).get("status") == "succeeded")
        self.assertEqual(self.wait(started)["status"], "succeeded")
        self.assertEqual((self.work / "input-count").read_text(), "1")
        self.assertIsNone(self.raw()["terminal"])

    def test_lost_cancellation_result_retires_only_after_original_worker_evidence(self):
        self.accept_plan()
        code = ('from pathlib import Path\nimport signal,time,sys\n'
            'def stop(*args):\n time.sleep(0.35)\n sys.exit(0)\n'
            'signal.signal(signal.SIGTERM,stop)\n'
            'p=Path("cancel-count"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n'
            'print("READY",flush=True)\nwhile True: time.sleep(0.02)\n')
        args = self.start_arguments(code, timeout_seconds=15)
        started = self.app.start_execution(**args)
        self.addCleanup(lambda: self.wait(self.app.cancel_execution("desktop", args["execution_id"])))
        self.until(lambda: "READY" in self.app.read_execution("desktop", args["execution_id"])["output"])
        self.app.task_admission.reconcile(self.view())
        original_replace = task_store._replace
        def lose_result(path, raw):
            if path == self.path and any(item["id"] != args["execution_id"] and item["result"] is not None for item in json.loads(raw)["operations"]):
                raise OSError("Disposable lost cancellation-result publication")
            return original_replace(path, raw)
        with patch.object(task_store, "_replace", side_effect=lose_result):
            self.fault("task_result_unverified", self.control, "cancel", reason="Cancel the original fixture only.")
        self.assertEqual(self.raw()["control"]["stop_intent"], "cancel")
        self.assertIsNone(self.raw()["terminal"])
        with patch.object(self.app.task_admission, "_dispatch", side_effect=AssertionError("No repeated cancellation")), self.app.task_observer.running():
            self.until(lambda: self.raw()["terminal"])
            record = self.raw()
            self.assertEqual(record["terminal"]["outcome"], "cancelled")
            self.assertEqual(record["control"]["state"], "cancelled")
            self.assertIsNone(record["handoff"])
            self.assertTrue(all(item["result"]["status"] == "cancelled" for item in record["operations"]))
            self.until(lambda: "structured-one" not in self.app.task_observer._snapshot["active"])
        original = self.wait(started)
        self.assertEqual(original["status"], "cancelled")
        self.assertEqual(original["exit_code"], 0, "Handled cancellation is not successful execution")
        self.assertEqual((self.work / "cancel-count").read_text(), "1")

    def test_partial_receipt_read_does_not_refresh_an_unread_control(self):
        from copy import deepcopy
        identity = self.seed()[0]
        def add_control(record, now):
            operation = deepcopy(record["operations"][0])
            operation["id"] = "f" * 32
            operation["locator"]["control_seq"] = 1
            record["operations"].append(operation)
        tasks.update(self.state, identity, add_control)
        def partial(operation):
            if operation["locator"]["control_seq"] is not None:
                raise Fault("task_evidence_unavailable", "Original control receipt unavailable.")
            return {"execution_id": operation["id"], "status": "running", "created_at": 1.0}
        with patch.object(self.app.task_admission, "_read", side_effect=partial), self.app.task_observer.running():
            self.until(lambda: self.app.task_observer._snapshot["last"].get(identity))
            view = self.until(lambda: self.app.read_task(identity))
            command, control = view["observations"][2:4]
            self.assertIsNotNone(command["observed_at_ms"])
            self.assertIsNone(control["observed_at_ms"])
            self.assertEqual(control["state"], "stale")
            self.assertEqual(view["record"]["recovery"]["reason_code"], "blocked_task_evidence_unavailable")

    def test_stale_recovery_heartbeat_has_no_live_timer(self):
        identity = self.seed()[0]
        tasks.update(self.state, identity, lambda value, now: self.observer_module.fail_observation(value, now, now, "source_unavailable"))
        observer = self.app.task_observer
        observer._snapshot = dict(observer._snapshot, state="running", observed_at_ms=self.observer_module.now_ms() - 1000)
        view = self.app.read_task(identity)
        self.assertEqual(view["observations"][1]["state"], "stale")
        self.assertEqual(view["observations"][1]["blocker"], "observer_heartbeat_stale")
        self.assertIsNone(view["observations"][1]["next_attempt_at_ms"])
        self.assertIsNotNone(view["record"]["recovery"])

    def test_permanent_result_publication_failure_stops_the_affected_read_loop(self):
        identity = self.seed()[0]
        before = self.raw(identity)
        with patch.object(self.app.task_admission, "_read", side_effect=self.fixture_result) as read:
            with patch.object(task_store, "_replace", side_effect=Fault("task_capacity", "Disposable publication blocker.")):
                with self.app.task_observer.running():
                    self.until(lambda: self.app.task_observer._snapshot["errors"].get(identity) == "task_capacity")
                    time.sleep(0.3)
                    self.assertEqual(read.call_count, 1)
                    self.assertNotIn(identity, self.app.task_observer._snapshot["active"])
                    self.assertEqual(self.raw(identity), before)
                    view = self.app.read_task(identity)
                    self.assertEqual(view["observations"][1]["state"], "blocked")
                    self.assertIsNone(view["observations"][1]["next_attempt_at_ms"])
