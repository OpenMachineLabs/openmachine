"""Shared native Task/GitHub recovery fixture; never a live provider write."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import uuid
from integrations import github
from openmachine import tasks
from openmachine.bootstrap import create_application
from test.support.tasks import TaskFixture
from test.support.github_cases import BASE, HEAD, REPO, GitHubCaseFixture, settings


class GoalRecoveryFixture(TaskFixture):
    """Actual Task/GitHub owners with a disposable lossy provider transport."""

    def setUp(self):
        super().setUp()
        self.provider = GitHubCaseFixture()
        self.provider.setUp()
        self.provider.metadata["permissions"] = {"push": True}
        self.provider.threads = [{"id": "PRRT_goal", "isResolved": False,
                                  "isOutdated": False, "path": "a.py", "line": 1}]
        self.thread_writes = 0
        self.write_applies = True
        self.transport = Mock(side_effect=self.response)
        self.config = self.config.model_copy(update={"github": settings().github})
        self.connect()
        self.request["contract"]["repositories"] = [REPO]
        self.accept_plan(tools=["github_review_thread", "github_merge", "github_pull_request",
                                "write_workspace_file", "read_task", "control_task"])

    def connect(self):
        self.app = create_application(self.config, self.state)
        self.app.github = github
        binding = {"connection_id": "d" * 32, "account_id": 1, "authorization_revision": 1}
        self.app.connections.select = Mock(return_value=SimpleNamespace(binding=lambda: binding))
        self.app.connections.request = Mock(return_value=self.transport)

    def response(self, method, endpoint, payload=None):
        if method == "POST" and endpoint == "graphql" and payload["query"].startswith("mutation"):
            thread = next(item for item in self.provider.threads if item["id"] == payload["variables"]["threadId"])
            self.thread_writes += 1
            if self.write_applies:
                thread["isResolved"] = "unresolveReviewThread" not in payload["query"]
            raise OSError("Synthetic lost mutation acknowledgement")
        return self.provider.response(method, endpoint, payload)

    def lose_ack(self, *, action="resolve", thread_id="PRRT_goal"):
        identity = uuid.uuid4().hex
        arguments = dict(action=action, repository=REPO, number=2, thread_id=thread_id,
                         expected_head_sha=HEAD, expected_base_sha=BASE,
                         task_context=self.context_for(identity))
        self.assertEqual(self.app.github_review_thread(**arguments)["status"], "write_outcome_unknown")
        return arguments, deepcopy(tasks.operation(self.state, "structured-one", identity))

    def recover(self, arguments):
        return self.app.github_review_thread(**dict(arguments,
            task_context=self.context_for(arguments["task_context"]["operation_id"])))

    def merge(self, **values):
        arguments = dict(repository=REPO, number=2, expected_head_sha=HEAD,
                         expected_base_sha=BASE, task_context=self.context_for(uuid.uuid4().hex))
        return self.app.github_merge(**(arguments | values))

    def accept_goal(self, operation):
        return self.control("record_decision", stage_id="verify", outcome="accepted", evidence=[operation["id"]],
                            reason="Fresh goal evidence proves the requested state, not historical write attribution.")
