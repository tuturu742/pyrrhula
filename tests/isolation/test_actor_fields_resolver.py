"""A character rolls with its own stats, not with everyone else's.

The dice tool validates expressions like ``1d20+STR`` against the acting entity's fields.
Those fields came from a fixed ``{dexterity: 14, strength: 14}`` stub that outlived the
entity system by two phases, so every character in every session rolled identically and
the sheet a player was handed had no effect on anything.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core.entities.schema import EntitySchemaRow
from core.entities.storage import EntityRow
from core.process.live_session import _UNSHEETED_ACTOR_FIELDS, _make_actor_fields_resolver
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

pytestmark = pytest.mark.asyncio


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _entity(tenant_id: uuid.UUID, key: str, data: dict[str, object]) -> uuid.UUID:
    """Insert a row directly: this is about what the resolver READS, so going through the
    schema-validating authoring path would only test that path instead."""
    workspace_id = await _workspace_of(tenant_id)
    async with tenant_scope(tenant_id) as session:
        schema = EntitySchemaRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            key=f"sheet-{uuid.uuid4().hex[:8]}",
            version=1,
            fields=[],
        )
        session.add(schema)
        await session.flush()
        row = EntityRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            schema_id=schema.id,
            key=key,
            name=key.title(),
            scope_key="workspace_public",
            data=data,
        )
        session.add(row)
        await session.flush()
        return row.id


async def test_an_actor_rolls_with_the_stats_on_its_own_sheet(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    resolve = _make_actor_fields_resolver(tenant_a)

    bram = await _entity(tenant_a, "bram", {"strength": 16, "dexterity": 12})
    pip = await _entity(tenant_a, "pip", {"strength": 9, "dexterity": 17})

    assert (await resolve(bram))["strength"] == 16
    assert (await resolve(pip))["strength"] == 9
    assert (await resolve(bram))["dexterity"] == 12, "two actors must not share one sheet"


async def test_a_sheet_missing_an_ability_still_answers_for_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A partial sheet must not discard the stats it does carry, and must not make the
    roll unresolvable -- fall back per key, not wholesale."""
    tenant_a, _tenant_b = two_tenants
    resolve = _make_actor_fields_resolver(tenant_a)

    partial = await _entity(tenant_a, "ghost", {"strength": 18})
    fields = await resolve(partial)

    assert fields["strength"] == 18
    assert fields["dexterity"] == _UNSHEETED_ACTOR_FIELDS["dexterity"]


async def test_an_actor_with_no_entity_still_rolls(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An NPC invented mid-scene has no sheet. The roll still has to resolve."""
    tenant_a, _tenant_b = two_tenants
    resolve = _make_actor_fields_resolver(tenant_a)
    assert await resolve(None) == dict(_UNSHEETED_ACTOR_FIELDS)


async def test_one_tenants_character_sheet_never_resolves_for_another(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Entity ids are attacker-influenced -- they arrive as a tool argument. Reading the
    row inside tenant_scope is what keeps a borrowed id from returning someone else's
    character instead of failing closed."""
    tenant_a, tenant_b = two_tenants
    theirs = await _entity(tenant_a, "elin", {"strength": 3, "dexterity": 3})

    resolve_b = _make_actor_fields_resolver(tenant_b)
    assert await resolve_b(theirs) == dict(_UNSHEETED_ACTOR_FIELDS), (
        "another tenant's entity id resolved to that tenant's stats"
    )
