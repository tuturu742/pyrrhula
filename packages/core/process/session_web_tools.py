"""In-session ``web_search`` tool (per-persona internet search).

Registered into a generate turn's ``ToolRegistry`` only when BOTH gates hold: the acting
persona's ``web_search`` switch (who may search) and a registered ``web_search`` MCP server
on the workspace allowlist (whether this workspace may egress at all -- CLAUDE.md rule 11:
the allowlist, not D14, controls MCP egress). The call itself goes through
``core.mcp.client.call_tool`` -- allowlist re-derivation, injection envelope, action-record
audit -- with a synthesized single-tool phase spec, the same shape
``worker.delegation._delegation_phase`` established: the phase argument is the client's
required interface; the *policy* here is the persona switch + allowlist pair, not the
process definition (a definition author opts actors out by never enabling their switch,
not by listing search in every phase).
"""

from __future__ import annotations

import json
import uuid

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.mcp.client import ToolNotAvailableError
from core.mcp.client import call_tool as mcp_call_tool
from core.mcp.registry import list_servers
from core.ports.mcp import McpTransport, McpTransportError
from core.process.dsl.schema import PhaseSpec, VisibilitySpec
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope

WEB_SEARCH_SERVER_KEY = "web_search"
WEB_FETCH_SERVER_KEY = "web_fetch"


def _search_phase() -> PhaseSpec:
    return PhaseSpec(
        label_key="phase.turn",
        actors=[],
        visibility=VisibilitySpec(
            knowledge_classes=[],
            scopes=["workspace_public"],
            entity_fields="all",
            secrets="none",
        ),
        tools=["search"],
    )


async def workspace_has_web_search(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    return any(
        row.key == WEB_SEARCH_SERVER_KEY and "search" in row.enabled_tools
        for row in await list_servers(tenant_id, workspace_id)
    )


async def workspace_has_web_fetch(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
    return any(
        row.key == WEB_FETCH_SERVER_KEY and "fetch" in row.enabled_tools
        for row in await list_servers(tenant_id, workspace_id)
    )


def make_web_fetch_handler(*, workspace_id: uuid.UUID, transport: McpTransport) -> ToolHandler:
    """Read one page. Same shape as search: a reserved event_seq, the call recorded in
    `mcp_call_record`, and the page returned inside the untrusted-output envelope -- a
    fetched page is the least trustworthy text in a session, being chosen by the model
    and written by a stranger."""

    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(content=json.dumps({"error": "no_url"}))
        if ctx.session_id is None:
            return ToolResult(content=json.dumps({"error": "no_session"}))

        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            event_seq = row.next_event_seq
            if ctx.turn_event_seq is not None and event_seq <= ctx.turn_event_seq:
                event_seq = ctx.turn_event_seq + 1
            row.next_event_seq = event_seq + 1

        try:
            invocation = await mcp_call_tool(
                ctx.tenant_id,
                workspace_id,
                ctx.session_id,
                event_seq,
                _search_phase(),
                WEB_FETCH_SERVER_KEY,
                "fetch",
                {"url": url},
                transport=transport,
            )
        except (McpTransportError, ToolNotAvailableError) as exc:
            return ToolResult(
                content=json.dumps({"error": "fetch_failed", "message": str(exc)[:200]})
            )
        return ToolResult(content=invocation.envelope)

    return handler


def make_web_search_handler(*, workspace_id: uuid.UUID, transport: McpTransport) -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(content=json.dumps({"error": "empty_query"}))
        if ctx.session_id is None:
            return ToolResult(content=json.dumps({"error": "no_session"}))

        # A per-call event_seq reservation (same pattern as the the randomizer). Search is
        # read-only, so core.mcp.client's effectful ledger doesn't record it -- the turn's
        # tool_calls_made counter and the enveloped output in context are its trace.
        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            event_seq = row.next_event_seq
            if ctx.turn_event_seq is not None and event_seq <= ctx.turn_event_seq:
                # Never hand out the slot the surrounding turn already claimed for
                # its message (peeked before generation) -- uq_session_event_seq.
                event_seq = ctx.turn_event_seq + 1
            row.next_event_seq = event_seq + 1

        try:
            invocation = await mcp_call_tool(
                ctx.tenant_id,
                workspace_id,
                ctx.session_id,
                event_seq,
                _search_phase(),
                WEB_SEARCH_SERVER_KEY,
                "search",
                {"query": query, "recency": str(args.get("recency") or "")},
                transport=transport,
            )
        except (McpTransportError, ToolNotAvailableError) as exc:
            return ToolResult(
                content=json.dumps({"error": "search_failed", "message": str(exc)[:200]})
            )
        # The envelope marks the results as untrusted tool output, not instructions.
        return ToolResult(content=invocation.envelope)

    return handler
