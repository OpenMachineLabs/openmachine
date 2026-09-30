"""Guarded edits, truthful fault windows and original target-owned change evidence."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import uuid

from openmachine import execution, service, workspace
from openmachine.bootstrap import create_application
from openmachine.config import Machine, Settings, Workspace
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease
from openmachine.workspace import changes, files, git
from openmachine.execution import api as execution_api
from integrations.ssh import PROTOCOL, client, endpoint
from openmachine import __version__


class WorkspaceChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="om-changes-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.file = self.work / "source.py"
        self.file.write_bytes(b"answer = 41\r\nuntouched = 7")
        self.state = self.root / "state"
        self.config = Settings.model_validate({"schema_version": 1,
            "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "workspaces": {"project": {"machine": "local", "root": str(self.work)}}})
        self.app = create_application(self.config, self.state)

    def arguments(self, **updates):
        read = self.app.read_workspace_file("local", "project", "source.py")
        return dict(machine="local", workspace_name="project", path="source.py",
                    edits=[{"old_text": "41", "new_text": "42"}],
                    expected_workspace_identity=read["context"]["workspace_identity"],
                    expected_head_sha=read["context"]["head_sha"] or "",
                    expected_sha256=read["sha256"], **updates)

    def apply(self, args=None, **updates):
        values = dict(args or self.arguments())
        values.setdefault("change_id", uuid.uuid4().hex)
        values.update(updates)
        return self.app.edit_workspace_file(**values)

    def fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args], stderr=subprocess.PIPE).decode().strip()

    def initialize_git(self):
        self.git("init", "--quiet", "-b", "main")
        self.git("add", "source.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                 "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture")

    def directory(self, change_id):
        return self.state / "workspace-changes" / change_id

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
                self.assertEqual(self.file.read_text(), source)
                self.assertFalse(list(self.work.glob(".openmachine-*")))

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
        self.assertEqual(self.apply(args)["error"], "context_mismatch")
        self.assertIn(b"41", self.file.read_bytes())

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
        self.assertEqual(result["effects"][0]["stage"], "unknown")
        self.assertIn(b"42", self.file.read_bytes())
        self.file.write_bytes(b"third party")
        with patch.object(changes, "publish_file", side_effect=AssertionError("No replay")):
            self.assertEqual(self.apply(args, change_id=identity)["status"], "outcome_unknown")
        self.assertEqual(self.file.read_bytes(), b"third party")

    def test_receipt_failure_before_intent_proves_no_planned_effect(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        original = changes.save_json
        def failed(path, value):
            if value["status"] == "applying":
                raise OSError("Fixture receipt failure")
            original(path, value)
        with patch.object(changes, "save_json", side_effect=failed):
            self.fault("change_outcome_unknown", lambda: self.apply(args, change_id=identity))
        read = self.app.read_workspace_change("local", identity)
        self.assertEqual((read["status"], read["reconciliation"]), ("prepared", "no_effects"))
        self.assertIn(b"41", self.file.read_bytes())

    def test_receipt_failure_after_effect_preserves_unknown_original_intent(self):
        args = self.arguments()
        identity = uuid.uuid4().hex
        original = changes.save_json
        def failed(path, value):
            if value["status"] == "applied":
                raise OSError("Fixture final receipt failure")
            original(path, value)
        with patch.object(changes, "save_json", side_effect=failed):
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
        with patch.object(changes, "MAX_CHANGES", 1):
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

    def test_non_git_subdirectory_and_idle_pty_hold_the_same_edit_lease(self):
        nested = self.work / "nested"
        nested.mkdir()
        identity = uuid.uuid4().hex
        result = self.app.start_execution("local", identity, str(nested),
            [sys.executable, "-I", "-q", "-i"], tty=True, timeout_seconds=15)
        try:
            edited = self.apply()
            self.assertEqual((edited["status"], edited["error"]), ("rejected", "resource_busy"))
            record = execution.read_record(self.state / "executions" / identity)
            self.assertEqual(record["context"]["workspace_lock_root"], str(self.work))
            self.fault("legacy_workspace_busy", lambda: execution_api.check_workspace_leases(self.config, self.state, legacy_candidate=True))
        finally:
            self.app.cancel_execution("local", identity)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = self.app.read_execution("local", identity)
                if result["status"] in {"cancelled", "succeeded", "failed", "outcome_unknown"}:
                    break
                time.sleep(0.03)
            self.assertEqual(result["cleanup"]["status"], "verified")

    def test_legacy_non_git_cwd_lock_blocks_transition_without_replacing_it(self):
        nested = self.work / "nested"
        nested.mkdir()
        args = self.arguments()
        identity = uuid.uuid4().hex
        # Use a real current execution record as the legacy-layout fixture, after it exits.
        result = self.app.start_execution("local", identity, str(nested), [sys.executable, "-c", "pass"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.app.read_execution("local", identity)
            if result["status"] == "succeeded":
                break
            time.sleep(0.03)
        self.assertEqual(result["status"], "succeeded")
        directory = self.state / "executions" / identity
        path = directory / "record.json"
        record = json.loads(path.read_text())
        record["context"].pop("workspace_lock_root")
        path.write_text(json.dumps(record))
        old_key = hashlib.sha256(str(nested).encode()).hexdigest()
        old_lock = self.state / "locks" / f"workspace-{old_key}.lock"
        with lease(old_lock):
            inode = old_lock.stat().st_ino
            edit = self.apply(args)
            self.assertEqual((edit["status"], edit["error"]), ("rejected", "legacy_workspace_busy"))
            self.assertEqual(old_lock.stat().st_ino, inode)
            other = self.root / "independent"
            other.mkdir()
            (other / "source.py").write_text("answer = 41")
            self.config.workspaces["independent"] = Workspace(machine="local", root=str(other))
            read = self.app.read_workspace_file("local", "independent", "source.py")
            separate = self.apply(args, workspace_name="independent",
                expected_workspace_identity=read["context"]["workspace_identity"], expected_sha256=read["sha256"])
            self.assertEqual(separate["status"], "applied")
        self.assertEqual(self.apply(args)["status"], "applied")

    def test_old_candidate_ignoring_new_store_is_rejected_by_actual_gate(self):
        self.apply()
        ignored = {"protocol": 1, "state_contract": 3}
        process = subprocess.CompletedProcess([], 0, json.dumps(ignored), "")
        with patch.object(service, "_run", return_value=process), patch.object(service, "load", return_value=self.config):
            self.fault("incompatible_state", lambda: service._candidate_state(Path(sys.executable), self.root / "config", self.state, 3))
        self.assertEqual(workspace.check_change_state(self.state)["workspace_change_records"], 1)

    def test_legacy_omitted_edit_grant_keeps_configuration_fingerprint(self):
        value = self.config.model_dump()
        for target in value["machines"].values():
            target.pop("workspace_write_enabled")
        expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(self.config.configuration_sha256(), expected)
        self.config.machines["local"].workspace_write_enabled = False
        self.assertNotEqual(self.config.configuration_sha256(), expected)

    def test_target_endpoint_uses_same_editor_and_preserves_error_evidence(self):
        identity = "a" * 64
        archive = self.root / ("b" * 64 + ".pyz")
        request = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                   "execution_enabled": False, "workspace_write_enabled": True,
                   "workspaces": {"p": str(self.work)}}
        def dispatch(operation, arguments):
            with patch.object(endpoint, "_identity", return_value=(identity, self.root)):
                return endpoint._dispatch(request | {"operation": operation, "arguments": arguments}, archive)["result"]
        read = dispatch("read_workspace_file", {"workspace": "p", "path": "source.py"})
        args = {"workspace": "p", "path": "source.py", "edits": [{"old_text": "41", "new_text": "42"}],
                "expected_workspace_identity": read["context"]["workspace_identity"], "expected_head_sha": "",
                "expected_sha256": read["sha256"], "change_id": uuid.uuid4().hex}
        first = dispatch("edit_workspace_file", args)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(dispatch("edit_workspace_file", args)["created_at"], first["created_at"])
        request["workspace_write_enabled"] = False
        self.assertEqual(dispatch("read_workspace_change", {"change_id": args["change_id"]})["status"], "applied")
        remote = Settings.model_validate({"schema_version": 1,
            "machines": {"ssh": {"transport": "ssh", "ssh_alias": "fixture", "ssh_worker": str(archive),
                                 "ssh_identity": identity, "workspace_write_enabled": True}},
            "workspaces": {"p": {"machine": "ssh", "root": str(self.work)}}})
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            error = self.fault("outcome_unknown", lambda: client.call(remote, "ssh", "edit_workspace_file", **args))
        self.assertEqual(error.evidence["change_id"], args["change_id"])
        self.assertEqual(exchange.call_count, 1)
        args.update(change_id="", dry_run=True)
        with patch.object(client, "_exchange", side_effect=TimeoutError):
            self.fault("ssh_unavailable", lambda: client.call(remote, "ssh", "edit_workspace_file", **args))

    def test_reader_never_calls_a_stale_prepared_snapshot_no_effects_after_writer_exit(self):
        prepared, release = threading.Event(), threading.Event()
        original_save, original_load = changes._save, changes._load
        reader_thread = threading.get_ident()
        identity = uuid.uuid4().hex
        args = self.arguments()
        def pause(directory, record):
            original_save(directory, record)
            if record["status"] == "prepared":
                prepared.set()
                if not release.wait(5):
                    raise AssertionError("Reader did not release the fixture writer")
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(changes, "_save", side_effect=pause):
            writer = pool.submit(self.apply, args, change_id=identity)
            try:
                self.assertTrue(prepared.wait(5))
                def switch(directory):
                    record = original_load(directory)
                    if threading.get_ident() == reader_thread:
                        release.set()
                        self.assertEqual(writer.result(timeout=5)["status"], "applied")
                    return record
                with patch.object(changes, "_load", side_effect=switch):
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
        with patch.object(changes, "save_json", side_effect=OSError("Initial record failed")):
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

    def test_edit_only_target_probe_needs_no_execution_scope(self):
        request = {"protocol": PROTOCOL, "version": __version__, "identity": "a" * 64,
                   "execution_enabled": False, "workspace_write_enabled": True,
                   "workspaces": {"p": str(self.work)}, "operation": "probe", "arguments": {}}
        with patch.object(endpoint, "_identity", return_value=("a" * 64, self.root)), \
             patch.object(endpoint, "_scope_ready", side_effect=AssertionError("No command grant")):
            reply = endpoint._dispatch(request, self.root / ("b" * 64 + ".pyz"))["result"]
        self.assertEqual(reply["workspace_change_format"], 1)
        self.assertEqual(reply["workspace_change_records"], 0)
        self.assertFalse(reply["workspace_change_store_present"])
        self.assertTrue(reply["legacy_workspace_compatible"])
        self.assertIsNone(reply["systemd"])
        self.assertFalse((self.root / ".local").exists())


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

    def test_line_windows_keep_original_bytes_and_eof_metadata(self):
        raw = b"\xef\xbb\xbffirst\r\n" + "\u754c second\n".encode() + b"last"
        self.file.write_bytes(raw)
        page = self.app.read_workspace_file("local", "project", "source.py", start_line=2, line_count=1)
        self.assertEqual(page["text"], "\u754c second\n")
        self.assertEqual((page["start_line"], page["end_line"], page["next_line"]), (2, 2, 3))
        self.assertEqual(page["offset"], len(b"\xef\xbb\xbffirst\r\n"))
        self.assertTrue(page["range_complete"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["sha256"], hashlib.sha256(raw).hexdigest())
        last = self.app.read_workspace_file("local", "project", "source.py", start_line=3, line_count=500)
        self.assertEqual(last["text"], "last")
        self.assertTrue(last["range_complete"])
        self.assertFalse(last["has_more"])
        self.assertNotIn("next_line", last)
        for raw in (b"", b"one\n", b"one", b"\xef\xbb\xbf"):
            self.file.write_bytes(raw)
            eof = self.app.read_workspace_file("local", "project", "source.py", start_line=99)
            self.assertEqual((eof["text"], eof["offset"], eof["start_line"], eof["end_line"]), ("", len(raw), None, None))
            self.assertTrue(eof["range_complete"])
            self.assertFalse(eof["has_more"])

    def test_long_unicode_line_continues_in_byte_mode_without_skips(self):
        original = "\u754c" * 25000 + "\nlast\n"
        self.file.write_text(original)
        page = self.app.read_workspace_file("local", "project", "source.py", start_line=1, line_count=1)
        self.assertTrue(page["line_truncated"])
        self.assertFalse(page["range_complete"])
        self.assertNotIn("next_line", page)
        parts = [page["text"]]
        while page["has_more"]:
            next_offset = page["next_offset"]
            page = self.app.read_workspace_file("local", "project", "source.py", offset=next_offset,
                                                 expected_sha256=page["sha256"])
            self.assertGreater(page["next_offset"], next_offset)
            self.assertLessEqual(len(page["text"].encode()), 32768)
            parts.append(page["text"])
        self.assertEqual("".join(parts), original)
        self.fault("invalid_range", lambda: self.app.read_workspace_file("local", "project", "source.py", offset=1))
        for delta in ({"start_line": True}, {"start_line": 0}, {"start_line": 1, "offset": 1},
                      {"line_count": 0}, {"line_count": 501}, {"line_count": True}):
            self.fault("invalid_range", lambda: self.app.read_workspace_file("local", "project", "source.py", **delta))

    def test_redaction_matches_full_source_before_byte_or_line_slicing(self):
        value = "private-fragment-" * 30
        for content in ("p" * 32755 + "\ntoken = " + value + "\npublic\n",
                        "p" * 32740 + "\n-----BEGIN PRIVATE KEY-----\n" + value + "\n-----END PRIVATE KEY-----\npublic\n"):
            self.file.write_text(content)
            offset, visible = 0, []
            while True:
                page = self.app.read_workspace_file("local", "project", "source.py", offset=offset)
                self.assertNotIn("private-fragment", page["text"])
                visible.append(page["text"])
                if not page["has_more"]:
                    break
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
            self.assertIn("public", "".join(visible))
        line = self.app.read_workspace_file("local", "project", "source.py", start_line=3, line_count=1)
        self.assertTrue(line["content_redacted"])
        self.assertNotIn("private-fragment", line["text"])

    def test_filtered_git_discovery_preserves_legacy_ignored_coverage(self):
        self.initialize_git()
        (self.work / ".gitignore").write_text("ignored.py\n")
        (self.work / "ignored.py").write_text("needle ignored\n")
        (self.work / "free.py").write_text("needle free\n")
        (self.work / "sub/deeper").mkdir(parents=True)
        (self.work / "sub/one.py").write_text("needle direct\n")
        (self.work / "sub/deeper/two.py").write_text("needle deeper\n")
        before = (self.work / ".git/index").read_bytes()
        legacy = self.app.inspect_workspace("local", "project", "search", query="needle")
        self.assertIn("ignored.py", [m["path"] for m in legacy["matches"]])
        filtered = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="*.py")
        self.assertEqual([m["path"] for m in filtered["matches"]], ["free.py"])
        listed = self.app.inspect_workspace("local", "project", "files", path_glob="sub/*.py")
        self.assertEqual([item["name"] for item in listed["entries"]], ["sub/one.py"])
        self.assertEqual(listed["discovery"], "git")
        self.assertEqual(self.app.inspect_workspace("local", "project", "files", path_glob="Sub/*.py")["entries"], [])
        self.assertIn("ignored", self.app.read_workspace_file("local", "project", "ignored.py")["text"])
        self.assertEqual((self.work / ".git/index").read_bytes(), before)

    def test_filtered_pages_bind_query_filter_names_and_source(self):
        for name in ("a.py", "b.py", "c.py"):
            (self.work / name).write_text("needle\n")
        first = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="?.py", limit=1)
        second = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="?.py",
                                             offset=first["next_offset"], expected_snapshot=first["snapshot"])
        self.assertEqual([m["path"] for m in second["matches"]], ["b.py", "c.py"])
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "search", query="needle",
            path_glob="[abc].py", expected_snapshot=first["snapshot"]))
        (self.work / "b.py").write_text("needle changed\n")
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "search", query="needle",
            path_glob="?.py", expected_snapshot=first["snapshot"]))
        page = self.app.inspect_workspace("local", "project", "files", path_glob="?.py", limit=1)
        (self.work / "d.py").write_text("new")
        self.fault("source_changed", lambda: self.app.inspect_workspace("local", "project", "files", path_glob="?.py",
            offset=1, expected_snapshot=page["snapshot"]))

    def test_filtered_git_failure_and_clipped_records_are_not_absence(self):
        self.initialize_git()
        with patch("openmachine.workspace.inspect.capture", return_value=(b"", 1, False)):
            self.fault("git_failed", lambda: self.app.inspect_workspace("local", "project", "files", path_glob="*.py"))
        with patch("openmachine.workspace.inspect.capture", return_value=(b"source.py\0incomplete", -9, True)):
            page = self.app.inspect_workspace("local", "project", "files", path_glob="*")
        self.assertEqual([item["name"] for item in page["entries"]], ["source.py"])
        self.assertTrue(page["truncated"])
        self.assertFalse(page["complete"])
        with patch("openmachine.workspace.inspect.capture", return_value=(b"\xff.py\0", 0, False)):
            page = self.app.inspect_workspace("local", "project", "files", path_glob="*")
        self.assertEqual(page["skipped"]["unsupported_path"], 1)
        self.assertFalse(page["complete"])

    def test_filtered_discovery_excludes_nested_repositories_and_links(self):
        (self.work / "nested").mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.work / "nested")], check=True)
        (self.work / "nested/file.py").write_text("needle hidden\n")
        (self.work / "link.py").symlink_to(self.file)
        page = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="nested/*.py")
        self.assertEqual(page["matches"], [])
        self.assertGreater(page["skipped"]["nested_repository"], 0)
        linked = self.app.inspect_workspace("local", "project", "files", path_glob="*.py")
        self.assertNotIn("link.py", [item["name"] for item in linked["entries"]])
        self.assertGreater(linked["skipped"]["symlink"], 0)

    def test_discovery_limits_and_invalid_filters_fail_truthfully(self):
        for index in range(5):
            (self.work / f"item{index}.py").write_text("ordinary\n")
        with patch("openmachine.workspace.inspect.MAX_SCAN_ENTRIES", 2):
            page = self.app.inspect_workspace("local", "project", "search", query="absent", path_glob="*.py")
        self.assertLessEqual(page["scanned_entries"], 2)
        self.assertTrue(page["truncated"])
        self.assertFalse(page["complete"])
        for pattern in ("**/*.py", "/absolute/*", "../*", "a//b", "a\\b", "x" * 1025):
            self.fault("invalid_filter", lambda: self.app.inspect_workspace("local", "project", "files", path_glob=pattern))
        self.fault("invalid_filter", lambda: self.app.inspect_workspace("local", "project", "status", path_glob="*.py"))
        with patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self.app.inspect_workspace("local", "project", "files", path_glob="*.py")["discovery"], "filesystem")

    def test_search_masks_before_cropping_and_large_filtered_sources_are_searchable(self):
        value = "s" * 400 + "needle" + "s" * 400
        self.file.write_text("token = " + value + "\nordinary needle\n")
        page = self.app.inspect_workspace("local", "project", "search", query="needle")
        self.assertTrue(page["matches"][0]["content_redacted"])
        self.assertNotIn("s" * 10, page["matches"][0]["text"])
        self.assertIn("ordinary needle", page["matches"][1]["text"])
        self.file.write_bytes(b"padding\n" * 150000 + b"needle tail\n")
        page = self.app.inspect_workspace("local", "project", "search", query="needle", path_glob="*.py")
        self.assertEqual(len(page["matches"]), 1)
        self.assertGreater(page["scanned_bytes"], 1024 * 1024)
        self.assertTrue(page["complete"])

    def test_nested_instruction_paths_are_metadata_not_implicit_prompts(self):
        (self.work / "sub").mkdir()
        (self.work / "sub/AGENTS.md").write_text("Do not execute this text.\n")
        (self.work / "sub/file.py").write_text("source\n")
        page = self.app.read_workspace_file("local", "project", "sub/file.py")
        self.assertIn(str(self.work / "sub/AGENTS.md"), page["context"]["agents_files"])
        self.assertEqual(page["text"], "source\n")
        self.assertFalse(self.state.exists())


    def test_target_line_and_filter_arguments_share_read_only_domain_code(self):
        self.file.write_bytes(b"padding\n" * 20000 + b"answer = 41\n")
        identity = "e" * 64
        archive = self.root / ("a" * 64 + ".pyz")
        request = {"protocol": PROTOCOL, "version": __version__, "identity": identity,
                   "execution_enabled": False, "workspace_write_enabled": False,
                   "workspaces": {"project": str(self.work)}, "operation": "read_workspace_file",
                   "arguments": {"workspace": "project", "path": "source.py", "start_line": 20001, "line_count": 1}}
        with patch.object(endpoint, "_identity", return_value=(identity, self.root)):
            response = endpoint._dispatch(request, archive)
            self.assertEqual(response["result"]["text"], "answer = 41\n")
            self.assertGreater(response["result"]["offset"], 131072)
            response = endpoint._dispatch(request | {"operation": "inspect_workspace",
                "arguments": {"workspace": "project", "action": "search", "query": "answer", "path_glob": "*.py"}}, archive)
            self.assertEqual(response["result"]["matches"][0]["line"], 20001)
            self.fault("protocol_mismatch", lambda: endpoint._dispatch(request | {"protocol": 4}, archive))
            self.fault("invalid_request", lambda: endpoint._dispatch(request | {
                "arguments": request["arguments"] | {"execute": True}}, archive))
        self.assertFalse((self.root / ".local").exists())

    def test_filtered_git_discovery_runs_no_external_filter(self):
        self.initialize_git()
        sentinel = self.work / "unexpected"
        program = self.work / "hook.sh"
        program.write_text("#!/bin/sh\ntouch " + str(sentinel) + "\ncat\n")
        program.chmod(0o700)
        (self.work / ".gitattributes").write_text("*.py filter=fixture diff=fixture\n")
        for setting in ("filter.fixture.clean", "filter.fixture.process", "diff.fixture.command", "diff.fixture.textconv", "core.fsmonitor"):
            self.git("config", setting, str(program))
        self.app.inspect_workspace("local", "project", "search", query="answer", path_glob="*.py")
        self.assertFalse(sentinel.exists())

    def test_page_window_redaction_and_response_failure_never_advance_hidden_bytes(self):
        from openmachine.foundation.files import text_page
        from openmachine.foundation.sanitize import redact_slice
        text = "token = abcdefghijklmnopqrstuvwxyz\n"
        self.assertEqual(redact_slice(text, 15, 15), "")
        page = text_page(text.encode(), 15)
        self.assertTrue(page["content_redacted"])
        self.assertNotIn("pqrst", page["text"])
        self.fault("workspace_response_limit", lambda: changes.bound_result(page, lambda result: len(json.dumps(result)), 5))
        self.assertEqual(page["next_offset"], len(text))
        self.fault("path_denied", lambda: self.app.read_workspace_file("local", "project", "\ud800"))
        self.fault("invalid_query", lambda: self.app.inspect_workspace("local", "project", "search", query="\ud800"))
