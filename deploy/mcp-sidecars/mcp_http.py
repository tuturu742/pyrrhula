"""Minimal MCP streamable-HTTP server core shared by the sidecars.

Speaks exactly the subset Pyrrhula's RemoteMcpTransport (and any spec-compliant MCP
client) needs: JSON-RPC 2.0 over POST -- ``initialize``, ``notifications/initialized``,
``tools/list``, ``tools/call``. Plain-JSON responses (the client accepts both JSON and
SSE). Zero dependencies beyond FastAPI, which the sidecar images install anyway.

A sidecar registers tools with the ``@server.tool`` decorator; handlers are async and
receive keyword arguments per their JSON schema.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PROTOCOL_VERSION = "2025-03-26"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class McpServer:
    name: str
    tools: dict[str, Tool] = field(default_factory=dict)

    def tool(
        self, name: str, description: str, parameters: dict[str, Any]
    ) -> Callable[
        [Callable[..., Awaitable[dict[str, Any]]]], Callable[..., Awaitable[dict[str, Any]]]
    ]:
        def register(fn: Callable[..., Awaitable[dict[str, Any]]]):
            self.tools[name] = Tool(name, description, parameters, fn)
            return fn

        return register

    def build_app(self) -> FastAPI:
        app = FastAPI(title=self.name)

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok", "server": self.name}

        @app.post("/mcp")
        async def mcp(request: Request) -> JSONResponse:
            payload = await request.json()
            method = payload.get("method")
            rpc_id = payload.get("id")

            def ok(result: dict[str, Any]) -> JSONResponse:
                return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

            def err(code: int, message: str) -> JSONResponse:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
                )

            if method == "initialize":
                return ok(
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": self.name, "version": "1.0"},
                    }
                )
            if method == "notifications/initialized":
                return JSONResponse(status_code=202, content=None)
            if method == "tools/list":
                return ok(
                    {
                        "tools": [
                            {
                                "name": t.name,
                                "description": t.description,
                                "inputSchema": t.parameters,
                            }
                            for t in self.tools.values()
                        ]
                    }
                )
            if method == "tools/call":
                params = payload.get("params") or {}
                tool = self.tools.get(str(params.get("name")))
                if tool is None:
                    return err(-32602, f"unknown tool {params.get('name')!r}")
                arguments = params.get("arguments") or {}
                accepted = set(inspect.signature(tool.handler).parameters)
                kwargs = {k: v for k, v in arguments.items() if k in accepted}
                try:
                    result = await tool.handler(**kwargs)
                except Exception as exc:  # noqa: BLE001 -- tool failure is a normal result
                    return ok(
                        {
                            "content": [{"type": "text", "text": f"tool failed: {exc}"}],
                            "isError": True,
                        }
                    )
                text = str(result.pop("_text", "")) or str(result)
                return ok(
                    {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                        "structuredContent": result,
                    }
                )
            return err(-32601, f"unknown method {method!r}")

        return app
