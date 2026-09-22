"""The in-turn ``delegate_work_item`` tool: a supervisor hands its own plan to coding
agents without waiting for a human to press a button.

Registered into a turn's ``ToolRegistry`` (``core.process.live_session``) only when the
phase's ``remote_tools`` allows it AND the composition root supplied a ``JobQueue`` -- a
phase that asks for delegation in a process with no queue behind it gets no tool, rather
than a tool that silently drops work.

The tool ENQUEUES; it does not do the work. A coding agent needs an exec environment and
minutes, neither of which a model turn has, so this is the same dispatch the HTTP endpoint
performs (``core.actions.dispatch``) and the worker picks it up the same way. What the
model gets back is what was queued, so it can say what is in flight instead of guessing.
"""

from __future__ import annotations

import json
import uuid

from core.actions.dispatch import DispatchError, dispatch_work_items
from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.ports.job_queue import JobQueue

DELEGATE_TOOL_NAME = "delegate_work_item"

DELEGATE_TOOL_DESCRIPTION = (
    "Hand work items to coding agents. Each id becomes a real branch and pull request, "
    "built by an agent in its own environment -- this call is what makes them exist. "
    "Pass every item you want built in one call."
)

DELEGATE_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "work_item_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "uuids of the work items to build, one branch and PR each",
        },
        "auto_review": {
            "type": "boolean",
            "description": (
                "true (default) to have the session review each landed pull request and "
                "drive the change requests itself; false to leave review to a human."
            ),
        },
        "repo_id": {
            "type": "string",
            "description": (
                "which of the session's repos to build in. Required only when the session "
                "selected more than one."
            ),
        },
    },
    "required": ["work_item_ids"],
}


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def make_delegate_handler(*, workspace_id: uuid.UUID, queue: JobQueue) -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.session_id is None:
            return ToolResult(content=json.dumps({"error": "no_session"}))

        raw_ids = args.get("work_item_ids")
        # A model that has one item to delegate tends to pass the bare string rather than
        # a one-element list. Refusing that would be pedantry about JSON shape.
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list) or not raw_ids:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "missing_args",
                        "message": "work_item_ids must be a non-empty list of entity uuids",
                    }
                )
            )
        work_item_ids: list[uuid.UUID] = []
        bad: list[str] = []
        for raw in raw_ids:
            parsed = _uuid_or_none(raw)
            if parsed is None:
                bad.append(str(raw))
            else:
                work_item_ids.append(parsed)
        if bad:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "invalid",
                        "message": (
                            "these are not entity uuids: "
                            + ", ".join(bad[:5])
                            + ". Use the id each entity_create call returned."
                        ),
                    }
                )
            )

        raw_auto = args.get("auto_review")
        auto_review = True if raw_auto is None else bool(raw_auto)

        try:
            result = await dispatch_work_items(
                ctx.tenant_id,
                workspace_id,
                ctx.session_id,
                work_item_ids,
                queue=queue,
                repo_id=_uuid_or_none(args.get("repo_id")),
                auto_review=auto_review,
            )
        except DispatchError as exc:
            return ToolResult(content=json.dumps({"error": "refused", "message": str(exc)}))
        except Exception as exc:  # report so the model can say what failed, not invent success
            return ToolResult(content=json.dumps({"error": "failed", "message": str(exc)[:300]}))

        return ToolResult(
            content=json.dumps(
                {
                    "delegated": len(result.job_ids),
                    "jobs": result.job_ids,
                    "repo": result.server_key,
                    "assignees": [
                        {"work_item_id": str(wid), "assignee": name or "(to be chosen)"}
                        for wid, _pid, name in result.assignments
                    ],
                    "unresolved_assignee_names": result.unresolved_names,
                    "message": (
                        f"{len(result.job_ids)} work item(s) queued. Each is being built now "
                        "in its own branch; the pull requests appear as the agents finish."
                    ),
                }
            )
        )

    return handler
