"""Internal tool registry: the seam the future Resolution Service plugs
real tools into. Deliberately empty by default -- no tools are hardcoded here. A caller
(a test, or the composition root) registers whatever handlers it has; the runtime
(``core.agents.runtime``) only ever sees the registry's own ``specs()``/``dispatch()``
interface, never a hardcoded
tool list.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.ports.model_provider import ToolCall, ToolSpec


@dataclass(frozen=True)
class ToolContext:
    tenant_id: uuid.UUID
    persona_id: uuid.UUID
    session_id: uuid.UUID | None
    # The event_seq slot the surrounding turn has already claimed for its message
    # (peeked before generation -- see run_agent_turn). Handlers that reserve their
    # own session-event seqs mid-turn must allocate PAST this slot, or the turn's
    # eventual message insert collides with them (uq_session_event_seq).
    turn_event_seq: int | None = None


@dataclass(frozen=True)
class ToolResult:
    content: str  # fed back to the model as the tool call's output message


ToolHandler = Callable[[dict[str, object], ToolContext], Awaitable[ToolResult]]


class UnknownToolError(Exception):
    """The model called a tool name that isn't registered."""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    async def dispatch(self, tool_call: ToolCall, ctx: ToolContext) -> ToolResult:
        handler = self._handlers.get(tool_call.name)
        if handler is None:
            raise UnknownToolError(f"no tool registered for {tool_call.name!r}")
        return await handler(tool_call.arguments, ctx)
