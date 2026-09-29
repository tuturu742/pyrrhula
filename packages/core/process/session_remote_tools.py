"""In-turn handlers for REMOTE MCP servers (M-C): every registered non-preset server's
allowed tools become native tools of the persona turn.

Mirrors ``session_resolution_tools`` exactly -- workspace registration is the policy
gate, each exposed tool routes through ``core.mcp.client.call_tool`` (allowlist +
effectful-confirmation + idempotency + injection envelope), and each call reserves its
own event seq PAST the turn's claimed slot. Differences from the resolution preset:

- no trusted ``_context`` is injected -- an external server gets exactly the model's
  arguments (minus any ``_context`` the model tried to smuggle in);
- an effectful tool whose registration keeps ``require_confirmation`` on cannot be
  auto-confirmed from inside a model turn: the ConfirmationRequiredError comes back to
  the model as an error result naming the operator action, never a silent bypass.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.mcp.client import (
    ConfirmationRequiredError,
    SessionCallCapError,
    ToolNotAvailableError,
    available_tools,
)
from core.mcp.client import call_tool as mcp_call_tool
from core.mcp.registry import list_servers
from core.ports.mcp import McpTransport, McpTransportError
from core.ports.model_provider import ToolSpec
from core.process.dsl.schema import BudgetSpec, PhaseSpec, VisibilitySpec
from core.sessions.models import SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope

OnEvent = Callable[[int, str, dict[str, Any]], Awaitable[None]]

# Keys served by dedicated transports/handlers -- never surfaced through this module.
# `web_fetch` belongs here for the same reason `web_search` does, and leaving it out was a
# hole in the two-switch egress design rather than a cosmetic omission: this path gates on
# workspace registration ALONE, so a registered web_fetch server handed a `fetch` tool to
# every persona in the workspace -- including the ones whose own `web_search` flag is off,
# which is the switch that is supposed to say "this seat does not call out". Observed in
# the newsroom sample, where the chief editor is deliberately offline and was offered a
# fetch tool anyway, while the desks were offered two tools that do the same thing.
_RESERVED_KEYS = {"web_search", "web_fetch", "resolution", "git"}


def _is_remote(key: str, url: str) -> bool:
    if key in _RESERVED_KEYS or key.startswith("git-"):
        return False
    return url.startswith(("http://", "https://"))


def _remote_phase(tool_names: list[str]) -> PhaseSpec:
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


async def remote_tools_for_workspace(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, *, transport: McpTransport
) -> list[tuple[str, ToolSpec]]:
    """(server_key, spec) for every allowed tool of every registered remote server.
    Empty for workspaces with none registered -- the common case. A server that is
    down contributes nothing rather than failing the turn."""
    rows = [r for r in await list_servers(tenant_id, workspace_id) if _is_remote(r.key, r.url)]
    if not rows:
        return []
    union = sorted({t for r in rows for t in r.enabled_tools})
    if not union:
        return []
    try:
        allowed = await available_tools(
            tenant_id, workspace_id, _remote_phase(union), transport=transport
        )
    except McpTransportError:
        return []
    remote_keys = {r.key for r in rows}
    out: list[tuple[str, ToolSpec]] = []
    seen: set[str] = set()
    for tool in allowed:
        if tool.server_key not in remote_keys or tool.spec.name in seen:
            continue
        seen.add(tool.spec.name)
        out.append(
            (
                tool.server_key,
                ToolSpec(
                    name=tool.spec.name,
                    description=tool.spec.description,
                    parameters=tool.spec.parameters,
                ),
            )
        )
    return out


# How much of a tool's answer the transcript keeps. The model saw the whole thing (up to
# the server's own result cap); the event is for a person reading along, and a page of
# search results in the middle of a conversation is not reading along.
_EVENT_RESULT_CHARS = 2000


async def _record_tool_call_event(
    ctx: ToolContext,
    event_seq: int,
    payload: dict[str, object],
    on_event: OnEvent | None,
) -> None:
    """The transcript's own account of a remote tool call, at the seq the handler
    reserved for it.

    Until this existed a call to a registered MCP server left the transcript exactly one
    trace: a `tool_calls_made` count on the message that followed. The request the model
    made and what came back lived only in the idempotency ledger, which has no reader --
    so a lab answer the inspector narrated could not be checked against what the lab said,
    and a refused or failed call looked the same as a successful one. This is a durable
    `session_event`, so it replays on reconnect and renders inline like a phase change.
    """
    assert ctx.session_id is not None
    async with tenant_scope(ctx.tenant_id) as session:
        session.add(
            SessionEventRow(
                tenant_id=ctx.tenant_id,
                session_id=ctx.session_id,
                event_seq=event_seq,
                kind="tool_call",
                payload=payload,
            )
        )
    if on_event is not None:
        await on_event(event_seq, "tool_call", payload)


def make_remote_tool_handler(
    *,
    workspace_id: uuid.UUID,
    server_key: str,
    tool_name: str,
    all_tool_names: list[str],
    transport: McpTransport,
    on_event: OnEvent | None = None,
    author: str = "",
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

        arguments = {k: v for k, v in args.items() if k != "_context"}
        event: dict[str, object] = {
            "server_key": server_key,
            "tool_name": tool_name,
            "arguments": arguments,
            "author": author,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            invocation = await mcp_call_tool(
                ctx.tenant_id,
                workspace_id,
                ctx.session_id,
                event_seq,
                _remote_phase(all_tool_names),
                server_key,
                tool_name,
                arguments,
                transport=transport,
            )
        except SessionCallCapError as exc:
            # The model gets a plain, honest refusal it can reason about -- the same
            # shape the sample lab's own budget message takes -- rather than a failure.
            await _record_tool_call_event(
                ctx, event_seq, {**event, "outcome": "refused", "message": str(exc)}, on_event
            )
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "session_call_cap_reached",
                        "message": str(exc),
                    }
                )
            )
        except ConfirmationRequiredError:
            message = (
                f"tool {tool_name!r} is effectful and its registration requires "
                "operator confirmation; it cannot be auto-invoked from a model turn"
            )
            await _record_tool_call_event(
                ctx, event_seq, {**event, "outcome": "refused", "message": message}, on_event
            )
            return ToolResult(
                content=json.dumps({"error": "confirmation_required", "message": message})
            )
        except (McpTransportError, ToolNotAvailableError) as exc:
            await _record_tool_call_event(
                ctx, event_seq, {**event, "outcome": "failed", "message": str(exc)[:300]}, on_event
            )
            return ToolResult(
                content=json.dumps({"error": "remote_tool_failed", "message": str(exc)[:300]})
            )
        result_text = invocation.raw.content
        await _record_tool_call_event(
            ctx,
            event_seq,
            {
                **event,
                "outcome": "failed" if invocation.raw.is_error else "completed",
                "result": result_text[:_EVENT_RESULT_CHARS],
                "result_truncated": len(result_text) > _EVENT_RESULT_CHARS,
            },
            on_event,
        )
        return ToolResult(content=invocation.envelope)

    return handler
