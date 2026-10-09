"""MCP GitHub and account connection registration; Application retains authority."""

import asyncio
from typing import Annotated, Literal
from mcp.types import CallToolResult
from pydantic import Field
from openmachine import tasks
from openmachine.mcp.protocol import (READ, WRITE, LOCAL_WRITE, GITHUB_READ, GITHUB_WRITE,
                                      GitHubNames, _reply, _submit_owned, _task_context)

def register_connections(application, guided_tool):
    @guided_tool(annotations=READ)
    def list_integrations(provider: Annotated[str, Field(max_length=64)] = "",
                          connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """List installed connection methods and retained accounts without network access or state writes. Saved connection status is not live provider health."""
        return _reply(lambda: application.list_integrations(provider, connection_id))

    @guided_tool(annotations=WRITE)
    async def connect_integration(request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                  provider: Annotated[str, Field(max_length=64)] = "github",
                                  method: Annotated[str, Field(max_length=64)] = "device_code",
                                  connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
        """Start native GitHub Device Flow owned by this runtime. Return only the public verification URL/code; credentials never enter tool results. Preserve request_id after uncertainty."""
        operation = lambda: _reply(lambda: application.connect_integration(request_id, provider, method, connection_id))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=READ)
    def read_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Read the original login result without restarting it. A runtime restart interrupts unfinished Device Flow but preserves established connections."""
        return _reply(lambda: application.read_integration_login(login_id))

    @guided_tool(annotations=LOCAL_WRITE)
    def cancel_integration_login(login_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]) -> CallToolResult:
        """Cancel one unfinished native login. This does not disconnect an established account or undo provider-side consent."""
        return _reply(lambda: application.cancel_integration_login(login_id))

    @guided_tool(annotations=LOCAL_WRITE)
    async def disconnect_integration(connection_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")],
                                     expected_revision: Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]) -> CallToolResult:
        """Disconnect only this saved OM connection using its observed authorization revision. Native credentials are removed from their pinned local store; remote GitHub grants are not claimed revoked."""
        operation = lambda: _reply(lambda: application.disconnect_integration(connection_id, expected_revision))
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))


def register_github_tools(application, config, guided_tool):
    if config.github is not None:
        github = application.github
        @guided_tool(annotations=GITHUB_READ)
        def github_read(action: github.ReadAction, repository: str = "", number: Annotated[int, Field(ge=0)] = 0,
                        ref: str = "", path: str = "", page: Annotated[int, Field(ge=1, le=1000)] = 1,
                        limit: Annotated[int, Field(ge=1, le=100)] = 30, cursor: Annotated[str, Field(max_length=1024)] = "",
                        expected_head_sha: str = "", expected_base_sha: str = "",
                        offset: Annotated[int, Field(ge=0, le=1048576)] = 0, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Read GitHub evidence, including organization Projects, Releases by tag and document history. Project lists use cursors; REST lists use page. Historical file content is fixed-SHA. No arbitrary endpoint or mutation dispatch."""
            return _reply(lambda: application.github_read(action, repository, number, ref, path, page, limit, cursor, expected_head_sha, expected_base_sha, offset, connection_id=connection_id))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_project(action: Literal["add_item", "update_field"], repository: str,
                           project_number: Annotated[int, Field(ge=1)],
                           issue_number: Annotated[int, Field(ge=0)] = 0, item_id: str = "",
                           field_id: str = "", value: dict | None = None,
                           expected_value: dict | None = None,
                           task_context: tasks.TaskContext | None = None,
                           connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Add an Issue to a Project or change one Project item field. Read Projects first for node IDs. Writes verify readback; never replay uncertain mutations."""
            return _reply(lambda: application.github_project(action, repository, project_number,
                issue_number, item_id, field_id, value, expected_value,
                _task_context(task_context), connection_id))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_issue(action: Literal["create", "update"], repository: str,
                         title: Annotated[str | None, Field(max_length=256)] = None,
                         body: Annotated[str | None, Field(max_length=60000)] = None,
                         number: Annotated[int, Field(ge=0)] = 0,
                         state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                         labels: GitHubNames | None = None, assignees: GitHubNames | None = None,
                         task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                         expected_fields: dict | None = None) -> CallToolResult:
            """Create/update an authorized Issue with readback. labels/assignees replace complete sets; omit to preserve, [] to clear. state is update-only. Update expected_fields must equal the observed values of every replaced field and no others; expected_updated_at adds a whole-object guard. Checks are sequential, not atomic CAS. Creation is non-idempotent: reconcile the original destination after uncertainty."""
            return _reply(lambda: application.github_issue(action, repository, title, body, number, state, expected_updated_at, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_pull_request(action: Literal["create", "update", "ready_for_review"], repository: str,
                                title: Annotated[str | None, Field(max_length=256)] = None,
                                body: Annotated[str | None, Field(max_length=60000)] = None,
                                number: Annotated[int, Field(ge=0)] = 0,
                                head: str = "", base: str = "", draft: bool = False,
                                state: Literal["open", "closed"] | None = None, expected_updated_at: str = "",
                                expected_head_sha: str = "", labels: GitHubNames | None = None,
                                assignees: GitHubNames | None = None, task_context: tasks.TaskContext | None = None,
                                connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                                expected_fields: dict | None = None) -> CallToolResult:
            """Create/update a scoped PR or mark an observed Draft ready. ready_for_review requires exact HEAD and accepts only number, optional draft/updated_at guards and connection identity; concurrent pushes can precede it and changed readback stays unverified. Updates replace complete labels/assignees sets (omit preserves, [] clears); keep metadata separate from title/body/state. expected_fields must cover exactly replaced observed fields; ready uses only draft. expected_updated_at is an extra guard; none is atomic CAS. Creation accepts neither metadata nor expected_fields and uses an existing same-repository branch. Reconcile the original destination after uncertainty; never blindly retry."""
            return _reply(lambda: application.github_pull_request(action, repository, title, body, number, head, base, draft, state, expected_updated_at, expected_head_sha, labels, assignees, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_comment(action: Literal["create", "update"], repository: str,
                           body: Annotated[str, Field(max_length=60000)], number: Annotated[int, Field(ge=0)] = 0,
                           comment_id: Annotated[int, Field(ge=0)] = 0, expected_updated_at: str = "",
                           task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                           expected_fields: dict | None = None) -> CallToolResult:
            """Create/update a top-level Issue/PR comment and verify its fields. Inline comments are outside this writer. Preserve the original comment identity after uncertainty; do not post it again. On update, expected_fields contains only the observed body value. Supplied expected_updated_at remains an additional whole-object guard. These detect observed edit conflicts without promising atomic CAS."""
            return _reply(lambda: application.github_comment(action, repository, body, number, comment_id, expected_updated_at, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_review(action: Literal["create", "update"], repository: str,
                          body: Annotated[str, Field(max_length=60000)],
                          number: Annotated[int, Field(ge=0)] = 0, review_id: Annotated[int, Field(ge=0)] = 0,
                          expected_head_sha: str = "",
                          event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT",
                          task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                          expected_fields: dict | None = None) -> CallToolResult:
            """Submit an exact-HEAD review summary after reading the complete relevant diff and discussions, or update its body. Missing gate evidence rejects creation; known failures can still be reviewed. Self-review must use COMMENT and is not independent approval. On update, expected_fields contains only the observed body value; this is a sequential precondition, not atomic CAS. Uncertain writes require original-review readback, never blind retry."""
            return _reply(lambda: application.github_review(action, repository, body, number, review_id, expected_head_sha, event, _task_context(task_context), connection_id=connection_id, expected_fields=expected_fields))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_review_thread(action: Literal["resolve", "unresolve"], repository: str,
                                 number: Annotated[int, Field(ge=1)],
                                 thread_id: Annotated[str, Field(pattern=r"^PRRT_[A-Za-z0-9_-]{1,128}$")],
                                 expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                                 expected_base_sha: str = "",
                                 task_context: tasks.TaskContext | None = None,
                                 connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Resolve or unresolve one verified review thread on the specified PR and exact HEAD. Verify its membership, state and account before and after mutation; never replay uncertain results."""
            return _reply(lambda: application.github_review_thread(action, repository, number, thread_id, expected_head_sha, expected_base_sha, _task_context(task_context), connection_id=connection_id))

        @guided_tool(annotations=GITHUB_WRITE)
        def github_merge(repository: str, number: Annotated[int, Field(ge=1)],
                         expected_head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")],
                         expected_base_sha: str = "", method: Literal["squash", "merge", "rebase"] = "squash",
                         commit_title: Annotated[str, Field(max_length=256)] = "",
                         commit_message: Annotated[str, Field(max_length=60000)] = "",
                         task_context: tasks.TaskContext | None = None, connection_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "") -> CallToolResult:
            """Merge only an explicitly authorized, current-HEAD reviewed PR with complete checks/discussions and clean native policy. No bypass, auto-merge or queue support. Bind both observed SHAs when available. Only merged_verified confirms the actual commit on the destination branch; pending/unknown results require readback, not resubmission."""
            return _reply(lambda: application.github_merge(repository, number, expected_head_sha, expected_base_sha, method, commit_title, commit_message, _task_context(task_context), connection_id=connection_id))
