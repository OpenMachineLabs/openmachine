"""Tests for workspace behavior at the component layer."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import stat
import sys
import time
import threading
import unittest
from unittest.mock import patch
import uuid
from openmachine import workspace
from openmachine.config import Machine, Workspace
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.workspace import changes, change_records, files, git
from openmachine.execution import api as execution_api
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
    def test_exact_bytes_staged_work_and_modes_survive(self):
        self.initialize_git()
        (self.work / "unrelated").write_bytes(b"keep")
        self.git("add", "unrelated")
        index = (self.work / ".git/index").read_bytes()
        self.file.chmod(0o755)
        result = self.apply()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.file.read_bytes(), b"answer = 42\r\nuntouched = 7")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        self.assertEqual((self.work / "unrelated").read_bytes(), b"keep")
        self.assertEqual(result["effects"][0]["result_sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_all_replacements_use_original_not_generated_content(self):
        for original, edits, expected in (
            (b"A B", [{"old_text": "A", "new_text": "B"}, {"old_text": "B", "new_text": "C"}], b"B C"),
            (b"A", [{"old_text": "A", "new_text": "B"}, {"old_text": "B", "new_text": "C"}], None),
        ):
            self.file.write_bytes(original)
            args = self.arguments()
            args["edits"] = edits
            result = self.apply(args)
            self.assertEqual(result["status"], "applied" if expected else "rejected")
            self.assertEqual(self.file.read_bytes(), expected or original)
            if expected is None:
                self.assertEqual(result["error"], "match_missing")

    def test_ambiguous_overlapping_and_missing_fragments_reject_before_effects(self):
        for source, fragments, code in (("aaa", ["aa"], "match_ambiguous"),
                ("abcd", ["abc", "bcd"], "match_overlap"), ("same same", ["same"], "match_ambiguous"),
                ("original", ["absent"], "match_missing")):
            with self.subTest(code=code):
                self.file.write_text(source)
                args = self.arguments()
                args["edits"] = [{"old_text": value, "new_text": "replacement"} for value in fragments]
                result = self.apply(args)
                self.assertEqual((result["status"], result["error"]), ("rejected", code))
                self.assertEqual("recovery_hint" in result, code in {"match_ambiguous", "match_missing"})
                self.assertEqual(result["effects"], [])
                self.assertEqual(self.file.read_text(), source)
                self.assertFalse(list(self.work.glob(".openmachine-*")))

    def test_rejected_edit_can_be_corrected_without_replaying_or_rewriting_history(self):
        self.file.write_text("install 41\nservice 41\n")
        args = self.arguments()
        identity = uuid.uuid4().hex
        rejected = self.apply(args, change_id=identity)
        self.assertEqual((rejected["status"], rejected["error"]), ("rejected", "match_ambiguous"))
        self.assertIn("unique surrounding text", rejected["recovery_hint"])
        self.assertIn("current authorization", rejected["recovery_hint"])
        record = self.directory(identity) / "record.json"
        retained = record.read_bytes()
        self.assertNotIn("recovery_hint", json.loads(retained))
        observed = self.app.read_workspace_change("local", identity)
        self.assertEqual(observed["recovery_hint"], rejected["recovery_hint"])
        corrected = self.arguments()
        corrected["edits"] = [{"old_text": "install 41", "new_text": "install 42"}]
        self.fault("change_id_conflict", lambda: self.apply(corrected, change_id=identity))
        self.assertEqual(self.apply(args, change_id=identity)["status"], "rejected")
        preview = self.app.edit_workspace_file(**corrected, dry_run=True)
        self.assertEqual(self.file.read_text(), "install 41\nservice 41\n")
        self.config.machines["local"].workspace_write_enabled = False
        self.fault("workspace_write_disabled", lambda: self.apply(corrected))
        self.config.machines["local"].workspace_write_enabled = True
        completed = self.apply(corrected, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual(completed["status"], "applied")
        self.assertNotIn("recovery_hint", completed)
        self.assertEqual(self.file.read_text(), "install 42\nservice 41\n")
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual(self.app.read_workspace_change("local", identity)["status"], "rejected")

    def test_durable_write_recovery_does_not_require_an_unsupported_preview(self):
        before = self.app.read_workspace_file("local", "project", "source.py")
        identity = uuid.uuid4().hex
        self.file.write_bytes(self.file.read_bytes() + b"\r\nother = 9")
        rejected = self.app.write_workspace_file("local", "project", "source.py", "answer = 42\n", "",
            before["sha256"], identity, before["context"]["workspace_identity"])
        self.assertEqual((rejected["status"], rejected["error"]), ("rejected", "content_mismatch"))
        self.assertEqual(rejected["effects"], [])
        self.assertIn("review the complete replacement", rejected["recovery_hint"])
        self.assertNotIn("preview", rejected["recovery_hint"])
        record = self.directory(identity) / "record.json"
        retained = record.read_bytes()
        current = self.app.read_workspace_file("local", "project", "source.py")
        replacement = current["text"].replace("41", "42")
        completed = self.app.write_workspace_file("local", "project", "source.py", replacement, "",
            current["sha256"], uuid.uuid4().hex, current["context"]["workspace_identity"])
        self.assertEqual(completed["status"], "applied")
        self.assertEqual(self.file.read_bytes(), b"answer = 42\r\nuntouched = 7\r\nother = 9")
        self.assertEqual(record.read_bytes(), retained)
        self.assertEqual(self.app.read_workspace_change("local", identity)["status"], "rejected")

    def test_content_mismatch_hint_requires_fresh_source_and_preserves_other_edits(self):
        stale = self.arguments()
        changed = self.file.read_bytes() + b"\r\nother = 9"
        self.file.write_bytes(changed)
        rejected = self.apply(stale)
        self.assertEqual((rejected["status"], rejected["error"]), ("rejected", "content_mismatch"))
        self.assertIn("reconcile", rejected["recovery_hint"])
        self.assertEqual(rejected["effects"], [])
        self.assertEqual(self.file.read_bytes(), changed)
        corrected = self.arguments()
        preview = self.app.edit_workspace_file(**corrected, dry_run=True)
        self.assertEqual(self.apply(corrected, expected_plan_sha256=preview["plan_sha256"])["status"], "applied")
        self.assertEqual(self.file.read_bytes(), changed.replace(b"41", b"42"))

    def test_preview_is_pure_with_no_write_or_execution_grant(self):
        self.config.machines["local"].execution_enabled = False
        self.config.machines["local"].workspace_write_enabled = False
        args = self.arguments()
        before = self.file.read_bytes()
        result = self.app.edit_workspace_file(**args, dry_run=True)
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["workspace_write_enabled"])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.file.read_bytes(), before)
        self.fault("invalid_change_id", lambda: self.app.edit_workspace_file(**args, dry_run=True, change_id=uuid.uuid4().hex))
        self.assertFalse(self.state.exists())
        self.fault("invalid_change_id", lambda: self.app.edit_workspace_file(**args))
        # A mutating application call may initialize its existing lifecycle lock, not a change record.
        self.assertFalse((self.state / "workspace-changes").exists())

    def test_edit_and_execution_grants_are_independent(self):
        self.config.machines["local"].execution_enabled = False
        self.config.machines["local"].workspace_write_enabled = True
        self.assertEqual(self.apply()["status"], "applied")
        self.fault("execution_disabled", lambda: self.app.start_execution("local", uuid.uuid4().hex,
            str(self.work), [sys.executable, "-c", "pass"]))
        self.config.machines["local"].execution_enabled = True
        self.config.machines["local"].workspace_write_enabled = False
        self.fault("workspace_write_disabled", lambda: self.apply())
        caps = self.app.get_capabilities()
        self.assertFalse(caps["machines"]["local"]["workspace_write_effective"])

    def test_plan_hash_binds_mode_and_source_without_a_preview_store(self):
        args = self.arguments()
        preview = self.app.edit_workspace_file(**args, dry_run=True)
        self.file.chmod(0o755)
        result = self.apply(args, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual((result["status"], result["error"]), ("rejected", "plan_mismatch"))
        self.assertIn(b"41", self.file.read_bytes())
        self.assertEqual(self.apply(args)["status"], "applied")

    def test_same_commit_branch_switch_invalidates_observed_identity(self):
        self.initialize_git()
        args = self.arguments()
        self.git("checkout", "--quiet", "-b", "other")
        current = self.arguments()
        self.assertEqual(args["expected_head_sha"], current["expected_head_sha"])
        self.assertNotEqual(args["expected_workspace_identity"], current["expected_workspace_identity"])
        rejected = self.apply(args)
        self.assertEqual(rejected["error"], "context_mismatch")
        self.assertIn("changed context", rejected["recovery_hint"])
        self.assertEqual(rejected["effects"], [])
        self.assertIn(b"41", self.file.read_bytes())
        preview = self.app.edit_workspace_file(**current, dry_run=True)
        self.assertEqual(self.apply(current, expected_plan_sha256=preview["plan_sha256"])["status"], "applied")

    def test_root_replacement_invalidates_identity_even_with_identical_text(self):
        args = self.arguments()
        self.work.rename(self.root / "original-root")
        self.work.mkdir()
        self.file.write_bytes(b"answer = 41\r\nuntouched = 7")
        self.assertEqual(self.apply(args)["error"], "context_mismatch")
        self.assertIn(b"41", self.file.read_bytes())

    def test_bom_crlf_unterminated_and_bare_cr_exact_edits_preserve_bytes(self):
        for raw in (b"\xef\xbb\xbfvalue 41\r\nlast", b"41", b"41\rrest", "unicode \u00e9 41\n".encode()):
            with self.subTest(raw=raw):
                self.file.write_bytes(raw)
                self.assertEqual(self.apply()["status"], "applied")
                self.assertEqual(self.file.read_bytes(), raw.replace(b"41", b"42"))

    def test_same_id_is_original_evidence_after_later_edits_and_disabled_writes(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        first = self.apply(args, change_id=identity)
        stored = (self.directory(identity) / "record.json").read_bytes()
        self.file.write_bytes(b"third party")
        self.config.machines["local"].workspace_write_enabled = False
        with patch.object(changes, "publish_file", side_effect=AssertionError("Must not replay")):
            second = self.apply(args, change_id=identity)
        self.assertEqual((second["status"], second["created_at"], second["revision"]),
                         (first["status"], first["created_at"], first["revision"]))
        current = self.app.read_workspace_change("local", identity, True)
        self.assertEqual(current["current"]["sha256"], hashlib.sha256(b"third party").hexdigest())
        self.assertEqual((self.directory(identity) / "record.json").read_bytes(), stored)
        self.assertEqual(self.file.read_bytes(), b"third party")
        self.assertFalse(current["historical_diff_available"])

    def test_same_id_changed_payload_or_preview_binding_conflicts(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        self.apply(args, change_id=identity)
        for delta in ({"edits": [{"old_text": "41", "new_text": "43"}]}, {"expected_plan_sha256": "a" * 64}):
            self.fault("change_id_conflict", lambda: self.apply(args, change_id=identity, **delta))
        self.assertIn(b"42", self.file.read_bytes())

    def test_workspace_and_machine_aliases_do_not_create_new_operation_identity(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        first = self.apply(args, change_id=identity)
        self.config.machines["alias"] = Machine(transport="local", execution_enabled=True)
        self.config.workspaces["alias-project"] = Workspace(machine="alias", root=str(self.work))
        again = self.apply(args, machine="alias", workspace_name="alias-project", change_id=identity)
        self.assertEqual(again["created_at"], first["created_at"])
        self.assertEqual(again["request_sha256"], first["request_sha256"])

    def test_remapped_alias_cannot_read_another_root_through_old_receipt(self):
        result = self.apply()
        other = self.root / "other"
        other.mkdir()
        self.config.workspaces["project"].root = str(other)
        self.fault("scope_unavailable", lambda: self.app.read_workspace_change("local", result["change_id"]))

    def test_durable_whole_write_checks_hash_before_unchanged_but_legacy_does_not(self):
        content = self.file.read_bytes().decode()
        read = self.arguments()
        result = self.app.write_workspace_file("local", "project", "source.py", content, "", "",
            uuid.uuid4().hex, read["expected_workspace_identity"])
        self.assertEqual((result["status"], result["error"]), ("rejected", "content_mismatch"))
        legacy = self.app.write_workspace_file("local", "project", "source.py", content, "", "")
        self.assertEqual(legacy["status"], "already_current")
        self.assertFalse(legacy["durable_recovery"])
        valid = self.app.write_workspace_file("local", "project", "source.py", content, "", read["expected_sha256"],
            uuid.uuid4().hex, read["expected_workspace_identity"])
        self.assertEqual(valid["status"], "unchanged")

    def test_new_file_no_clobber_if_destination_appears_after_preflight(self):
        read = self.app.read_workspace_file("local", "project", "new.py")
        original = changes.publish_file
        def raced(config, machine, workspace_name, source, desired, temporary):
            (self.work / "new.py").write_text("other writer")
            return original(config, machine, workspace_name, source, desired, temporary)
        with patch.object(changes, "publish_file", side_effect=raced):
            result = self.app.write_workspace_file("local", "project", "new.py", "new", "", "", uuid.uuid4().hex,
                                                  read["context"]["workspace_identity"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual((self.work / "new.py").read_text(), "other writer")

    def test_parent_swap_is_not_hidden_by_identical_source_bytes(self):
        parent = self.work / "nested"
        parent.mkdir()
        self.file.rename(parent / "source.py")
        read = self.app.read_workspace_file("local", "project", "nested/source.py")
        args = dict(machine="local", workspace_name="project", path="nested/source.py",
            edits=[{"old_text": "41", "new_text": "42"}], expected_workspace_identity=read["context"]["workspace_identity"],
            expected_head_sha="", expected_sha256=read["sha256"])
        original = changes.publish_file
        def raced(*values):
            parent.rename(self.work / "detached")
            parent.mkdir()
            (parent / "source.py").write_bytes(b"answer = 41\r\nuntouched = 7")
            return original(*values)
        with patch.object(changes, "publish_file", side_effect=raced):
            result = self.apply(args)
        self.assertEqual(result["status"], "rejected")
        self.assertIn(b"41", (parent / "source.py").read_bytes())
        self.assertIn(b"41", (self.work / "detached/source.py").read_bytes())

    def test_failure_after_actual_replace_is_unknown_and_cannot_replay(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        original = os.replace
        def replaced(source, target, *a, **kw):
            result = original(source, target, *a, **kw)
            if str(source) == ".openmachine-change-" + identity:
                raise OSError("Lost post-rename acknowledgement")
            return result
        with patch.object(files.os, "replace", side_effect=replaced):
            result = self.apply(args, change_id=identity)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertNotIn("recovery_hint", result)
        self.fault("activation_deferred", lambda: workspace.check_change_state(self.state, require_idle=True))
        self.assertNotIn("recovery_hint", self.app.read_workspace_change("local", identity))
        self.assertEqual(result["effects"][0]["stage"], "unknown")
        self.assertIn(b"42", self.file.read_bytes())
        self.file.write_bytes(b"third party")
        with patch.object(changes, "publish_file", side_effect=AssertionError("No replay")):
            self.assertEqual(self.apply(args, change_id=identity)["status"], "outcome_unknown")
        self.assertEqual(self.file.read_bytes(), b"third party")

    def test_receipt_failure_before_intent_proves_no_planned_effect(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        original = change_records.save_json
        def failed(path, value):
            if value["status"] == "applying":
                raise OSError("Fixture receipt failure")
            original(path, value)
        with patch.object(change_records, "save_json", side_effect=failed):
            self.fault("change_outcome_unknown", lambda: self.apply(args, change_id=identity))
        read = self.app.read_workspace_change("local", identity)
        self.assertEqual((read["status"], read["reconciliation"]), ("prepared", "no_effects"))
        self.assertIn(b"41", self.file.read_bytes())

    def test_receipt_failure_after_effect_preserves_unknown_original_intent(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        original = change_records.save_json
        def failed(path, value):
            if value["status"] == "applied":
                raise OSError("Fixture final receipt failure")
            original(path, value)
        with patch.object(change_records, "save_json", side_effect=failed):
            error = self.fault("change_outcome_unknown", lambda: self.apply(args, change_id=identity))
        self.assertEqual(error.evidence["effects"][0]["stage"], "applied")
        self.assertIn(b"42", self.file.read_bytes())
        read = self.app.read_workspace_change("local", identity, True)
        self.assertEqual((read["status"], read["reconciliation"]), ("applying", "outcome_unknown"))
        self.assertEqual(read["current"]["sha256"], hashlib.sha256(self.file.read_bytes()).hexdigest())
        with patch.object(changes, "publish_file", side_effect=AssertionError("No replay")):
            self.assertEqual(self.apply(args, change_id=identity)["status"], "applying")

    def test_failed_parent_sync_is_unknown_not_success(self):
        original = os.fsync
        inode = self.work.stat().st_ino
        def failed(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and info.st_ino == inode:
                raise OSError("Fixture parent sync failure")
            return original(fd)
        with patch.object(files.os, "fsync", side_effect=failed):
            result = self.apply()
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertFalse(result["effects"][0]["synced"])
        self.assertIn(b"42", self.file.read_bytes())

    def test_missing_reads_and_previews_do_not_initialize_or_repair_state(self):
        self.fault("change_not_found", lambda: self.app.read_workspace_change("local", uuid.uuid4().hex))
        self.assertFalse(self.state.exists())
        self.assertEqual(workspace.check_change_state(self.state)["workspace_change_records"], 0)
        self.assertFalse(self.state.exists())

    def test_capacity_keeps_existing_identity_readable_and_deduplicated(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        first = self.apply(args, change_id=identity)
        with patch.object(change_records, "MAX_CHANGES", 1):
            self.fault("change_store_full", lambda: self.apply(args))
            self.assertEqual(self.apply(args, change_id=identity)["created_at"], first["created_at"])
        self.assertEqual(len(list((self.state / "workspace-changes").iterdir())), 1)

    def test_corrupt_or_incomplete_history_is_never_a_new_request(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        self.apply(args, change_id=identity)
        path = self.directory(identity) / "record.json"
        record = json.loads(path.read_text())
        record["format"] = 99
        path.write_text(json.dumps(record))
        self.fault("unsupported_change_format", lambda: self.apply(args, change_id=identity))
        path.unlink()
        self.fault("change_history_unavailable", lambda: self.apply(args, change_id=identity))
        self.assertFalse(path.exists())
        self.assertIn(b"42", self.file.read_bytes())

    def test_record_contains_no_source_or_edit_payload_and_diff_is_redacted(self):
        secret = "sk-" + "x" * 30
        self.file.write_text("token = " + secret + "\nanswer = 41")
        result = self.apply()
        record = (self.directory(result["change_id"]) / "record.json").read_text()
        self.assertNotIn(secret, record)
        self.assertNotIn("old_text", record)
        self.assertNotIn("answer = 41", record)
        self.assertNotIn(secret, result["diff"])
        self.assertEqual((self.directory(result["change_id"]) / "record.json").stat().st_mode & 0o777, 0o600)

    def test_invalid_payloads_and_source_bounds(self):
        for edits in ([], [{"old_text": "", "new_text": "x"}], [{"old_text": "x", "new_text": "x"}],
                      [{"old_text": "41", "new_text": "\0"}], [{"old_text": "41", "new_text": "\ud800"}]):
            args = self.arguments()
            args["edits"] = edits
            with self.assertRaises(Fault):
                self.apply(args)
        args = self.arguments()
        self.file.write_bytes(b"x" * (files.MAX_SOURCE + 1))
        result = self.apply(args)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.file.stat().st_size, files.MAX_SOURCE + 1)
        self.assertEqual(self.app.get_capabilities()["workspace"]["source_bytes"], files.MAX_SOURCE)

    def test_git_classifications_and_missing_git_fail_closed(self):
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(git.context(self.work)["git_state"], "non_git")
            self.assertEqual(self.apply()["status"], "applied")
        self.initialize_git()
        self.assertEqual(git.context(self.work)["git_state"], "worktree")
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(git.context(self.work)["git_state"], "unavailable")
        self.git("checkout", "--detach", "--quiet")
        self.assertEqual(git.context(self.work)["git_state"], "detached")
        (self.work / ".git/HEAD").write_text("broken HEAD")
        self.assertEqual(git.context(self.work)["git_state"], "unavailable")

    def test_unborn_git_and_unsupported_object_format(self):
        self.git("init", "--quiet", "-b", "main")
        self.assertEqual(git.context(self.work)["git_state"], "unborn")
        self.assertEqual(self.apply()["status"], "applied")
        actual = git.capture
        def unsupported(root, args):
            raw, code, clipped = actual(root, args)
            if "--show-object-format" in args:
                raw = raw.replace(b"sha1\n", b"sha256\n")
            return raw, code, clipped
        with patch.object(git, "capture", side_effect=unsupported):
            result = git.context(self.work)
        self.assertEqual((result["git_state"], result["context_error"]), ("unavailable", "unsupported_git_object_format"))

    def test_overlapping_non_git_roots_reject_mutation_but_not_read(self):
        nested = self.work / "nested"
        nested.mkdir()
        self.config.workspaces["nested"] = Workspace(machine="local", root=str(nested))
        self.assertTrue(self.app.read_workspace_file("local", "project", "source.py")["exists"])
        result = self.apply()
        self.assertEqual((result["status"], result["error"]), ("rejected", "ambiguous_workspace"))

    def test_ambiguous_historical_non_git_root_requires_free_original_lease(self):
        nested = self.work / "nested"
        nested.mkdir()
        identity = uuid.uuid4().hex
        result = self.app.start_execution("local", identity, str(nested), [sys.executable, "-I", "-c", "pass"])
        deadline = time.monotonic() + 10
        while result["status"] != "succeeded":
            self.assertLess(time.monotonic(), deadline)
            result = self.app.read_execution("local", identity, wait_seconds=0.2)
        self.assertEqual((result["exit_code"], result["cleanup"]["status"]), (0, "verified"))
        path = self.state / "executions" / identity / "record.json"
        record = json.loads(path.read_text())
        record["context"].pop("workspace_lock_root")
        path.write_text(json.dumps(record))
        retained = path.read_bytes()
        old_key = hashlib.sha256(str(nested).encode()).hexdigest()
        old_lock = self.state / "locks" / f"workspace-{old_key}.lock"
        self.config.workspaces["outer"] = Workspace(machine="local", root=str(self.root))
        fingerprint = self.config.configuration_sha256()
        with lease(old_lock):
            inode = old_lock.stat().st_ino
            # A terminal label alone must not bypass an original lease still held.
            self.fault("legacy_workspace_busy", lambda: execution_api.check_workspace_leases(self.config, self.state))
        execution_api.check_workspace_leases(self.config, self.state)
        execution_api.check_workspace_leases(self.config, self.state, legacy_candidate=True)
        self.assertEqual((path.read_bytes(), old_lock.stat().st_ino), (retained, inode))
        self.assertEqual(self.config.configuration_sha256(), fingerprint)
        self.assertEqual(self.apply()["error"], "ambiguous_workspace", "New ambiguous writes still reject")
        self.fault("ambiguous_workspace", lambda: self.app.start_execution(
            "local", uuid.uuid4().hex, str(nested), [sys.executable, "-I", "-c", "pass"]))
        self.assertEqual(len(list((self.state / "executions").iterdir())), 1)
        old_lock.unlink()
        self.fault("legacy_workspace_busy", lambda: execution_api.check_workspace_leases(self.config, self.state))
        self.assertFalse(old_lock.exists(), "Missing old evidence must never be recreated")
        self.assertEqual(path.read_bytes(), retained)

    def test_legacy_omitted_edit_grant_keeps_configuration_fingerprint(self):
        value = self.config.model_dump()
        # A default advisory update preference is also omitted from legacy identity.
        self.assertTrue(value.pop("check_for_updates"))
        for target in value["machines"].values():
            target.pop("workspace_write_enabled")
            self.assertIsNone(target.pop("ssh_connection"))
        expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(self.config.configuration_sha256(), expected)
        self.config.machines["local"].workspace_write_enabled = False
        self.assertNotEqual(self.config.configuration_sha256(), expected)

    def test_reader_never_calls_a_stale_prepared_snapshot_no_effects_after_writer_exit(self):
        prepared, release = threading.Event(), threading.Event()
        original_save, original_load = change_records._save, change_records._load
        reader_thread = threading.get_ident()
        identity = uuid.uuid4().hex
        args = self.arguments()
        def pause(directory, record):
            original_save(directory, record)
            if record["status"] == "prepared":
                prepared.set()
                if not release.wait(5):
                    raise AssertionError("Reader did not release the fixture writer")
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(change_records, "_save", side_effect=pause):
            writer = pool.submit(self.apply, args, change_id=identity)
            try:
                self.assertTrue(prepared.wait(5))
                def switch(directory):
                    record = original_load(directory)
                    if threading.get_ident() == reader_thread:
                        release.set()
                        self.assertEqual(writer.result(timeout=5)["status"], "applied")
                    return record
                with patch.object(change_records, "_load", side_effect=switch):
                    observed = self.app.read_workspace_change("local", identity)
                self.assertNotEqual(observed["reconciliation"], "no_effects")
                self.assertIn(b"42", self.file.read_bytes())
            finally:
                release.set()
                writer.result(timeout=5)

    def test_same_id_concurrent_request_observes_original_publisher(self):
        entered, release = threading.Event(), threading.Event()
        original = changes.publish_file
        identity = uuid.uuid4().hex
        args = self.arguments()
        def paused(*values):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Concurrent fixture did not release its publisher")
            return original(*values)
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(changes, "publish_file", side_effect=paused) as publisher:
            writer = pool.submit(self.apply, args, change_id=identity)
            try:
                self.assertTrue(entered.wait(5))
                other = self.apply(args, change_id=identity)
                self.assertEqual(other["status"], "applying")
                self.assertTrue(other["writer_alive"])
                self.fault("change_id_conflict", lambda: self.apply(args, change_id=identity,
                    edits=[{"old_text": "41", "new_text": "43"}]))
                self.assertEqual(publisher.call_count, 1)
            finally:
                release.set()
                self.assertEqual(writer.result(timeout=5)["status"], "applied")
        self.assertEqual(self.file.read_bytes(), b"answer = 42\r\nuntouched = 7")

    def test_initial_record_failure_and_missing_lock_never_reinitialize_identity(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        with patch.object(change_records, "save_json", side_effect=OSError("Initial record failed")):
            self.fault("change_record_unverified", lambda: self.apply(args, change_id=identity))
        self.fault("change_history_unavailable", lambda: self.apply(args, change_id=identity))
        self.assertFalse((self.directory(identity) / "record.json").exists())
        self.assertIn(b"41", self.file.read_bytes())
        result = self.apply(args)
        original_lock = self.directory(result["change_id"]) / "writer.lock"
        with lease(original_lock):
            original_lock.unlink()
            self.fault("change_history_unavailable", lambda: self.app.read_workspace_change("local", result["change_id"]))
            self.assertFalse(original_lock.exists())

    def test_record_status_and_effect_corruption_are_rejected_without_repair(self):
        result = self.apply()
        path = self.directory(result["change_id"]) / "record.json"
        original = path.read_bytes()
        for field, value in (("revision", True), ("status", "accepted"), ("effects", [])):
            candidate = json.loads(original)
            candidate[field] = value
            path.write_text(json.dumps(candidate))
            corrupted = path.read_bytes()
            self.fault("change_store_corrupt", lambda: self.app.read_workspace_change("local", result["change_id"]))
            self.assertEqual(path.read_bytes(), corrupted)
        path.write_bytes(original)
        self.assertEqual(self.app.read_workspace_change("local", result["change_id"])["status"], "applied")

    def test_edit_paths_and_new_writer_ids_reject_unsafe_requests(self):
        outside = self.root / "outside"
        outside.write_text("unchanged")
        (self.work / "link").symlink_to(outside)
        os.link(outside, self.work / "hard")
        os.mkfifo(self.work / "fifo")
        for path in ("../outside", ".git/config", ".env", "link", "hard", "fifo"):
            args = self.arguments()
            args["path"] = path
            before = self.file.read_bytes()
            try:
                result = self.apply(args)
                self.assertEqual(result["status"], "rejected")
            except Fault:
                pass
            self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(outside.read_text(), "unchanged")
        self.fault("invalid_precondition", lambda: workspace.write_text_file(self.config, self.state, "local",
            "project", "source.py", "must not write", "", self.arguments()["expected_sha256"], 0))

    def test_preview_counts_deleted_final_line_without_inventing_an_added_line(self):
        self.file.write_bytes(b"keep\nremove\n")
        args = self.arguments()
        args["edits"] = [{"old_text": "remove\n", "new_text": ""}]
        preview = self.app.edit_workspace_file(**args, dry_run=True)
        self.assertEqual(preview["diff"], "--- a/source.py\n+++ b/source.py\n@@ -2,1 +1,0 @@\n-remove\n")
        self.assertFalse(self.state.exists())
        applied = self.apply(args, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual(applied["diff"], preview["diff"])
        self.assertEqual(self.file.read_bytes(), b"keep\n")

    def test_large_source_small_edit_preserves_original_receipt_and_index(self):
        original = b"padding = 0\n" * 15000 + b"answer = 41\r\nlast"
        self.file.write_bytes(original)
        self.initialize_git()
        (self.work / "unrelated").write_text("staged")
        self.git("add", "unrelated")
        index = (self.work / ".git/index").read_bytes()
        args = self.arguments()
        self.assertLess(len(json.dumps(args).encode()), 2048)
        preview = self.app.edit_workspace_file(**args, dry_run=True)
        self.assertGreater(preview["plan"]["ranges"][0]["old_start"], 131072)
        self.assertFalse((self.state / "workspace-changes").exists())
        identity = uuid.uuid4().hex
        result = self.apply(args, change_id=identity, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.file.read_bytes(), original.replace(b"41", b"42"))
        self.assertEqual((self.work / ".git/index").read_bytes(), index)
        record_path = self.directory(identity) / "record.json"
        retained = record_path.read_bytes()
        self.assertEqual(workspace.check_change_state(self.state)["workspace_change_records"], 1)
        current = self.app.read_workspace_change("local", identity, observe_current=True)
        self.assertEqual(current["current"]["sha256"], result["plan"]["after_sha256"])
        self.file.write_bytes(original.replace(b"41", b"99"))
        retry = self.apply(args, change_id=identity, expected_plan_sha256=preview["plan_sha256"])
        self.assertEqual(retry["created_at"], result["created_at"])
        self.assertTrue(self.file.read_bytes().endswith(b"99\r\nlast"))
        self.assertEqual(record_path.read_bytes(), retained)

    def test_source_and_payload_bounds_are_independent(self):
        self.file.write_bytes(b"x" * (files.MAX_SOURCE - 2) + b"41")
        args = self.arguments()
        result = self.apply(args)
        self.assertEqual(result["status"], "applied")
        self.assertLessEqual(len(result["diff"].encode()), 32768)
        self.assertTrue(result["truncated"])
        args = self.arguments()
        args["edits"] = [{"old_text": "42", "new_text": "longer"}]
        before = self.file.read_bytes()
        rejected = self.apply(args)
        self.assertEqual((rejected["status"], rejected["error"]), ("rejected", "source_limit"))
        self.assertEqual(self.file.read_bytes(), before)
        args["edits"] = [{"old_text": "42", "new_text": "a" * 131072}]
        self.fault("request_limit", lambda: self.apply(args))
        self.fault("invalid_content", lambda: self.app.write_workspace_file(
            "local", "project", "new.txt", "a" * 131073, "", ""))
        self.assertFalse((self.work / "new.txt").exists())
        with files.file_parent(self.work, ("source.py",)) as parent:
            self.fault("source_limit", lambda: files.file_bytes(parent, "source.py"))
        caps = self.app.get_capabilities()
        self.assertEqual(caps["workspace"]["payload_bytes"], 131072)
        self.assertEqual(caps["limits"]["skill_text_file_bytes"], 131072)


if __name__ == "__main__":
    unittest.main()
