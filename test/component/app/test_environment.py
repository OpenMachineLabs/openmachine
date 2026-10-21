"""Scoped context behavior with real disposable Git and Task owners."""

from copy import deepcopy
import json
import subprocess
import unittest
from unittest.mock import patch

from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from test.support.tasks import TaskFixture


class ContextTests(TaskFixture, unittest.TestCase):
    def git(self, *argv):
        return subprocess.check_output(["git", "-C", str(self.work), *argv], stderr=subprocess.PIPE, text=True).strip()

    def repository(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.work / "AGENTS.md").write_text("Fixture instructions must be read explicitly.\n")
        self.git("add", "AGENTS.md")
        self.git("commit", "-m", "fixture: establish baseline")

    def observe(self, **kwargs):
        return self.app.inspect_context("desktop", "project", **kwargs)

    def test_default_read_does_not_initialize_state_or_load_instructions(self):
        (self.work / "AGENTS.md").write_text("DO NOT IMPLICITLY LOAD THIS CONTENT\n")
        result = self.observe()
        self.assertFalse(self.state.exists())
        self.assertFalse(result["atomic"])
        self.assertFalse(result["target"]["instructions_loaded"])
        self.assertNotIn("DO NOT IMPLICITLY", json.dumps(result))
        self.assertEqual(set(result["sections"]), {"workspace", "activity", "runtime"})
        self.assertEqual(result["sections"]["workspace"]["status"], "not_applicable")
        self.assertEqual(result["sections"]["activity"]["status"], "not_selected")
        self.assertFalse(result["sections"]["activity"]["complete"])
        runtime = result["sections"]["runtime"]
        self.assertEqual(runtime["kind"], "configuration_not_health")
        self.assertEqual(runtime["data"]["selected_machine"]["probe_status"], "not_probed")
        self.assertLessEqual(result["started_at_ms"], result["finished_at_ms"])

    def test_git_pages_keep_original_snapshots_and_selected_worktree(self):
        self.repository()
        for name in ("one.txt", "two.txt"):
            (self.work / name).write_text(name)
        result = self.observe(sections=["workspace", "worktrees"], limit=1)
        status = result["sections"]["workspace"]
        self.assertFalse(status["complete"])
        self.assertTrue(status["data"]["has_more"])
        continuation = self.app.inspect_workspace(**status["next_ref"]["arguments"])
        self.assertEqual(continuation["snapshot"], status["snapshot"])
        self.assertNotEqual(continuation["entries"], status["data"]["entries"])
        self.assertTrue(result["sections"]["worktrees"]["complete"])
        tree = self.root / "other"
        self.git("worktree", "add", "-b", "other", str(tree))
        self.config.workspaces["other"] = type(self.config.workspaces["project"])(machine="desktop", root=str(tree))
        selected = self.observe(worktree=str(tree), sections=["workspace", "worktrees"])
        grants = {item["worktree"]: item["authorized"] for item in selected["sections"]["worktrees"]["data"]["entries"]}
        self.assertTrue(grants[str(self.work)])
        self.assertTrue(grants[str(tree)])
        self.assertEqual(selected["target"]["branch"], "other")
        self.assertEqual(selected["target"]["cwd"], str(tree))
        self.assertEqual(selected["sections"]["workspace"]["data"]["entries"], [])
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_noncanonical_selector_matches_the_observed_task_target(self):
        self.repository()
        tree = self.root / "other"
        self.git("worktree", "add", "-b", "other", str(tree))
        self.config.workspaces["other"] = type(self.config.workspaces["project"])(machine="desktop", root=str(tree))
        context = self.app.read_workspace_file("desktop", "project", "AGENTS.md", worktree=str(tree))["context"]
        request = deepcopy(self.request)
        request["contract"]["targets"] = [{"machine": "desktop", "workspace": "project", "worktree": str(tree),
            "workspace_identity": context["workspace_identity"], "head_sha": context["head_sha"]}]
        self.create(request)
        raw_selector = str(tree.parent) + "//other"
        result = self.observe(worktree=raw_selector, task_id="structured-one", sections=["activity"])
        self.assertEqual(result["target"]["requested_worktree"], raw_selector)
        self.assertEqual(result["target"]["worktree"], str(tree))
        self.assertTrue(result["sections"]["activity"]["data"]["target_binding"]["matches"])
        self.assertEqual(self.config.workspaces["project"].root, str(self.work))

    def test_git_failure_and_skipped_coverage_are_not_cleanliness(self):
        self.repository()
        original = self.app.inspect_workspace
        with patch.object(self.app, "inspect_workspace", side_effect=Fault("git_failed", "Fixture Git failure.")):
            result = self.observe(sections=["workspace", "runtime"])
        self.assertEqual(result["sections"]["workspace"]["error"]["code"], "git_failed")
        self.assertFalse(result["sections"]["workspace"]["complete"])
        self.assertEqual(result["sections"]["runtime"]["status"], "observed")
        page = original("desktop", "project", "status")
        with patch.object(self.app, "inspect_workspace", return_value=page | {"skipped_paths": 1}):
            self.assertFalse(self.observe(sections=["workspace"])["sections"]["workspace"]["complete"])

    def test_target_drift_and_inconsistent_intermediate_page_reject_composition(self):
        self.repository()
        original = self.app.read_workspace_file
        count = 0
        def switching(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.git("switch", "-c", "moved")
            return original(*args, **kwargs)
        with patch.object(self.app, "read_workspace_file", side_effect=switching):
            self.fault("source_changed", self.observe, sections=["runtime"])
        page = self.app.inspect_workspace("desktop", "project", "status")
        page["context"]["branch"] = "different-intermediate-branch"
        with patch.object(self.app, "inspect_workspace", return_value=page):
            self.fault("source_changed", self.observe, sections=["workspace"])

    def test_explicit_task_keeps_unknown_original_evidence_and_target_binding(self):
        self.create()
        def seed(record, now):
            record["operations"] = [{"id": f"{index:032x}", "stage_id": "verify", "plan_revision": 1, "epoch": 0,
                "request_sha256": "b" * 64, "source_sha256": record["plan"]["stages"][0]["source_sha256"],
                "locator": {"kind": "execution", "machine": "desktop", "target_identity": None,
                    "execution_id": f"{index:032x}", "control_seq": None},
                "admitted_at_ms": now, "state": "observed", "observed_at_ms": now,
                "result": {"status": "unknown", "receipt_sha256": None,
                           "summary": "Synthetic retained uncertainty, not execution proof.", "log_offset": None}}
                for index in (1, 2)]
        tasks.update(self.state, "structured-one", seed)
        before = self.task_bytes()
        original_stamp = self.app.read_task("structured-one", section="operations", limit=1)["items"][0]["observed_at_ms"]
        result = self.observe(task_id="structured-one", sections=["activity"], limit=1)
        activity = result["sections"]["activity"]
        self.assertFalse(activity["complete"])
        self.assertEqual(activity["kind"], "retained_evidence")
        self.assertTrue(activity["data"]["target_binding"]["matches"])
        self.assertEqual(activity["data"]["counts"]["unknown_count"], 2)
        self.assertEqual(activity["data"]["operations"][0]["observed_at_ms"], original_stamp)
        self.assertEqual(activity["data"]["operations"][0]["result"]["status"], "unknown")
        self.assertTrue(self.app.read_task(**activity["next_ref"]["arguments"])["items"])
        self.assertEqual(self.task_bytes(), before)
        self.repository()
        changed = self.observe(task_id="structured-one", sections=["activity"])
        self.assertFalse(changed["sections"]["activity"]["data"]["target_binding"]["matches"])
        self.assertEqual(self.task_bytes(), before)

    def test_stage_dependencies_preserve_acceptance_and_original_evidence(self):
        stage = self.request["stages"][0]
        self.request["stages"] += [
            dict(stage, id="integration", depends_on=["verify"],
                 result="Consumer integration", verification="Original consumer acceptance"),
            dict(stage, id="independent", depends_on=[], result="Independent source check")]
        self.accept_plan()
        arguments = self.write_arguments()
        self.app.write_workspace_file(**arguments)
        self.control("record_decision", stage_id="verify", outcome="accepted",
                     evidence=[arguments["change_id"]], reason="Verified fixture file.")
        self.control("record_decision", stage_id="integration", outcome="blocked",
                     evidence=[], reason="Fixture consumer acceptance is unavailable.")
        original = self.app.read_task("structured-one")["record"]
        before = self.task_bytes()
        activity = self.observe(task_id="structured-one", sections=["activity"], limit=1)["sections"]["activity"]
        data = activity["data"]
        self.assertEqual(activity["revision"], original["revision"])
        self.assertEqual(data["plan_revision"], original["plan"]["revision"])
        self.assertEqual([s["id"] for s in data["stages"]], ["verify", "integration", "independent"])
        self.assertEqual([s["state"] for s in data["stages"]], ["accepted", "blocked", "pending"])
        self.assertEqual([s["depends_on"] for s in data["stages"]], [[], ["verify"], []])
        self.assertEqual(data["stages"][1]["verification"], "Original consumer acceptance")
        self.assertEqual(data["stages"][1]["result"], "Consumer integration")
        self.assertEqual(data["control"], original["control"])
        self.assertEqual(data["stages"][0]["evidence_ref"], original["plan"]["stages"][0]["evidence_ref"])
        evidence = self.app.read_task(**data["stages"][0]["evidence_ref"])
        self.assertIn(arguments["change_id"], json.dumps(evidence))
        self.assertTrue(all("evidence" not in s for s in data["stages"]))
        self.assertEqual(self.task_bytes(), before)
        self.assertNotIn("runnable", data)

    def test_stage_summary_is_bounded_by_the_plan_not_the_operation_limit(self):
        stage = self.request["stages"][0]
        self.request["stages"] = [dict(stage, id=f"stage-{i}", depends_on=[]) for i in range(16)]
        self.create()
        before = self.task_bytes()
        data = self.observe(task_id="structured-one", sections=["activity"], limit=1)["sections"]["activity"]["data"]
        self.assertEqual(len(data["stages"]), 16)
        self.assertEqual(data["stages"][-1]["id"], "stage-15")
        self.assertEqual(data["operations"], [])
        self.assertLess(len(json.dumps(data, ensure_ascii=True).encode()), 65536)
        self.assertEqual(self.task_bytes(), before)

    def test_missing_task_and_revision_drift_preserve_other_sections(self):
        missing = self.observe(task_id="not-created")
        self.assertEqual(missing["sections"]["activity"]["status"], "unavailable")
        self.assertFalse(self.state.exists())
        self.create()
        original = self.app.read_task
        calls = 0
        def drift(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 3:
                result["record"]["revision"] += 1
            return result
        with patch.object(self.app, "read_task", side_effect=drift):
            result = self.observe(task_id="structured-one")
        self.assertEqual(result["sections"]["activity"]["error"]["code"], "source_changed")
        self.assertEqual(result["sections"]["activity"]["data"], {})
        self.assertEqual(result["sections"]["runtime"]["status"], "observed")

    def test_input_scope_and_omission_do_not_read_unselected_sources(self):
        with patch.object(self.app, "read_workspace_file", side_effect=AssertionError("Unexpected target IO")):
            for kwargs in ({"limit": True}, {"limit": 0}, {"sections": []}, {"sections": "runtime"},
                           {"sections": ["runtime", "runtime"]}, {"sections": ["hardware"]}, {"task_id": "bad/id"}):
                with self.subTest(kwargs=kwargs):
                    self.fault("invalid_context_request", self.observe, **kwargs)
        with patch.object(self.app, "read_task", side_effect=AssertionError("Unexpected task IO")), \
             patch.object(self.app, "inspect_workspace", side_effect=AssertionError("Unexpected Git IO")):
            self.observe(sections=["runtime"], task_id="not-read")
            self.fault("unknown_workspace", self.app.inspect_context, "desktop", "missing")
            self.fault("path_denied", self.observe, worktree="../outside", sections=["activity"], task_id="not-read")

    def test_json_budget_counts_escaped_content_without_truncating_evidence(self):
        caps = self.app.get_capabilities()
        caps["boundary"] = "\U0001f600" * 6000
        with patch.object(self.app, "get_capabilities", return_value=caps):
            self.fault("context_response_limit", self.observe, sections=["runtime"])

    def test_ssh_target_uses_existing_routing_and_never_local_measurements(self):
        context = deepcopy(self.context)
        context.update(cwd="/remote/project", worktree="/remote/project")
        config = Settings.model_validate({"schema_version": 1,
            "machines": {"remote": {"transport": "ssh", "ssh_alias": "fixture",
                "ssh_worker": "/worker/" + "a" * 64 + ".pyz", "ssh_identity": "b" * 64}},
            "workspaces": {"project": {"machine": "remote", "root": "/remote/project"}}})
        app = create_application(config, self.state)
        with patch.object(app.machines, "ssh_call", return_value={"context": context}) as routed:
            result = app.inspect_context("remote", "project", sections=["runtime"])
        self.assertEqual(routed.call_count, 2)
        self.assertTrue(all(call.args[1:3] == ("remote", "read_workspace_file") for call in routed.call_args_list))
        self.assertEqual(result["target"]["cwd"], "/remote/project")
        self.assertEqual(result["target"]["transport"], "ssh")
        self.assertNotIn("os", result["sections"]["runtime"]["data"])
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
