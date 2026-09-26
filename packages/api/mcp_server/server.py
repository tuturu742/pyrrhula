"""Pyrrhula as an MCP server.

**One implementation, two surfaces.** Every handler here calls the same service the HTTP
route calls. That is not a style preference: the permission model, the visibility
resolution, the idempotency, and INV-7's render-from-the-record rule all live in those
services, and a second implementation of any of them is a second implementation to get
wrong -- in the surface that is, by construction, the one automated callers use.

**The token is the scope.** An MCP call has no URL to carry a workspace and no session to
infer a principal from, so the token carries `(tenant, workspace, principal)` and *every*
authorisation resolves from it. There is no parameter by which a caller can name a
different principal; asking as someone else is not a thing the protocol can express.

**Handlers are thin, and a lint says so.** `tests/architecture/test_mcp_handler_imports.py`
fails if anything under `mcp_server/` imports `core.knowledge.repo` or `core.secrets.repo`
-- INV-1's rule, applied at the surface most likely to grow a shortcut because "it's just
a read".

Transport framing (JSON-RPC over stdio or streamable HTTP) is deliberately not here. This
module owns *dispatch*: name plus arguments plus token in, structured result out. A
transport is a thin shell around `dispatch`, and keeping the two apart is what let every
tool be tested without one.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from api.mcp_server.tokens import InvalidMcpTokenError, McpTokenClaims, verify_mcp_token

Handler = Callable[[McpTokenClaims, dict[str, Any]], Awaitable[dict[str, Any]]]


class UnknownMcpToolError(Exception):
    pass


class McpArgumentError(ValueError):
    """A required argument is missing or malformed. Distinct from `UnknownMcpToolError`
    so a caller can tell "you asked for something that doesn't exist" from "you asked
    correctly but wrongly"."""


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler


class McpServer:
    """The dispatch table. Registration is explicit rather than by decorator scan: a tool
    that appears on this surface should appear because someone wrote it down."""

    def __init__(self) -> None:
        self._tools: dict[str, McpTool] = {}

    def register(self, tool: McpTool) -> None:
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.parameters}
            for t in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    async def dispatch(self, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        claims = verify_mcp_token(token)  # raises InvalidMcpTokenError
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownMcpToolError(f"no MCP tool {name!r}; registered: {sorted(self._tools)}")
        return await tool.handler(claims, arguments)


def require_uuid(arguments: dict[str, Any], key: str) -> uuid.UUID:
    raw = arguments.get(key)
    if not raw:
        raise McpArgumentError(f"{key} is required")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise McpArgumentError(f"{key} is not a uuid: {raw!r}") from exc


def require_str(arguments: dict[str, Any], key: str) -> str:
    raw = arguments.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise McpArgumentError(f"{key} is required and must be a non-empty string")
    return raw


__all__ = [
    "Handler",
    "InvalidMcpTokenError",
    "McpArgumentError",
    "McpServer",
    "McpTool",
    "UnknownMcpToolError",
    "require_str",
    "require_uuid",
]
