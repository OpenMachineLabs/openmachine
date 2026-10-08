"""Explicit worktree task targets and original creation-operation admission."""

from contextlib import contextmanager
from copy import deepcopy
import subprocess
import unittest
from unittest.mock import patch
import uuid

from openmachine.foundation.sanitize import Fault
from test.support.tasks import TaskFixture, task_contract


class WorktreeAdmissionTests(TaskFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "Initial fixture")
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.config.workspaces["trees"] = self.config.workspaces["project"].model_copy(
            update={"root": str(self.trees)})
        self.linked = self.trees / "linked"
        self.git("worktree", "add", "-q", "-b", "fixture-linked", str(self.linked))
        self.context = self.app.read_workspace_file("desktop", "project", "AGENTS.md")["context"]
        self.linked_context = self.app.read_workspace_file(
            "desktop", "project", "AGENTS.md", worktree=str(self.linked))["context"]
        self.request = task_contract(self.context)

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                               "-C", str(self.work), *arguments], check=True,
                              capture_output=True, text=True).stdout.strip()

    def accept_both(self):
        target = dict(self.request["contract"]["targets"][0], worktree=str(self.linked),
                      workspace_identity=self.linked_context["workspace_identity"],
                      head_sha=self.linked_context["head_sha"])
        self.request["contract"]["targets"].append(target)
        return self.accept_plan()

    def create_arguments(self, identity=None, **values):
        identity = identity or uuid.uuid4().hex
        return dict(machine="desktop", workspace_name="project", destination=str(self.trees / "created"),
                    base_ref=self.context["head_sha"], new_branch="fixture-created", execution_id=identity,
                    expected_workspace_identity=self.context["workspace_identity"],
                    task_context=self.context_for(identity), **values)

    @contextmanager
    def creation_owner(self):
        """Synthetic domain receipts isolate admission; no worker is launched."""
        receipts = {}

        def read(machine, execution_id, **arguments):
            if execution_id not in receipts:
                raise Fault("execution_not_found", "No fixture receipt exists.")
            return receipts[execution_id]

        def create(**arguments):
            identity = arguments["execution_id"]
            result = {"execution_id": identity, "status": "succeeded", "exit_code": 0,
                      "cleanup": {"status": "verified"}, "created_at": 1.0}
            receipts[identity] = result
            return result

        with patch.object(self.app.machines, "read_execution", side_effect=read), \
                patch.object(self.app.machines, "create_worktree", side_effect=create) as dispatch:
            yield dispatch, receipts

    def test_selected_file_target_does_not_validate_another_tree_first(self):
        self.accept_both()
        self.git("checkout", "-q", "-b", "unrelated-source-branch")
        arguments = self.write_arguments()
        arguments.update(worktree=str(self.linked), expected_head_sha=self.linked_context["head_sha"],
                         expected_workspace_identity=self.linked_context["workspace_identity"])
        written = self.app.write_workspace_file(**arguments)
        self.assertEqual(written["status"], "applied")
        self.assertEqual((self.linked / "result.txt").read_text(), "original\n")
        self.assertFalse((self.work / "result.txt").exists())
        self.assertEqual(self.app.read_workspace_change("desktop", arguments["change_id"])["context"]["root"],
                         str(self.linked))

    def test_file_target_requires_its_explicit_selector(self):
        self.accept_both()
        arguments = self.write_arguments()
        arguments.update(expected_head_sha=self.linked_context["head_sha"],
                         expected_workspace_identity=self.linked_context["workspace_identity"])
        self.fault("context_mismatch", self.app.write_workspace_file, **arguments)
        self.assertEqual(self.view()["record"]["operations"], [])
        self.assertFalse((self.linked / "result.txt").exists())

    def test_command_cwd_selects_the_matching_accepted_tree(self):
        self.accept_both()
        self.git("checkout", "-q", "-b", "unrelated-source-branch")
        arguments = self.start_arguments(expected_head_sha=self.linked_context["head_sha"])
        arguments["cwd"] = str(self.linked)
        receipt = {"execution_id": arguments["execution_id"], "status": "succeeded", "exit_code": 0,
                   "cleanup": {"status": "verified"}, "created_at": 1.0}
        with patch.object(self.app.machines, "read_execution", side_effect=Fault("execution_not_found", "Fixture.")), \
                patch.object(self.app.machines, "start_execution", return_value=receipt) as dispatch:
            observed = self.app.start_execution(**arguments)
        self.assertEqual(observed["status"], "succeeded")
        self.assertEqual(dispatch.call_args.kwargs["cwd"], str(self.linked))
        self.assertTrue(dispatch.call_args.kwargs["task_admitted"])

    def test_empty_file_selector_retains_the_original_request_fingerprint(self):
        self.accept_plan()
        arguments = self.write_arguments()
        arguments["expected_head_sha"] = self.context["head_sha"]
        self.app.write_workspace_file(**arguments)
        original = deepcopy(self.view()["record"]["operations"][0])
        arguments["worktree"] = ""
        observed = self.app.write_workspace_file(**arguments)
        self.assertFalse(observed["task_operation_replayed"])
        self.assertEqual(self.view()["record"]["operations"], [original])
        self.assertEqual((self.work / "result.txt").read_text(), "original\n")

    def test_creation_keeps_source_identity_but_uses_its_explicit_base(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        original_targets = deepcopy(self.view()["record"]["contract"]["targets"])
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-q", "--allow-empty", "-m", "Unrelated source advancement")
        arguments = self.create_arguments()
        with self.creation_owner() as (dispatch, receipts):
            observed = self.app.create_worktree(**arguments)
            self.assertEqual(observed["status"], "succeeded")
            self.assertEqual(dispatch.call_args.kwargs["base_ref"], self.context["head_sha"])
            self.assertTrue(dispatch.call_args.kwargs["task_admitted"])
            self.assertEqual(dispatch.call_args.kwargs["task_id"], "structured-one")
            self.assertEqual(self.view()["record"]["contract"]["targets"], original_targets)

    def test_creation_requires_accepted_source_and_explicit_tool_authority(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            arguments = self.create_arguments()
            arguments["expected_workspace_identity"] = "a" * 64
            self.fault("context_mismatch", self.app.create_worktree, **arguments)
            arguments = self.create_arguments(worktree=str(self.linked))
            self.fault("task_scope_denied", self.app.create_worktree, **arguments)
            self.git("checkout", "-q", "-b", "changed-source-identity")
            self.fault("context_mismatch", self.app.create_worktree, **self.create_arguments())
            dispatch.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_start_execution_authority_does_not_implicitly_grant_creation(self):
        self.accept_plan()
        with self.creation_owner() as (dispatch, receipts):
            self.fault("task_scope_denied", self.app.create_worktree, **self.create_arguments())
            dispatch.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_creation_retry_observes_original_execution_without_new_scope_checks(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            arguments = self.create_arguments(worktree="")
            original = self.app.create_worktree(**arguments)
            self.config.machines["desktop"].execution_enabled = False
            retry = dict(arguments)
            retry.pop("worktree")
            observed = self.app.create_worktree(**retry)
            self.assertEqual(observed["execution_id"], original["execution_id"])
            self.assertFalse(observed["task_operation_replayed"])
            dispatch.assert_called_once()
            changed = dict(arguments, new_branch="different-intent")
            self.fault("task_operation_conflict", self.app.create_worktree, **changed)

    def test_bare_creation_retry_reaches_the_original_domain_verifier(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            arguments = self.create_arguments()
            self.app.create_worktree(**arguments)
            arguments.pop("task_context")
            dispatch.side_effect = Fault("execution_conflict", "The original task binding differs.")
            self.fault("execution_conflict", self.app.create_worktree, **arguments)
            self.assertEqual(dispatch.call_count, 2)
            self.assertTrue(dispatch.call_args.kwargs["task_admitted"])
            self.assertEqual(dispatch.call_args.kwargs["task_id"], "")
            self.assertEqual(len(self.view()["record"]["operations"]), 1)

    def test_existing_unbound_creation_receipt_cannot_be_adopted(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            arguments = self.create_arguments()
            receipts[arguments["execution_id"]] = {"execution_id": arguments["execution_id"], "status": "running"}
            self.fault("task_operation_conflict", self.app.create_worktree, **arguments)
            dispatch.assert_not_called()
        self.assertEqual(self.view()["record"]["operations"], [])

    def test_creation_pre_effect_failure_retains_failed_original_intent(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            dispatch.side_effect = Fault("invalid_worktree_base", "Fixture rejected before dispatch.",
                                         effect="known_no_effect")
            arguments = self.create_arguments()
            self.fault("invalid_worktree_base", self.app.create_worktree, **arguments)
            operation = self.view()["record"]["operations"][0]
        self.assertEqual(operation["locator"]["execution_id"], arguments["execution_id"])
        self.assertEqual(operation["result"]["status"], "failed")
        self.assertEqual(operation["result"]["effect"], "known_no_effect")
        self.assertEqual(operation["result"]["failure_code"], "invalid_worktree_base")

    def test_unknown_creation_retains_original_id_and_blocks_replacement(self):
        self.accept_plan(tools=["read_task", "control_task", "create_worktree"])
        with self.creation_owner() as (dispatch, receipts):
            dispatch.side_effect = Fault("outcome_unknown", "Fixture reply was lost.")
            arguments = self.create_arguments()
            self.fault("outcome_unknown", self.app.create_worktree, **arguments)
            original = deepcopy(self.view()["record"]["operations"][0])
            self.fault("execution_not_found", self.app.create_worktree, **arguments)
            self.fault("task_effect_unresolved", self.app.create_worktree, **self.create_arguments())
            dispatch.assert_called_once()
            self.assertEqual(self.view()["record"]["operations"], [original])
        self.assertEqual(original["result"]["status"], "unknown")
