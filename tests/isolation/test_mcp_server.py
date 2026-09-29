"""Acceptance criteria for the MCP server surface: a `randomizer` call over MCP and
over the service produce equivalent records, `knowledge.query` returns exactly the token
principal's visible slice, `entity.mutate` requires and honours an idempotency key, and no
handler imports a repo module (that last one is `tests/architecture/
test_mcp_handler_imports.py`, run from CI's architecture job).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from api.mcp_server.build import build_server
from api.mcp_server.server import McpArgumentError, UnknownMcpToolError
from api.mcp_server.tokens import InvalidMcpTokenError, issue_mcp_token
from api.mcp_server.tools.entity_tools import EntityNotVisibleError
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.process.skeleton import create_session
from core.resolution.records import ResolutionRecordRow
from core.resolution.registry import RANDOMIZER_DEFINITION, register_tool_definition
from core.resolution.rule_system import (
    COIN_FLIP_SYSTEM,
    RuleSystemDefinition,
    create_rule_system,
    get_or_create_default_rule_system,
)
from core.resolution.service import resolve
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=role)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        await session.flush()
        session.expunge(principal)
        return principal


def _token(tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal: Principal) -> str:
    return issue_mcp_token(
        tenant_id=tenant_id, workspace_id=workspace_id, principal_id=principal.id
    )


async def _seed_scoped_knowledge(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> None:
    source = await create_source(
        tenant_id, key=f"src-{uuid.uuid4().hex[:8]}", name="Guide", class_="lore"
    )
    for entry_key, body, scope_key in (
        ("open-gate", "The gate stands open to anyone who asks about it.", "workspace_public"),
        ("sealed-vault", "Behind the third pillar, a vault nobody asks about.", "facilitator_only"),
    ):
        await upsert_draft_entry(
            tenant_id,
            source.id,
            entry_key,
            EntryFields(title=entry_key, body_md=body, class_="lore", scope_key=scope_key),
        )
    # Publishing chunks the version (one chunk per short entry), so nothing to seed.
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, "workspace_public", version_pin=version.id
    )


async def test_mcp_and_http_tool_calls_produce_equivalent_resolution_records(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """ "Indistinguishable except surface metadata": the same inputs through the MCP handler
    and through `resolve()` directly (which is what the HTTP route calls) produce records
    whose *mechanical* fields agree. Ids and event_seqs differ because they are two
    different rolls; everything the roll itself determined does not."""
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    caller = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    await register_tool_definition(tenant_id, RANDOMIZER_DEFINITION)

    server = await build_server(tenant_id)
    assert "randomizer" in {t["name"] for t in server.list_tools()}, (
        "the deterministic tool list is registry-driven; a registered tool must appear"
    )

    arguments = {
        "session_id": str(sess.id),
        "event_seq": 3,
        "expression": "1d20",
        "check_type": "stealth",
        "actor_fields": {"dexterity": 14},
        "target": 12,
    }
    over_mcp = await server.dispatch(
        _token(tenant_id, workspace_id, caller), "randomizer", arguments
    )

    rule_row = await get_or_create_default_rule_system(tenant_id)
    direct = await resolve(
        tenant_id=tenant_id,
        session_id=sess.id,
        event_seq=4,
        tool_key="randomizer",
        actor_entity_id=None,
        expression="1d20",
        check_type="stealth",
        actor_fields={"dexterity": 14},
        target=12,
        rule_system=RuleSystemDefinition.from_row(rule_row),
        rule_system_id=rule_row.id,
        legal_check_types=frozenset({"stealth"}),
    )

    assert over_mcp["expression"] == direct.expression
    assert over_mcp["modifiers"] == direct.modifiers
    assert over_mcp["target"] == direct.target
    # INV-7 over the wire: the record's id comes back, so a caller renders from the record.
    assert uuid.UUID(over_mcp["resolution_record_id"])
    assert over_mcp["row_hash"]

    async with tenant_scope(tenant_id) as session:
        stored = await session.get(ResolutionRecordRow, uuid.UUID(over_mcp["resolution_record_id"]))
        assert stored is not None
        assert stored.total == over_mcp["total"]
        assert stored.outcome == over_mcp["outcome"]
        assert stored.seed == over_mcp["seed"]

    with pytest.raises(UnknownMcpToolError):
        await server.dispatch(_token(tenant_id, workspace_id, caller), "no_such_tool", {})
    with pytest.raises(InvalidMcpTokenError):
        await server.dispatch("not-a-token", "randomizer", arguments)


async def test_mcp_knowledge_query_is_scope_filtered_by_token_principal(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator = await _member(tenant_id, workspace_id, "facilitator")
    participant = await _member(tenant_id, workspace_id, "participant")
    await _seed_scoped_knowledge(tenant_id, workspace_id)

    server = await build_server(tenant_id)

    async def keys_for(principal: Principal) -> set[str]:
        result = await server.dispatch(
            _token(tenant_id, workspace_id, principal),
            "knowledge.query",
            {"query": "asks", "class": "lore"},
        )
        return {hit["entry_key"] for hit in result["hits"]}

    assert await keys_for(facilitator) == {"open-gate", "sealed-vault"}
    assert await keys_for(participant) == {"open-gate"}, (
        "a participant's MCP query returned a facilitator-scoped entry -- INV-4 must apply "
        "to MCP callers identically"
    )

    # A stranger to the workspace resolves to no scopes, and no scopes means no results --
    # never "unfiltered".
    async with tenant_scope(tenant_id) as session:
        outsider = Principal(tenant_id=tenant_id, kind="human", display_name="outsider")
        session.add(outsider)
        await session.flush()
        session.expunge(outsider)
    assert await keys_for(outsider) == set()

    # There is no parameter by which a caller supplies scopes: the surface simply has none.
    schema = next(t for t in server.list_tools() if t["name"] == "knowledge.query")["inputSchema"]
    assert "scope" not in str(schema).lower()


async def test_mcp_entity_mutate_requires_and_honors_idempotency_key(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    caller = await _member(tenant_id, workspace_id, "facilitator")

    definition = EntitySchemaDefinition(fields=[FieldDef(key="hp", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "npc", 1, definition)
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"npc-{uuid.uuid4().hex[:8]}",
        name="Innkeeper",
        scope_key="workspace_public",
        data={"hp": 10},
    )

    server = await build_server(tenant_id)
    token = _token(tenant_id, workspace_id, caller)

    # No key: refused outright rather than defaulting to a generated one, which would make
    # every retry a fresh mutation -- silently.
    with pytest.raises(McpArgumentError, match="idempotency_key"):
        await server.dispatch(
            token,
            "entity.mutate",
            {"entity_id": str(entity.id), "changes": {"hp": 3}},
        )

    key = f"mcp-{uuid.uuid4().hex[:8]}"
    first = await server.dispatch(
        token,
        "entity.mutate",
        {"entity_id": str(entity.id), "changes": {"hp": 3}, "idempotency_key": key},
    )
    assert first["data"]["hp"] == 3

    # A replay with the same key is a no-op: same version, no second state change.
    replay = await server.dispatch(
        token,
        "entity.mutate",
        {"entity_id": str(entity.id), "changes": {"hp": 3}, "idempotency_key": key},
    )
    assert replay["version"] == first["version"]

    async with tenant_scope(tenant_id) as session:
        changes = (
            await session.execute(
                text("SELECT count(*) FROM entity_state_change WHERE entity_id = :e"),
                {"e": entity.id},
            )
        ).scalar_one()
    assert changes == 1, "the replayed mutation wrote a second state change"

    # `entity.read` goes through the same visibility helper the HTTP view uses.
    view = await server.dispatch(token, "entity.read", {"entity_id": str(entity.id)})
    assert view["fields"]["hp"] == 3
    assert view["version"] == first["version"]

    with pytest.raises(EntityNotVisibleError):
        await server.dispatch(token, "entity.read", {"entity_id": str(uuid.uuid4())})


async def test_every_builtin_backed_tool_reaches_the_mcp_surface(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The list is keyed on `impl_ref`, not on one hardcoded name.

    It used to be a one-entry dict keyed on the tool *key*, so a pack registering a
    second deterministic tool -- the same builtin bound to its own rule system, which
    is what every one of them is -- got a surface that silently omitted it. The tool
    also resolves in the system *its own* definition names, not the tenant default."""
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    caller = await _member(tenant_id, workspace_id, "facilitator")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    await create_rule_system(tenant_id, COIN_FLIP_SYSTEM)
    await register_tool_definition(
        tenant_id,
        RANDOMIZER_DEFINITION.model_copy(
            update={"key": "table_toss", "validation_ref": "coin_flip"}
        ),
    )

    server = await build_server(tenant_id)
    assert "table_toss" in {t["name"] for t in server.list_tools()}

    result = await server.dispatch(
        _token(tenant_id, workspace_id, caller),
        "table_toss",
        {
            "session_id": str(sess.id),
            "event_seq": 7,
            "expression": "1d2",
            "check_type": "call",
            "actor_fields": {},
        },
    )
    # `1d2` is illegal in the tenant default (a d20 system); landing an outcome at all
    # proves the binding, and the banded outcome proves *which* system answered.
    assert result["outcome"] in ("heads", "tails"), result
