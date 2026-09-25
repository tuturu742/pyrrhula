"""Deterministic tools over MCP (G4.13, plan §9.1, §13.7, INV-7).

**The registry drives the tool list.** `randomizer` is not special-cased here; every
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
from collections.abc import Awaitable, Callable
from typing import Any

from api.mcp_server.server import McpArgumentError, McpTool, require_str, require_uuid
from api.mcp_server.tokens import McpTokenClaims
from core.resolution.registry import list_tool_definitions
from core.resolution.rule_system import RuleSystemDefinition, get_or_create_default_rule_system
from core.resolution.service import UnknownRuleSystemError, effective_rule_system, resolve


def _make_handler(
    tool_key: str,
) -> Callable[[McpTokenClaims, dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
        return await _resolve_tool(tool_key, claims, arguments)

    return handler


async def _resolve_tool(
    tool_key: str, claims: McpTokenClaims, arguments: dict[str, Any]
) -> dict[str, Any]:
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
    default_system = RuleSystemDefinition.from_row(rule_row)

    # Same precedence as the in-process handler -- the call's own selector, then the
    # tool definition's `validation_ref`, then this tenant's default. Without it a
    # bundle that binds the randomizer to its own ruleset (karsh-vale does) would have
    # its remote rolls silently validated against the stock d20 system instead, which
    # produces a wrong record rather than an error.
    requested = arguments.get("rule_system")
    try:
        rule_system, rule_system_id = await effective_rule_system(
            claims.tenant_id,
            str(requested) if requested else None,
            default_system,
            rule_row.id,
            tool_key=tool_key,
        )
    except UnknownRuleSystemError as exc:
        raise McpArgumentError(str(exc)) from exc

    record = await resolve(
        tenant_id=claims.tenant_id,
        session_id=session_id,
        event_seq=event_seq,
        tool_key=tool_key,
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
        rule_system_id=rule_system_id,
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


# The one builtin. A definition naming anything else describes a handler this process
# does not have, and is skipped rather than advertised as a name that fails when called.
_BUILTIN_RANDOMIZER = "builtin:randomizer"


async def deterministic_tools(tenant_id: uuid.UUID) -> list[McpTool]:
    """Every registered deterministic tool backed by the builtin randomizer.

    This used to be a one-entry dict keyed on the tool *name*, which made the surface
    exactly one tool wide however many a pack registered -- a pack's `policy_lookup` or
    `checklist_eval` was silently skipped despite being the same builtin over its own
    rule system. Keying on `impl_ref` is what the docstring above always claimed."""
    tools: list[McpTool] = []
    for definition in await list_tool_definitions(tenant_id):
        if definition.kind != "deterministic" or definition.impl_ref != _BUILTIN_RANDOMIZER:
            continue
        tools.append(
            McpTool(
                name=definition.key,
                description=f"Deterministic tool {definition.key} (records a ResolutionRecord).",
                parameters=dict(definition.input_schema),
                handler=_make_handler(definition.key),
            )
        )
    return tools
