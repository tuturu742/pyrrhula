"""Generic resolution ``McpTransport``: the tenant's registered deterministic tools,
served over the MCP surface.

Nothing here is domain-specific (rule 1): packs register *tool definitions* (an RPG
pack ships a die roller; another pack might ship a card draw) and this transport
exposes exactly those -- ``list_tools`` is the tenant's registry filtered by the
workspace allowlist, ``call_tool`` executes through ``core.resolution`` so the RNG is
server-side and the outcome is a persisted ``ResolutionRecord`` (INV-7: the record is
the truth; model prose around it is decoration).

Addressing: the registered server's ``url`` carries the tenant id (the workflow
manifest writes ``{tenant_id}``, substituted at registration -- the same
value-in-url convention the git servers use for their repo key). Call context
(session, event seq, acting persona) is injected by the trusted in-session handler
under ``_context``; anything a model supplied under that key is discarded there.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError


def _tenant_from(server: McpServerRef) -> uuid.UUID:
    try:
        return uuid.UUID(server.url.rsplit("/", 1)[-1])
    except ValueError as exc:
        raise McpTransportError(
            f"resolution server {server.key!r} has no tenant address in url {server.url!r}"
        ) from exc


class ResolutionMcpTransport:
    def __init__(self, *, permission_service: Any) -> None:
        self._permissions = permission_service

    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
        from core.resolution.registry import list_tool_definitions

        tenant_id = _tenant_from(server)
        specs: list[McpToolSpec] = []
        for row in await list_tool_definitions(tenant_id):
            specs.append(
                McpToolSpec(
                    name=row.key,
                    description=(
                        f"Deterministic {row.determinism} resolution tool; the server "
                        "rolls/draws and records the outcome -- never invent results."
                    ),
                    parameters=dict(row.input_schema),
                    effectful=False,
                )
            )
        return specs

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        from core.entities.storage import get_entity
        from core.resolution.registry import get_tool_definition
        from core.resolution.rule_system import RuleSystemDefinition, get_rule_system

        tenant_id = _tenant_from(server)
        context = arguments.get("_context") or {}
        session_id = context.get("session_id")
        # The MCP envelope is untyped JSON, so these arrive as `object`. Normalise
        # once, here, rather than coercing at each of the four call sites -- the
        # same shape the uuid fields below already use.
        raw_event_seq = context.get("event_seq")
        event_seq = int(str(raw_event_seq)) if raw_event_seq is not None else 0
        if not session_id or event_seq is None:
            raise McpTransportError("resolution tools run inside sessions only (no call context)")
        session_id = uuid.UUID(str(session_id))

        tool = await get_tool_definition(tenant_id, name)
        if tool is None:
            raise McpTransportError(f"no tool definition {name!r} in this tenant")
        rule_system_row = await get_rule_system(tenant_id, str(tool.validation_ref))
        if rule_system_row is None:
            raise McpTransportError(
                f"tool {name!r}: rule system {tool.validation_ref!r} is not loaded"
            )
        rule_system = RuleSystemDefinition.from_row(rule_system_row)

        expression = str(arguments.get("expression") or "1d2")
        check_type = str(arguments.get("check_type") or next(iter(rule_system.check_types), "call"))
        raw_target = arguments.get("target")
        target = int(str(raw_target)) if raw_target is not None else None
        actor_entity_id: uuid.UUID | None = None
        actor_fields: dict[str, Any] = {}
        if arguments.get("actor_entity_id"):
            try:
                actor_entity_id = uuid.UUID(str(arguments["actor_entity_id"]))
            except ValueError as exc:
                raise McpTransportError("actor_entity_id must be a uuid") from exc
            entity = await get_entity(tenant_id, actor_entity_id)
            if entity is not None:
                actor_fields = dict(entity.data)

        from core.resolution.rule_system import OutcomeBandingError
        from core.resolution.service import InvalidResolutionError

        machine_key = str(arguments.get("machine_key") or "").strip()
        trigger = str(arguments.get("trigger") or "").strip()
        try:
            return await self._resolve_and_wrap(
                name=name,
                arguments=arguments,
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                actor_entity_id=actor_entity_id,
                actor_fields=actor_fields,
                expression=expression,
                check_type=check_type,
                target=target,
                machine_key=machine_key,
                trigger=trigger,
                rule_system=rule_system,
                rule_system_row=rule_system_row,
                context=context,
            )
        except (InvalidResolutionError, OutcomeBandingError) as exc:
            # A malformed check is the MODEL's mistake to correct, not a turn-fatal
            # fault: hand the message back as the tool's result so the model can fix
            # its arguments and try again within the same turn.
            error = {"error": "invalid_resolution", "message": str(exc)}
            return McpToolResult(content=json.dumps(error), structured=error)

    async def _resolve_and_wrap(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        event_seq: int,
        actor_entity_id: uuid.UUID | None,
        actor_fields: dict[str, object],
        expression: str,
        check_type: str,
        target: int | None,
        machine_key: str,
        trigger: str,
        rule_system: Any,
        rule_system_row: Any,
        context: dict[str, Any],
    ) -> McpToolResult:
        from core.resolution.consequence import resolve_and_apply
        from core.resolution.service import resolve

        if machine_key and trigger and actor_entity_id is not None:
            # Outcome drives the actor's state machine in the same breath (the
            # existing consequence path; idempotent through the mutation layer).
            result = await resolve_and_apply(
                tenant_id=tenant_id,
                workspace_id=uuid.UUID(str(context["workspace_id"])),
                principal_id=uuid.UUID(str(context["principal_id"])),
                session_id=session_id,
                event_seq=event_seq,
                tool_key=name,
                actor_entity_id=actor_entity_id,
                expression=expression,
                check_type=check_type,
                machine_key=machine_key,
                trigger=trigger,
                set_fields={},
                rule_system=rule_system,
                rule_system_id=rule_system_row.id,
                legal_check_types=rule_system.check_types,
                permission_service=self._permissions,
                actor_fields=actor_fields,
                target=target,
            )
            return McpToolResult(content=json.dumps(result), structured=result)

        record = await resolve(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            tool_key=name,
            actor_entity_id=actor_entity_id,
            expression=expression,
            check_type=check_type,
            actor_fields=actor_fields,
            target=target,
            rule_system=rule_system,
            rule_system_id=rule_system_row.id,
            legal_check_types=rule_system.check_types,
        )
        structured = {
            "resolution_id": str(record.id),
            "outcome": record.outcome,
            "total": record.total,
            "expression": expression,
            "check_type": check_type,
        }
        return McpToolResult(content=json.dumps(structured), structured=structured)
