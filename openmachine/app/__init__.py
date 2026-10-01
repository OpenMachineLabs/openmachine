"""Typed application operations; every mutation shares one lifecycle admission."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import threading
from types import ModuleType
from typing import Literal

from openmachine import __version__, execution, service, skills, tasks, workspace
from openmachine.config import Settings
from openmachine.execution import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, SEGMENT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT
from openmachine.foundation.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, TEXT_PAGE
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

from .machines import Machines

MAX_WORKSPACE_RESULT = workspace.MAX_WORKSPACE_RESULT
MAX_SOURCE = workspace.MAX_SOURCE
bound_workspace_result = workspace.bound_result


class Application:
    def __init__(self, config: Settings, state: Path, machines: Machines, github: ModuleType | None):
        self.config = config
        self.state = state
        self.machines = machines
        self.github = github

    @contextmanager
    def _admit(self) -> Iterator[None]:
        private_directory(self.state)
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
            "version": __version__, "configuration_sha256": self.config.configuration_sha256(),
            "machines": {name: {**item.model_dump(), "workspace_write_effective": item.execution_enabled if item.workspace_write_enabled is None else item.workspace_write_enabled,
                                  "probe_status": "not_probed"} for name, item in self.config.machines.items()},
            "workspaces": {name: item.model_dump() for name, item in self.config.workspaces.items()},
            "skills": ["openmachine"], "skill_roots": self.config.skills.roots, "task_continuity": True,
            "tasks": {"structured_records": True, "record_read_formats": list(tasks.READ_FORMATS),
                      "index_format": 2, "supervised_admission": False, "runtime_recovery": False,
                      "events_delivery": False},
            "integrations": (["github"] if self.config.github is not None else []) + (["ssh"] if has_ssh else []),
            "github": None if self.config.github is None else {**self.config.github.model_dump(), "backend": "gh",
                                                           "probe_status": "not_probed", "read_and_merge": True},
            "transport": "private_unix_socket",
            "execution": {"receipt_format": execution.FORMAT, "io_modes": ["pipe", "pty"], "controls": sorted(execution.CONTROL_ACTIONS),
                          "control_protocol": execution.CONTROL_PROTOCOL, "max_input_bytes": execution.MAX_INPUT,
                          "control_frame_bytes": execution.MAX_FRAME, "control_seconds": execution.CONTROL_SECONDS,
                          "terminal_dimensions": {"default": {"rows": 24, "cols": 80}, "min": 1, "max": 1000},
                          "wait_seconds": {"default": 0, "min": 0, "max": execution.MAX_WAIT},
                          "max_waiting_readers": execution.MAX_WAITERS,
                          "termination_grace_seconds": {"default": execution.DEFAULT_GRACE, "min": 0, "max": 30},
                          "containment": {"native": "posix_session", "ssh": "systemd_scope"}},
            "workspace": {"change_format": workspace.CHANGE_FORMAT, "exact_edits": True, "preview": True,
                          "patch": True, "patch_language": workspace.PATCH_LANGUAGE,
                          "change_read_formats": list(workspace.READ_CHANGE_FORMATS),
                          "max_patch_paths": workspace.MAX_PATHS, "max_patch_hunks": workspace.MAX_HUNKS,
                          "prepared_bytes": workspace.MAX_PREPARED, "max_created_parents": workspace.MAX_PARENTS,
                          "max_parent_depth": workspace.MAX_PARENT_DEPTH, "max_edits": workspace.MAX_EDITS, "source_bytes": workspace.MAX_SOURCE,
                          "line_windows": True, "max_lines": workspace.MAX_LINES,
                          "path_glob": {"recursive": False, "case_sensitive": True, "scope": "workspace_relative"},
                          "payload_bytes": MAX_TEXT_FILE, "change_record_bytes": workspace.MAX_RECORD,
                          "retained_change_ids": workspace.MAX_CHANGES, "result_bytes": workspace.MAX_WORKSPACE_RESULT},
            "lifecycle": {"protocol": service.PROTOCOL, "state_contract": service.STATE_CONTRACT, "pid": os.getpid()},
            "limits": {"default_timeout_seconds": execution.DEFAULT_TIMEOUT, "max_timeout_seconds": execution.MAX_TIMEOUT, "max_read_bytes": execution.MAX_READ,
                       "max_execution_result_bytes": execution.MAX_RESULT_BYTES,
                       "log_bytes": execution.MAX_LOG, "log_retention": "rotating", "log_segments": 4, "log_segment_bytes": SEGMENT_BYTES,
                       "workspace_access": ["exclusive", "shared"], "max_resource_keys": 16,
                       "git_capture_bytes": workspace.MAX_CAPTURE, "search_entries": MAX_SCAN_ENTRIES,
                       "search_bytes": workspace.MAX_SCAN_BYTES, "search_matches": workspace.MAX_MATCHES,
                       "text_file_bytes": workspace.MAX_SOURCE, "text_page_bytes": TEXT_PAGE,
                       "skill_text_file_bytes": MAX_TEXT_FILE,
                       "checkpoint_bytes": tasks.MAX_CHECKPOINT, "task_index_bytes": tasks.MAX_INDEX,
                       "task_record_bytes": tasks.MAX_RECORD, "task_acceptance_bytes": tasks.MAX_ACCEPTANCE},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        }

    def inspect_workspace(self, machine: str, workspace_name: str,
                          action: Literal["files", "read", "search", "status", "diff"], path: str = ".",
                          offset: int = 0, limit: int = 100, query: str = "", staged: bool = False,
                          expected_snapshot: str = "", path_glob: str = "") -> dict:
        return self.machines.inspect_workspace(machine, workspace_name, action, path, offset, limit, query,
                                               staged, expected_snapshot, path_glob)

    def inspect_skills(self, action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: str = "", offset: int = 0, limit: int = 100, expected_snapshot: str = "") -> dict:
        return skills.inspect(self.config, action, skill, path, query, offset, limit, expected_snapshot)

    def read_workspace_file(self, machine: str, workspace_name: str, path: str, offset: int = 0,
                            expected_sha256: str = "", start_line: int | None = None,
                            line_count: int = 200) -> dict:
        return self.machines.read_workspace_file(machine, workspace_name, path, offset, expected_sha256,
                                                 start_line, line_count)

    def write_workspace_file(self, machine: str, workspace_name: str, path: str, content: str,
                             expected_head_sha: str, expected_sha256: str, change_id: str = "",
                             expected_workspace_identity: str = "") -> dict:
        with self._admit():
            return self.machines.write_workspace_file(machine, workspace_name, path, content, expected_head_sha,
                                                      expected_sha256, change_id, expected_workspace_identity)

    def edit_workspace_file(self, machine: str, workspace_name: str, path: str, edits: list[dict],
                            expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                            change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "") -> dict:
        arguments = (machine, workspace_name, path, edits, expected_workspace_identity, expected_head_sha,
                     expected_sha256, change_id, dry_run, expected_plan_sha256)
        if dry_run is True:
            return self.machines.edit_workspace_file(*arguments)
        with self._admit():
            return self.machines.edit_workspace_file(*arguments)

    def apply_workspace_patch(self, machine: str, workspace_name: str, patch: str,
                              expected_workspace_identity: str, expected_head_sha: str, preconditions: list[dict],
                              create_parents: bool = False, change_id: str = "", dry_run: bool = False,
                              expected_plan_sha256: str = "") -> dict:
        arguments = (machine, workspace_name, patch, expected_workspace_identity, expected_head_sha,
                     preconditions, create_parents, change_id, dry_run, expected_plan_sha256)
        if dry_run is True:
            return self.machines.apply_workspace_patch(*arguments)
        with self._admit():
            return self.machines.apply_workspace_patch(*arguments)

    def read_workspace_change(self, machine: str, change_id: str, observe_current: bool = False) -> dict:
        return self.machines.read_workspace_change(machine, change_id, observe_current)

    def find_tasks(self, query: str = "", anchors: list[str] | None = None, task_id: str = "", offset: int = 0,
                   limit: int = 20, expected_generation: int = -1) -> dict:
        return tasks.find(self.state, query, anchors or [], task_id, offset, limit, expected_generation)

    def read_task(self, task_id: str) -> dict:
        return tasks.read(self.state, task_id)

    def save_task(self, task_id: str, expected_revision: int, title: str, body: str,
                  anchors: list[str] | None = None) -> dict:
        with self._admit():
            return tasks.save(self.state, task_id, expected_revision, title, body, anchors or [])

    def _validate_task_scope(self, request: dict) -> None:
        contract = request["contract"]
        tools = set(contract["tools"])
        known = {"get_capabilities", "inspect_workspace", "inspect_skills", "read_workspace_file",
                 "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "read_workspace_change",
                 "find_tasks", "read_task", "save_task", "create_task", "start_execution", "read_execution",
                 "list_executions", "cancel_execution", "control_execution", "github_read", "github_issue",
                 "github_pull_request", "github_comment", "github_review", "github_merge"}
        if not tools <= known:
            raise Fault("invalid_task_contract", "Task scope must name implemented tools, not future actions.")
        if tools & {"start_execution", "control_execution", "write_workspace_file", "edit_workspace_file", "apply_workspace_patch"} and not contract["targets"]:
            raise Fault("invalid_task_contract", "Native mutation scope requires an explicit configured target.")
        for target in contract["targets"]:
            machine, name = target["machine"], target["workspace"]
            self.config.machine(machine)
            configured = self.config.workspaces.get(name)
            if configured is None or configured.machine != machine:
                raise Fault("invalid_task_contract", "Task target must bind an existing workspace to its configured machine.")
            if tools & {"start_execution", "control_execution"}:
                self.config.machine(machine, write=True)
            if tools & {"write_workspace_file", "edit_workspace_file", "apply_workspace_patch"}:
                self.config.workspace_machine(machine)
            observed = self.machines.read_workspace_file(machine, name, "AGENTS.md", start_line=1, line_count=1)["context"]
            if (observed.get("workspace_identity") != target["workspace_identity"]
                    or (observed.get("head_sha") or None) != target["head_sha"]):
                raise Fault("context_mismatch", "The task target identity or HEAD changed; inspect it before accepting a new contract.")
        github_write = bool(tools & {"github_issue", "github_pull_request", "github_comment", "github_review", "github_merge"})
        if any(name.startswith("github_") for name in tools):
            self._provider()
        if github_write and not contract["repositories"]:
            raise Fault("invalid_task_contract", "A GitHub mutation scope needs explicit repositories.")
        for repository in contract["repositories"]:
            observed = self._provider().read(self.config, "repository", repository)
            if github_write and (observed.get("configured_write") is not True
                    or observed["data"].get("permissions", {}).get("push") is not True):
                raise Fault("github_not_allowed", "The task repository has no verified current write grant.")

    def create_task(self, task_id: str, update_id: str, title: str, anchors: list[str], origin: dict,
                    contract: dict, stages: list[dict]) -> dict:
        # Pure input validation precedes lifecycle initialization; target reads hold no task lock.
        tasks.normalize_create({"task_id": task_id, "update_id": update_id, "title": title,
            "anchors": anchors, "origin": origin, "contract": contract, "stages": stages})
        with self._admit():
            return tasks.create(self.state, task_id, update_id, title, anchors, origin, contract, stages,
                                validate_new=self._validate_task_scope)

    def rebuild_task_index(self, expected_generation: int = -1) -> dict:
        with self._admit():
            return tasks.rebuild_index(self.state, expected_generation)

    def start_execution(self, machine: str, execution_id: str, cwd: str, argv: list[str],
                        timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
                        workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: list[str] | None = None, task_id: str = "",
                        termination_grace_seconds: float | None = None, tty: bool = False,
                        terminal_size: dict | None = None) -> dict:
        with self._admit():
            return self.machines.start_execution(machine, execution_id, cwd, argv, timeout_seconds,
                                                 expected_head_sha, workspace_access, resource_keys, task_id,
                                                 termination_grace_seconds, tty, terminal_size)

    def read_execution(self, machine: str, execution_id: str, offset: int = 0, max_bytes: int = 65536,
                       wait_seconds: float = 0, *, cancelled: threading.Event | None = None) -> dict:
        return self.machines.read_execution(machine, execution_id, offset, max_bytes, wait_seconds, cancelled=cancelled)

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

    def control_execution(self, machine: str, execution_id: str, expected_control_seq: int,
                          action: Literal["write_stdin", "interrupt", "resize"], text: str | None = None,
                          terminal_size: dict | None = None) -> dict:
        with self._admit():
            return self.machines.control_execution(machine, execution_id, expected_control_seq, action, text, terminal_size)
