"""Application admission with one selected directory and original guards."""

import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from openmachine import app as application, tasks
from openmachine.bootstrap import create_application
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory, save_json
from openmachine.service import state as service_state


class ApplicationAdmissionFixture(object):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="om-admission-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = private_directory(self.root / "state")
        private_directory(self.state / "locks")
        private_directory(self.state / "service")
        self.config = self.root / "config.toml"
        self.a, self.b = "a" * 64, "b" * 64
        private_directory(self.state / "service/versions" / self.a)
        self.record = {"format": 1, "config": str(self.config),
                       "probe": {"workspace": "project", "path": "AGENTS.md"},
                       "current": self.a, "previous": None, "pending": None,
                       "launch_python": str(Path(sys._base_executable).resolve()),
                       "path_environment": os.defpath}
        self.save(self.record)

    def save(self, record):
        save_json(self.state / "service/service.json", record)

    def assert_fault(self, code, operation):
        with self.assertRaises(Fault) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def assert_admission_dispatch(self, names):
        import inspect
        config = Settings.model_validate({"schema_version": 1,
            "machines": {"local": {"transport": "local", "execution_enabled": True}},
            "github": {"repositories": ["example/project"]}})
        app = create_application(config, self.state)
        issue = ("update", "example/project", "Title", "Body", 1, "closed", "observed-time")
        pull = ("update", "example/project", "Title", "Body", 1, "topic", "main", False,
                "closed", "observed-time", "f" * 40)
        cases = [
            ("apply_workspace_patch", ("local", "project", "patch", "a" * 64, "", [], False, "c" * 32, False, ""),
             app.machines, "apply_workspace_patch"),
            ("edit_workspace_file", ("local", "project", "file", [{"old_text": "a", "new_text": "b"}],
                                     "a" * 64, "", "b" * 64, "c" * 32, False, ""), app.machines, "edit_workspace_file"),
            ("write_workspace_file", ("local", "project", "file", "text", "", "", "", ""), app.machines, "write_workspace_file"),
            ("start_execution", ("local", "c" * 32, str(self.root), ["true"], 600, "", "exclusive", None, "", None, False, None),
             app.machines, "start_execution"),
            ("cancel_execution", ("local", "c" * 32), app.machines, "cancel_execution"),
            ("reconcile_execution", ("local", "c" * 32), application.execution, "reconcile"),
            ("control_execution", ("local", "c" * 32, 0, "interrupt", None, None), app.machines, "control_execution"),
            ("save_task", ("task", 0, "Title", "Body", []), tasks, "save"),
            ("github_issue", issue, app.github, "issue"),
            ("github_pull_request", pull, app.github, "pull_request"),
            ("github_comment", ("create", "example/project", "Body", 1, 0, ""), app.github, "comment"),
            ("github_review", ("create", "example/project", "Body", 1, 0, "f" * 40, "COMMENT"), app.github, "review"),
            ("github_review_thread", ("resolve", "example/project", 1, "PRRT_fixture", "f" * 40, "e" * 40), app.github, "review_thread"),
            ("github_merge", ("example/project", 1, "f" * 40, "e" * 40, "squash", "Title", "Body"), app.github, "merge"),
        ]
        cases = [case for case in cases if case[0] in names]
        for name, arguments, owner, method in cases:
            operation = getattr(app, name)
            signature = inspect.signature(getattr(owner, method))
            with self.subTest(operation=name), patch.object(owner, method) as dispatch, patch.object(app.connections, "request") as request:
                self.record["pending"] = {"from": self.a, "to": self.b, "previous": None}
                self.save(self.record)
                self.assert_fault("activation_pending", lambda: operation(*arguments))
                self.record["pending"] = None
                self.save(self.record)
                self.assert_fault("service_identity_mismatch", lambda: operation(*arguments))
                with lease(self.state / "locks/lifecycle.lock"):
                    self.assert_fault("resource_busy", lambda: operation(*arguments))
                    self.assertEqual(app.find_tasks()["status"], "uninitialized")
                    self.assertEqual(app.get_capabilities()["lifecycle"]["state_contract"], 20)
                dispatch.assert_not_called()

                def admitted(*args, **kwargs):
                    with self.assertRaises(Fault) as caught:
                        with lease(self.state / "locks/lifecycle.lock"):
                            self.fail("Admission lease was released before dispatch")
                    self.assertEqual(caught.exception.code, "resource_busy")
                    return {"dispatched": True}

                dispatch.side_effect = admitted
                with patch.object(sys, "prefix", str(service_state._runtime(self.state, self.a))), \
                        patch.object(application, "lease", wraps=lease) as admission:
                    response = {"dispatched": True}
                    if name in {"start_execution", "reconcile_execution"}:
                        response["task_feedback"] = {"status": "unbound"}
                    self.assertEqual(operation(*arguments), response)
                    admission.assert_called_once_with(self.state / "locks/lifecycle.lock", shared=True)
                prefix = ((config, self.state) if name == "reconcile_execution" else (config,) if name.startswith("github_")
                          else (self.state,) if name == "save_task" else ())
                self.assertEqual(dispatch.call_count, 1)
                actual = signature.bind(*dispatch.call_args.args, **dispatch.call_args.kwargs)
                injected = {"transport": request.return_value} if name.startswith("github_") else {}
                expected = signature.bind(*prefix, *arguments, **injected)
                actual.apply_defaults()
                expected.apply_defaults()
                if name.startswith("github_"):
                    self.assertIs(actual.arguments.pop("transport"), request.return_value)
                    expected.arguments.pop("transport")
                    request.assert_called_once_with("example/project", connection_id="", binding=None, write=True, historical_unbound=False)
                else:
                    request.assert_not_called()
                self.assertEqual(actual.arguments, expected.arguments)
                with lease(self.state / "locks/lifecycle.lock"):
                    pass
