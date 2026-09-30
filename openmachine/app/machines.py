"""Configured machine routing and controller-owned execution task references."""

from collections.abc import Callable
from pathlib import Path

from openmachine import execution, tasks, workspace
from openmachine.config import Settings
from openmachine.foundation.sanitize import Fault


class Machines:
    def __init__(self, config: Settings, state: Path, ssh_call: Callable[..., dict] | None):
        self.config = config
        self.state = state
        self.ssh_call = ssh_call

    def _ssh(self, machine: str, operation: str, **arguments) -> dict:
        if self.ssh_call is None:
            raise Fault("not_ssh", "Select a configured SSH target, or omit the explicit doctor target.")
        return self.ssh_call(self.config, machine, operation, **arguments)

    def probe(self, machine: str) -> dict:
        self.config.machine(machine)
        return self._ssh(machine, "probe")

    def inspect_workspace(self, machine: str, workspace_name: str, action: str, path: str = ".",
                          offset: int = 0, limit: int = 100, query: str = "", staged: bool = False,
                          expected_snapshot: str = "") -> dict:
        if self.config.machine(machine).transport == "ssh":
            return self._ssh(machine, "inspect_workspace", workspace=workspace_name, action=action,
                             path=path, offset=offset, limit=limit, query=query, staged=staged,
                             expected_snapshot=expected_snapshot)
        return workspace.inspect(self.config, machine, workspace_name, action, path, offset, limit,
                                 query=query, staged=staged, expected_snapshot=expected_snapshot)

    def read_workspace_file(self, machine: str, workspace_name: str, path: str,
                            offset: int = 0, expected_sha256: str = "") -> dict:
        if self.config.machine(machine).transport == "ssh":
            return self._ssh(machine, "read_workspace_file", workspace=workspace_name, path=path,
                             offset=offset, expected_sha256=expected_sha256)
        return workspace.read_text_file(self.config, machine, workspace_name, path, offset, expected_sha256)

    def write_workspace_file(self, machine: str, workspace_name: str, path: str, content: str,
                             expected_head_sha: str, expected_sha256: str) -> dict:
        if self.config.machine(machine, write=True).transport == "ssh":
            return self._ssh(machine, "write_workspace_file", workspace=workspace_name, path=path,
                             content=content, expected_head_sha=expected_head_sha, expected_sha256=expected_sha256)
        return workspace.write_text_file(self.config, self.state, machine, workspace_name, path, content,
                                         expected_head_sha, expected_sha256)

    def start_execution(self, machine: str, execution_id: str, cwd: str, argv: list[str],
                        timeout_seconds: int = execution.DEFAULT_TIMEOUT, expected_head_sha: str = "",
                        workspace_access: str = "exclusive", resource_keys: list[str] | None = None,
                        task_id: str = "") -> dict:
        target = self.config.machine(machine, write=True)
        if target.transport == "ssh":
            keys = execution.validate_start(cwd, argv, timeout_seconds, expected_head_sha,
                                            workspace_access, resource_keys, task_id)
            if task_id:
                # A pre-read only decides whether a new controller reference is needed.
                # The target start must still compare the original normalized request.
                try:
                    self._ssh(machine, "read_execution", execution_id=execution_id)
                except Fault as error:
                    if error.code != "execution_not_found":
                        raise
                    tasks.read(self.state, task_id)
            return self._ssh(machine, "start_execution", execution_id=execution_id, cwd=cwd, argv=argv,
                             timeout_seconds=timeout_seconds, expected_head_sha=expected_head_sha,
                             workspace_access=workspace_access, resource_keys=keys, task_id=task_id)
        return execution.start(self.config, self.state, machine, execution_id, cwd, argv, timeout_seconds,
                               expected_head_sha, workspace_access, resource_keys, task_id,
                               validate_new=(lambda: tasks.read(self.state, task_id)) if task_id else None)

    def read_execution(self, machine: str, execution_id: str, offset: int = 0,
                       max_bytes: int = execution.MAX_READ) -> dict:
        if self.config.machine(machine).transport == "ssh":
            return self._ssh(machine, "read_execution", execution_id=execution_id, offset=offset, max_bytes=max_bytes)
        return execution.read(self.config, self.state, machine, execution_id, offset, max_bytes)

    def list_executions(self, machine: str, cursor: str = "", limit: int = 20) -> dict:
        if self.config.machine(machine).transport == "ssh":
            return self._ssh(machine, "list_executions", cursor=cursor, limit=limit)
        return execution.list_records(self.config, self.state, machine, cursor, limit)

    def cancel_execution(self, machine: str, execution_id: str) -> dict:
        if self.config.machine(machine).transport == "ssh":
            return self._ssh(machine, "cancel_execution", execution_id=execution_id)
        return execution.cancel(self.config, self.state, machine, execution_id)
