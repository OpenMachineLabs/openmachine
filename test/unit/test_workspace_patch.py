"""Behavior checks for WorkspaceChangeTests."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch
import uuid
from openmachine import workspace
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.workspace import changes, change_records, files
from integrations.ssh import PROTOCOL, client, endpoint
from openmachine import __version__
from support_workspace import WorkspaceFixture


class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_patch_all_operations_have_ordered_effects_and_preserve_index_modes(self):
        self.initialize_git()
        self.file.chmod(0o755)
        (self.work / "delete.txt").write_bytes(b"obsolete\n")
        (self.work / "unrelated.txt").write_bytes(b"staged, do not change\n")
        self.git("add", "unrelated.txt")
        index = (self.work / ".git/index").read_bytes()
        body = self.patch_document("*** Update File: source.py\n*** Move to: new/deeper/source.py\n@@\n-answer = 41\n+answer = 42\n"
                                   "*** Add File: new/test.txt\n+new test\n*** Delete File: delete.txt\n")
        args = self.patch_arguments(body, create_parents=True)
        before = self.file.read_bytes()
        preview = self.app.apply_workspace_patch(**args, dry_run=True)
        self.assertFalse((self.work / "new").exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.file.read_bytes(), before)
        result = self.patch_apply(args, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(result["format"], 2)
        self.assertEqual([(effect["kind"], effect["path"]) for effect in result["effects"]], [
            ("mkdir", "new"), ("mkdir", "new/deeper"), ("publish", "new/deeper/source.py"),
            ("remove", "source.py"), ("publish", "new/test.txt"), ("remove", "delete.txt")])
        self.assertTrue(all(effect["stage"] == "applied" and effect["synced"] and effect["readback_verified"] for effect in result["effects"]))
        self.assertEqual((self.work / "new/deeper/source.py").read_bytes(), b"answer = 42\r\nuntouched = 7")
        self.assertEqual((self.work / "new/deeper/source.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.work / "new/test.txt").read_bytes(), b"new test\n")
        self.assertFalse(self.file.exists())
        self.assertFalse((self.work / "delete.txt").exists())
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        self.assertEqual((self.work / "unrelated.txt").read_bytes(), b"staged, do not change\n")
        self.assertEqual(result["diff"], preview["diff"])
        self.assertEqual(workspace.check_change_state(self.state)["workspace_change_formats_present"], [2])

    def test_patch_preflight_last_file_rejects_all_files_and_planned_parents(self):
        (self.work / "third.txt").write_text("actual\n")
        body = self.patch_document("*** Update File: source.py\n@@\n-answer = 41\n+answer = 42\n"
                                   "*** Add File: new/second.txt\n+new\n*** Update File: third.txt\n@@\n-absent\n+changed\n")
        before = self.file.read_bytes()
        result = self.patch_apply(self.patch_arguments(body, create_parents=True))
        self.assertEqual((result["status"], result["error"]), ("rejected", "match_missing"))
        self.assertIsNone(result["plan"])
        self.assertEqual(result["effects"], [])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertFalse((self.work / "new").exists())
        self.assertEqual((self.work / "third.txt").read_text(), "actual\n")

    def test_patch_second_io_failure_preserves_applied_prefix_without_rollback(self):
        body = self.patch_document("*** Add File: first.txt\n+one\n*** Add File: second.txt\n+two\n*** Add File: third.txt\n+three\n")
        args = self.patch_arguments(body)
        original = changes.publish_file
        calls = []
        def failing(config, machine, name, source, desired, temporary, **kwargs):
            calls.append(source["path"])
            if source["path"] == "second.txt":
                return {"stage": "failed_no_effect", "result_sha256": None, "synced": False,
                        "readback_verified": False, "error": "fixture_io", "cleanup": "not_created"}
            return original(config, machine, name, source, desired, temporary, **kwargs)
        with patch.object(changes, "publish_file", side_effect=failing):
            result = self.patch_apply(args)
        self.assertEqual(result["status"], "partial", result)
        self.assertNotIn("recovery_hint", result)
        self.assertEqual([e["stage"] for e in result["effects"]], ["applied", "failed_no_effect", "not_attempted"])
        self.assertEqual(calls, ["first.txt", "second.txt"])
        self.assertEqual((self.work / "first.txt").read_text(), "one\n")
        self.assertFalse((self.work / "second.txt").exists())
        self.assertFalse((self.work / "third.txt").exists())
        self.assertNotIn("diff", result)
        read = self.app.read_workspace_change("local", result["change_id"])
        self.assertEqual(read["status"], "partial")

    def test_patch_lost_replace_ack_retains_prefix_and_unknown_without_replay(self):
        (self.work / "second.txt").write_text("old\n")
        args = self.patch_arguments(self.patch_document("*** Add File: first.txt\n+one\n"
            "*** Update File: second.txt\n@@\n-old\n+new\n*** Add File: third.txt\n+three\n"))
        identity = uuid.uuid4().hex
        original = os.replace
        def lost(source, destination, *values, **kwargs):
            result = original(source, destination, *values, **kwargs)
            if str(source) == ".openmachine-change-" + identity + "-01":
                raise OSError("lost acknowledgement")
            return result
        with patch.object(files.os, "replace", side_effect=lost):
            result = self.patch_apply(args, change_id=identity)
        self.assertEqual(result["status"], "outcome_unknown", result)
        self.assertEqual([e["stage"] for e in result["effects"]], ["applied", "unknown", "not_attempted"])
        (self.work / "first.txt").write_text("third party\n")
        with patch.object(changes, "publish_file", side_effect=AssertionError("No replay")):
            retry = self.patch_apply(args, change_id=identity)
        self.assertEqual(retry["revision"], result["revision"])
        self.assertEqual((self.work / "first.txt").read_text(), "third party\n")
        self.assertEqual((self.work / "second.txt").read_text(), "new\n")
        self.assertFalse((self.work / "third.txt").exists())

    def test_patch_move_changed_source_preserves_both_paths(self):
        args = self.patch_arguments(self.patch_document("*** Update File: source.py\n*** Move to: moved.py\n"))
        original = changes.publish_file
        before = self.file.read_bytes()
        def changed(*values, **kwargs):
            result = original(*values, **kwargs)
            self.file.write_bytes(b"external edit\n")
            return result
        with patch.object(changes, "publish_file", side_effect=changed):
            result = self.patch_apply(args)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual([e["stage"] for e in result["effects"]], ["applied", "failed_no_effect"])
        self.assertEqual(self.file.read_bytes(), b"external edit\n")
        self.assertEqual((self.work / "moved.py").read_bytes(), before)

    def test_patch_late_add_or_parent_collision_never_overwrites(self):
        for parent in (False, True):
            with self.subTest(parent=parent):
                self.state = self.root / ("parent-state" if parent else "file-state")
                self.app = create_application(self.config, self.state)
                body = self.patch_document("*** Add File: " + ("new/file" if parent else "occupied") + "\n+ours\n")
                args = self.patch_arguments(body, create_parents=parent)
                owner = "publish_path" if parent else "publish_file"
                original = getattr(changes, owner)
                def collision(*values, **kwargs):
                    if parent:
                        (self.work / "new").mkdir()
                        (self.work / "new/external").write_text("keep")
                    else:
                        (self.work / "occupied").write_text("external")
                    return original(*values, **kwargs)
                with patch.object(changes, owner, side_effect=collision):
                    result = self.patch_apply(args)
                self.assertEqual(result["status"], "rejected", result)
                target = self.work / ("new/external" if parent else "occupied")
                self.assertEqual(target.read_text(), "keep" if parent else "external")
                self.assertFalse((self.work / "new/file").exists())

    def test_patch_delete_lost_unlink_ack_is_unknown_and_does_not_continue(self):
        args = self.patch_arguments(self.patch_document("*** Delete File: source.py\n*** Add File: later\n+no\n"))
        original = os.unlink
        def lost(path, *values, **kwargs):
            result = original(path, *values, **kwargs)
            if path == "source.py":
                raise OSError("lost unlink acknowledgement")
            return result
        with patch.object(files.os, "unlink", side_effect=lost):
            result = self.patch_apply(args)
        self.assertEqual(result["status"], "outcome_unknown", result)
        self.assertEqual([e["stage"] for e in result["effects"]], ["unknown", "not_attempted"])
        self.assertFalse(self.file.exists())
        self.assertFalse((self.work / "later").exists())

    def test_patch_each_observed_effect_is_saved_before_next_effect(self):
        args = self.patch_arguments(self.patch_document("*** Add File: first\n+yes\n*** Add File: second\n+no\n"))
        identity = uuid.uuid4().hex
        original = change_records.save_json
        def failed(path, value):
            if value["status"] == "applying" and value["effects"][0]["stage"] == "applied":
                raise OSError("observed effect receipt unavailable")
            return original(path, value)
        with patch.object(change_records, "save_json", side_effect=failed):
            error = self.fault("change_outcome_unknown", lambda: self.patch_apply(args, change_id=identity))
        self.assertEqual(error.evidence["effects"][0]["stage"], "applied")
        read = self.app.read_workspace_change("local", identity, True)
        self.assertEqual(read["reconciliation"], "outcome_unknown")
        self.assertEqual([e["stage"] for e in read["effects"]], ["attempting", "not_attempted"])
        self.assertEqual((self.work / "first").read_text(), "yes\n")
        self.assertFalse((self.work / "second").exists())

    def test_patch_terminal_receipt_failure_preserves_all_known_effects(self):
        args = self.patch_arguments(self.patch_document("*** Add File: first\n+yes\n*** Add File: second\n+yes\n"))
        identity = uuid.uuid4().hex
        original = change_records.save_json
        def failed(path, value):
            if value["status"] == "applied":
                raise OSError("terminal save failure")
            return original(path, value)
        with patch.object(change_records, "save_json", side_effect=failed):
            self.fault("change_outcome_unknown", lambda: self.patch_apply(args, change_id=identity))
        read = self.app.read_workspace_change("local", identity)
        self.assertEqual(read["status"], "applying")
        self.assertEqual(read["reconciliation"], "outcome_unknown")
        self.assertEqual([e["stage"] for e in read["effects"]], ["applied", "applied"])
        with patch.object(changes, "publish_file", side_effect=AssertionError("No replay")):
            self.assertEqual(self.patch_apply(args, change_id=identity)["revision"], read["revision"])

    def test_patch_preview_purity_bounds_and_stale_plan(self):
        self.config.machines["local"].execution_enabled = False
        self.config.machines["local"].workspace_write_enabled = False
        body = self.patch_document("*** Add File: nested/new.py\n+value = 1\n")
        args = self.patch_arguments(body, create_parents=True)
        preview = self.app.apply_workspace_patch(**args, dry_run=True)
        self.assertFalse(preview["workspace_write_enabled"])
        self.assertFalse(self.state.exists())
        self.assertFalse((self.work / "nested").exists())
        self.fault("workspace_write_disabled", lambda: self.patch_apply(args))
        self.config.machines["local"].workspace_write_enabled = True
        (self.work / "nested").mkdir()
        rejected = self.patch_apply(args, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual((rejected["status"], rejected["error"]), ("rejected", "plan_mismatch"))
        self.assertFalse((self.work / "nested/new.py").exists())

    def test_patch_hunks_are_exact_source_ordered_and_never_generated_matches(self):
        cases = [
            (b"a\na\n", "@@\n-a\n+b\n", "match_ambiguous"),
            (b"a\nb\nc\n", "@@\n a\n-b\n+B\n@@\n b\n-c\n+C\n", "match_overlap"),
            (b"a\nb\n", "@@\n-b\n+B\n@@\n-a\n+A\n", "match_overlap"),
            (b"a\n", "@@\n-a\n+b\n@@\n-b\n+c\n", "match_missing"),
            (b"a   \n", "@@\n-a\n+b\n", "match_missing"),
            (b"a\nb\n", "@@\n-a\n+A\n*** End of File\n", "match_missing"),
        ]
        for original, hunks, code in cases:
            with self.subTest(code=code, original=original):
                self.file.write_bytes(original)
                args = self.patch_arguments(self.patch_document("*** Update File: source.py\n" + hunks))
                result = self.patch_apply(args)
                self.assertEqual((result["status"], result["error"]), ("rejected", code))
                self.assertEqual(self.file.read_bytes(), original)
        self.file.write_bytes(b"a\na\n")
        args = self.patch_arguments(self.patch_document("*** Update File: source.py\n@@\n-a\n+b\n*** End of File\n"))
        self.assertEqual(self.patch_apply(args)["status"], "applied")
        self.assertEqual(self.file.read_bytes(), b"a\nb\n")

    def test_patch_bom_mixed_endings_and_eof_changes_preserve_exact_bytes(self):
        cases = [
            (b"\xef\xbb\xbfa\r\nb\nc", "@@\n-b\n+B\n", b"\xef\xbb\xbfa\r\nB\r\nc"),
            (b"a\nb", "@@\n-b\n", b"a"),
            (b"a", "@@\n a\n+b\n", b"a\nb"),
            (b"a\r\n", "@@\n a\n+b\n", b"a\r\nb\r\n"),
            (b"\xef\xbb\xbfa", "@@\n-a\n", b"\xef\xbb\xbf"),
            (b"a\nb\nc", "@@\n-a\n+A\n@@\n-c\n+C\n", b"A\nb\nC"),
        ]
        for original, hunks, expected in cases:
            with self.subTest(original=original, hunks=hunks):
                self.file.write_bytes(original)
                args = self.patch_arguments(self.patch_document("*** Update File: source.py\n" + hunks))
                preview = self.app.apply_workspace_patch(**args, dry_run=True)
                result = self.patch_apply(args, expected_plan_sha256=preview["plan_sha256"])
                self.assertEqual(result["status"], "applied", result)
                self.assertEqual(self.file.read_bytes(), expected)
                self.assertEqual(result["diff"], preview["diff"])
        self.file.write_bytes(b"a\rb")
        args = self.patch_arguments(self.patch_document("*** Update File: source.py\n@@\n-a\n+b\n"))
        self.assertEqual(self.patch_apply(args)["error"], "invalid_patch_source")
        pure = self.patch_arguments(self.patch_document("*** Update File: source.py\n*** Move to: raw.txt\n"))
        self.assertEqual(self.patch_apply(pure)["status"], "applied")
        self.assertEqual((self.work / "raw.txt").read_bytes(), b"a\rb")

    def test_patch_empty_add_and_full_delete_are_distinct_from_absence(self):
        body = self.patch_document("*** Add File: empty\n")
        result = self.patch_apply(self.patch_arguments(body))
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.work / "empty").read_bytes(), b"")
        self.assertEqual(result["plan"]["files"][0]["after_sha256"], hashlib.sha256(b"").hexdigest())
        args = self.patch_arguments(self.patch_document("*** Delete File: empty\n"))
        self.assertEqual(self.patch_apply(args)["status"], "applied")
        self.assertFalse((self.work / "empty").exists())

    def test_patch_invalid_grammar_paths_and_preconditions_reject_before_admission(self):
        from openmachine.workspace import patch as patcher
        valid = self.patch_document("*** Add File: new\n+ok\n")
        invalid = ["```\n" + valid, valid + "\n", valid.replace("\n", "\r\n"),
                   self.patch_document("*** Update File: source.py\n@@ function\n-a\n+b\n"),
                   self.patch_document("*** Update File: source.py\n@@\n+unanchored\n"),
                   self.patch_document("*** Delete File: source.py\n-payload\n"),
                   self.patch_document("*** Add File: a\n*** Add File: a/b\n"),
                   self.patch_document("*** Add File: A/a\n*** Add File: a/b\n"),
                   self.patch_document("*** Add File: caf\u00e9\n*** Add File: cafe\u0301\n"),
                   self.patch_document("*** Update File: a\n*** Move to: b\n*** Update File: b\n*** Move to: a\n"),
                   self.patch_document("*** Add File: .git/config\n+x\n"),
                   self.patch_document("*** Add File: ../escape\n+x\n"),
                   self.patch_document("*** Add File: .env\n+x\n")]
        for text in invalid:
            with self.subTest(text=text[:80]), self.assertRaises(Fault):
                patcher.parse(text)
        args = self.patch_arguments(valid)
        for preconditions in ([], args["preconditions"] * 2,
                              [{"path": "extra", "expected_sha256": ""}],
                              [{"path": "new", "expected_sha256": "short"}]):
            self.fault("invalid_precondition", lambda: self.patch_apply(args | {"preconditions": preconditions}))
        self.assertFalse((self.state / "workspace-changes").exists())
        self.assertFalse((self.work / "new").exists())

    def test_patch_initial_record_budget_rejects_without_incomplete_allocation(self):
        self.initialize_git()
        (self.work / "unrelated").write_bytes(b"keep staged")
        self.git("add", "unrelated")
        index = (self.work / ".git/index").read_bytes()
        component = "\U0001f9ed" * 55
        names = ["/".join([component] * 4 + [f"f{i:02}.txt"]) for i in range(32)]
        body = self.patch_document("".join(f"*** Add File: {name}\n+x\n" for name in names))
        self.assertLessEqual(max(len(name.encode()) for name in names), 1024)
        self.assertLessEqual(len(body.encode()), 131072)
        for retained in (False, True):
            with self.subTest(retained=retained):
                self.state = self.root / f"initial-budget-{retained}"
                self.app = create_application(self.config, self.state)
                if retained:
                    self.apply()
                before = workspace.check_change_state(self.state)
                records = {p: p.read_bytes() for p in self.state.glob("workspace-changes/*/record.json")}
                source = self.file.read_bytes()
                args = self.patch_arguments(body, create_parents=True)
                self.assertLess(len(json.dumps(args, ensure_ascii=True).encode()), 1024 * 1024)
                identity = uuid.uuid4().hex
                self.fault("request_limit", lambda: self.patch_apply(args, change_id=identity))
                self.assertFalse(self.directory(identity).exists())
                self.assertEqual(workspace.check_change_state(self.state), before)
                self.assertEqual({p: p.read_bytes() for p in records}, records)
                self.assertEqual(self.file.read_bytes(), source)
                self.assertFalse((self.work / component).exists())
                self.assertEqual((self.work / ".git/index").read_bytes(), index)
                self.assertEqual((self.work / "unrelated").read_bytes(), b"keep staged")

    def test_retained_patch_is_reconciled_before_new_record_budget(self):
        args = self.patch_arguments(self.patch_document("*** Add File: first\n+one\n"))
        identity = uuid.uuid4().hex
        original = self.patch_apply(args, change_id=identity)
        path = self.directory(identity) / "record.json"
        record = path.read_bytes()
        (self.work / "first").write_text("later\n")
        with patch.object(change_records, "_record_budget", side_effect=AssertionError("No new admission")):
            recovered = self.patch_apply(args, change_id=identity)
        self.assertEqual((recovered["created_at"], recovered["revision"]),
                         (original["created_at"], original["revision"]))
        self.assertEqual(path.read_bytes(), record)
        self.assertEqual((self.work / "first").read_text(), "later\n")

    def test_patch_limits_and_complete_record_budget_reject_before_target_effects(self):
        from openmachine.workspace import patch as patcher
        self.fault("request_limit", lambda: patcher.parse(self.patch_document("*** Add File: new\n+" + "x" * 131072 + "\n")))
        self.fault("request_limit", lambda: patcher.parse(self.patch_document("".join(f"*** Add File: p{i}\n" for i in range(33)))))
        body = self.patch_document("*** Update File: source.py\n" + "@@\n-a\n+b\n" * 65)
        self.fault("request_limit", lambda: patcher.parse(body))
        original = self.file.read_bytes()
        args = self.patch_arguments(self.patch_document("*** Update File: source.py\n@@\n-answer = 41\n+answer = 42\n"))
        with patch.object(change_records, "MAX_PREPARED", len(original) * 2 - 1):
            result = self.patch_apply(args)
        self.assertEqual((result["status"], result["error"]), ("rejected", "source_limit"))
        self.assertEqual(self.file.read_bytes(), original)
        with patch.object(change_records, "MAX_RECORD", 3000):
            result = self.patch_apply(args)
        self.assertEqual((result["status"], result["error"]), ("rejected", "request_limit"))
        self.assertEqual(self.file.read_bytes(), original)
        args = self.patch_arguments(self.patch_document("*** Add File: " + "/".join(["new"] * 9) + "/file\n+x\n"), create_parents=True)
        self.assertEqual(self.patch_apply(args)["error"], "request_limit")
        self.assertFalse((self.work / "new").exists())

    def test_patch_same_id_current_drift_and_changed_request_are_separate(self):
        args = self.patch_arguments(self.patch_document("*** Add File: one\n+first\n*** Add File: two\n+second\n"))
        identity = uuid.uuid4().hex
        original = self.patch_apply(args, change_id=identity)
        path = self.directory(identity) / "record.json"
        retained = path.read_bytes()
        (self.work / "one").write_text("later\n")
        (self.work / "two").unlink()
        self.config.machines["local"].workspace_write_enabled = False
        retry = self.patch_apply(args, change_id=identity)
        self.assertEqual((retry["created_at"], retry["revision"]), (original["created_at"], original["revision"]))
        current = self.app.read_workspace_change("local", identity, True)
        self.assertEqual(current["status"], "applied")
        self.assertEqual(current["current"]["files"][0]["sha256"], files.digest(b"later\n"))
        self.assertFalse(current["current"]["files"][1]["exists"])
        self.assertEqual(path.read_bytes(), retained)
        self.fault("change_id_conflict", lambda: self.patch_apply(args | {"patch": args["patch"].replace("first", "different")}, change_id=identity))
        self.assertFalse(current["historical_diff_available"])

    def test_patch_concurrent_same_id_has_one_original_publisher(self):
        entered, release = threading.Event(), threading.Event()
        args = self.patch_arguments(self.patch_document("*** Add File: one\n+first\n"))
        identity = uuid.uuid4().hex
        original = changes.publish_file
        def paused(*values, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Reader did not release publisher")
            return original(*values, **kwargs)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(changes, "publish_file", side_effect=paused) as publish:
            writer = pool.submit(self.patch_apply, args, change_id=identity)
            try:
                self.assertTrue(entered.wait(5))
                read = self.patch_apply(args, change_id=identity)
                self.assertTrue(read["writer_alive"])
                self.assertEqual(read["status"], "applying")
                self.assertEqual(publish.call_count, 1)
            finally:
                release.set()
            self.assertEqual(writer.result(timeout=5)["status"], "applied")

    def test_patch_corrupt_multi_effect_record_is_never_repaired(self):
        result = self.patch_apply(self.patch_arguments(self.patch_document("*** Add File: one\n+x\n*** Add File: two\n+y\n")))
        path = self.directory(result["change_id"]) / "record.json"
        before = path.read_bytes()
        for field, value in (("effects", []), ("status", "prepared"), ("format", 1)):
            record = json.loads(before)
            record[field] = value
            path.write_text(json.dumps(record))
            retained = path.read_bytes()
            self.fault("change_store_corrupt", lambda: self.app.read_workspace_change("local", result["change_id"]))
            self.assertEqual(path.read_bytes(), retained)
        path.write_bytes(before)
        self.assertEqual(self.app.read_workspace_change("local", result["change_id"])["status"], "applied")

    def test_patch_target_dispatch_and_lost_transport_use_original_identity(self):
        identity = "a" * 64
        archive = self.root / ("b" * 64 + ".pyz")
        request = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                   "execution_enabled": False, "workspace_write_enabled": True,
                   "workspaces": {"project": str(self.work)}}
        args = self.patch_arguments(self.patch_document("*** Add File: target.txt\n+target\n"))
        arguments = {key: value for key, value in args.items() if key not in {"machine", "workspace_name", "patch"}}
        arguments.update(workspace="project", patch_text=args["patch"], change_id=uuid.uuid4().hex)
        with patch.object(endpoint, "_identity", return_value=(identity, self.root)):
            reply = endpoint._dispatch(request | {"operation": "apply_workspace_patch", "arguments": arguments}, archive)["result"]
        self.assertEqual(reply["status"], "applied", reply)
        self.assertEqual((self.work / "target.txt").read_text(), "target\n")
        remote = Settings.model_validate({"schema_version": 1,
            "machines": {"ssh": {"transport": "ssh", "ssh_alias": "uncontacted-fixture", "ssh_worker": str(archive),
                                 "ssh_identity": identity, "workspace_write_enabled": True}},
            "workspaces": {"project": {"machine": "ssh", "root": str(self.work)}}})
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            error = self.fault("outcome_unknown", lambda: client.call(remote, "ssh", "apply_workspace_patch", **arguments))
        self.assertEqual(error.evidence["change_id"], arguments["change_id"])
        self.assertEqual(exchange.call_count, 1)
        arguments.update(dry_run=True, change_id="")
        with patch.object(client, "_exchange", side_effect=TimeoutError):
            self.fault("ssh_unavailable", lambda: client.call(remote, "ssh", "apply_workspace_patch", **arguments))

    def test_patch_preview_enforces_record_budget_without_state(self):
        args = self.patch_arguments(self.patch_document("*** Add File: nested/new\n+new\n"), create_parents=True)
        with patch.object(change_records, "MAX_RECORD", 3000):
            self.fault("request_limit", lambda: self.app.apply_workspace_patch(**args, dry_run=True))
        self.assertFalse(self.state.exists())
        self.assertFalse((self.work / "nested").exists())

    def test_patch_guarded_delete_rejects_links_and_special_sources_before_other_effects(self):
        target = self.work / "target"
        target.write_text("original\n")
        for linked in (True, False):
            with self.subTest(symlink=linked):
                path = self.work / "unsafe"
                if linked:
                    path.symlink_to(target)
                else:
                    os.link(target, path)
                args = self.patch_arguments(self.patch_document("*** Add File: first\n+no\n*** Delete File: unsafe\n"))
                result = self.patch_apply(args)
                self.assertEqual(result["status"], "rejected", result)
                self.assertEqual(result["effects"], [])
                self.assertFalse((self.work / "first").exists())
                self.assertEqual(target.read_text(), "original\n")
                path.unlink()

    def test_patch_created_directory_sync_failure_is_unknown_and_stops_before_file(self):
        args = self.patch_arguments(self.patch_document("*** Add File: new/file\n+no\n"), create_parents=True)
        original = os.fsync
        def fail(descriptor):
            if (self.work / "new").exists() and os.fstat(descriptor).st_ino == (self.work / "new").stat().st_ino:
                raise OSError("directory sync fixture")
            return original(descriptor)
        with patch.object(files.os, "fsync", side_effect=fail):
            result = self.patch_apply(args)
        self.assertEqual(result["status"], "outcome_unknown", result)
        self.assertEqual([effect["stage"] for effect in result["effects"]], ["unknown", "not_attempted"])
        self.assertTrue((self.work / "new").is_dir())
        self.assertFalse((self.work / "new/file").exists())
