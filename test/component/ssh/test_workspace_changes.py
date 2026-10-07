"""Tests for ssh behavior at the component layer."""

import unittest
from unittest.mock import patch
import uuid
from openmachine.config import Settings
from integrations.ssh import PROTOCOL, client, endpoint
from openmachine import __version__
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
