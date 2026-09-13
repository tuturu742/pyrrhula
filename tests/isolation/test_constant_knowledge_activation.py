"""A `constant` knowledge entry reaches the context on a turn that matches nothing.

`core.knowledge.activation` implemented constant entries, keyword triggers, sticky,
cooldown and inclusion groups -- and had no production caller, so an entry marked "always
include this" was included by nothing. The failure is silent and looks like a model
problem: on the opening turn there is no query text, so lexical retrieval matches no
tsvector and dense retrieval matches no vector, and a workspace full of briefing material
produces agents who behave as if they had never been briefed.

That is what these tests hold shut. The first is the regression; the second is the reason
it must be scoped rather than global.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from core.assembler.context_assembler import _activate_for_workspace
from core.assembler.visibility import seed_default_scopes
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.ports.scope import ScopeSet
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _attach_handbook(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    constant: bool,
    scope_key: str = "workspace_public",
) -> None:
    key = f"handbook-{uuid.uuid4().hex[:8]}"
    source = await create_source(tenant_id, key=key, name="Handbook", class_="lore")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "the-manor",
        EntryFields(
            title="The manor",
            body_md="The service staircase comes up ten steps from the bedroom door.",
            class_="lore",
            scope_key=scope_key,
        ),
    )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, scope_key, version_pin=version.id
    )
    if constant:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("UPDATE knowledge_entry SET constant = true WHERE knowledge_source_id = :s"),
                {"s": source.id},
            )


@pytest.mark.asyncio
async def test_a_constant_entry_activates_when_the_query_matches_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The opening turn of any session: no query text, nothing to match. A constant entry
    has to arrive anyway -- that is the entire meaning of the flag."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    await _attach_handbook(tenant_id, workspace_id, constant=True)

    activated = await _activate_for_workspace(
        tenant_id,
        workspace_id,
        scope_set=ScopeSet({"workspace_public"}),
        classes=["lore", "misc"],
        scan_text="",
        turn_index=0,
        session_id=uuid.uuid4(),
    )
    assert "lore" in activated, "a constant entry did not activate on an empty query"
    assert [a.why for a in activated["lore"]] == ["constant"]


@pytest.mark.asyncio
async def test_a_non_constant_entry_needs_its_keys_to_match(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The counterpart: activation is not "include everything". An ordinary entry with no
    matching keys stays out, so wiring this in did not turn every workspace's whole
    library into every turn's context."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    await _attach_handbook(tenant_id, workspace_id, constant=False)

    activated = await _activate_for_workspace(
        tenant_id,
        workspace_id,
        scope_set=ScopeSet({"workspace_public"}),
        classes=["lore"],
        scan_text="",
        turn_index=0,
        session_id=uuid.uuid4(),
    )
    assert activated == {}


@pytest.mark.asyncio
async def test_activation_respects_the_viewers_scope_set(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Constant means "always, for those entitled to it" -- never "always, for everyone".
    An entry in a compartment the viewer cannot read must not be activated into their
    turn, because activation feeds the context directly."""
    tenant_id, _ = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    await _attach_handbook(tenant_id, workspace_id, constant=True, scope_key="facilitator_only")

    outsider = await _activate_for_workspace(
        tenant_id,
        workspace_id,
        scope_set=ScopeSet({"workspace_public"}),
        classes=["lore"],
        scan_text="",
        turn_index=0,
        session_id=uuid.uuid4(),
    )
    assert outsider == {}, "a constant entry leaked past the viewer's scope set"

    entitled = await _activate_for_workspace(
        tenant_id,
        workspace_id,
        scope_set=ScopeSet({"workspace_public", "facilitator_only"}),
        classes=["lore"],
        scan_text="",
        turn_index=0,
        session_id=uuid.uuid4(),
    )
    assert "lore" in entitled
