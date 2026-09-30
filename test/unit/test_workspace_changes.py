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
        self.assertEqual(reply["workspace_change_format"], 2)
        self.assertEqual(reply["workspace_change_read_formats"], [1, 2])
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


    def patch_arguments(self, body, *, paths=None, **updates):
        from openmachine.workspace.patch import parse, paths as touched_paths
        names = paths if paths is not None else touched_paths(parse(body))
        ctx = git.context(self.work)
        preconditions = []
        for name in names:
            target = self.work / name
            raw = target.read_bytes() if target.is_file() else None
            preconditions.append({"path": name, "expected_sha256": files.digest(raw)})
        return {"machine": "local", "workspace_name": "project", "patch": body,
                "expected_workspace_identity": ctx["workspace_identity"], "expected_head_sha": ctx["head_sha"] or "",
                "preconditions": preconditions, **updates}

    def patch_apply(self, args, **updates):
        return self.app.apply_workspace_patch(**(args | {"change_id": uuid.uuid4().hex} | updates))

    def patch_document(self, body):
        return "*** Begin Patch\n" + body + "*** End Patch\n"

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
        original = changes.save_json
        def failed(path, value):
            if value["status"] == "applying" and value["effects"][0]["stage"] == "applied":
                raise OSError("observed effect receipt unavailable")
            return original(path, value)
        with patch.object(changes, "save_json", side_effect=failed):
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
        original = changes.save_json
        def failed(path, value):
            if value["status"] == "applied":
                raise OSError("terminal save failure")
            return original(path, value)
        with patch.object(changes, "save_json", side_effect=failed):
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

    def test_patch_limits_and_complete_record_budget_reject_before_target_effects(self):
        from openmachine.workspace import patch as patcher
        self.fault("request_limit", lambda: patcher.parse(self.patch_document("*** Add File: new\n+" + "x" * 131072 + "\n")))
        self.fault("request_limit", lambda: patcher.parse(self.patch_document("".join(f"*** Add File: p{i}\n" for i in range(33)))))
        body = self.patch_document("*** Update File: source.py\n" + "@@\n-a\n+b\n" * 65)
        self.fault("request_limit", lambda: patcher.parse(body))
        original = self.file.read_bytes()
        args = self.patch_arguments(self.patch_document("*** Update File: source.py\n@@\n-answer = 41\n+answer = 42\n"))
        with patch.object(changes, "MAX_PREPARED", len(original) * 2 - 1):
            result = self.patch_apply(args)
        self.assertEqual((result["status"], result["error"]), ("rejected", "source_limit"))
        self.assertEqual(self.file.read_bytes(), original)
        with patch.object(changes, "MAX_RECORD", 3000):
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
        with patch.object(changes, "MAX_RECORD", 3000):
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
