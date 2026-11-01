"""Thin MCP adapter for the optional local project catalog."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from openmachine.mcp.protocol import READ, _reply


def register_project_tools(application, guided_tool):
    @guided_tool(annotations=READ)
    def inspect_projects(machine: str, workspace_name: str = "",
                         action: Literal["list", "inspect"] = "list",
                         path: Annotated[str, Field(max_length=1024)] = ".",
                         offset: Annotated[int, Field(strict=True, ge=0, le=2000)] = 0,
                         limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 50,
                         expected_snapshot: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "",
                         expected_target_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{64})?$")] = "") -> CallToolResult:
        """List configured local workspaces or their direct child directories, without file contents. Inspect one relative directory to select it; echo its target_id as expected_target_id to revalidate. IDs exclude branch/HEAD and grant no access. Echo snapshot when paging. No SSH fallback, recursive scan, configuration writes or automatic registration."""
        return _reply(lambda: application.inspect_projects(machine, workspace_name, action, path, offset,
                                                           limit, expected_snapshot, expected_target_id))
