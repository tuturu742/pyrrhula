"""`entity.read` / `entity.mutate` over MCP.

Both call the same services the HTTP routes call -- `core.entities.storage` /
`core.entities.mutation` -- so the scope filter, the permission check, and the idempotency
are the ones already tested, not a second copy that has to be kept in step.

**`entity.mutate` requires an idempotency key and says so.** Rule 8 applies over MCP
identically, and an automated caller is *more* likely to retry than a human one, not less.
Refusing the call outright is better than defaulting to a generated key: a generated key
makes every retry a fresh mutation, which is the exact failure the rule exists to prevent,
and it fails silently.
"""

from __future__ import annotations

from typing import Any

from api.mcp_server.server import McpArgumentError, McpTool, require_str, require_uuid
from api.mcp_server.tokens import McpTokenClaims
from api.permission_service_factory import get_permission_service
from core.assembler.visibility import EXPORT, scopes_for
from core.entities.injection import visible_fields
from core.entities.mutation import PermissionDeniedError, mutate
from core.entities.repo import get_schema
from core.entities.storage import get_entity
from core.entities.validation import compute_derived


class EntityNotVisibleError(Exception):
    """The entity does not exist, or its scope is outside the token principal's set. One
    error for both: distinguishing them would confirm the existence of entities in
    compartments the caller cannot see."""


async def _entity_read(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = require_uuid(arguments, "entity_id")
    entity = await get_entity(claims.tenant_id, entity_id)
    if entity is None or entity.workspace_id != claims.workspace_id:
        raise EntityNotVisibleError(f"no entity {entity_id} visible to this token")

    scope_set = await scopes_for(
        claims.tenant_id, claims.principal_id, claims.workspace_id, EXPORT, None
    )
    if entity.scope_key not in scope_set:
        raise EntityNotVisibleError(f"no entity {entity_id} visible to this token")

    schema_row = await get_schema(claims.tenant_id, entity.schema_id)
    if schema_row is None:
        raise EntityNotVisibleError(f"entity {entity_id} has no schema")
    definition = schema_row.to_definition()

    # Field-level filtering, same helper the HTTP view uses: an entity you may see can
    # still hold fields you may not.
    visible = visible_fields(entity, definition, frozenset(scope_set))
    return {
        "id": str(entity.id),
        "key": entity.key,
        "name": entity.name,
        "version": entity.version,
        "fields": dict(sorted(visible.items())),
        "derived": compute_derived(definition, entity.data),
        "fsm_states": dict(entity.fsm_states),
    }


async def _entity_mutate(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = require_uuid(arguments, "entity_id")
    idempotency_key = require_str(arguments, "idempotency_key")
    changes = arguments.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise McpArgumentError("changes is required and must be a non-empty object")

    try:
        result = await mutate(
            claims.principal_id,
            claims.tenant_id,
            claims.workspace_id,
            entity_id,
            changes,
            "agent",
            f"mcp:{idempotency_key}",
            idempotency_key,
            permission_service=get_permission_service(),
        )
    except PermissionDeniedError as exc:
        raise EntityNotVisibleError(str(exc)) from exc
    return {"entity_id": str(entity_id), "version": result["version"], "data": result["data"]}


ENTITY_READ = McpTool(
    name="entity.read",
    description="Read one entity's visible fields, derived values, and FSM states.",
    parameters={
        "type": "object",
        "properties": {"entity_id": {"type": "string", "format": "uuid"}},
        "required": ["entity_id"],
    },
    handler=_entity_read,
)

ENTITY_MUTATE = McpTool(
    name="entity.mutate",
    description="Change entity fields. Requires an idempotency key (CLAUDE.md rule 8).",
    parameters={
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "format": "uuid"},
            "changes": {"type": "object"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["entity_id", "changes", "idempotency_key"],
    },
    handler=_entity_mutate,
)
