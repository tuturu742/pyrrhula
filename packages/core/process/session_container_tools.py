"""The in-turn ``container_activity`` tool: what ran under this persona's name, and what
it cost.

Delegated work is asynchronous -- the phase waits on ``await: delegated_work`` while a
container runs for minutes -- so a persona learns about its own work on a *later* turn.
The bounded summary note (``core.harness.events``) puts the headline into its own history
automatically; this answers the follow-ups: which environment ran, did it end cleanly,
how much has my connection spent on delegated work.

Scoped to the caller, and enforced rather than requested. ``ToolContext.persona_id`` is
the acting persona, so "mine" is a SQL predicate on
``exec_environment.spawned_by_persona_id`` and ``usage_record.persona_id``, not a promise
in a prompt. What every *other* container is doing is the supervisor's view -- the
Director's View and the usage dashboards -- not an ambient fact in each participant's
context.

Answered entirely from the database on purpose. The per-pull-request detail (the test
output, the harness's step list) lives in the git store's record, which core cannot reach
and should not: it is already delivered where it is needed, to the reviewer and to the
rework brief. A tool that duplicated it would be a second copy to keep true.

Deliberately a core tool rather than un-reserving the git MCP server: un-reserving would
also hand the model ``delegate_work_item`` as a direct MCP call, which the design routes
through a queue instead.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.audit.models import UsageRecordRow
from core.exec_envs import ExecEnvironmentRow
from core.tenancy.scope import tenant_scope

CONTAINER_ACTIVITY_TOOL_NAME = "container_activity"

CONTAINER_ACTIVITY_DESCRIPTION = (
    "What has run in the execution environments assigned to you: which ones, whether "
    "they finished cleanly, and what your delegated work has cost so far. Use it when "
    "you need to account for work you handed to a container."
)

CONTAINER_ACTIVITY_PARAMETERS: dict[str, object] = {"type": "object", "properties": {}}

# The newest handful. A long session accumulates environments, and the ones a turn is
# asking about are the recent ones -- an unbounded answer would crowd out the conversation
# it is meant to inform.
_MAX_ENVIRONMENTS = 8


def make_container_activity_handler() -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        async with tenant_scope(ctx.tenant_id) as session:
            query = (
                select(ExecEnvironmentRow)
                .where(
                    ExecEnvironmentRow.tenant_id == ctx.tenant_id,
                    # The scoping. Not a request to the model to only ask about its own.
                    ExecEnvironmentRow.spawned_by_persona_id == ctx.persona_id,
                )
                .order_by(ExecEnvironmentRow.updated_at.desc())
                .limit(_MAX_ENVIRONMENTS)
            )
            if ctx.session_id is not None:
                query = query.where(ExecEnvironmentRow.session_id == ctx.session_id)
            environments = list((await session.execute(query)).scalars())

            prompt_tokens, completion_tokens = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(UsageRecordRow.prompt_tokens), 0),
                        func.coalesce(func.sum(UsageRecordRow.completion_tokens), 0),
                    ).where(
                        UsageRecordRow.tenant_id == ctx.tenant_id,
                        UsageRecordRow.persona_id == ctx.persona_id,
                        UsageRecordRow.purpose == "delegation",
                    )
                )
            ).one()

        payload: dict[str, Any] = {
            "environments": [
                {
                    "environment": row.name,
                    "image": row.image,
                    "status": row.status,
                    "last_exit_code": row.last_exit_code,
                    "engine": row.engine_key,
                }
                for row in environments
            ],
            "delegated_spend": {
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
            },
        }
        if not environments:
            # Said plainly, because "nothing" has two very different causes and a model
            # that cannot tell them apart will pick one and state it with confidence.
            payload["note"] = (
                "no execution environment has run under your name in this session. "
                "Work you delegated may still be in progress."
            )
        return ToolResult(content=json.dumps(payload))

    return handler
