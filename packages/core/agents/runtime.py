"""The agent runtime (B1.7, plan §12.4, §13.6): executes one agent turn -- provider call,
streaming, an internal tool loop, retries + fallback profile, usage metering. The final
message, every usage_record from the turn (including tool-loop intermediate calls), and
the durable session event all commit in one transaction -- all or nothing, matching
T0.8's own "usage metering that drifts from the thing it meters will drift" principle,
now extended across a whole tool loop instead of one call.

**Idempotency is scoped to tool dispatch, not the provider call itself.** Retrying a
*failed* provider call is what retry-with-backoff means -- there's no "duplicate side
effect" to protect against there. A tool call that already *succeeded*, however, is a real
side effect (later: dice rolls, entity mutations, MCP calls) that must never run twice if
the surrounding turn is retried/resumed after a crash -- so only ``_dispatch_tool``
is wrapped in T0.7's ``@idempotent``, keyed on ``(idempotency_key, tool_call.id)``. This
mirrors B1.2's own scoping decision (idempotency around the external call, not around
retryable infrastructure) one layer up.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.actions.idempotency import idempotent
from core.agents.authoring import resolve_connection_api_key
from core.agents.models import Agent, Persona
from core.agents.tools import ToolContext, ToolRegistry
from core.audit.models import PriceTableRow, UsageRecordRow
from core.observability.otel import get_tracer
from core.ports.encryptor import Encryptor
from core.ports.model_provider import GenerationRequest, ModelProvider, ToolCall
from core.resolution.contradiction import scan_for_contradictions
from core.resolution.records import ResolutionRecordRow
from core.sessions.lifecycle import resolve_author_name
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope, unscoped_session

_tracer = get_tracer(__name__)

_DEFAULT_MAX_TOOL_LOOP = 8
_DEFAULT_MAX_RETRIES = 2  # attempts on the primary profile before falling back
_BACKOFF_BASE_SECONDS = 0.1

ModelProviderFactory = Callable[[str], ModelProvider]
OnChunk = Callable[[str], Awaitable[None]]


class AllRetriesExhaustedError(Exception):
    """Neither the primary profile (after retries) nor the fallback profile (if any)
    produced a successful response."""


class ToolLoopExceededError(Exception):
    """The model kept calling tools past ``max_tool_loop`` iterations without producing
    a final answer -- a runaway-loop guard, matching B1.2's own ``max_steps``."""


class EmptyGenerationError(Exception):
    """The provider streamed a response with no text and no tool calls. Ollama does
    this silently under model-swap pressure or context overflow; an empty turn is
    never a valid outcome, so it is raised inside the retry loop to buy another
    attempt (and ultimately the fallback profile) instead of persisting silence."""


def _extract_resolution_id(tool_result_content: str) -> str | None:
    """D1.3: ``make_dice_roller_handler`` (C1.6) reports its result as
    ``{"resolution_id": ..., "total": ..., "outcome": ...}`` JSON -- the same shape any
    future resolve()-backed tool would emit, so this isn't specific to ``dice_roller`` by
    name. A tool result that isn't that shape (a non-resolution tool, or a resolution
    error response with no ``resolution_id`` key) simply contributes nothing, not an
    error -- most tool calls in a turn aren't resolutions at all."""
    try:
        parsed = json.loads(tool_result_content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    resolution_id = parsed.get("resolution_id")
    return resolution_id if isinstance(resolution_id, str) else None


@dataclass(frozen=True)
class _UsagePoint:
    profile_id: uuid.UUID
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class TurnResult:
    content_md: str
    message_id: uuid.UUID
    usage_record_ids: tuple[uuid.UUID, ...]
    tool_calls_made: int


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
                select(PriceTableRow).where(
                    PriceTableRow.provider == "*", PriceTableRow.model == "*"
                )
            )
    if row is None:
        return Decimal(0)
    return (Decimal(prompt_tokens) / 1_000_000) * row.input_per_mtok + (
        Decimal(completion_tokens) / 1_000_000
    ) * row.output_per_mtok


async def _call_provider_with_retry(
    profile: Agent,
    fallback_profile: Agent | None,
    messages: list[dict[str, object]],
    tools: tuple[Any, ...],
    purpose: str,
    model_provider_factory: ModelProviderFactory,
    on_chunk: OnChunk | None,
    max_retries: int,
    cache_boundary_index: int | None,
    api_keys: dict[uuid.UUID, str | None] | None = None,
    egress_policy: dict[str, list[str]] | None = None,
) -> tuple[str, list[ToolCall], _UsagePoint]:
    """Tries ``profile`` up to ``max_retries`` times with exponential backoff; on total
    failure, tries ``fallback_profile`` once (if set). Raises
    ``AllRetriesExhaustedError`` if every attempt failed."""
    with _tracer.start_as_current_span("runtime.call_provider_with_retry") as span:
        candidates: list[Agent] = [profile] * max_retries
        if fallback_profile is not None:
            candidates.append(fallback_profile)

        last_exc: Exception | None = None
        for attempt_index, candidate in enumerate(candidates):
            is_fallback = fallback_profile is not None and candidate is fallback_profile
            try:
                provider = model_provider_factory(candidate.provider)
                model_string = f"{candidate.provider}/{candidate.model}"
                req = GenerationRequest(
                    model=model_string,
                    messages=messages,
                    purpose=purpose,
                    tools=tools,
                    cache_boundary_index=cache_boundary_index,
                    api_base=candidate.api_base,
                    api_key=(api_keys or {}).get(candidate.id),
                    egress_policy=egress_policy or {},
                )
                start = time.monotonic()
                full_text: list[str] = []
                tool_calls: tuple[ToolCall, ...] = ()
                cached_tokens = 0
                async for chunk in provider.generate(req):
                    if chunk.text:
                        full_text.append(chunk.text)
                        if on_chunk is not None:
                            await on_chunk(chunk.text)
                    if chunk.tool_calls:
                        tool_calls = chunk.tool_calls
                    if chunk.cached_tokens:
                        cached_tokens = chunk.cached_tokens
                latency_ms = int((time.monotonic() - start) * 1000)
                content = "".join(full_text)
                if not content.strip() and not tool_calls:
                    raise EmptyGenerationError(f"{model_string} returned an empty generation")
                prompt_tokens = sum(
                    provider.count_tokens(str(m.get("content") or ""), model_string)
                    for m in messages
                )
                completion_tokens = provider.count_tokens(content, model_string)
                usage = _UsagePoint(
                    candidate.id,
                    candidate.provider,
                    candidate.model,
                    prompt_tokens,
                    completion_tokens,
                    cached_tokens,
                    latency_ms,
                )
                span.set_attribute("pyrrhula.runtime.used_fallback", is_fallback)
                span.set_attribute("pyrrhula.runtime.attempts", attempt_index + 1)
                return content, list(tool_calls), usage
            except Exception as exc:  # noqa: BLE001 -- any provider failure triggers retry/fallback
                last_exc = exc
                if attempt_index < len(candidates) - 1:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt_index))
                continue

        raise AllRetriesExhaustedError(str(last_exc)) from last_exc


@idempotent(
    key_fn=lambda *_a, tenant_id, idempotency_key, **_k: idempotency_key  # noqa: ARG005
)
async def _dispatch_tool_idempotent(
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    tool_registry: ToolRegistry,
    tool_call: ToolCall,
    ctx: ToolContext,
) -> dict[str, Any]:
    result = await tool_registry.dispatch(tool_call, ctx)
    return {"content": result.content}


async def run_agent_turn(
    tenant_id: uuid.UUID,
    persona_id: uuid.UUID,
    session_id: uuid.UUID,
    messages: list[dict[str, object]],
    *,
    model_provider_factory: ModelProviderFactory,
    tool_registry: ToolRegistry,
    idempotency_key: str,
    encryptor: Encryptor | None = None,
    context_manifest_id: uuid.UUID | None = None,
    on_chunk: OnChunk | None = None,
    max_tool_loop: int = _DEFAULT_MAX_TOOL_LOOP,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    # C1.4: index into `messages` marking the prompt-cache stable/volatile boundary (see
    # core.assembler.layout.LayoutSections) -- a fixed position, valid across the whole
    # tool loop below, since tool-loop iterations only ever *append* to `conversation`.
    cache_boundary_index: int | None = None,
    # S2/S3 (E2.7 + G4.14): applied to the FINAL reply text before it is persisted --
    # the post-generation leak check and moderation live behind this seam, so the
    # durable message is always the checked one. None = passthrough.
    finalize_reply: Callable[[str], Awaitable[str]] | None = None,
    # Provenance: WHAT caused this turn to happen -- "scheduler" (the interpreter chose
    # the speaker autonomously), "conducted" (a human asked for this persona's turn),
    # "override" (a human wrote the words), "driver" (an automated conductor, e.g. a
    # benchmark runner). Recorded on the durable event so a transcript can say who
    # moved, the same way resolutions and disclosures carry their cause.
    triggered_by: str = "unknown",
    # B1.8: when the interpreter is driving this turn, it -- not this function -- is the
    # one that peeked the event_seq slot this turn must claim (its own idempotency key
    # is derived from that same peeked value). None (every pre-B1.8 call site) preserves
    # the original self-claim-from-session_row behaviour exactly.
    event_seq: int | None = None,
) -> TurnResult:
    with _tracer.start_as_current_span("runtime.run_agent_turn") as span:
        span.set_attribute("pyrrhula.persona_id", str(persona_id))

        async with tenant_scope(tenant_id) as session:
            agent = await session.get(Persona, persona_id)
            assert agent is not None
            profile = await session.get(Agent, agent.agent_id)
            assert profile is not None
            fallback_profile = (
                await session.get(Agent, profile.fallback_agent_id)
                if profile.fallback_agent_id is not None
                else None
            )
            agent_principal_id = agent.principal_id
            workspace_id = agent.workspace_id

        # Hard daily caps (core.usage_limits) -- checked before any provider call.
        from core.usage_limits import ensure_within_limits

        await ensure_within_limits(tenant_id, agent_id=profile.id, persona_id=persona_id)

        # Resolve the connection credentials once (per profile + fallback), keyed by
        # connection id, so a cloud provider gets its api_key. No encryptor -> no keys
        # (local providers need none), preserving every existing call site.
        api_keys: dict[uuid.UUID, str | None] = {}
        if encryptor is not None:
            api_keys[profile.id] = await resolve_connection_api_key(
                tenant_id, profile.credential_ref, encryptor=encryptor
            )
            if fallback_profile is not None:
                api_keys[fallback_profile.id] = await resolve_connection_api_key(
                    tenant_id, fallback_profile.credential_ref, encryptor=encryptor
                )

        from core.tenancy.egress import load_egress_policy

        egress_policy = await load_egress_policy(tenant_id)
        conversation = list(messages)
        usage_points: list[_UsagePoint] = []
        tool_calls_made = 0
        resolution_record_ids: list[str] = []
        tools = tool_registry.specs()

        for _iteration in range(max_tool_loop):
            content, tool_calls, usage = await _call_provider_with_retry(
                profile,
                fallback_profile,
                conversation,
                tools,
                "generation",
                model_provider_factory,
                on_chunk,
                max_retries,
                cache_boundary_index,
                api_keys,
                egress_policy,
            )
            usage_points.append(usage)

            if not tool_calls:
                if finalize_reply is not None:
                    content = await finalize_reply(content)
                return await _commit_turn(
                    triggered_by=triggered_by,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    author_principal_id=agent_principal_id,
                    workspace_id=workspace_id,
                    persona_id=persona_id,
                    content=content,
                    usage_points=usage_points,
                    tool_calls_made=tool_calls_made,
                    resolution_record_ids=resolution_record_ids,
                    context_manifest_id=context_manifest_id,
                    event_seq=event_seq,
                )

            # The assistant turn that REQUESTED the tools must carry tool_calls back in
            # the transcript, in the OpenAI shape. OpenAI and ollama tolerate its
            # absence; DeepSeek strictly rejects a following role:"tool" message whose
            # preceding assistant message has no tool_calls.
            conversation.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tool_call in tool_calls:
                tool_calls_made += 1
                tool_result = await _dispatch_tool_idempotent(
                    tenant_id=tenant_id,
                    idempotency_key=f"{idempotency_key}:tool:{tool_call.id}",
                    tool_registry=tool_registry,
                    tool_call=tool_call,
                    ctx=ToolContext(
                        tenant_id=tenant_id,
                        persona_id=persona_id,
                        session_id=session_id,
                        turn_event_seq=event_seq,
                    ),
                )
                resolution_id = _extract_resolution_id(tool_result["content"])
                if resolution_id is not None:
                    resolution_record_ids.append(resolution_id)
                conversation.append(
                    {
                        "role": "tool",
                        "content": tool_result["content"],
                        "tool_call_id": tool_call.id,
                    }
                )

        raise ToolLoopExceededError(f"tool loop exceeded {max_tool_loop} iterations")


async def _commit_turn(
    *,
    _retry_on_seq_conflict: bool = True,
    triggered_by: str = "unknown",
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author_principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    persona_id: uuid.UUID,
    content: str,
    usage_points: list[_UsagePoint],
    tool_calls_made: int,
    resolution_record_ids: list[str],
    context_manifest_id: uuid.UUID | None,
    event_seq: int | None = None,
) -> TurnResult:
    """One transaction: the message, every usage_record from the loop, the durable
    session_event, and -- when this turn's tool calls produced any ResolutionRecords
    (C1.6) -- the contradiction scan (C1.7) against this same reply, all or nothing.

    B1.8: ``event_seq`` -- when given (the interpreter driving this turn already peeked
    it) -- is used directly instead of reading ``session_row.next_event_seq``, but this
    function still performs the ``next_event_seq`` claim itself either way: whichever
    transaction actually persists the message is the one that owns the claim, so there
    is never a second, independent counter-advancing code path for the same turn.

    A peeked seq can go stale while a slow model call runs (another writer -- a
    delegation note, a concurrently conducted persona -- takes the slot first). That is
    a race, not a fault: on ``uq_session_event_seq`` this retries ONCE with a freshly
    claimed seq rather than failing the whole turn (observed live as lost turns during
    a conducted session)."""
    try:
        return await _commit_turn_once(
            triggered_by=triggered_by,
            tenant_id=tenant_id,
            session_id=session_id,
            author_principal_id=author_principal_id,
            workspace_id=workspace_id,
            persona_id=persona_id,
            content=content,
            usage_points=usage_points,
            tool_calls_made=tool_calls_made,
            resolution_record_ids=resolution_record_ids,
            context_manifest_id=context_manifest_id,
            event_seq=event_seq,
        )
    except IntegrityError as exc:
        # The pre-check inside _commit_turn_once closes the common case; this catches
        # the genuinely concurrent one (another committer between our check and insert).
        if not _retry_on_seq_conflict or "uq_session_event_seq" not in str(exc):
            raise
    # Fresh claim: pass None so the commit reads next_event_seq itself.
    return await _commit_turn_once(
        triggered_by=triggered_by,
        tenant_id=tenant_id,
        session_id=session_id,
        author_principal_id=author_principal_id,
        workspace_id=workspace_id,
        persona_id=persona_id,
        content=content,
        usage_points=usage_points,
        tool_calls_made=tool_calls_made,
        resolution_record_ids=resolution_record_ids,
        context_manifest_id=context_manifest_id,
        event_seq=None,
    )


async def _commit_turn_once(
    *,
    triggered_by: str = "unknown",
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    author_principal_id: uuid.UUID,
    workspace_id: uuid.UUID,
    persona_id: uuid.UUID,
    content: str,
    usage_points: list[_UsagePoint],
    tool_calls_made: int,
    resolution_record_ids: list[str],
    context_manifest_id: uuid.UUID | None,
    event_seq: int | None = None,
) -> TurnResult:
    async with tenant_scope(tenant_id) as session:
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        if event_seq is None:
            event_seq = session_row.next_event_seq
        else:
            # A peeked seq goes stale when another writer (a delegation note, a
            # concurrently conducted persona) claims it during a slow model call. Take
            # the next free slot instead of losing the turn to uq_session_event_seq --
            # the unique index remains the real guard; this only avoids the avoidable
            # collision. Checked in THIS transaction, immediately before the insert.
            taken = await session.scalar(
                select(SessionEventRow.event_seq).where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.event_seq == event_seq,
                )
            )
            if taken is not None:
                event_seq = max(session_row.next_event_seq, event_seq + 1)
        # max(), never a plain overwrite: a turn commits long after its seq was peeked
        # (a slow model call), and other writers (delegation notes, reserved delegation
        # seqs) may have advanced the counter meanwhile -- rewinding it hands their slots
        # out twice (observed live as uq_session_event_seq violations on notes).
        session_row.next_event_seq = max(session_row.next_event_seq, event_seq + 1)

        message = MessageRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=event_seq,
            author_principal_id=author_principal_id,
            role="assistant",
            content_md=content,
            context_manifest_id=context_manifest_id,
            resolution_record_ids=resolution_record_ids,
        )
        session.add(message)
        await session.flush()

        if resolution_record_ids:
            records = (
                (
                    await session.execute(
                        select(ResolutionRecordRow).where(
                            ResolutionRecordRow.id.in_(
                                uuid.UUID(rid) for rid in resolution_record_ids
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            flags = scan_for_contradictions(content, list(records))
            if flags:
                message.moderation_flags = {"contradiction": [str(f.record_id) for f in flags]}

        usage_record_ids: list[uuid.UUID] = []
        for point in usage_points:
            estimated_cost = await _estimate_cost(
                point.provider, point.model, point.prompt_tokens, point.completion_tokens
            )
            usage_row = UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                session_id=session_id,
                message_id=message.id,
                persona_id=persona_id,
                agent_id=point.profile_id,
                provider=point.provider,
                model=point.model,
                purpose="generation",
                prompt_tokens=point.prompt_tokens,
                completion_tokens=point.completion_tokens,
                cached_tokens=point.cached_tokens,
                estimated_cost=estimated_cost,
                latency_ms=point.latency_ms,
            )
            session.add(usage_row)
            await session.flush()
            usage_record_ids.append(usage_row.id)

        author_name = await resolve_author_name(session, tenant_id, author_principal_id)
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={
                    "id": str(message.id),
                    "role": "assistant",
                    "content": content,
                    "tool_calls_made": tool_calls_made,
                    "author": author_name,
                    # so the transcript can show when each turn landed, on replay too
                    "created_at": datetime.now(UTC).isoformat(),
                    "triggered_by": triggered_by,
                },
                actor_principal_id=author_principal_id,
            )
        )

        return TurnResult(
            content_md=content,
            message_id=message.id,
            usage_record_ids=tuple(usage_record_ids),
            tool_calls_made=tool_calls_made,
        )
