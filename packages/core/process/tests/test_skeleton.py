"""T0.8 walking skeleton: engine-level tests (no HTTP), using the EchoModelProvider so
they're deterministic and don't need a live Ollama.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from adapters.models.echo.provider import EchoModelProvider
from core.agents.seed import seed_dev_agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import ModelProvider
from core.process.skeleton import create_session, generate_agent_response, submit_user_message
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _echo_factory(provider_name: str) -> ModelProvider:
    return EchoModelProvider()


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    return tenant_id, owner_id, workspace_id, persona_id


async def test_full_round_trip_writes_message_event_and_usage(db_available: None) -> None:
    tenant_id, owner_id, workspace_id, persona_id = await _setup("skel")
    sess = await create_session(tenant_id, workspace_id, persona_id)
    assert sess.current_phase == "prompt"

    user_msg = await submit_user_message(tenant_id, sess.id, owner_id, "hello there")
    assert user_msg.role == "user"

    async with tenant_scope(tenant_id) as session:
        mid_sess = await session.get(SessionRow, sess.id)
        assert mid_sess is not None
        assert mid_sess.current_phase == "respond"

    chunks = [
        c
        async for c in generate_agent_response(
            tenant_id, sess.id, model_provider_factory=_echo_factory
        )
    ]
    assert "".join(chunks).strip() == "echo: hello there"

    async with tenant_scope(tenant_id) as session:
        final_sess = await session.get(SessionRow, sess.id)
        assert final_sess is not None
        assert final_sess.current_phase == "prompt"  # transitioned back

        messages = (
            (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.session_id == sess.id)
                    .order_by(MessageRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].content_md.strip() == "echo: hello there"

        events = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(SessionEventRow.session_id == sess.id)
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert [e.event_seq for e in events] == [0, 1]
        # D1.3: each message event's payload carries the message's own id, so a
        # streamed session view can fetch that message's citations/resolutions.
        assert events[0].payload["id"] == str(messages[0].id)
        assert events[1].payload["id"] == str(messages[1].id)

        usage = (
            (
                await session.execute(
                    select(UsageRecordRow).where(UsageRecordRow.session_id == sess.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(usage) == 1
        assert usage[0].purpose == "generation"
        assert usage[0].provider == "echo"
        assert usage[0].prompt_tokens > 0
        assert usage[0].completion_tokens > 0


async def test_create_session_rejects_agent_from_different_workspace(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id, persona_id = await _setup("skel-x")
    bogus_workspace_id = uuid.uuid4()

    with pytest.raises(ValueError, match="not found"):
        await create_session(tenant_id, bogus_workspace_id, persona_id)


async def test_create_session_rejects_agent_from_different_tenant(db_available: None) -> None:
    tenant_a, _owner_a, workspace_a, _agent_a = await _setup("skel-a")
    tenant_b, _owner_b, workspace_b, agent_b = await _setup("skel-b")

    # tenant_b's agent is invisible under tenant_a's RLS scope -- session.get() returns
    # None, which create_session must treat as "not found", not silently succeed via the
    # FK constraint (which would bypass RLS -- see docs/agent-guide.md Sec8).
    with pytest.raises(ValueError, match="not found"):
        await create_session(tenant_a, workspace_a, agent_b)


async def test_second_tenant_cannot_see_first_tenants_session(db_available: None) -> None:
    tenant_a, owner_a, workspace_a, agent_a = await _setup("skel-iso-a")
    tenant_b, _owner_b, _workspace_b, _agent_b = await _setup("skel-iso-b")

    sess = await create_session(tenant_a, workspace_a, agent_a)
    await submit_user_message(tenant_a, sess.id, owner_a, "secret")

    async with tenant_scope(tenant_b) as session:
        rows = (await session.execute(select(SessionRow).where(SessionRow.id == sess.id))).all()
    assert rows == []
