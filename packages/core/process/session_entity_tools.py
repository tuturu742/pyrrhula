"""In-session tool handlers for entity creation + resolved consequences (P1).

Registered into a turn's ``ToolRegistry`` (``core.process.live_session``) only when the phase
grants the tool (``phase.tools``) -- the phase grant is the authorization to offer the tool;
the underlying ``core.entities.mutation`` calls still run the real ``entity:create`` /
``entity:mutate`` ``PermissionService`` check on the acting persona's principal (rule 12).

Both handlers get the acting persona + session from ``ToolContext``; ``workspace_id``,
``permission_service`` and the rule system come from the turn's composition (closed over by the
factory), never from the model's arguments. ``scope_key`` for a created entity is engine-derived
here, never model-chosen (INV-4 visibility).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.agents.models import Persona
from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.entities.mutation import PermissionDeniedError, SchemaNotFoundError, create
from core.ports.permission import PermissionService
from core.resolution.consequence import resolve_and_apply
from core.resolution.registry import get_tool_definition
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.sessions.models import SessionRow
from core.tenancy.scope import tenant_scope

# Entities created in a session are visible to the whole roster, not private notes. A
# per-workspace scope that the default tenant provisioning always creates.
_ENTITY_SCOPE_KEY = "workspace_public"


def make_entity_create_handler(
    *, workspace_id: uuid.UUID, permission_service: PermissionService
) -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        schema_key = str(args.get("schema_key") or "").strip()
        name = str(args.get("name") or "").strip()
        if not schema_key or not name:
            return ToolResult(
                content=json.dumps(
                    {"error": "missing_args", "message": "schema_key and name are required"}
                )
            )
        raw_fields = args.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else {}
        # Whether the new entity *is* the caller -- the record it acts through -- or is
        # merely something it is creating. Only the caller knows, so it is an argument,
        # and it defaults to "not me": binding is the special case, not the norm.
        bind_to_self = bool(args.get("bind_to_self") or False)

        async with tenant_scope(ctx.tenant_id) as session:
            persona = await session.get(Persona, ctx.persona_id)
            if persona is None:
                return ToolResult(content=json.dumps({"error": "no_persona"}))
            principal_id = persona.principal_id
            existing_entity = persona.entity_id

        # "One per persona" is a rule about the entity a persona ACTS THROUGH, and it was
        # being applied to everything the persona created. Worse, the first entity created
        # was bound as the persona's own whatever it was: a lead that once created a work
        # item owned that work item forever, and every later create -- in that session and
        # in every session after it -- was refused. So the cap now guards exactly one
        # thing, the self-binding, and only when the caller asked for one.
        if bind_to_self and existing_entity is not None:
            return ToolResult(
                content=json.dumps(
                    {
                        "created": False,
                        "entity_id": str(existing_entity),
                        "message": (
                            "this persona already acts through an entity; mutate that one "
                            "instead of creating a second, or create without bind_to_self"
                        ),
                    }
                )
            )

        # A key per entity, not per persona: a deterministic per-principal key made a
        # second create collide with the first.
        entity_key = f"{schema_key}-{uuid.uuid4().hex[:8]}"
        try:
            result = await create(
                principal_id,
                ctx.tenant_id,
                workspace_id,
                schema_key,
                entity_key,
                name,
                fields,
                _ENTITY_SCOPE_KEY,
                # Keyed on the entity being created, not merely on who is creating:
                # a persona making six work items in one session made six distinct
                # calls, and one key for all of them returned the first result to
                # every one of them.
                idempotency_key=f"create:{ctx.session_id}:{principal_id}:{entity_key}",
                permission_service=permission_service,
            )
        except PermissionDeniedError as exc:
            return ToolResult(content=json.dumps({"error": "forbidden", "message": str(exc)}))
        except SchemaNotFoundError as exc:
            return ToolResult(content=json.dumps({"error": "no_schema", "message": str(exc)}))
        except Exception as exc:  # validation etc. -- report so the model can retry
            return ToolResult(content=json.dumps({"error": "invalid", "message": str(exc)[:200]}))

        # Bind only when asked: this is the record the persona acts through, so later
        # turns resolve its state (INV-7) instead of falling back to defaults.
        if bind_to_self:
            async with tenant_scope(ctx.tenant_id) as session:
                persona = await session.get(Persona, ctx.persona_id)
                if persona is not None and persona.entity_id is None:
                    persona.entity_id = uuid.UUID(result["entity_id"])
                    await session.flush()

        return ToolResult(content=json.dumps({"created": True, **result}))

    return handler


def make_resolve_apply_handler(
    *,
    workspace_id: uuid.UUID,
    rule_system: RuleSystemDefinition,
    rule_system_id: uuid.UUID,
    permission_service: PermissionService,
) -> ToolHandler:
    async def handler(args: dict[str, object], ctx: ToolContext) -> ToolResult:
        if ctx.session_id is None:
            return ToolResult(content=json.dumps({"error": "no_session"}))
        try:
            actor_entity_id = uuid.UUID(str(args["actor_entity_id"]))
        except (KeyError, ValueError):
            return ToolResult(
                content=json.dumps(
                    {"error": "missing_args", "message": "actor_entity_id (a uuid) is required"}
                )
            )
        machine_key = str(args.get("machine_key") or "").strip()
        trigger = str(args.get("trigger") or "").strip()
        if not machine_key or not trigger:
            return ToolResult(
                content=json.dumps(
                    {"error": "missing_args", "message": "machine_key and trigger are required"}
                )
            )
        expression = str(args.get("expression") or "1d2")
        raw_set = args.get("set_fields")
        set_fields = raw_set if isinstance(raw_set, dict) else {}

        # The tool's OWN bound rule system wins over the session default: a pack that
        # registers `resolve_and_apply` against e.g. a coin-pool system must resolve in
        # that system, not in whatever get_or_create_default_rule_system handed the turn
        # (observed live: a d2 pool validated against mvp_d20 and failed). Falls back to
        # the injected default when the tool declares no validation_ref.
        effective_system, effective_system_id = rule_system, rule_system_id
        tool_def = await get_tool_definition(ctx.tenant_id, "resolve_and_apply")
        if tool_def is not None and tool_def.validation_ref:
            bound = await get_rule_system(ctx.tenant_id, str(tool_def.validation_ref))
            if bound is not None:
                effective_system = RuleSystemDefinition.from_row(bound)
                effective_system_id = bound.id

        # A check type the caller did not name must be DETERMINISTIC -- `next(iter(set))`
        # picked an arbitrary member of an unordered frozenset, so the same call could
        # resolve differently between processes.
        check_type = str(
            args.get("check_type") or (sorted(effective_system.check_types) or ["call"])[0]
        )

        from core.entities.storage import get_entity

        entity = await get_entity(ctx.tenant_id, actor_entity_id)
        actor_fields = dict(entity.data) if entity is not None else {}

        async with tenant_scope(ctx.tenant_id) as session:
            persona = await session.get(Persona, ctx.persona_id)
            principal_id = persona.principal_id if persona is not None else ctx.persona_id
            session_row = await session.get(SessionRow, ctx.session_id)
            assert session_row is not None
            event_seq = session_row.next_event_seq
            if ctx.turn_event_seq is not None and event_seq <= ctx.turn_event_seq:
                # Never hand out the slot the surrounding turn already claimed for
                # its message (peeked before generation) -- uq_session_event_seq.
                event_seq = ctx.turn_event_seq + 1
            session_row.next_event_seq = event_seq + 1

        try:
            result: dict[str, Any] = await resolve_and_apply(
                tenant_id=ctx.tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                session_id=ctx.session_id,
                event_seq=event_seq,
                tool_key="resolve_and_apply",
                actor_entity_id=actor_entity_id,
                expression=expression,
                check_type=check_type,
                machine_key=machine_key,
                trigger=trigger,
                set_fields=set_fields,
                rule_system=effective_system,
                rule_system_id=effective_system_id,
                legal_check_types=effective_system.check_types,
                permission_service=permission_service,
                actor_fields=actor_fields,
                target=(
                    int(str(args["target"])) if str(args.get("target") or "").strip() else None
                ),
            )
        except Exception as exc:
            return ToolResult(
                content=json.dumps({"error": "apply_failed", "message": str(exc)[:200]})
            )

        return ToolResult(content=json.dumps(result))

    return handler
