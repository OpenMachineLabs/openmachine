"""Tests for ssh behavior at the component layer."""

import unittest
from unittest.mock import patch
from integrations.ssh import PROTOCOL, endpoint
from openmachine import __version__
from test.support.workspace import WorkspaceFixture



class WorkspaceChangeTests(WorkspaceFixture, unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
