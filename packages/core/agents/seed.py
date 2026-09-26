"""Dev seed helper: a demo agent + model profile in a workspace. Not a runtime path —
used by the walking-skeleton tests and by manual/live demos so there's something to
create a session against without going through the full agent-management CRUD.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.models import Agent, Persona
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope


async def seed_dev_agent(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    key: str = "arbiter",
    name: str = "Arbiter",
    provider: str = "echo",
    model: str = "echo-1",
    persona_type: str = "supervisor",
    fallback_agent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Idempotent: returns the existing agent's id if one with this key already exists."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(Persona).where(Persona.tenant_id == tenant_id, Persona.key == key)
        )
        if existing is not None:
            return existing.id

        agent_principal = Principal(tenant_id=tenant_id, kind="agent", display_name=name)
        session.add(agent_principal)
        await session.flush()

        profile = Agent(
            tenant_id=tenant_id,
            name=f"{name} model",
            provider=provider,
            model=model,
            fallback_agent_id=fallback_agent_id,
        )
        session.add(profile)
        await session.flush()

        agent = Persona(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=agent_principal.id,
            key=key,
            name=name,
            agent_id=profile.id,
            persona_type=persona_type,
        )
        session.add(agent)
        await session.flush()
        return agent.id
