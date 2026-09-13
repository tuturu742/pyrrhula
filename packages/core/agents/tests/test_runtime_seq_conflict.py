"""The seq-conflict retry (process backlog): a turn whose peeked event_seq was taken
by another writer while its model call ran must land at a fresh seq, not lose the turn
to uq_session_event_seq."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from sqlalchemy import text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent
from core.agents.runtime import run_agent_turn
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolRegistry
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest
from core.process.skeleton import create_session
from core.sessions.models import MessageRow, SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


class _Provider:
    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        yield Chunk(text="my turn, late but landed.")

    def count_tokens(self, text_: str, model: str) -> int:
        return 1

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(supports_prompt_caching=False)


async def test_taken_seq_is_reclaimed_instead_of_losing_the_turn(db_available: None) -> None:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"seq-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    profile = await create_agent(
        tenant_id, "seq-profile", "echo", "echo-1", encryptor=IdentityEncryptor()
    )
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text("update persona set agent_id=:a where id=:p"),
            {"a": profile.id, "p": persona_id},
        )
        # Another writer already took seq 0 while this turn's model call was running.
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=sess.id,
                event_seq=0,
                kind="note",
                payload={"text": "someone else got here first"},
            )
        )

    result = await run_agent_turn(
        tenant_id,
        persona_id,
        sess.id,
        [{"role": "user", "content": "go"}],
        model_provider_factory=lambda _p: _Provider(),
        tool_registry=ToolRegistry(),
        idempotency_key=f"turn:{sess.id}:0:{persona_id}",
        event_seq=0,  # the stale, already-taken slot
    )

    async with tenant_scope(tenant_id) as session:
        message = await session.get(MessageRow, result.message_id)
        assert message is not None
        assert message.event_seq > 0, "the turn should have re-claimed a fresh seq"
        assert message.content_md == "my turn, late but landed."
