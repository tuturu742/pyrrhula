"""Turn provenance: the durable message event records WHAT caused the turn, so a
transcript can distinguish an autonomous turn from one a person -- or a script --
asked for. (A live spectator saw 'managed mode' while messages flowed on their own;
the record now says which.)"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.runtime import run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolRegistry
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.process.skeleton import create_session
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


class _Provider:
    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        yield Chunk(text="spoken.")

    def count_tokens(self, text_: str, model: str) -> int:
        return 1

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


async def test_message_event_records_what_triggered_the_turn(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"trig-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "trig-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )

    await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "go"}],
        model_provider_factory=lambda _p: _Provider(),
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{sess.id}:0:{persona_id}",
        event_seq=0,
        triggered_by="driver",
    )

    async with tenant_scope(tenant_id) as session:
        event = (
            (
                await session.execute(
                    select(SessionEventRow).where(
                        SessionEventRow.session_id == sess.id, SessionEventRow.kind == "message"
                    )
                )
            )
            .scalars()
            .one()
        )
    assert event.payload["triggered_by"] == "driver"
    assert event.payload["created_at"]  # timestamps ride along for the transcript
