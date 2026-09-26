"""MCP transport port.

The wire protocol is an adapter's problem. What core needs is two operations -- "what tools
does this server offer" and "call one" -- and a shape for their results. Keeping the
transport behind a port is what lets the allowlist, the phase policy, the idempotency, and
the injection envelope all be tested without a running MCP server, which matters because
those four things are the entire security surface and the transport is not.

``McpToolSpec.effectful`` is declared by the *server*, and Pyrrhula treats it as a floor
rather than a fact: a workspace may mark additional tools effectful, never fewer. A server
that under-declares is a server whose "read-only" tool books a flight, and the cost of
being wrong in that direction is unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class McpServerRef:
    """Everything the transport needs to reach a server. ``credential_ref`` points *into* a
    secret manager and never holds a key (CLAUDE.md: `agent.credential_ref` follows
    the same rule, and for the same reason -- a credential in a row is a credential in every
    backup, every export, and every support ticket)."""

    key: str
    url: str
    credential_ref: str | None = None
    # Per-server limits; None means the transport's own default. A timeout belongs to the
    # server (a lookup answers instantly, an engine tool runs a build), not to the
    # deployment that happens to host both.
    timeout_seconds: int | None = None
    max_result_chars: int | None = None
    # Transport-specific knobs for this server; each transport reads only its own keys.
    options: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    effectful: bool = False


@dataclass(frozen=True)
class McpToolResult:
    """``content`` is text the model will see -- *after* it is wrapped in the injection
    envelope by ``core.mcp.client``. Never before: a result that reached context unwrapped
    is content the model may read as instructions."""

    content: str
    is_error: bool = False
    structured: dict[str, Any] = field(default_factory=dict)


class McpTransportError(Exception):
    """The server could not be reached, timed out, or answered unintelligibly. Distinct
    from a tool *returning* an error, which is a normal outcome the model should see."""


class McpTransport(Protocol):
    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]: ...

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult: ...
