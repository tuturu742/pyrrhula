"""A persona holds the workspace membership its type implies, from the moment it exists.

Permission checks key on ``workspace_membership`` (``entity:create``, ``secret:create``
are workspace grants). Until this was in ``create_persona`` only the onboarding shortcut
granted one, so a cast made in the editor or imported from a bundle could speak but was
refused every sheet it rolled."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent, create_persona, update_persona
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _role_of(tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID) -> str:
    async with tenant_scope(tenant_id) as session:
        return await session.scalar(
            select(WorkspaceMembership.role).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )


async def test_a_new_persona_gets_the_workspace_role_its_type_implies(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"pm-{uuid.uuid4().hex[:8]}")
    profile = await create_agent(tenant_id, "echo", "echo", "echo-1", encryptor=IdentityEncryptor())
    for persona_type, role in (
        ("supervisor", "facilitator"),
        ("participant", "participant"),
        ("informational", "viewer"),
    ):
        persona = await create_persona(
            tenant_id,
            workspace_id,
            persona_type,
            persona_type,
            profile.id,
            persona_type=persona_type,
        )
        assert await _role_of(tenant_id, workspace_id, persona.principal_id) == role


async def test_changing_a_personas_type_changes_its_role(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"pm2-{uuid.uuid4().hex[:8]}")
    profile = await create_agent(tenant_id, "echo", "echo", "echo-1", encryptor=IdentityEncryptor())
    persona = await create_persona(tenant_id, workspace_id, "bram", "Bram", profile.id)
    assert await _role_of(tenant_id, workspace_id, persona.principal_id) == "participant"

    await update_persona(tenant_id, persona.id, persona_type="supervisor")
    assert await _role_of(tenant_id, workspace_id, persona.principal_id) == "facilitator"
