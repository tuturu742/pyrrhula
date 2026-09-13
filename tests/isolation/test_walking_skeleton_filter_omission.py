"""Filter-omission coverage (T0.4/T0.8) for the walking skeleton's tables:
``agent``, ``agent``, ``session``, ``session_event``, ``message``,
``usage_record``. Same pattern as ``test_filter_omission_matrix.py`` — a raw query with
no ``WHERE tenant_id = ...`` clause, scoped to tenant A, must never surface tenant B's
rows.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select, text

from core.agents.models import Agent, Persona
from core.audit.models import UsageRecordRow
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.models import Principal, Workspace
from core.tenancy.scope import tenant_scope


async def _seed_agent(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, owner_id: uuid.UUID
) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        profile = Agent(tenant_id=tenant_id, name="test", provider="echo", model="echo-1")
        session.add(profile)
        await session.flush()
        agent = Persona(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            principal_id=owner_id,
            key="test-agent",
            name="Test Persona",
            agent_id=profile.id,
        )
        session.add(agent)
        await session.flush()
        return agent.id


async def _seed_full_chain(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, owner_id: uuid.UUID
) -> None:
    persona_id = await _seed_agent(tenant_id, workspace_id, owner_id)
    async with tenant_scope(tenant_id) as session:
        sess = SessionRow(tenant_id=tenant_id, workspace_id=workspace_id, persona_id=persona_id)
        session.add(sess)
        await session.flush()
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                event_seq=0,
                kind="message",
                payload={},
                actor_principal_id=owner_id,
            )
        )
        session.add(
            MessageRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                event_seq=0,
                author_principal_id=owner_id,
                role="user",
                content_md="hello",
            )
        )
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                persona_id=persona_id,
                provider="echo",
                model="echo-1",
                purpose="generation",
                estimated_cost=Decimal("0"),
            )
        )


async def _seed_both_tenants(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    for tenant_id in two_tenants:
        async with tenant_scope(tenant_id) as session:
            workspace_id = (
                await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
            ).scalar_one()
            owner_id = (
                await session.execute(select(Principal.id).where(Principal.tenant_id == tenant_id))
            ).scalar_one()
        await _seed_full_chain(tenant_id, workspace_id, owner_id)


async def test_model_profile_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM agent"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_agent_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM agent"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_session_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM session"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_session_event_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM session_event"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_message_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM message"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_usage_record_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    await _seed_both_tenants(two_tenants)

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM usage_record"))).all()
    assert {row[0] for row in rows} == {tenant_a}
