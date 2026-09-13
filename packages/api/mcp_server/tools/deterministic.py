"""Deterministic tools over MCP (G4.13, plan §9.1, §13.7, INV-7).

**The registry drives the tool list.** `dice_roller` is not special-cased here; every
`tool_definition` row of kind `deterministic` becomes an MCP tool, so a pack that registers
a new deterministic tool gets it on the MCP surface without a code change. Hardcoding one
would have made the surface a list somebody maintains by hand, which is a list that drifts.

**Results are the `ResolutionRecord`'s data, by id** (INV-7 over the wire). The handler
returns the record's own fields and its id; it never composes prose about an outcome, and a
caller wanting to display a result renders from what came back rather than from anything a
model said about it.
"""

from __future__ import annotations

import uuid
from typing import Any

from api.mcp_server.server import McpArgumentError, McpTool, require_str, require_uuid
from api.mcp_server.tokens import McpTokenClaims
from core.resolution.registry import list_tool_definitions
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.resolution.service import resolve


async def _dice_roller(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = require_uuid(arguments, "session_id")
    expression = require_str(arguments, "expression")
    check_type = require_str(arguments, "check_type")
    event_seq = arguments.get("event_seq")
    if not isinstance(event_seq, int):
        raise McpArgumentError("event_seq is required and must be an integer")

    actor_fields = arguments.get("actor_fields")
    if not isinstance(actor_fields, dict):
        raise McpArgumentError("actor_fields is required and must be an object")

    rule_row = await get_or_create_default_rule_system(claims.tenant_id)
    rule_system = RuleSystemDefinition.from_row(rule_row)

    record = await resolve(
        tenant_id=claims.tenant_id,
        session_id=session_id,
        event_seq=event_seq,
        tool_key="dice_roller",
        actor_entity_id=(
            uuid.UUID(str(arguments["actor_entity_id"]))
            if arguments.get("actor_entity_id")
            else None
        ),
        expression=expression,
        check_type=check_type,
        actor_fields=actor_fields,
        target=arguments.get("target"),
        rule_system=rule_system,
        rule_system_id=rule_row.id,
        legal_check_types=frozenset(rule_system.check_types),
    )
    # The record, by id -- not a sentence about it. Same data the HTTP surface returns and
    # the same data the session widget renders from (INV-7).
    return {
        "resolution_record_id": str(record.id),
        "expression": record.expression,
        "rolls": list(record.rolls),
        "modifiers": dict(record.modifiers),
        "total": record.total,
        "target": record.target,
        "outcome": record.outcome,
        "seed": record.seed,
        "row_hash": record.row_hash,
    }


_HANDLERS = {"dice_roller": _dice_roller}


async def deterministic_tools(tenant_id: uuid.UUID) -> list[McpTool]:
    """Every registered deterministic tool that has a handler here. A definition without
    one is skipped rather than exposed as a name that fails when called -- an advertised
    tool that cannot run is worse than an absent one."""
    tools: list[McpTool] = []
    for definition in await list_tool_definitions(tenant_id):
        if definition.kind != "deterministic":
            continue
        handler = _HANDLERS.get(definition.key)
        if handler is None:
            continue
        tools.append(
            McpTool(
                name=definition.key,
                description=f"Deterministic tool {definition.key} (records a ResolutionRecord).",
                parameters=dict(definition.input_schema),
                handler=handler,
            )
        )
    return tools
