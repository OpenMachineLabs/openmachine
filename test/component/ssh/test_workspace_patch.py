"""Tests for ssh behavior at the component layer."""

import unittest
from unittest.mock import patch
import uuid
from openmachine.config import Settings
from integrations.ssh import PROTOCOL, client, endpoint
from openmachine import __version__
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
