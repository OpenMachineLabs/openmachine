"""Operation scope admission: whole-map validation and typed-tool enforcement before effects."""

import copy
import os
import unittest
from unittest.mock import patch
import uuid

from openmachine.app import scope as operation_scope
from openmachine.execution import seatbelt
from test.support.operation_scope import ScopeFixture
from test.support.tasks import task_contract


class ScopeAdmissionTests(ScopeFixture, unittest.TestCase):
    def read(self, workspace, path, scope=None):
        return self.app.read_workspace_file("local", str(workspace), path, operation_scope=scope or self.scope())

    def test_malformed_foreign_or_duplicate_maps_are_rejected(self):
        valid = self.scope()
        cases = [dict(valid, protocol=2), dict(valid, unexpected=True), dict(valid, roots=[]),
                 dict(valid, authority=dict(valid["authority"], policy_revision=True)),
                 dict(valid, roots=[dict(valid["roots"][0], machine="other")]),
                 dict(valid, roots=[valid["roots"][1], dict(valid["roots"][1])])]
        for value in cases:
            with self.subTest(value=value):
                self.fault("scope_invalid", lambda: self.read(self.core, "file.txt", value))

    def test_a_replaced_directory_is_never_rebound(self):
        value = self.scope()
        self.core.rename(self.root / "moved")
        self.core.mkdir()
        self.fault("scope_target_changed", lambda: self.read(self.cloud, "file.txt", value))

    def test_nesting_is_a_union_but_misleading_or_broad_maps_fail(self):
        (self.core / "docs").mkdir()
        same_mode = self.scope(self.binding(self.dev, "read_only"), self.binding(self.desktop, "read_only"))
        self.assertEqual(self.read(self.desktop, "file.txt", same_mode)["text"], "desktop\n")
        inner = self.scope(self.binding(self.core, "full_access"), self.binding(self.core / "docs", "read_only"))
        self.fault("scope_conflict", lambda: self.read(self.core, "file.txt", inner))
        alias = self.binding(self.dev, "full_access", workspace=self.root, relative="Development")
        self.fault("scope_conflict", lambda: self.read(self.dev, "README", self.scope(self.binding(self.dev, "read_only"), alias)))
        for home in (self.dev, self.core):
            with self.subTest(home=home), patch.object(operation_scope.execution, "account_home", return_value=home):
                self.fault("scope_too_broad", lambda: self.read(self.dev, "README"))
        with patch.object(operation_scope, "control_paths", return_value=(self.cloud / "state",)):
            self.fault("scope_conflict", lambda: self.read(self.dev, "README"))

    def test_typed_reads_follow_the_map_lexically_and_physically(self):
        self.assertEqual(self.read(self.desktop, "file.txt")["text"], "desktop\n")
        self.assertEqual(self.read(self.root, "Development/core/file.txt")["text"], "core\n")
        self.fault("path_out_of_scope", lambda: self.read(self.personal, "secret.txt"))
        self.fault("path_out_of_scope", lambda: self.read(self.root, "personal/secret.txt"))
        (self.desktop / "escape").symlink_to(self.personal, target_is_directory=True)
        self.fault("path_out_of_scope", lambda: self.app.inspect_workspace(
            "local", str(self.root), "files", "Development/desktop/escape", operation_scope=self.scope()))
        self.fault("path_out_of_scope", lambda: self.app.inspect_workspace(
            "local", str(self.root), "search", ".", query="SECRET", operation_scope=self.scope()))
        self.fault("path_out_of_scope", lambda: self.app.inspect_context(
            "local", str(self.personal), operation_scope=self.scope()))

    def test_typed_writes_need_full_and_never_touch_protected_files(self):
        def write(workspace, path):
            read = self.app.read_workspace_file("local", str(workspace), "file.txt")
            return self.app.write_workspace_file("local", str(workspace), path, "changed\n", read["context"]["head_sha"],
                                                 "", uuid.uuid4().hex, read["context"]["workspace_identity"],
                                                 operation_scope=self.scope())
        self.fault("path_out_of_scope", lambda: write(self.desktop, "new.txt"))
        self.fault("path_out_of_scope", lambda: write(self.core, ".vscode/tasks.json"))
        self.assertEqual(write(self.core, "new.txt")["status"], "applied")
        self.assertEqual((self.core / "new.txt").read_text(), "changed\n")
        self.assertFalse((self.desktop / "new.txt").exists())
        patch_text = "*** Begin Patch\n*** Add File: desktop/new.txt\n+x\n*** End Patch\n"
        self.fault("path_out_of_scope", lambda: self.app.apply_workspace_patch(
            "local", str(self.dev), patch_text, "0" * 64, "", [{"path": "desktop/new.txt", "expected_sha256": ""}],
            change_id=uuid.uuid4().hex, operation_scope=self.scope()))

    def test_unconfinable_or_unsupported_work_starts_nothing(self):
        self.fault("scope_unconfined_capability", lambda: self.app.start_codex_task(
            "local", uuid.uuid4().hex, str(self.core), "Goal", "https://example.invalid/", ["https://example.invalid"],
            operation_scope=self.scope()))
        self.fault("path_out_of_scope", lambda: self.app.start_execution(
            "local", uuid.uuid4().hex, str(self.desktop), ["true"], operation_scope=self.scope()))
        with (patch.object(seatbelt, "unavailable_reason", return_value="platform_unsupported"),
              patch.object(operation_scope.execution, "seatbelt_unavailable_reason", return_value="platform_unsupported")):
            capability = self.app.get_capabilities()["operation_scope"]
            self.assertEqual(capability["process"], {"backend": None, "status": "unavailable", "reason": "platform_unsupported"})
            self.fault("scope_unsupported", lambda: self.app.start_execution(
                "local", uuid.uuid4().hex, str(self.core), ["true"], operation_scope=self.scope()))
            self.assertEqual(self.read(self.core, "file.txt")["text"], "core\n")
        self.assertFalse((self.state / "executions").exists() and any((self.state / "executions").iterdir()))
        self.assertEqual(self.app.get_capabilities()["operation_scope"]["tools"], list(operation_scope.TOOLS))

    def test_task_contract_targets_must_be_full(self):
        def create(target, identity):
            context = self.app.read_workspace_file("local", str(target), "file.txt")["context"]
            request = copy.deepcopy(task_contract(None, identity))
            request["contract"]["targets"] = [{"machine": "local", "workspace": str(target),
                "workspace_identity": context["workspace_identity"], "head_sha": context["head_sha"]}]
            return self.app.create_task(**request, operation_scope=self.scope())
        self.fault("path_out_of_scope", lambda: create(self.desktop, "scoped-read-only"))
        self.assertEqual(create(self.core, "scoped-full")["task_id"], "scoped-full")


if __name__ == "__main__":
    unittest.main()
