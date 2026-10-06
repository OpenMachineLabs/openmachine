"""Browser delegation contracts with a disposable native-bridge/CLI fixture."""

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from integrations import codex
from openmachine.bootstrap import create_application
from openmachine.foundation.state import save_json
from support_tasks import TaskFixture


class CodexTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        save_json(self.home / "auth.json", {})
        (self.home / "AGENTS.md").write_text("Unrelated fixture instructions must not be inherited.\n")
        resources = self.root / "App/Resources"
        bridge = resources / "cua_node/bin/node_repl"
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\nexit 0\n")
        bridge.chmod(0o700)
        root = resources / "plugins/openai-bundled/plugins/browser"
        skill = root / "skills/control-in-app-browser/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Disposable Browser Skill, not a real browser backend.\n")
        (root / "scripts").mkdir()
        (root / "scripts/browser-client.mjs").write_text("// Fixture only.\n")
        self.config_text = ('model="fixture-model"\nmodel_reasoning_effort="xhigh"\n'
            '[mcp_servers.node_repl]\ncommand=' + json.dumps(str(bridge)) + '\nargs=[]\n'
            '[mcp_servers.unrelated]\ncommand="never-inherit"\n')
        (self.home / "config.toml").write_text(self.config_text)
        binary = self.root / "bin/codex"
        binary.parent.mkdir()
        binary.write_text("#!" + sys.executable + "\n" + '''import json,os,sys,time
from pathlib import Path
text=sys.stdin.read()
home=Path(os.environ["CODEX_HOME"])
assert not (home/"AGENTS.md").exists() and not (home/"AGENTS.override.md").exists()
assert (home/"auth.json").is_symlink()
packet=json.loads(text.rsplit("Delegated request:\\n",1)[1])
args=sys.argv[1:]
assert all(k in args for k in ["--ephemeral","--ignore-user-config","--json","--sandbox"])
assert args[args.index("--sandbox")+1]=="read-only"
assert "never-inherit" not in " ".join(args)
job=Path(args[args.index("-C")+1])
count=job/"fixture-counter"
count.write_text(str(int(count.read_text())+1) if count.exists() else "1")
if packet["goal"]=="hold":
    time.sleep(30)
report=dict(status="completed",summary="Observed fixture page",final_url=packet["start_url"],
            tab_id="tab-1",evidence=["Fixture heading is visible"],pending_action="")
if packet["goal"]=="blocked":
    report.update(status="blocked",pending_action="Sign in manually",evidence=[])
if packet["goal"]=="unicode":
    report["evidence"]=["\\u6d4b"*2000 for _ in range(12)]
print(json.dumps(dict(type="item.completed",item=dict(type="agent_message",text="PRIVATE_RAW_EVENT_SENTINEL"))),flush=True)
if packet["goal"]!="no-native":
    meta={"codex/browserUse":True,"browser_use":{"url":packet["start_url"]},
          "codex/toolSurface":{"browserId":"browser-1","openTabIds":["tab-1"],
                               "screenshot":{"url":"data:image/png;base64,fixture","tabId":"tab-1","pageUrl":packet["start_url"]}}}
    event=dict(type="item.completed",item=dict(type="mcp_tool_call",server="node_repl",tool="js",
        status="completed",arguments={"code":"await tab.ax.write(\\"both\\", { disableDiffing: true });"},
        result={"_meta":meta}))
    print(json.dumps(event),flush=True)
if packet["goal"]=="turn-failed":
    print(json.dumps(dict(type="turn.failed")),flush=True)
last=Path(args[args.index("--output-last-message")+1])
if packet["goal"]!="missing-final":
    last.write_text(json.dumps(report))
sys.exit(7 if packet["goal"]=="exit-failed" else 0)
''')
        binary.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.home),
            "PATH": str(binary.parent) + os.pathsep + os.environ.get("PATH", "")}))

    def test_packaged_runtime_can_use_the_configured_native_app_without_shell_path(self):
        native = self.root / "App/Resources/codex-cli"
        with patch.object(codex.shutil, "which", return_value=None):
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

    def arguments(self, goal="inspect", **updates):
        return dict(machine="desktop", execution_id=uuid.uuid4().hex, cwd=str(self.work),
            goal=goal, start_url="https://example.com/path?q=fixture#end",
            allowed_origins=["https://example.com"], **updates)

    def finished(self, arguments):
        started = self.app.start_codex_task(**arguments)
        self.wait(started)
        return self.app.read_codex_task("desktop", arguments["execution_id"])

    def report_path(self, identity):
        return self.state / "executions" / identity / "codex-result.json"

    def event(self, url="https://example.com/path?q=fixture#end", code=None):
        return {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "node_repl", "tool": "js",
            "status": "completed", "arguments": {"code": code or 'await tab.ax.write("both", { disableDiffing: true });'},
            "result": {"_meta": {"codex/browserUse": True, "browser_use": {"url": url},
                "codex/toolSurface": {"browserId": "b", "openTabIds": ["tab-1"]}},
                "content": [{"type": "image", "mimeType": "image/png", "data": "fixture"}]}}}

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

    def test_original_execution_report_restart_and_identical_retry_do_not_replay(self):
        args = self.arguments()
        report = self.finished(args)
        self.assertEqual(report["status"], "completed", report)
        self.assertEqual(report["native"]["snapshot"]["url"], args["start_url"])
        self.assertTrue(report["controller_review_required"])
        self.app = create_application(self.config, self.state)
        with patch.object(codex.sys, "executable", "/unavailable/new/python"):
            same = self.app.start_codex_task(**args)
        self.assertEqual(same["execution_id"], args["execution_id"])
        job = self.report_path(args["execution_id"]).parent
        self.assertEqual((job / "fixture-counter").read_text(), "1")
        self.assertFalse((job / "codex-last.json").exists())
        output = self.app.read_execution("desktop", args["execution_id"])["output"]
        self.assertNotIn("PRIVATE_RAW_EVENT_SENTINEL", output)
        self.assertNotIn("data:image", output)
        self.fault("execution_conflict", self.app.start_codex_task, **(args | {"goal": "different"}))

    def test_process_success_is_not_semantic_success_or_fabricated_evidence(self):
        for goal in ("blocked", "no-native", "turn-failed", "missing-final", "exit-failed"):
            with self.subTest(goal=goal):
                report = self.finished(self.arguments(goal))
                self.assertEqual(report["status"], "blocked" if goal == "blocked" else "failed", report)
        (self.home / "config.toml").unlink()
        report = self.finished(self.arguments())
        self.assertEqual((report["execution_status"], report["status"]), ("succeeded", "blocked"))
        self.assertEqual(report["native"]["calls"], 0)

    def test_original_result_tampering_or_unsafe_file_is_never_completion(self):
        args = self.arguments()
        self.finished(args)
        path = self.report_path(args["execution_id"])
        raw = path.read_bytes()
        original = json.loads(raw)
        for delta in ({"format": True}, {"request_sha256": "0" * 64}, {"native": {}},
                      {"report": {"status": "completed"}}):
            save_json(path, original | delta)
            self.assertEqual(self.app.read_codex_task("desktop", args["execution_id"])["status"], "outcome_unknown")
        path.write_bytes(raw)
        path.chmod(0o644)
        self.assertEqual(self.app.read_codex_task("desktop", args["execution_id"])["status"], "outcome_unknown")
        path.chmod(0o600)
        backup = path.with_suffix(".original")
        path.rename(backup)
        path.symlink_to(backup)
        self.assertEqual(self.app.read_codex_task("desktop", args["execution_id"])["status"], "outcome_unknown")

    def test_task_admission_claim_and_original_receipt_are_reused(self):
        self.accept_plan(tools=["start_codex_task", "read_codex_task", "start_execution", "control_task"])
        args = self.arguments()
        args["task_context"] = self.context_for(args["execution_id"])
        started = self.app.start_codex_task(**args)
        # This test observes admitted work; lock contention during admission is a separate contract.
        self.app.task_observer.start()
        self.addCleanup(self.app.task_observer.stop)
        self.wait(started)
        report = self.app.read_codex_task("desktop", args["execution_id"])
        self.assertEqual(report["status"], "completed")
        deadline = time.monotonic() + 5
        view = self.view()
        while view["record"]["handoff"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                view = self.view()
            except codex.Fault as error:
                if error.code != "resource_busy":
                    raise

        operation = view["record"]["operations"][0]
        self.assertEqual(operation["id"], args["execution_id"])
        self.assertEqual(operation["locator"]["kind"], "execution")
        self.assertIsNotNone(view["record"]["handoff"])
        self.control("pause", reason="Pause before a second browser operation")
        second = self.arguments(task_context=self.context_for(uuid.uuid4().hex))
        second["task_context"]["operation_id"] = second["execution_id"]
        with self.assertRaises(codex.Fault):
            self.app.start_codex_task(**second)
        self.assertFalse((self.state / "executions" / second["execution_id"]).exists())

    def test_delegation_never_implicitly_adds_a_native_execution_grant(self):
        self.request["contract"]["tools"] = ["start_codex_task"]
        self.fault("invalid_task_contract", self.create)
        disabled = self.config.model_copy(deep=True)
        disabled.machines["desktop"].execution_enabled = False
        app = create_application(disabled, self.state)
        self.fault("execution_disabled", app.start_codex_task, **self.arguments())
        self.assertFalse((self.state / "executions").exists())

    def test_native_snapshot_is_invalidated_by_later_reads_failures_or_foreign_tools(self):
        packet = codex.request({k: v for k, v in self.arguments().items() if k not in {"machine", "cwd"}})
        evidence = {"calls": 0, "origins": [], "snapshot": None}
        surface = {}
        codex.observe_event(self.event(), evidence, packet, surface)
        self.assertEqual(evidence["snapshot"]["tab_id"], "tab-1")
        late = self.event(code="await tab.url();")
        codex.observe_event(late, evidence, packet, surface)
        self.assertIsNone(evidence["snapshot"])
        codex.observe_event(self.event(), evidence, packet, surface)
        late["item"]["status"] = "failed"
        codex.observe_event(late, evidence, packet, surface)
        self.assertIsNone(evidence["snapshot"])
        non_browser = self.event(code="1 + 1")
        non_browser["item"]["result"]["_meta"].pop("codex/browserUse")
        with self.assertRaisesRegex(ValueError, "browser-use evidence"):
            codex.observe_event(non_browser, evidence, packet, surface)
        self.assertIsNone(evidence["snapshot"])
        for mutate in (lambda e: e["item"].update(server="another-server"),
                       lambda e: e["item"]["result"]["_meta"]["browser_use"].update(url="https://other.example/")):
            event = self.event()
            mutate(event)
            with self.assertRaises(ValueError):
                codex.observe_event(event, evidence, packet, surface)

    def test_timeout_cancel_and_shared_browser_lease_use_existing_execution(self):
        held = self.arguments("hold", timeout_seconds=8)
        started = self.app.start_codex_task(**held)
        try:
            # One OM browser session is serialized, not an extra browser queue.
            second = self.arguments()
            self.fault("resource_busy", self.app.start_codex_task, **second)
            self.assertFalse((self.state / "executions" / second["execution_id"]).exists())
            self.app.cancel_execution("desktop", held["execution_id"])
            self.wait(started)
            report = self.app.read_codex_task("desktop", held["execution_id"])
            self.assertEqual(report["status"], "cancelled")
        finally:
            self.app.cancel_execution("desktop", held["execution_id"])
        timed = self.finished(self.arguments("hold", timeout_seconds=1))
        self.assertEqual(timed["status"], "timed_out")

    def test_oversized_unicode_report_is_bounded_and_not_accepted(self):
        args = self.arguments("unicode")
        report = self.finished(args)
        self.assertEqual(report["status"], "failed")
        self.assertLessEqual(self.report_path(args["execution_id"]).stat().st_size, codex.MAX_RESULT)

    def test_native_location_and_tab_identity_are_bounded_before_publication(self):
        packet = codex.request({k: v for k, v in self.arguments().items() if k not in {"machine", "cwd"}})
        evidence = {"calls": 0, "origins": [], "snapshot": None}
        with self.assertRaises(ValueError):
            codex.observe_event(self.event("https://example.com/" + "x" * 2048), evidence, packet, {})
        event = self.event()
        event["item"]["result"]["_meta"]["codex/toolSurface"]["openTabIds"] = ["x" * 129]
        codex.observe_event(event, evidence, packet, {})
        self.assertIsNone(evidence["snapshot"])
        self.assertLess(len(json.dumps(evidence)), 1024)

    def test_delegation_home_is_empty_and_keeps_original_auth_reference(self):
        (self.home / "AGENTS.override.md").write_text("Unrelated override fixture.\n")
        auth = self.home / "auth.json"
        before, inode = auth.read_bytes(), auth.stat().st_ino
        binary, _, options = codex.discover()
        argv = codex.command(binary, self.root, options | {"project_doc_max_bytes": 99999})
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertIn('cli_auth_credentials_store="file"', argv)
        with codex.delegation_home(self.root) as name:
            home = Path(name)
            self.assertEqual({p.name for p in home.iterdir()}, {"auth.json"})
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertTrue((home / "auth.json").is_symlink())
            self.assertEqual((home / "auth.json").resolve(), auth.resolve())
            self.assertEqual(os.environ["CODEX_HOME"], str(self.home))
        self.assertFalse(home.exists())
        self.assertEqual((auth.read_bytes(), auth.stat().st_ino), (before, inode))
        auth.chmod(0o644)
        with self.assertRaises(ValueError):
            codex.delegation_home(self.root)
        auth.unlink()
        with self.assertRaises(FileNotFoundError):
            codex.delegation_home(self.root)
