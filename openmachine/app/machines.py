"""Native machine operations; Cloud device routing is owned by the Connector."""

from pathlib import Path
import threading

from openmachine import execution, tasks, workspace
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault


class Machines:
    def __init__(self, config: Settings, state: Path):
        self.config = config
        self.state = state

    def _native_read(self, machine: str):
        selected = self.config.machine(machine)
        if selected.local_access is None:
            raise Fault("local_approval_required", "Explicit Read Only or Full Access is required for native reads.")
        return selected

    def inspect_environment(self, machine: str, workspace_name: str, section: str, worktree: str = "",
                            executables=None, ports=None, limit: int = 10) -> dict:
        self._native_read(machine)
        return execution.inspect_environment(self.config, machine, workspace_name=workspace_name,
            section=section, worktree=worktree, executables=executables, ports=ports, limit=limit)

    def inspect_workspace(self, machine: str, workspace_name: str, action: str, path: str = ".",
                          offset: int = 0, limit: int = 100, query: str = "", staged: bool = False,
                          expected_snapshot: str = "", path_glob: str = "", worktree: str = "") -> dict:
        self._native_read(machine)
        return workspace.inspect(self.config, machine, workspace_name, action, path, offset, limit,
            query=query, staged=staged, expected_snapshot=expected_snapshot, path_glob=path_glob,
            **({"worktree": worktree} if worktree else {}))

    def read_workspace_file(self, machine: str, workspace_name: str, path: str,
                            offset: int = 0, expected_sha256: str = "", start_line: int | None = None,
                            line_count: int = 200, worktree: str = "") -> dict:
        self._native_read(machine)
        config = workspace.select_worktree(self.config, machine, workspace_name, worktree)
        return workspace.read_text_file(config, machine, workspace_name, path, offset, expected_sha256,
                                        start_line, line_count)

    def write_workspace_file(self, machine: str, workspace_name: str, path: str, content: str,
                             expected_head_sha: str, expected_sha256: str, change_id: str = "",
                             expected_workspace_identity: str = "", worktree: str = "") -> dict:
        config = workspace.select_worktree(self.config, machine, workspace_name, worktree)
        return workspace.write_text_file(config, self.state, machine, workspace_name, path, content,
            expected_head_sha, expected_sha256, change_id, expected_workspace_identity,
            validate_new=lambda root: execution.check_workspace_leases(config, self.state, scope=root))

    def edit_workspace_file(self, machine: str, workspace_name: str, path: str, edits: list[dict],
                            expected_workspace_identity: str, expected_head_sha: str, expected_sha256: str,
                            change_id: str = "", dry_run: bool = False, expected_plan_sha256: str = "",
                            worktree: str = "") -> dict:
        arguments = dict(workspace=workspace_name, path=path, edits=edits,
                         expected_workspace_identity=expected_workspace_identity, expected_head_sha=expected_head_sha,
                         expected_sha256=expected_sha256, change_id=change_id, dry_run=dry_run,
                         expected_plan_sha256=expected_plan_sha256)
        config = workspace.select_worktree(self.config, machine, workspace_name, worktree)
        return workspace.edit_text_file(config, self.state, machine, **arguments,
            validate_new=lambda root: execution.check_workspace_leases(config, self.state, scope=root))

    def apply_workspace_patch(self, machine: str, workspace_name: str, patch: str,
                              expected_workspace_identity: str, expected_head_sha: str, preconditions: list[dict],
                              create_parents: bool = False, change_id: str = "", dry_run: bool = False,
                              expected_plan_sha256: str = "", worktree: str = "") -> dict:
        arguments = dict(workspace=workspace_name, patch_text=patch,
                         expected_workspace_identity=expected_workspace_identity, expected_head_sha=expected_head_sha,
                         preconditions=preconditions, create_parents=create_parents, change_id=change_id,
                         dry_run=dry_run, expected_plan_sha256=expected_plan_sha256)
        config = workspace.select_worktree(self.config, machine, workspace_name, worktree)
        return workspace.apply_patch(config, self.state, machine, **arguments,
            validate_new=lambda root: execution.check_workspace_leases(config, self.state, scope=root))

    def create_worktree(self, machine: str, workspace_name: str, destination: str, base_ref: str,
                        new_branch: str, execution_id: str, expected_workspace_identity: str,
                        worktree: str = "", task_id: str = "", *, task_admitted: bool = False) -> dict:
        return execution.create_worktree(self.config, self.state, machine,
            workspace_name=workspace_name, destination=destination, base_ref=base_ref,
            new_branch=new_branch, execution_id=execution_id, expected_workspace_identity=expected_workspace_identity,
            worktree=worktree, task_id=task_id,
            validate_new=(lambda: tasks.require_legacy(self.state, task_id)) if task_id and not task_admitted else None)

    def read_workspace_change(self, machine: str, change_id: str, observe_current: bool = False,
                              *, cancelled: threading.Event | None = None) -> dict:
        self._native_read(machine)
        return workspace.read_change(self.config, self.state, machine, change_id, observe_current)

    def start_execution(self, machine: str, execution_id: str, cwd: str, argv: list[str],
                        timeout_seconds: int = execution.DEFAULT_TIMEOUT, expected_head_sha: str = "",
                        workspace_access: str = "exclusive", resource_keys: list[str] | None = None,
                        task_id: str = "", termination_grace_seconds: float | None = None,
                        tty: bool = False, terminal_size: dict | None = None, *, task_admitted: bool = False) -> dict:
        self.config.machine(machine, write=True)
        return execution.start(self.config, self.state, machine, execution_id, cwd, argv, timeout_seconds,
            expected_head_sha, workspace_access, resource_keys, task_id, termination_grace_seconds, tty, terminal_size,
            validate_new=(lambda: tasks.require_legacy(self.state, task_id)) if task_id and not task_admitted else None)

    def read_execution(self, machine: str, execution_id: str, offset: int = 0,
                       max_bytes: int = execution.MAX_READ, wait_seconds: float = 0,
                       *, cancelled: threading.Event | None = None) -> dict:
        self._native_read(machine)
        execution.validate_read(offset, max_bytes, wait_seconds)
        return execution.read(self.config, self.state, machine, execution_id, offset, max_bytes,
                              wait_seconds, cancelled=cancelled)

    def list_executions(self, machine: str, cursor: str = "", limit: int = 20) -> dict:
        self._native_read(machine)
        return execution.list_records(self.config, self.state, machine, cursor, limit)

    def cancel_execution(self, machine: str, execution_id: str) -> dict:
        return execution.cancel(self.config, self.state, machine, execution_id)

    def control_execution(self, machine: str, execution_id: str, expected_control_seq: int,
                          action: str, text: str | None = None, terminal_size: dict | None = None) -> dict:
        arguments = execution.control_request(expected_control_seq, action, text, terminal_size)
        self.config.machine(machine, write=action in {"write_stdin", "resize"})
        return execution.control_execution(self.config, self.state, machine, execution_id, **arguments)
