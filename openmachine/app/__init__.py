"""Typed application operations; every mutation shares one lifecycle admission."""

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
from types import ModuleType
from typing import Literal

from openmachine import __version__, execution, service, skills, tasks, workspace
from openmachine.config import Settings
from openmachine.execution import DEFAULT_TIMEOUT, MAX_TIMEOUT, SEGMENT_BYTES
from openmachine.foundation.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, TEXT_PAGE
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

from .machines import Machines


class Application:
    def __init__(self, config: Settings, state: Path, machines: Machines, github: ModuleType | None):
        self.config = config
        self.state = state
        self.machines = machines
        self.github = github

    @contextmanager
    def _admit(self) -> Iterator[None]:
        private_directory(self.state / "locks")
        with lease(self.state / "locks/lifecycle.lock", shared=True):
            service.guard_writes(self.state)
            yield

    def _provider(self) -> ModuleType:
        if self.github is None:
            raise Fault("github_not_allowed", "GitHub is not enabled in this Open Machine configuration.")
        return self.github

    def probe_machine(self, machine: str) -> dict:
        return self.machines.probe(machine)

    def get_capabilities(self) -> dict:
        has_ssh = any(item.transport == "ssh" for item in self.config.machines.values())
        return {
            "version": __version__, "configuration_sha256": hashlib.sha256(self.config.model_dump_json().encode()).hexdigest(),
            "machines": {name: {**item.model_dump(), "probe_status": "not_probed"} for name, item in self.config.machines.items()},
            "workspaces": {name: item.model_dump() for name, item in self.config.workspaces.items()},
            "skills": ["openmachine"], "skill_roots": self.config.skills.roots, "task_continuity": True,
            "integrations": (["github"] if self.config.github is not None else []) + (["ssh"] if has_ssh else []),
            "github": None if self.config.github is None else {**self.config.github.model_dump(), "backend": "gh",
                                                           "probe_status": "not_probed", "read_and_merge": True},
            "transport": "private_unix_socket",
            "lifecycle": {"protocol": service.PROTOCOL, "state_contract": service.STATE_CONTRACT, "pid": os.getpid()},
            "limits": {"default_timeout_seconds": execution.DEFAULT_TIMEOUT, "max_timeout_seconds": execution.MAX_TIMEOUT, "max_read_bytes": execution.MAX_READ,
                       "log_bytes": execution.MAX_LOG, "log_retention": "rotating", "log_segments": 4, "log_segment_bytes": SEGMENT_BYTES,
                       "workspace_access": ["exclusive", "shared"], "max_resource_keys": 16,
                       "git_capture_bytes": workspace.MAX_CAPTURE, "search_entries": MAX_SCAN_ENTRIES,
                       "search_bytes": workspace.MAX_SCAN_BYTES, "search_matches": workspace.MAX_MATCHES,
                       "text_file_bytes": MAX_TEXT_FILE, "text_page_bytes": TEXT_PAGE,
                       "checkpoint_bytes": tasks.MAX_CHECKPOINT, "task_index_bytes": tasks.MAX_INDEX},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        }

    def inspect_workspace(self, machine: str, workspace_name: str,
                          action: Literal["files", "read", "search", "status", "diff"], path: str = ".",
                          offset: int = 0, limit: int = 100, query: str = "", staged: bool = False,
                          expected_snapshot: str = "") -> dict:
        return self.machines.inspect_workspace(machine, workspace_name, action, path, offset, limit, query,
                                               staged, expected_snapshot)

    def inspect_skills(self, action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: str = "", offset: int = 0, limit: int = 100, expected_snapshot: str = "") -> dict:
        return skills.inspect(self.config, action, skill, path, query, offset, limit, expected_snapshot)

    def read_workspace_file(self, machine: str, workspace_name: str, path: str, offset: int = 0,
                            expected_sha256: str = "") -> dict:
        return self.machines.read_workspace_file(machine, workspace_name, path, offset, expected_sha256)

    def write_workspace_file(self, machine: str, workspace_name: str, path: str, content: str,
                             expected_head_sha: str, expected_sha256: str) -> dict:
        with self._admit():
            return self.machines.write_workspace_file(machine, workspace_name, path, content, expected_head_sha,
                                                      expected_sha256)

    def find_tasks(self, query: str = "", anchors: list[str] | None = None, task_id: str = "", offset: int = 0,
                   limit: int = 20, expected_generation: int = -1) -> dict:
        return tasks.find(self.state, query, anchors or [], task_id, offset, limit, expected_generation)

    def read_task(self, task_id: str) -> dict:
        return tasks.read(self.state, task_id)

    def save_task(self, task_id: str, expected_revision: int, title: str, body: str,
                  anchors: list[str] | None = None) -> dict:
        with self._admit():
            return tasks.save(self.state, task_id, expected_revision, title, body, anchors or [])

    def start_execution(self, machine: str, execution_id: str, cwd: str, argv: list[str],
                        timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
                        workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: list[str] | None = None, task_id: str = "") -> dict:
        with self._admit():
            return self.machines.start_execution(machine, execution_id, cwd, argv, timeout_seconds,
                                                 expected_head_sha, workspace_access, resource_keys, task_id)

    def read_execution(self, machine: str, execution_id: str, offset: int = 0, max_bytes: int = 65536) -> dict:
        return self.machines.read_execution(machine, execution_id, offset, max_bytes)

    def list_executions(self, machine: str, cursor: str = "", limit: int = 20) -> dict:
        return self.machines.list_executions(machine, cursor, limit)

    def cancel_execution(self, machine: str, execution_id: str) -> dict:
        with self._admit():
            return self.machines.cancel_execution(machine, execution_id)

    def github_read(self, action: str, repository: str = "", number: int = 0, ref: str = "", path: str = "",
                    page: int = 1, limit: int = 30, cursor: str = "", expected_head_sha: str = "",
                    expected_base_sha: str = "", offset: int = 0) -> dict:
        return self._provider().read(self.config, action, repository, number, ref, path, page, limit, cursor,
                                     expected_head_sha, expected_base_sha, offset)

    def github_issue(self, action: Literal["create", "update"], repository: str, title: str | None = None,
                     body: str | None = None, number: int = 0, state: Literal["open", "closed"] | None = None,
                     expected_updated_at: str = "") -> dict:
        with self._admit():
            return self._provider().issue(self.config, action, repository, title, body, number, state,
                                          expected_updated_at)

    def github_pull_request(self, action: Literal["create", "update"], repository: str,
                            title: str | None = None, body: str | None = None, number: int = 0, head: str = "",
                            base: str = "", draft: bool = False, state: Literal["open", "closed"] | None = None,
                            expected_updated_at: str = "", expected_head_sha: str = "") -> dict:
        with self._admit():
            return self._provider().pull_request(self.config, action, repository, title, body, number, head,
                                                 base, draft, state, expected_updated_at,
                                                 expected_head_sha)

    def github_comment(self, action: Literal["create", "update"], repository: str, body: str, number: int = 0,
                       comment_id: int = 0, expected_updated_at: str = "") -> dict:
        with self._admit():
            return self._provider().comment(self.config, action, repository, body, number, comment_id,
                                            expected_updated_at)

    def github_review(self, action: Literal["create", "update"], repository: str, body: str, number: int = 0,
                      review_id: int = 0, expected_head_sha: str = "",
                      event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT") -> dict:
        with self._admit():
            return self._provider().review(self.config, action, repository, body, number, review_id,
                                           expected_head_sha, event)

    def github_merge(self, repository: str, number: int, expected_head_sha: str, expected_base_sha: str = "",
                     method: Literal["squash", "merge", "rebase"] = "squash", commit_title: str = "",
                     commit_message: str = "") -> dict:
        with self._admit():
            return self._provider().merge(self.config, repository, number, expected_head_sha, expected_base_sha,
                                          method, commit_title, commit_message)
