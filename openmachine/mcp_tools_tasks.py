"""MCP Task tool registration; Application owns all business decisions."""

import asyncio
from functools import partial
import threading
from typing import Annotated, Literal
from mcp.types import CallToolResult
from pydantic import Field
from openmachine import tasks
from openmachine.mcp_protocol import (READ, WRITE, LOCAL_WRITE, MAX_TASK_WAIT,
                                      _reply, _result, _submit_owned)

def register_task_tools(application, guided_tool, waiting):
    @guided_tool(annotations=WRITE)
    def create_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                    update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                    title: Annotated[str, Field(min_length=1, max_length=200)],
                    anchors: Annotated[list[str], Field(max_length=16)], origin: tasks.Origin, contract: tasks.Contract,
                    stages: Annotated[list[tasks.StageDefinition], Field(min_length=1, max_length=16)]) -> CallToolResult:
        """Create an authorized structured task; preserve readable instructions and inputs with correctly hashed stage sources. Preserve task/update IDs after uncertainty; committed creation is not business permission. Explicit claim/current task_context remain required. Never convert legacy history or supply runtime facts."""
        return _reply(lambda: application.create_task(task_id, update_id, title, anchors, origin.model_dump(),
                      contract.model_dump(), [stage.model_dump() for stage in stages]), task_output=True)

    @guided_tool(annotations=WRITE)
    async def control_task(task_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                           expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_plan_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                           expected_epoch: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)],
                           update_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")],
                           transition: tasks.Transition) -> CallToolResult:
        """Control the original task using exact task/plan/epoch revisions; retain update_id after uncertainty. Renew the original claim_id/principal/epoch. Expired authority requires a revised contract with fresh evidence. bind_effect_scope needs original normalized arguments and never resolves an unknown effect. Claims grant no permission. Pause blocks new admission; cancellation preserves admitted/unknown effects. Finish requires action, outcome, acceptance references to accepted original evidence and limitations. Inspect returned state and observation blockers."""
        operation = partial(_reply, lambda: application.control_task(task_id, expected_revision,
            expected_plan_revision, expected_epoch, update_id, transition.model_dump()), task_output=True)
        return await asyncio.shield(asyncio.wrap_future(_submit_owned(operation)))

    @guided_tool(annotations=READ)
    def find_tasks(query: Annotated[str, Field(max_length=200)] = "", anchors: Annotated[list[str], Field(max_length=16)] = [],
                   task_id: str = "", offset: Annotated[int, Field(ge=0)] = 0,
                   limit: Annotated[int, Field(ge=1, le=50)] = 20,
                   expected_generation: Annotated[int, Field(ge=-1)] = -1) -> CallToolResult:
        """Find private checkpoints by exact task/anchors or title/ID keywords. Anchor filters intersect; descriptive results are candidates, never continuation authority. Follow pagination/generation. Reads do not initialize state or start work; missing/corrupt history is explicit. resume_authorized=false means this lookup grants no authority, not that current user authorization is denied."""
        return _reply(lambda: application.find_tasks(query, anchors, task_id, offset, limit, expected_generation), task_output=True)

    @guided_tool(annotations=READ)
    def read_task(task_id: str,
                  section: Literal["summary", "operations", "history", "evidence"] = "summary",
                  cursor: Annotated[str, Field(max_length=1024)] = "",
                  limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20,
                  operation_id: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{32})?$")] = "",
                  stage_id: Annotated[str, Field(max_length=64, pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$")] = "",
                  decision_version: Annotated[int, Field(strict=True, ge=0, le=9007199254740991)] = 0) -> CallToolResult:
        """Read schema v2 current state or a bounded operations/history/evidence page. Summary omits cumulative history; follow complete/has_more, cursor and revision watermarks. Use the exact original operation_id, or an evidence_ref's stage_id and decision_version together on evidence without cursor/operation_id. Reads never initialize, repair, claim or execute work."""
        return _reply(lambda: application.read_task(task_id, section=section,
                      cursor=cursor, limit=limit, operation_id=operation_id,
                      stage_id=stage_id, decision_version=decision_version), task_output=True)

    @guided_tool(annotations=READ)
    async def wait_task(task_id: Annotated[str, Field(min_length=1, max_length=64,
                        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")],
                        expected_revision: Annotated[int, Field(strict=True, ge=1, le=9007199254740991)],
                        wait_seconds: Annotated[float, Field(strict=True, ge=0, le=MAX_TASK_WAIT,
                        allow_inf_nan=False)] = MAX_TASK_WAIT) -> CallToolResult:
        """Wait once for bounded structured-task progress. Renewal required returns immediately for guarded same-claim renewal without ending the turn. Attention or wait expiry sets foreground.stop_foreground and requires ending the turn. This read never claims, renews, retries, cancels, or extends original work."""
        if wait_seconds and not waiting.acquire(blocking=False):
            return _result({"error": {"code": "reader_busy",
                "message": "At most 16 bounded waits are admitted; immediate task reads and cancellation remain available."}}, True)
        cancelled = threading.Event()
        operation = partial(_reply, lambda: application.wait_task(
            task_id, expected_revision, wait_seconds, cancelled=cancelled), task_output=True)
        try:
            if wait_seconds:
                try:
                    future = _submit_owned(operation)
                except BaseException:
                    waiting.release()
                    raise
                future.add_done_callback(lambda _: waiting.release())
                return await asyncio.wrap_future(future)
            return await asyncio.to_thread(operation)
        finally:
            cancelled.set()

    @guided_tool(annotations=LOCAL_WRITE)
    def save_task(task_id: str, expected_revision: Annotated[int, Field(ge=0)],
                  title: Annotated[str, Field(min_length=1, max_length=200)],
                  body: Annotated[str, Field(min_length=1, max_length=32768)],
                  anchors: Annotated[list[str], Field(max_length=16)] = []) -> CallToolResult:
        """Save an existing legacy workflow, not a parallel structured-task checkpoint or conversion. Revision zero creates; updates require observed revision. Identical last-write retries are no-ops; stale/corrupt state is not overwritten. Anchors are additive. Retain authority/evidence/next action, never credentials or hidden reasoning. Torn saves stay explicit; saving does not execute, publish or resume work."""
        return _reply(lambda: application.save_task(task_id, expected_revision, title, body, anchors), task_output=True)
