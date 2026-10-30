"""Typed application operations; every mutation shares one lifecycle admission."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from types import ModuleType
from typing import Literal

from openmachine import __version__, execution, service, skills, tasks, workspace
from openmachine.config import Settings
from openmachine.execution import DEFAULT_TIMEOUT, MAX_TIMEOUT, MAX_WAIT, MAX_WAITERS, MAX_RESULT_BYTES, SEGMENT_BYTES, bound_output, MAX_CONTROL_SEQ, MAX_INPUT
from openmachine.foundation.files import MAX_SCAN_ENTRIES, MAX_TEXT_FILE, TEXT_PAGE
from openmachine.foundation.sanitize import Fault
from openmachine.foundation.state import lease, private_directory

from . import environment, task_attention
from .machines import Machines
from .task_admission import TaskAdmission
from .task_observer import TaskObserver
from .task_receipts import TaskReceipts

MAX_WORKSPACE_RESULT = workspace.MAX_WORKSPACE_RESULT
MAX_SOURCE = workspace.MAX_SOURCE
bound_workspace_result = workspace.bound_result
MAX_TASK_WAIT = task_attention.MAX_WAIT_SECONDS
MAX_TASK_RESULT_BYTES = 512 * 1024


class Application:
    def __init__(self, config: Settings, state: Path, machines: Machines, github: ModuleType | None,
                 connections=None, codex=None):
        self.config = config
        self.state = state
        self.machines = machines
        self.github = github
        self.connections = connections
        self.codex = codex
        if connections is not None:
            connections.admit = self._admit
        self.task_receipts = TaskReceipts(state, config, machines, self._github_call)
        self.task_admission = TaskAdmission(self)
        self.task_observer = TaskObserver(self)

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

    def list_integrations(self, provider: str = "", connection_id: str = "") -> dict:
        return self.connections.list(provider, connection_id)

    def connect_integration(self, request_id: str, provider: str = "github", method: str = "device_code",
                            connection_id: str = "") -> dict:
        with self._admit():
            return self.connections.connect(request_id, provider, method, connection_id)

    def read_integration_login(self, login_id: str) -> dict:
        return self.connections.read_login(login_id)

    def cancel_integration_login(self, login_id: str) -> dict:
        with self._admit():
            return self.connections.cancel_login(login_id)

    def disconnect_integration(self, connection_id: str, expected_revision: int) -> dict:
        with self._admit():
            return self.connections.disconnect(connection_id, expected_revision)

    def _github_call(self, operation: str, arguments: dict, *, binding=None, historical_unbound: bool = False) -> dict:
        arguments = dict(arguments)
        selected = arguments.pop("connection_id", "")
        repository = arguments.get("repository", (arguments.get("locator") or {}).get("repository", ""))
        try:
            provider = self._provider()
            transport = self.connections.request(repository, connection_id=selected, binding=binding,
                write=operation not in {"read", "reconcile"}, historical_unbound=historical_unbound)
        except Fault as error:
            if operation not in {"read", "reconcile"}:
                raise Fault(error.code, str(error), error.evidence, effect="known_no_effect") from error
            raise
        except OSError as error:
            if operation not in {"read", "reconcile"}:
                raise Fault("integration_store_unavailable", "Business transport preparation failed before mutation.",
                            effect="known_no_effect") from error
            raise
        return getattr(provider, operation)(self.config, **arguments, transport=transport)

    def probe_machine(self, machine: str) -> dict:
        return self.machines.probe(machine)

    def get_capabilities(self) -> dict:
        has_ssh = any(item.transport == "ssh" for item in self.config.machines.values())
        return {
            "environment_observation": environment.capabilities(),
            "version": __version__, "commit": service.running_commit(__version__), "configuration_sha256": self.config.configuration_sha256(),
            "machines": {name: {**item.model_dump(exclude={"ssh_connection"}), "workspace_write_effective": item.execution_enabled if item.workspace_write_enabled is None else item.workspace_write_enabled,
                                  "probe_status": "not_probed"} for name, item in self.config.machines.items()},
            "workspaces": {name: item.model_dump() for name, item in self.config.workspaces.items()},
            "skills": ["openmachine"], "skill_roots": self.config.skills.roots, "task_continuity": True,
            "skill_navigation": {"entry": skills.for_tool("get_capabilities"),
                                 "tool": "inspect_skills", "selector": "tool"},
            "tasks": {"structured_records": True, "record_read_formats": list(tasks.READ_FORMATS),
                      "storage": "sqlite", "read_schema": 2,
                      "read_sections": ["summary", "operations", "history", "evidence"],
                      "supervised_admission": True, "runtime_recovery": False,
                      "events_delivery": False,
                      "foreground_wait": {"max_seconds": task_attention.MAX_WAIT_SECONDS,
                          "handoff_grace_seconds": task_attention.HANDOFF_GRACE_MS // 1000,
                          "attention_reasons": list(task_attention.ATTENTION_REASONS)},
                      "local_result_observation": True,
                      "ssh_result_observation": self.machines.ssh_recovery is not None},
            "integrations": (["github"] if self.config.github is not None else []) + (["ssh"] if has_ssh else []),
            "github": None if self.config.github is None else {**self.config.github.model_dump(),
                                                           "backend": "native", "legacy_gh": "retired",
                                                           "probe_status": "not_probed", "read_and_merge": True},
            "connections": {"protocol": 2,
                            "methods": ["device_code"] if self.config.github and self.connections.native.CLIENT_ID else [],
                            "native_login": bool(self.config.github and self.connections.native.CLIENT_ID),
                            "login_cancellation": True},
            "codex": self.codex.capabilities(self.config) if self.codex is not None else None,
            "transport": "private_unix_socket",
            "execution": {"receipt_format": execution.FORMAT, "worktree_receipt_format": execution.CREATION_FORMAT,
                          "io_modes": ["pipe", "pty"], "controls": sorted(execution.CONTROL_ACTIONS),
                          "control_protocol": execution.CONTROL_PROTOCOL, "max_input_bytes": execution.MAX_INPUT,
                          "control_frame_bytes": execution.MAX_FRAME, "control_seconds": execution.CONTROL_SECONDS,
                          "terminal_dimensions": {"default": {"rows": 24, "cols": 80}, "min": 1, "max": 1000},
                          "wait_seconds": {"default": 0, "min": 0, "max": execution.MAX_WAIT},
                          "max_waiting_readers": execution.MAX_WAITERS,
                          "termination_grace_seconds": {"default": execution.DEFAULT_GRACE, "min": 0, "max": 30},
                          "containment": {"native": "posix_session", "ssh": "systemd_scope"}},
            "workspace": {"change_format": workspace.CHANGE_FORMAT, "exact_edits": True, "preview": True,
                          "worktrees": {"discovery": True, "selection": True, "creation": True,
                                        "automatic_allocation": False, "automatic_cleanup": False,
                                        "destination_scope": "configured_roots", "copies_uncommitted_changes": False},
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
                       "checkpoint_bytes": tasks.MAX_CHECKPOINT,
                       "task_operation_bytes": tasks.MAX_OPERATION_BYTES, "task_event_bytes": tasks.MAX_EVENT_BYTES,
                       "task_page_bytes": tasks.MAX_PAGE_BYTES, "task_page_entries": tasks.MAX_PAGE_ITEMS,
                       "task_active_operations": tasks.MAX_ACTIVE_OPERATIONS,
                       "task_result_bytes": MAX_TASK_RESULT_BYTES},
            "boundary": "Native account access, not a sandbox. No live ChatGPT/Tunnel health probe.",
        }

    def inspect_context(self, machine: str, workspace_name: str, worktree: str = "", task_id: str = "",
                        sections: list[str] | None = None, limit: int = 10,
                        executables: list[str] | None = None, ports: list[int] | None = None) -> dict:
        return environment.inspect_context(self, machine, workspace_name, worktree, task_id, sections, limit, executables, ports)

    def inspect_workspace(self, machine: str, workspace_name: str,
                          action: Literal["files", "read", "search", "status", "diff", "worktrees"], path: str = ".",
                          offset: int = 0, limit: int = 100, query: str = "", staged: bool = False,
                          expected_snapshot: str = "", path_glob: str = "", worktree: str = "") -> dict:
        return self.machines.inspect_workspace(machine, workspace_name, action, path, offset, limit, query,
                                               staged, expected_snapshot, path_glob, worktree)

    def inspect_skills(self, action: Literal["list", "read", "files"] = "list", skill: str = "", path: str = "",
                       query: str = "", offset: int = 0, limit: int = 100, expected_snapshot: str = "",
                       tool: str = "") -> dict:
        return skills.inspect(self.config, action, skill, path, query, offset, limit, expected_snapshot, tool)

    def read_workspace_file(self, machine: str, workspace_name: str, path: str, offset: int = 0,
                            expected_sha256: str = "", start_line: int | None = None,
                            line_count: int = 200, worktree: str = "") -> dict:
        return self.machines.read_workspace_file(machine, workspace_name, path, offset, expected_sha256,
                                                 start_line, line_count, worktree)

    def write_workspace_file(self, machine: str, workspace_name: str, path: str, content: str,
                             expected_head_sha: str, expected_sha256: str, change_id: str = "",
                             expected_workspace_identity: str = "", task_context: dict | None = None,
                             worktree: str = "") -> dict:
        with self._admit():
            return self.task_admission.run("write_workspace_file", dict(machine=machine, workspace_name=workspace_name,
                path=path, content=content, expected_head_sha=expected_head_sha, expected_sha256=expected_sha256,
                change_id=change_id, expected_workspace_identity=expected_workspace_identity,
                **({"worktree": worktree} if worktree else {})), task_context)

    def edit_workspace_file(self, machine: str, workspace_name: str, path: str, edits: list[dict],
                            expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                            change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "",
                            task_context: dict | None = None, worktree: str = "") -> dict:
        arguments = dict(machine=machine, workspace_name=workspace_name, path=path, edits=edits,
            expected_workspace_identity=expected_workspace_identity, expected_head_sha=expected_head_sha,
            expected_sha256=expected_sha256, change_id=change_id, dry_run=dry_run, expected_plan_sha256=expected_plan_sha256,
            **({"worktree": worktree} if worktree else {}))
        if dry_run is True:
            return self.machines.edit_workspace_file(**arguments)
        with self._admit():
            return self.task_admission.run("edit_workspace_file", arguments, task_context)

    def apply_workspace_patch(self, machine: str, workspace_name: str, patch: str,
                              expected_workspace_identity: str, expected_head_sha: str, preconditions: list[dict],
                              create_parents: bool = False, change_id: str = "", dry_run: bool = False,
                              expected_plan_sha256: str = "", task_context: dict | None = None,
                              worktree: str = "") -> dict:
        arguments = dict(machine=machine, workspace_name=workspace_name, patch=patch,
            expected_workspace_identity=expected_workspace_identity, expected_head_sha=expected_head_sha,
            preconditions=preconditions, create_parents=create_parents, change_id=change_id, dry_run=dry_run,
            expected_plan_sha256=expected_plan_sha256, **({"worktree": worktree} if worktree else {}))
        if dry_run is True:
            return self.machines.apply_workspace_patch(**arguments)
        with self._admit():
            return self.task_admission.run("apply_workspace_patch", arguments, task_context)

    def read_workspace_change(self, machine: str, change_id: str, observe_current: bool = False) -> dict:
        return self.machines.read_workspace_change(machine, change_id, observe_current)

    def create_worktree(self, machine: str, workspace_name: str, destination: str, base_ref: str,
                        execution_id: str, expected_workspace_identity: str, new_branch: str = "",
                        worktree: str = "", task_context: dict | None = None) -> dict:
        arguments = dict(machine=machine, workspace_name=workspace_name, destination=destination,
                         base_ref=base_ref, new_branch=new_branch, execution_id=execution_id,
                         expected_workspace_identity=expected_workspace_identity, task_id="",
                         **({"worktree": worktree} if worktree else {}))
        with self._admit():
            result = self.task_admission.run("create_worktree", arguments, task_context)
        return self._execution_task_feedback(machine, result)

    def find_tasks(self, query: str = "", anchors: list[str] | None = None, task_id: str = "", offset: int = 0,
                   limit: int = 20, expected_generation: int = -1, *, machine: str = "",
                   workspace: str = "", workspace_identity: str = "", target_identity: str = "") -> dict:
        return tasks.find(self.state, query, anchors or [], task_id, offset, limit, expected_generation,
                          machine=machine, workspace=workspace, workspace_identity=workspace_identity,
                          target_identity=target_identity)

    def _task_view(self, task_id: str, *, wait_expired: bool = False, operation_ids: tuple[str, ...] = ()) -> dict:
        view = self.task_observer.project(tasks.read(self.state, task_id, operation_ids=operation_ids))
        return task_attention.project(view, wait_expired=wait_expired)

    def read_task(self, task_id: str, *,
                  section: Literal["summary", "operations", "history", "evidence"] = "summary",
                  cursor: str = "", limit: int = 20, operation_id: str = "",
                  stage_id: str = "", decision_version: int = 0) -> dict:
        if (section not in {"summary", "operations", "history", "evidence"}
                or type(limit) is not int or not 1 <= limit <= tasks.MAX_PAGE_ITEMS):
            raise Fault("invalid_task_read", "Use a supported section and a 1..50 entry limit.")
        if (type(decision_version) is not int or not 0 <= decision_version <= 9007199254740991
                or bool(stage_id) != bool(decision_version)
                or (stage_id and (section != "evidence" or cursor or operation_id))):
            raise Fault("invalid_task_read", "Exact stage evidence requires its stage ID and decision version in the evidence section.")
        view = self._task_view(task_id)
        if section == "summary" and not cursor and not operation_id:
            return tasks.present(view)
        page = tasks.public_read(self.state, task_id, section=section, cursor=cursor, limit=limit,
            operation_id=operation_id, stage_id=stage_id, decision_version=decision_version,
            expected_revision=view.get("record", {}).get("revision", -1))
        return {**page, **{key: view[key] for key in ("foreground", "observations", "support") if key in view}}

    def wait_task(self, task_id: str, expected_revision: int, wait_seconds: float = MAX_TASK_WAIT,
                  *, cancelled: threading.Event | None = None) -> dict:
        if type(expected_revision) is not int or expected_revision < 1 or not 0 <= wait_seconds <= MAX_TASK_WAIT:
            raise Fault("invalid_task_wait", "Supply an observed positive task revision and a finite 0..30 second wait.")
        started = time.monotonic()
        sequence = self.task_observer.sequence()
        while True:
            view = self._task_view(task_id)
            if "record" not in view:
                raise Fault("task_legacy", "Foreground waiting requires a structured task record.")
            record, foreground = view["record"], view["foreground"]
            if foreground["status"] in {"attention_required", "renewal_required", "ready", "terminal"}:
                wait_status = foreground["status"]
                break
            if record["revision"] != expected_revision:
                wait_status = "changed"
                break
            if cancelled is not None and cancelled.is_set():
                view = self._task_view(task_id, wait_expired=True)
                wait_status = "cancelled"
                break
            remaining = wait_seconds - (time.monotonic() - started)
            if remaining <= 0:
                view = self._task_view(task_id, wait_expired=True)
                wait_status = "expired"
                break
            sequence = self.task_observer.wait(
                sequence, min(remaining, task_attention.WAIT_SLICE_SECONDS), cancelled)
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        view = {**view, "wait": {"status": wait_status, "expected_revision": expected_revision,
            "current_revision": view["record"]["revision"], "elapsed_ms": elapsed}}
        return tasks.present(view)

    def save_task(self, task_id: str, expected_revision: int, title: str, body: str,
                  anchors: list[str] | None = None) -> dict:
        with self._admit():
            return tasks.save(self.state, task_id, expected_revision, title, body, anchors or [])

    def control_task(self, task_id: str, expected_revision: int, expected_plan_revision: int,
                     expected_epoch: int, update_id: str, transition: dict) -> dict:
        request = tasks.normalize_control(dict(task_id=task_id, expected_revision=expected_revision,
            expected_plan_revision=expected_plan_revision, expected_epoch=expected_epoch,
            update_id=update_id, transition=transition))
        with self._admit():
            view = self.task_admission.control(request)
        self.task_observer.notify()
        return tasks.present(view)

    def _validate_task_scope(self, request: dict, *, bind_new: bool = False, check_head: bool = True,
                             check_identity: bool = True) -> None:
        contract = request["contract"]
        tools = set(contract["tools"])
        known = {"inspect_context", "get_capabilities", "inspect_workspace", "inspect_skills", "read_workspace_file", "create_worktree",
                 "write_workspace_file", "edit_workspace_file", "apply_workspace_patch", "read_workspace_change",
                 "find_tasks", "read_task", "wait_task", "save_task", "create_task", "control_task", "start_execution", "read_execution",
                 "list_executions", "cancel_execution", "reconcile_execution", "control_execution", "github_read", "github_release", "github_issue",
                 "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge", "github_project",
                 "list_integrations", "connect_integration", "read_integration_login", "cancel_integration_login",
                 "disconnect_integration", "start_codex_task", "read_codex_task"}
        if "start_codex_task" in tools and "start_execution" not in tools:
            raise Fault("invalid_task_contract", "Codex delegation requires the underlying start_execution grant.")
        if not tools <= known:
            raise Fault("invalid_task_contract", "Task scope must name implemented tools, not future actions.")
        if tools & {"start_execution", "create_worktree", "control_execution", "write_workspace_file", "edit_workspace_file", "apply_workspace_patch"} and not contract["targets"]:
            raise Fault("invalid_task_contract", "Native mutation scope requires an explicit configured target.")
        for target in contract["targets"]:
            machine, name = target["machine"], target["workspace"]
            self.config.machine(machine)
            configured = self.config.workspaces.get(name)
            if configured is None or configured.machine != machine:
                raise Fault("invalid_task_contract", "Task target must bind an existing workspace to its configured machine.")
            if tools & {"start_execution", "create_worktree", "control_execution"}:
                self.config.machine(machine, write=True)
            if tools & {"write_workspace_file", "edit_workspace_file", "apply_workspace_patch"}:
                self.config.workspace_machine(machine)
            selection = {"worktree": target["worktree"]} if target.get("worktree") else {}
            observed = self.machines.read_workspace_file(machine, name, "AGENTS.md", start_line=1, line_count=1,
                                                         **selection)["context"]
            if (check_identity and observed.get("workspace_identity") != target["workspace_identity"]
                    or check_head and (observed.get("head_sha") or None) != target["head_sha"]):
                raise Fault("context_mismatch", "The task target identity or HEAD changed; inspect it before accepting a new contract.")
        github_write = bool(tools & {"github_issue", "github_pull_request", "github_comment", "github_review", "github_review_thread", "github_merge", "github_project", "github_release"})
        if any(name.startswith("github_") for name in tools):
            self._provider()
        if github_write and not contract["repositories"]:
            raise Fault("invalid_task_contract", "A GitHub mutation scope needs explicit repositories.")
        bindings = dict(contract.get("github_connections", {}))
        for repository in contract["repositories"]:
            binding = bindings.get(repository)
            if bind_new:
                binding = self.connections.select(repository, binding=binding, write=github_write).binding()
                bindings[repository] = binding
            observed = self._github_call("read", dict(action="repository", repository=repository),
                                         binding=binding, historical_unbound=not bind_new and binding is None)
            if github_write and (observed.get("configured_write") is not True
                    or observed["data"].get("permissions", {}).get("push") is not True):
                raise Fault("github_not_allowed", "The task repository has no verified current write grant.")
        if bind_new and bindings:
            contract["github_connections"] = bindings

    def create_task(self, task_id: str, update_id: str, title: str, anchors: list[str], origin: dict,
                    contract: dict, stages: list[dict]) -> dict:
        # Pure input validation precedes lifecycle initialization; target reads hold no task lock.
        tasks.normalize_create({"task_id": task_id, "update_id": update_id, "title": title,
            "anchors": anchors, "origin": origin, "contract": contract, "stages": stages})
        with self._admit():
            view = tasks.create(self.state, task_id, update_id, title, anchors, origin, contract, stages,
                                validate_new=lambda request: self._validate_task_scope(request, bind_new=True))
        self.task_observer.notify()
        return tasks.present(view)

    def rebuild_task_index(self, expected_generation: int = -1) -> dict:
        with self._admit():
            return tasks.rebuild_index(self.state, expected_generation)

    def start_codex_task(self, machine: str, execution_id: str, cwd: str, goal: str,
                         start_url: str, allowed_origins: list[str], mode: str = "browser", browser: str = "auto",
                         timeout_seconds: int = 600, expected_head_sha: str = "", task_context: dict | None = None) -> dict:
        if self.codex is None:
            raise Fault("codex_unavailable", "The Codex adapter is not assembled in this runtime.")
        return self.codex.start(self.config, self.state, self.start_execution, machine, execution_id, cwd,
            goal, start_url, allowed_origins, mode, browser, timeout_seconds, expected_head_sha, task_context)

    def read_codex_task(self, machine: str, execution_id: str) -> dict:
        if self.codex is None:
            raise Fault("codex_unavailable", "The Codex adapter is not assembled in this runtime.")
        return self.codex.read(self.config, self.state, self.read_execution, machine, execution_id)

    def start_execution(self, machine: str, execution_id: str, cwd: str, argv: list[str],
                        timeout_seconds: int = DEFAULT_TIMEOUT, expected_head_sha: str = "",
                        workspace_access: Literal["exclusive", "shared"] = "exclusive",
                        resource_keys: list[str] | None = None, task_id: str = "",
                        termination_grace_seconds: float | None = None, tty: bool = False,
                        terminal_size: dict | None = None, task_context: dict | None = None) -> dict:
        with self._admit():
            result = self.task_admission.run("start_execution", dict(machine=machine, execution_id=execution_id,
                cwd=cwd, argv=argv, timeout_seconds=timeout_seconds, expected_head_sha=expected_head_sha,
                workspace_access=workspace_access, resource_keys=resource_keys, task_id=task_id,
                termination_grace_seconds=termination_grace_seconds, tty=tty, terminal_size=terminal_size), task_context)
        return self._execution_task_feedback(machine, result)

    def _execution_task_feedback(self, machine: str, result: dict) -> dict:
        """Annotate an original receipt without changing its outcome or task history."""
        identity = result.get("task_id", "")
        feedback = {"status": "unbound"}
        if identity:
            feedback = {"status": "legacy", "task_id": identity}
            try:
                view = self._task_view(identity, operation_ids=(result["execution_id"],))
                if "record" in view:
                    target = self.config.machine(machine).ssh_identity or None
                    if not any(item["id"] == result["execution_id"] and item["locator"]["kind"] == "execution"
                               and item["locator"]["execution_id"] == result["execution_id"]
                               and item["locator"]["target_identity"] == target
                               for item in view["record"]["operations"]):
                        raise Fault("task_operation_conflict", "The receipt is not an admitted execution of this task.")
                    feedback = {"status": "supervised", "task_id": identity,
                                "task_revision": view["record"]["revision"], "foreground": view["foreground"]}
            except (Fault, OSError) as error:
                feedback = {"status": "unavailable", "task_id": identity,
                            "error_code": error.code if isinstance(error, Fault) else "source_unavailable",
                            "resume_prompt": f"Read Open Machine task {identity} and this original execution before continuing. "
                                             "Task status is unavailable; do not replay or reassign the execution."}
        return {**result, "task_feedback": feedback}

    def read_execution(self, machine: str, execution_id: str, offset: int = 0, max_bytes: int = 65536,
                       wait_seconds: float = 0, *, cancelled: threading.Event | None = None) -> dict:
        result = self.machines.read_execution(machine, execution_id, offset, max_bytes, wait_seconds, cancelled=cancelled)
        return self._execution_task_feedback(machine, result)

    def list_executions(self, machine: str, cursor: str = "", limit: int = 20) -> dict:
        return self.machines.list_executions(machine, cursor, limit)

    def cancel_execution(self, machine: str, execution_id: str, task_context: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("cancel_execution", dict(machine=machine, execution_id=execution_id), task_context)

    def reconcile_execution(self, machine: str, execution_id: str) -> dict:
        with self._admit():
            result = execution.reconcile(self.config, self.state, machine, execution_id)
        return self._execution_task_feedback(machine, result)

    def github_read(self, action: str, repository: str = "", number: int = 0, ref: str = "", path: str = "",
                    page: int = 1, limit: int = 30, cursor: str = "", expected_head_sha: str = "",
                    expected_base_sha: str = "", offset: int = 0, connection_id: str = "") -> dict:
        return self._github_call("read", dict(action=action, repository=repository, number=number, ref=ref,
            path=path, page=page, limit=limit, cursor=cursor, expected_head_sha=expected_head_sha,
            expected_base_sha=expected_base_sha, offset=offset, connection_id=connection_id))

    def github_release(self, action: Literal["create", "publish"], repository: str, tag_name: str,
                       name: str = "", body: str = "", draft: bool = True,
                       prerelease: bool = False, task_context: dict | None = None,
                       connection_id: str = "", release_id: int = 0,
                       expected_body_sha256: str = "") -> dict:
        if action == "create" and (not isinstance(body, str) or not body.strip()):
            raise Fault("invalid_github_request", "Release notes must contain nonempty text.")
        with self._admit():
            return self.task_admission.run("github_release", dict(
                action=action, repository=repository, tag_name=tag_name, name=name, body=body,
                draft=draft, prerelease=prerelease,
                **({"release_id": release_id, "expected_body_sha256": expected_body_sha256}
                   if action == "publish" else {}),
                **({"connection_id": connection_id} if connection_id else {})), task_context)

    def github_project(self, action: Literal["add_item", "update_field"], repository: str, project_number: int,
                       issue_number: int = 0, item_id: str = "", field_id: str = "",
                       value: dict | None = None, expected_value: dict | None = None,
                       task_context: dict | None = None, connection_id: str = "",
                       pull_request_number: int = 0) -> dict:
        with self._admit():
            return self.task_admission.run("github_project", dict(
                action=action, repository=repository, project_number=project_number,
                issue_number=issue_number, item_id=item_id, field_id=field_id,
                value=value, expected_value=expected_value,
                **({"pull_request_number": pull_request_number} if pull_request_number else {}),
                **({"connection_id": connection_id} if connection_id else {})), task_context)

    def github_issue(self, action: Literal["create", "update"], repository: str, title: str | None = None,
                     body: str | None = None, number: int = 0, state: Literal["open", "closed"] | None = None,
                     expected_updated_at: str = "", labels: list[str] | None = None,
                     assignees: list[str] | None = None, task_context: dict | None = None, connection_id: str = "",
                     expected_fields: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("github_issue", dict(action=action, repository=repository, title=title,
                body=body, number=number, state=state, expected_updated_at=expected_updated_at,
                labels=labels, assignees=assignees, **({"connection_id": connection_id} if connection_id else {}),
                **({"expected_fields": expected_fields} if expected_fields is not None else {})), task_context)

    def github_pull_request(self, action: Literal["create", "update", "ready_for_review"], repository: str,
                            title: str | None = None, body: str | None = None, number: int = 0, head: str = "",
                            base: str = "", draft: bool = False, state: Literal["open", "closed"] | None = None,
                            expected_updated_at: str = "", expected_head_sha: str = "",
                            labels: list[str] | None = None, assignees: list[str] | None = None,
                            task_context: dict | None = None, connection_id: str = "",
                            expected_fields: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("github_pull_request", dict(action=action, repository=repository,
                title=title, body=body, number=number, head=head, base=base, draft=draft, state=state,
                expected_updated_at=expected_updated_at, expected_head_sha=expected_head_sha,
                labels=labels, assignees=assignees, **({"connection_id": connection_id} if connection_id else {}),
                **({"expected_fields": expected_fields} if expected_fields is not None else {})), task_context)

    def github_comment(self, action: Literal["create", "update", "delete"], repository: str, body: str = "", number: int = 0,
                       comment_id: int = 0, expected_updated_at: str = "", task_context: dict | None = None, connection_id: str = "",
                       expected_fields: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("github_comment", dict(action=action, repository=repository, body=body,
                number=number, comment_id=comment_id, expected_updated_at=expected_updated_at, **({"connection_id": connection_id} if connection_id else {}),
                **({"expected_fields": expected_fields} if expected_fields is not None else {})), task_context)

    def github_review(self, action: Literal["create", "update"], repository: str, body: str, number: int = 0,
                      review_id: int = 0, expected_head_sha: str = "",
                      event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT",
                      task_context: dict | None = None, connection_id: str = "",
                      expected_fields: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("github_review", dict(action=action, repository=repository, body=body,
                number=number, review_id=review_id, expected_head_sha=expected_head_sha, event=event, **({"connection_id": connection_id} if connection_id else {}),
                **({"expected_fields": expected_fields} if expected_fields is not None else {})), task_context)

    def github_review_thread(self, action: Literal["resolve", "unresolve"], repository: str, number: int,
                             thread_id: str, expected_head_sha: str, expected_base_sha: str = "",
                             task_context: dict | None = None, connection_id: str = "") -> dict:
        with self._admit():
            return self.task_admission.run("github_review_thread", dict(
                action=action, repository=repository, number=number, thread_id=thread_id,
                expected_head_sha=expected_head_sha, expected_base_sha=expected_base_sha,
                **({"connection_id": connection_id} if connection_id else {})), task_context)

    def github_merge(self, repository: str, number: int, expected_head_sha: str, expected_base_sha: str = "",
                     method: Literal["squash", "merge", "rebase"] = "squash", commit_title: str = "",
                     commit_message: str = "", task_context: dict | None = None, connection_id: str = "") -> dict:
        with self._admit():
            return self.task_admission.run("github_merge", dict(repository=repository, number=number,
                expected_head_sha=expected_head_sha, expected_base_sha=expected_base_sha,
                method=method, commit_title=commit_title, commit_message=commit_message, **({"connection_id": connection_id} if connection_id else {})), task_context)

    def control_execution(self, machine: str, execution_id: str, expected_control_seq: int,
                          action: Literal["write_stdin", "interrupt", "resize"], text: str | None = None,
                          terminal_size: dict | None = None, task_context: dict | None = None) -> dict:
        with self._admit():
            return self.task_admission.run("control_execution", dict(machine=machine, execution_id=execution_id,
                expected_control_seq=expected_control_seq, action=action, text=text, terminal_size=terminal_size), task_context)
