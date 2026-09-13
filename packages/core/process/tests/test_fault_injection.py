"""T0.8 acceptance criterion: the message insert and its usage_record commit or roll
back together. Simulates a failure partway through the same transaction
``generate_agent_response`` uses (message + session_event + usage_record all in one
``tenant_scope()`` block) and confirms NOTHING from that block was persisted -- not just
that the two rows are consistent with each other, but that a mid-transaction failure
can't leave a half-written turn at all.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.process.skeleton import create_session
from core.sessions.models import MessageRow, SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def test_partial_write_rolls_back_entirely(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"fault-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    with pytest.raises(IntegrityError):
        async with tenant_scope(tenant_id) as session:
            session.add(
                MessageRow(
                    tenant_id=tenant_id,
                    session_id=sess.id,
                    event_seq=0,
                    author_principal_id=owner_id,
                    role="assistant",
                    content_md="this must not survive",
                )
            )
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
            # Deliberately invalid: usage_record.provider is NOT NULL. This is the
            # "something fails partway through" injection point.
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    session_id=sess.id,
                    provider=None,  # type: ignore[arg-type]
                    model="echo-1",
                    purpose="generation",
                    estimated_cost=Decimal("0"),
                )
            )
            await session.flush()

    async with tenant_scope(tenant_id) as session:
        messages = (
            await session.execute(select(MessageRow).where(MessageRow.session_id == sess.id))
        ).all()
        events = (
            await session.execute(
                select(SessionEventRow).where(SessionEventRow.session_id == sess.id)
            )
        ).all()
        usage = (
            await session.execute(
                select(UsageRecordRow).where(UsageRecordRow.session_id == sess.id)
            )
        ).all()

    assert messages == [], "message survived a transaction that should have rolled back"
    assert events == [], "session_event survived a transaction that should have rolled back"
    assert usage == [], "usage_record survived a transaction that should have rolled back"
