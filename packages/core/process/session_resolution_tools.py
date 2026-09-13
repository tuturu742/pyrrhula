"""In-turn handlers for the ``resolution`` MCP preset: the tenant's registered
deterministic tools, callable by models mid-turn.

Mirrors ``session_web_tools`` exactly: gated by the workspace's registered
``resolution`` server (the workflow's capability declaration -- the egress/policy
control), each exposed tool becomes a native handler that reserves an event seq and
routes through ``core.mcp.client.call_tool`` (allowlist + phase authorization), which
reaches the resolution transport. The handler injects the trusted ``_context``
(session, seq, actor) -- and strips any model-supplied one first: context is
system-owned, and arguments are attacker-influenced.
"""

from __future__ import annotations

import json
import uuid

from core.agents.models import Persona
from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.mcp.client import ToolNotAvailableError, available_tools
from core.mcp.client import call_tool as mcp_call_tool
from core.mcp.registry import list_servers
from core.ports.mcp import McpTransport, McpTransportError
from core.ports.model_provider import ToolSpec
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope

RESOLUTION_SERVER_KEY = "resolution"


def _resolution_phase(tool_names: list[str]) -> PhaseSpec:
    """A minimal phase authorizing exactly these tools for the envelope's re-check."""
    return PhaseSpec(
        label_key="phase.turn",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=1),
        tools=list(tool_names),
    )


async def resolution_tools_for_workspace(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, transport: McpTransport
) -> list[ToolSpec]:
    """The workspace's allowed resolution tools as native specs (empty when the
    workflow declares no resolution server -- the common case outside game-like
    workflows)."""
    rows = await list_servers(tenant_id, workspace_id)
    if not any(r.key == RESOLUTION_SERVER_KEY and r.enabled_tools for r in rows):
        return []
    try:
        allowed = await available_tools(
            tenant_id,
            workspace_id,
            _resolution_phase(
                [t for r in rows if r.key == RESOLUTION_SERVER_KEY for t in r.enabled_tools]
            ),
            transport=transport,
        )
    except McpTransportError:
        return []
    return [
        ToolSpec(
            name=t.spec.name,
            description=t.spec.description,
            parameters=t.spec.parameters,
        )
        for t in allowed
        if t.server_key == RESOLUTION_SERVER_KEY
    ]


def make_resolution_handler(
    *,
    workspace_id: uuid.UUID,
    tool_name: str,
    all_tool_names: list[str],
    transport: McpTransport,
) -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.session_id is None:
            return ToolResult(content=json.dumps({"error": "no_session"}))

        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            event_seq = row.next_event_seq
            if ctx.turn_event_seq is not None and event_seq <= ctx.turn_event_seq:
                # Never hand out the slot the surrounding turn already claimed for
                # its message (peeked before generation) -- uq_session_event_seq.
                event_seq = ctx.turn_event_seq + 1
            row.next_event_seq = event_seq + 1
            # The acting PRINCIPAL, not the persona id -- permission checks and audit
            # attribution key on the principal (same resolution the entity handlers do).
            persona = await session.get(Persona, ctx.persona_id)
            principal_id = persona.principal_id if persona is not None else ctx.persona_id

        arguments = {k: v for k, v in args.items() if k != "_context"}
        arguments["_context"] = {
            "session_id": str(ctx.session_id),
            "workspace_id": str(workspace_id),
            "event_seq": event_seq,
            "principal_id": str(principal_id),
        }
        try:
            invocation = await mcp_call_tool(
                ctx.tenant_id,
                workspace_id,
                ctx.session_id,
                event_seq,
                _resolution_phase(all_tool_names),
                RESOLUTION_SERVER_KEY,
                tool_name,
                arguments,
                transport=transport,
            )
        except (McpTransportError, ToolNotAvailableError) as exc:
            return ToolResult(
                content=json.dumps({"error": "resolution_failed", "message": str(exc)[:200]})
            )
        return ToolResult(content=invocation.envelope)

    return handler
