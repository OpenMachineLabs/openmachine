"""MCP adapter for bounded environment observations."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from openmachine.mcp.protocol import _reply


def register_environment_tools(application, guided_tool, machine_read):
    @guided_tool(annotations=machine_read)
    def inspect_context(machine: str, workspace_name: str,
                        worktree: Annotated[str, Field(max_length=4096)] = "",
                        task_id: Annotated[str, Field(max_length=64)] = "",
                        sections: Annotated[list[Literal["workspace", "worktrees", "activity", "runtime"]] | None,
                                            Field(min_length=1, max_length=4)] = None,
                        limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 10) -> CallToolResult:
        """Observe the selected target and requested workspace/worktrees/activity/runtime sections. Defaults omit worktree enumeration. Activity needs an explicit task ID and indexes retained evidence, not live process occupancy. Follow source/next references, revisions and coverage; configured capabilities are not health. No authority, allocation, probe, replay or implicit instruction loading."""
        return _reply(lambda: application.inspect_context(machine, workspace_name, worktree, task_id, sections, limit))
