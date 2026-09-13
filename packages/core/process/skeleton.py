"""The deliberately dumb, deletable T0.8 walking skeleton: a hardcoded two-phase process
(``prompt`` — waiting for a user message — and ``respond`` — the agent is generating).

This exists only to prove the pipe end to end (plan §15.3) before any real feature
exists: request -> tenant-scoped persistence -> a real model call through the
``ModelProvider`` port -> streaming -> durable, same-transaction usage metering. The real
interpreter, arbitrary phase graphs, checkpoints, and await/resume land at B1.1-B1.6 and
replace this module; ``session``/``session_event``/``message``/``usage_record`` themselves
are NOT thrown away — B1.4/B1.7 extend this schema in place.

Two things are deliberately NOT imported here, for the same reason: this module is
``core`` and must never depend on ``api`` or on a specific ``adapters.*`` implementation
(the ports-and-adapters dependency rule — core depends on the *port*, the composition
root decides which concrete adapter to inject; adapters are hardcoded in exactly one
place, not scattered through core). Streaming delivery (Redis pub/sub, SSE): callers pass
``on_chunk``/``on_event`` callbacks — ``packages/api/routes/sessions.py`` wires those to
``api.streaming.pubsub``. Which ``ModelProvider`` adapter to use: callers pass a
``model_provider_factory``; the same file wires the ``echo``/real-provider selection.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.agents.models import Agent, Persona
from core.audit.models import PriceTableRow, UsageRecordRow
from core.observability.otel import get_tracer
from core.ports.model_provider import Chunk, GenerationRequest, ModelProvider
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope, unscoped_session

_tracer = get_tracer(__name__)

OnChunk = Callable[[str], Awaitable[None]]
OnEvent = Callable[[int, str, dict[str, Any]], Awaitable[None]]
ModelProviderFactory = Callable[[str], ModelProvider]


async def create_session(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, persona_id: uuid.UUID
) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        # Postgres foreign keys are enforced independently of RLS: a FK to a row that
        # exists in a *different* tenant still satisfies the constraint, since FK checks
        # run with elevated internal privileges that bypass RLS. Without this explicit,
        # RLS-filtered lookup, a caller could create a session referencing another
        # tenant's real agent/workspace by id and the INSERT would silently succeed.
        agent = await session.get(Persona, persona_id)
        if agent is None or agent.workspace_id != workspace_id:
            raise ValueError(
                f"agent {persona_id} not found in workspace {workspace_id} for this tenant"
            )

        row = SessionRow(tenant_id=tenant_id, workspace_id=workspace_id, persona_id=persona_id)
        session.add(row)
        await session.flush()
        return row


async def submit_user_message(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    principal_id: uuid.UUID,
    content: str,
    *,
    on_event: OnEvent | None = None,
) -> MessageRow:
    """The 'prompt' phase: append the user's message, log the durable event, transition
    to 'respond'. All in one transaction — a message that exists without its event, or
    vice versa, is exactly the kind of drift usage_record's docstring warns about."""
    with _tracer.start_as_current_span("skeleton.submit_user_message"):
        async with tenant_scope(tenant_id) as session:
            sess = await session.get(SessionRow, session_id)
            if sess is None:
                raise ValueError(f"no session {session_id} in this tenant")

            event_seq = sess.next_event_seq
            sess.next_event_seq = event_seq + 1
            sess.current_phase = "respond"

            message = MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                author_principal_id=principal_id,
                role="user",
                content_md=content,
            )
            session.add(message)
            await session.flush()  # populates message.id for the event payload below
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    kind="message",
                    payload={"id": str(message.id), "role": "user", "content": content},
                    actor_principal_id=principal_id,
                )
            )
            await session.flush()

        if on_event is not None:
            # D1.3: the message's own id, so the session view can fetch its
            # citations/resolutions (GET /messages/{id}/citations|resolutions) -- SSE
            # payloads never carried this before, which made both endpoints unreachable
            # from a live-streamed message.
            await on_event(
                event_seq, "message", {"id": str(message.id), "role": "user", "content": content}
            )
        return message


async def _estimate_cost(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal:
    async with unscoped_session() as session:
        row = await session.scalar(
            select(PriceTableRow)
            .where(PriceTableRow.provider == provider, PriceTableRow.model == model)
            .order_by(PriceTableRow.effective_from.desc())
            .limit(1)
        )
        if row is None:
            row = await session.scalar(
                select(PriceTableRow)
                .where(PriceTableRow.provider == provider, PriceTableRow.model == "*")
                .limit(1)
            )
        if row is None:
            row = await session.scalar(
                select(PriceTableRow)
                .where(PriceTableRow.provider == "*", PriceTableRow.model == "*")
                .limit(1)
            )
    if row is None:
        return Decimal(0)
    return (Decimal(prompt_tokens) / 1_000_000) * row.input_per_mtok + (
        Decimal(completion_tokens) / 1_000_000
    ) * row.output_per_mtok


async def generate_agent_response(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    model_provider_factory: ModelProviderFactory,
    on_chunk: OnChunk | None = None,
    on_event: OnEvent | None = None,
) -> AsyncIterator[str]:
    """The 'respond' phase: call the agent's model through the ModelProvider port,
    optionally streaming chunks live via ``on_chunk``, then persist the assistant
    message + its session_event + its usage_record in ONE transaction (§12.8: usage
    metering that can drift from the thing it meters will drift), and transition back to
    'prompt'.
    """
    with _tracer.start_as_current_span("skeleton.generate_agent_response") as span:
        async with tenant_scope(tenant_id) as session:
            sess = await session.get(SessionRow, session_id)
            if sess is None:
                raise ValueError(f"no session {session_id} in this tenant")
            agent = await session.get(Persona, sess.persona_id)
            assert agent is not None
            profile = await session.get(Agent, agent.agent_id)
            assert profile is not None

            history = (
                (
                    await session.execute(
                        select(MessageRow)
                        .where(MessageRow.session_id == session_id)
                        .order_by(MessageRow.event_seq)
                    )
                )
                .scalars()
                .all()
            )
            messages: list[dict[str, object]] = [
                {"role": m.role, "content": m.content_md} for m in history
            ]
            agent_principal_id = agent.principal_id
            persona_id = agent.id
            agent_id = profile.id
            provider_name, model_name = profile.provider, profile.model

        span.set_attribute("pyrrhula.provider", provider_name)
        span.set_attribute("pyrrhula.model", model_name)

        provider = model_provider_factory(provider_name)
        model_string = f"{provider_name}/{model_name}"
        req = GenerationRequest(
            model=model_string,
            messages=messages,
            purpose="generation",
            egress_policy=await load_egress_policy(tenant_id),
        )

        start = time.monotonic()
        full_text: list[str] = []
        cached_tokens = 0
        chunk: Chunk
        async for chunk in provider.generate(req):
            if chunk.text:
                full_text.append(chunk.text)
                if on_chunk is not None:
                    await on_chunk(chunk.text)
                yield chunk.text
            if chunk.cached_tokens:
                cached_tokens = chunk.cached_tokens
        latency_ms = int((time.monotonic() - start) * 1000)

        content = "".join(full_text)
        prompt_tokens = sum(
            provider.count_tokens(str(m.get("content") or ""), model_string) for m in messages
        )
        completion_tokens = provider.count_tokens(content, model_string)
        estimated_cost = await _estimate_cost(
            provider_name, model_name, prompt_tokens, completion_tokens
        )

        async with tenant_scope(tenant_id) as session:
            sess = await session.get(SessionRow, session_id)
            assert sess is not None
            event_seq = sess.next_event_seq
            sess.next_event_seq = event_seq + 1
            sess.current_phase = "prompt"

            message = MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                author_principal_id=agent_principal_id,
                role="assistant",
                content_md=content,
            )
            session.add(message)
            await session.flush()  # populates message.id for the event payload below
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    kind="message",
                    payload={"id": str(message.id), "role": "assistant", "content": content},
                    actor_principal_id=agent_principal_id,
                )
            )
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=sess.workspace_id,
                    session_id=session_id,
                    message_id=message.id,
                    persona_id=persona_id,
                    agent_id=agent_id,
                    provider=provider_name,
                    model=model_name,
                    phase="respond",
                    purpose="generation",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                    estimated_cost=estimated_cost,
                    latency_ms=latency_ms,
                )
            )
            await session.flush()

        if on_event is not None:
            await on_event(
                event_seq,
                "message",
                {"id": str(message.id), "role": "assistant", "content": content},
            )
