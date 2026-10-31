"""Tests for codex behavior at the component layer."""

import unittest
from unittest.mock import patch
from integrations import codex
from openmachine.bootstrap import create_application
from test.support.codex_cases import CodexCaseFixture


class CodexTests(CodexCaseFixture, unittest.TestCase):
    def test_packaged_runtime_can_use_the_configured_native_app_without_shell_path(self):
        for relative in ("codex-cli", "codex-cli/CodexCLI.app/Contents/MacOS/codex"):
            with self.subTest(layout=relative), patch.object(codex.shutil, "which", return_value=None):
                native = self.root / "App/Resources" / relative
                native.parent.mkdir(parents=True, exist_ok=True)
                with self.assertRaisesRegex(ValueError, "native Codex CLI unavailable"):
                    codex.discover()
                native.write_text("#!/bin/sh\nexit 0\n")
                native.chmod(0o600)
                with self.assertRaisesRegex(ValueError, "native Codex CLI unavailable"):
                    codex.discover()
                native.chmod(0o700)
                binary, _, options = codex.discover()
                self.assertEqual(binary, str(native))
                self.assertEqual(options["model"], "fixture-model")
                self.assertFalse((self.state / "executions").exists())
                native.unlink()

    def test_scope_rejects_ambiguous_and_non_browser_requests_before_execution(self):
        for changes in ({"goal": " "}, {"mode": "computer"}, {"allowed_origins": []},
                        {"start_url": "https://elsewhere.example"}, {"allowed_origins": ["https://*.example.com"]},
                        {"allowed_origins": ["https://example.com/path"]}, {"timeout_seconds": True},
                        {"timeout_seconds": 1801}, {"start_url": "https://user:pass@example.com"}):
            with self.subTest(changes=changes):
                self.fault("invalid_codex_task", self.app.start_codex_task, **(self.arguments() | changes))
        self.assertFalse((self.state / "executions").exists())
        self.assertEqual(codex.origin("HTTPS://EXAMPLE.COM:443/x"), "https://example.com")

    def test_passive_capabilities_and_discovery_inherit_only_native_bridge(self):
        with patch.object(codex, "discover", side_effect=AssertionError("Passive metadata launched a probe")):
            self.assertEqual(self.app.get_capabilities()["codex"]["availability"], "not_probed")
        binary, skill, options = codex.discover()
        self.assertEqual(set(options["mcp_servers"]), {"node_repl"})
        self.assertEqual(options["mcp_servers"]["node_repl"]["enabled_tools"], ["js"])
        self.assertEqual(options["mcp_servers"]["node_repl"]["env"]["CODEX_HOME"], str(self.home.resolve()))
        self.assertFalse(any(options["features"].values()))
        self.assertEqual(options["approval_policy"], "never")
        self.assertEqual(options["model"], "fixture-model")
        argv = codex.command(binary, self.root, options)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        with (self.home / "config.toml").open("a") as target:
            target.write('\n[mcp_servers.node_repl.env]\nUNAPPROVED_SECRET="not-copied"\n')
        with self.assertRaises(ValueError):
            codex.discover()
        self.assertTrue(skill.is_file())

    def test_delegation_never_implicitly_adds_a_native_execution_grant(self):
        self.request["contract"]["tools"] = ["start_codex_task"]
        self.fault("invalid_task_contract", self.create)
        disabled = self.config.model_copy(deep=True)
        disabled.machines["desktop"].execution_enabled = False
        app = create_application(disabled, self.state)
        self.fault("execution_disabled", app.start_codex_task, **self.arguments())
        self.assertFalse((self.state / "executions").exists())

    def test_native_bridge_environment_forwarding_is_explicit_and_bounded(self):
        configured = self.config_text.replace('args=[]', 'args=[]\nenv_vars=['
            '"BROWSER_USE_ACCESS_EXECUTOR_ID", "BROWSER_USE_ACCESS_PIPE_PATH", '
            '"BROWSER_USE_ACCESS_VERIFYING_IDENTITY"]')
        (self.home / "config.toml").write_text(configured)
        with patch.dict(codex.os.environ, {"BROWSER_USE_ACCESS_EXECUTOR_ID": "private-fixture-value"}):
            binary, _, options = codex.discover()
        names = options["mcp_servers"]["node_repl"]["env_vars"]
        self.assertEqual(names, ["BROWSER_USE_ACCESS_EXECUTOR_ID", "BROWSER_USE_ACCESS_PIPE_PATH",
                                 "BROWSER_USE_ACCESS_VERIFYING_IDENTITY"])
        self.assertNotIn("private-fixture-value", " ".join(codex.command(binary, self.root, options)))
        for value in ('"BROWSER_USE_ACCESS_EXECUTOR_ID"', '["UNAPPROVED_SECRET"]',
                      '[42]', '[{ name="BROWSER_USE_ACCESS_EXECUTOR_ID", source="remote" }]'):
            with self.subTest(value=value):
                (self.home / "config.toml").write_text(self.config_text.replace('args=[]', 'args=[]\nenv_vars=' + value))
                with self.assertRaises(ValueError):
                    codex.discover()


if __name__ == "__main__":
    unittest.main()
