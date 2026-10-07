"""Tests for app behavior at the component layer."""

import json
import threading
import unittest
from unittest.mock import patch
from integrations.ssh import client
from openmachine import tasks
from openmachine.bootstrap import create_application
from openmachine.foundation.sanitize import Fault
from test.support.ssh_cases import SSHCaseFixture


class SSHTests(SSHCaseFixture, unittest.TestCase):
    def test_controller_checkpoint_is_required_only_for_a_new_execution(self):
        app = create_application(self.settings, self.root / "controller")
        arguments = {"execution_id": "c" * 32, "cwd": str(self.project), "argv": ["true"], "task_id": "original-task"}
        missing = (0, b'{"error":{"code":"execution_not_found","message":"No original receipt."}}')
        with patch.object(client, "_exchange", return_value=missing) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "tasks_uninitialized")
            self.assertEqual(exchange.call_count, 1)
        app.save_task("original-task", 0, "Task", "Authorized disposable task.")
        with patch.object(client, "_exchange", side_effect=[missing, self.response({"status": "accepted"})]) as exchange:
            self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "accepted")
            self.assertEqual(exchange.call_count, 2)
        conflict = (0, b'{"error":{"code":"execution_conflict","message":"Changed original request."}}')
        for final in (self.response({"status": "succeeded"}), conflict):
            with self.subTest(final=final), \
                    patch.object(client, "_exchange", side_effect=[self.response({"status": "succeeded"}), final]) as exchange, \
                    patch.object(tasks, "read", side_effect=AssertionError("Old receipts do not need a checkpoint")):
                if final == conflict:
                    with self.assertRaises(Fault) as caught:
                        app.start_execution("build-machine", **(arguments | {"argv": ["false"]}))
                    self.assertEqual(caught.exception.code, "execution_conflict")
                else:
                    self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "succeeded")
                operations = [json.loads(call.args[1])["operation"] for call in exchange.call_args_list]
                self.assertEqual(operations, ["read_execution", "start_execution"])
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "ssh_unavailable")
            self.assertEqual(exchange.call_count, 1)
        with patch.object(client, "_exchange", side_effect=AssertionError("Invalid requests never reach SSH")):
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **(arguments | {"task_id": "invalid/task"}))
            self.assertEqual(caught.exception.code, "invalid_task_id")

    def test_structured_task_is_not_a_new_remote_execution_grant(self):
        from test.support.tasks import task_contract
        state = self.root / "controller"
        app = create_application(self.settings, state)
        request = task_contract()
        tasks.create(state, request.pop("task_id"), **request)
        arguments = {"execution_id": "d" * 32, "cwd": str(self.project), "argv": ["true"], "task_id": "structured-one"}
        missing = (0, b'{"error":{"code":"execution_not_found","message":"No original receipt."}}')
        with patch.object(client, "_exchange", return_value=missing) as exchange:
            with self.assertRaises(Fault) as caught:
                app.start_execution("build-machine", **arguments)
            self.assertEqual(caught.exception.code, "supervised_admission_unavailable")
            self.assertEqual([json.loads(call.args[1])["operation"] for call in exchange.call_args_list], ["read_execution"])
        (state / "tasks/structured-one/task.json").write_text("Uncertain original task")
        with patch.object(client, "_exchange", side_effect=[self.response({"status": "succeeded"}),
                self.response({"status": "succeeded"})]) as exchange:
            self.assertEqual(app.start_execution("build-machine", **arguments)["status"], "succeeded")
            self.assertEqual([json.loads(call.args[1])["operation"] for call in exchange.call_args_list],
                             ["read_execution", "start_execution"])

    def test_application_routes_remote_files_without_resolving_controller_paths(self):
        app = create_application(self.settings, self.root / "controller")
        self.settings.workspaces["project"].root = "/remote/not-on-controller"
        operations = [
            (app.apply_workspace_patch, ("build-machine", "project", "patch", "a" * 64, "", [], False, "c" * 32), "apply_workspace_patch"),
            (app.inspect_workspace, ("build-machine", "project", "files"), "inspect_workspace"),
            (app.read_workspace_file, ("build-machine", "project", "file.txt"), "read_workspace_file"),
            (app.write_workspace_file, ("build-machine", "project", "file.txt", "new", "", ""), "write_workspace_file"),
            (app.read_execution, ("build-machine", "c" * 32), "read_execution"),
            (app.list_executions, ("build-machine",), "list_executions"),
            (app.cancel_execution, ("build-machine", "c" * 32), "cancel_execution"),
        ]
        for operation, arguments, name in operations:
            with self.subTest(operation=name), patch.object(client, "_exchange", return_value=self.response({})) as exchange:
                operation(*arguments)
                request = json.loads(exchange.call_args.args[1])
                self.assertEqual(request["operation"], name)
                self.assertEqual(request["workspaces"], {"project": "/remote/not-on-controller"})
        self.settings.machines["build-machine"].execution_enabled = False
        with patch.object(client, "_exchange", return_value=self.response({"status": "succeeded"})):
            self.assertEqual(app.cancel_execution("build-machine", "c" * 32)["status"], "succeeded")
        self.assertFalse((app.state / "executions").exists())

    def test_wait_routes_once_and_observer_event_never_enters_wire(self):
        app = create_application(self.settings, self.root / "controller")
        cancelled = threading.Event()
        with patch.object(client, "_exchange", return_value=self.response({"status": "running", "wait_expired": True})) as exchange:
            result = app.read_execution("build-machine", "c" * 32, 7, 100, 0.25, cancelled=cancelled)
        self.assertTrue(result["wait_expired"])
        self.assertEqual(exchange.call_count, 1)
        self.assertIs(exchange.call_args.kwargs["cancelled"], cancelled)
        request = json.loads(exchange.call_args.args[1])
        self.assertEqual(request["arguments"], {"execution_id": "c" * 32, "offset": 7, "max_bytes": 100, "wait_seconds": 0.25})
        with patch.object(client, "_exchange", side_effect=AssertionError("Invalid waits must not contact the target")):
            with self.assertRaises(Fault) as caught:
                app.read_execution("build-machine", "c" * 32, wait_seconds=True)
        self.assertEqual(caught.exception.code, "invalid_wait")
        cancelled.set()
        with patch.object(client, "_exchange", side_effect=AssertionError("Cancelled reads must not contact the target")):
            with self.assertRaises(Fault) as caught:
                app.read_execution("build-machine", "c" * 32, wait_seconds=1, cancelled=cancelled)
        self.assertEqual(caught.exception.code, "observation_cancelled")
        with self.assertRaises(Fault) as caught:
            self.dispatch("read_execution", execution_id="c" * 32, cancelled=True)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_control_routes_once_and_never_replays_an_unknown_ssh_effect(self):
        app = create_application(self.settings, self.root / "controller")
        with patch.object(client, "_exchange", return_value=self.response({"status": "running"})) as exchange:
            app.control_execution("build-machine", "c" * 32, 0, "write_stdin", "literal\n")
        self.assertEqual(exchange.call_count, 1)
        frame = json.loads(exchange.call_args.args[1])
        self.assertEqual(frame["operation"], "control_execution")
        self.assertEqual(frame["arguments"], {"execution_id": "c" * 32, "expected_control_seq": 0,
                                              "action": "write_stdin", "text": "literal\n", "terminal_size": None})
        with patch.object(client, "_exchange", side_effect=TimeoutError) as exchange:
            with self.assertRaises(Fault) as caught:
                app.control_execution("build-machine", "c" * 32, 0, "interrupt")
            self.assertEqual(caught.exception.code, "outcome_unknown")
            self.assertEqual(exchange.call_count, 1)
        self.settings.machines["build-machine"].execution_enabled = False
        with patch.object(client, "_exchange", return_value=self.response({})) as exchange:
            with self.assertRaises(Fault) as caught:
                app.control_execution("build-machine", "c" * 32, 0, "write_stdin", "denied")
            self.assertEqual(caught.exception.code, "execution_disabled")
            exchange.assert_not_called()
            app.control_execution("build-machine", "c" * 32, 0, "interrupt")
            self.assertEqual(exchange.call_count, 1)
        with self.assertRaises(Fault) as caught:
            self.dispatch("control_execution", execution_id="c" * 32, expected_control_seq=0,
                          action="interrupt", unapproved=True)
        self.assertEqual(caught.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
