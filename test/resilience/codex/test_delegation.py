"""Tests for codex behavior at the resilience layer."""

import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import uuid
from integrations import codex
from openmachine.bootstrap import create_application
from openmachine.foundation.state import save_json
from test.support.codex_cases import CodexCaseFixture


class CodexTests(CodexCaseFixture, unittest.TestCase):
    def test_only_configured_bridge_environment_reaches_the_child(self):
        forwarded = {"BROWSER_USE_ACCESS_EXECUTOR_ID": "fixture-executor",
                     "BROWSER_USE_ACCESS_PIPE_PATH": "fixture-pipe",
                     "BROWSER_USE_ACCESS_VERIFYING_IDENTITY": "fixture-identity"}
        (self.home / "config.toml").write_text(self.config_text.replace(
            "args=[]", "args=[]\nenv_vars=" + json.dumps(list(forwarded))))
        args = self.arguments("forwarded-environment")
        with patch.dict(os.environ, forwarded | {"UNAPPROVED_SECRET": "never-inherit",
                                                 "NODE_REPL_NODE_PATH": "not-configured-for-forwarding"}):
            report = self.finished(args)
        self.assertEqual(report["status"], "completed", report)
        job = self.report_path(args["execution_id"]).parent
        output = self.app.read_execution("desktop", args["execution_id"])["output"]
        for value in forwarded.values():
            self.assertNotIn(value, output)
            self.assertNotIn(value, self.report_path(args["execution_id"]).read_text())
            self.assertNotIn(value, (job / "record.json").read_text())

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


if __name__ == "__main__":
    unittest.main()
