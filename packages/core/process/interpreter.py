"""The process interpreter: the loop that executes a validated
ProcessDefinition against a session. It is the real engine behind every roster session;
the hardcoded 2-phase ``core.process.skeleton`` remains only for the legacy single-persona
``/sessions`` path, and nothing in this module touches that surface. The agent runtime
supplies ``execute_turn`` and the scheduler supplies ``next_actor_fn``; this module owns
neither.

**Actor resolution is entirely injected, not implemented here.** Two reasons: (1) the
``persona_type`` lives on the persona table and this module must not query it directly,
so it cannot ask "which personas have role X" even if it wanted to; (2) turn
ordering (declared/initiative/free, cursor persistence across resume) is the whole job,
not something to half-build inline here just because it runs first in the dependency
order. ``next_actor_fn``/``execute_turn`` are the seam the scheduler and the agent
runtime slot their implementations into, matching the ``model_provider_factory``/
``on_chunk`` injection pattern established for the same reason (core must not import a
specific adapter, and here additionally: core must not depend on schema that doesn't
exist yet).

**Checkpointing is a hook, not implemented here** -- the checkpoint module owns the
real ``checkpoint`` table and write. ``checkpoint_hook`` is called at every transition if
provided; ``None`` (the default) is a legitimate, documented no-op for now, not a stub
pretending to be complete.

**CEL evaluation results are explicitly re-typed against the DSL's own declared ``state:``
types before being stored**, not stored as whatever celpy's internal type happens to be.
This matters concretely: celpy's ``BoolType`` subclasses Python ``int``, not ``bool`` --
``json.dumps(BoolType(True))`` serialises as ``1``, not ``true``. Storing a CEL boolean
result into a JSONB ``state`` column without coercing it first would silently corrupt a
`boolean` state variable into an integer the moment it's next read back.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import celpy
from celpy.adapter import json_to_cel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.actions.idempotency import idempotent
from core.observability.otel import get_tracer
from core.process.dsl.schema import PhaseSpec, ProcessDefinitionDSL, StateVarSpec
from core.sessions.lifecycle import resolve_author_name
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope

_tracer = get_tracer(__name__)
_cel_env = celpy.Environment()

_MAX_STEPS_PER_ADVANCE = 200

_COERCE_BY_TYPE: dict[str, Callable[[Any], object]] = {
    "integer": int,
    "number": float,
    "string": str,
    "boolean": bool,
}


class InterpreterFaultError(Exception):
    """Raised internally when a step can't complete for a reason that isn't itself a bug
    in the caller's setup (e.g. a CEL runtime error against real state) -- caught by
    ``advance_session``, which pauses the session rather than leaving it stuck mid-lock."""


class HumanTurnPendingError(Exception):
    """raised by an ``execute_turn`` implementation when ``actor.mode == "free"``
    -- a human-typed turn can't be synthesized synchronously the way a model-generated
    one can. Not a fault: ``advance_session`` catches this specifically and returns
    ``'awaiting_human'`` without pausing the session. This is what makes a phase like
    ``MINIMAL_MVP_FLOW``'s ``player_act`` (a free-mode human actor with **no** ``await``
    block) work: the scheduler's cursor has already durably advanced past this actor by
    the time this is raised (``core.process.scheduler.make_scheduler``'s ``next_actor``
    persists the cursor as a side effect of being called, before this exception ever
    propagates) -- so a later ``submit_human_turn`` + re-``advance_session`` resumes
    correctly, never re-offering or skipping the turn."""


@dataclass(frozen=True)
class ActorRef:
    """An abstract "who's up next" the interpreter attributes a turn to -- resolved by
    the injected ``next_actor_fn``, never looked up here."""

    principal_id: uuid.UUID
    mode: str  # 'free' | 'generate' | 'generate_as'


@dataclass(frozen=True)
class ActorTurnResult:
    content_md: str
    # set by an execute_turn implementation that already performed its own full
    # commit (core.agents.runtime.run_agent_turn's _commit_turn writes the message,
    # usage records, resolution correlation, and contradiction scan all in one
    # transaction) -- _run_actor_turn must not write a second, poorer MessageRow for the
    # same turn in that case. False (the original execute_turn contract) preserves the
    # original "the interpreter writes the message" behaviour exactly.
    already_persisted: bool = False
    message_id: uuid.UUID | None = None


@dataclass(frozen=True)
class InterpreterContext:
    """Everything a ``next_actor_fn``/``execute_turn`` implementation needs, read-only.
    ``state`` is the session's current typed variables (already merged with the
    definition's declared defaults at session start -- see ``start_session``)."""

    tenant_id: uuid.UUID
    session_id: uuid.UUID
    phase_key: str
    phase: PhaseSpec
    state: dict[str, Any]
    # the event_seq this turn's execute_turn call must claim if it does its own
    # commit (see ActorTurnResult.already_persisted) -- only meaningful inside
    # _run_actor_turn's own ctx construction, where it's the real peeked value; every
    # other ctx construction in this module (next_actor_fn/checkpoint_hook/on_await) has
    # no turn in flight yet, so it's left at the default.
    event_seq: int = 0


NextActorFn = Callable[[InterpreterContext], Awaitable[ActorRef | None]]
ActorTurnExecutor = Callable[[ActorRef, InterpreterContext], Awaitable[ActorTurnResult]]
CheckpointHook = Callable[[InterpreterContext], Awaitable[None]]
# called when the interpreter yields at an unsatisfied await, before falling back
# to the plain status='awaiting' flip. None (the default) preserves the original
# behaviour exactly -- core.process.awaits.make_await_hook is the real implementation
# that persists a real await_state row instead.
OnAwaitHook = Callable[[InterpreterContext], Awaitable[None]]
# live SSE delivery for interpreter-driven events -- matches
# core.process.skeleton's identical OnEvent shape (not imported from there: skeleton is
# the deliberately deletable walking skeleton, this module must not depend on it).
OnEvent = Callable[[int, str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class AdvanceResult:
    status: str  # 'active' | 'awaiting' | 'awaiting_human' | 'paused' | 'terminal'
    steps_taken: int
    final_phase: str
    flags: tuple[str, ...]


async def start_session(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    process_definition_id: uuid.UUID,
    process_definition_version: int,
) -> None:
    """Pins a session to a specific, immutable definition version and applies the DSL's
    declared ``state:`` defaults -- once, at start, matching 's "defaults applied at
    session start" (the schema declares the defaults; this is where they're realised)."""
    defaults: dict[str, object] = {name: spec.default for name, spec in definition.state.items()}
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        row.process_definition_id = process_definition_id
        row.process_definition_version = process_definition_version
        row.current_phase = definition.initial_phase
        row.state = defaults


def _eval_cel(expression: str, state: dict[str, Any]) -> Any:
    try:
        ast = _cel_env.compile(expression)
        program = _cel_env.program(ast)
        activation = json_to_cel({"state": state})
        return program.evaluate(activation)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 -- any CEL failure here is an interpreter fault
        raise InterpreterFaultError(f"CEL expression {expression!r} failed: {exc}") from exc


def evaluate_gates(phase: PhaseSpec, state: dict[str, Any]) -> str | None:
    """Declaration order, first match wins (the validator already guarantees an
    ``else`` gate, if any, is last). Returns the target phase key, or None if nothing
    matched -- ``on:`` event gates never match here (no event has occurred; the
    interpreter only calls this once a phase's actors are exhausted, not on an external
    event), and are structurally guaranteed present-but-unmatchable in that case."""
    for gate in phase.gates:
        if gate.else_:
            return gate.to
        if gate.when is not None and bool(_eval_cel(gate.when, state)):
            return gate.to
    return phase.on_complete


def apply_effects(
    phase: PhaseSpec, state: dict[str, Any], state_vars: dict[str, StateVarSpec]
) -> dict[str, Any]:
    new_state = dict(state)
    for effect in phase.effects:
        raw = _eval_cel(effect.to, new_state)
        var_spec = state_vars.get(effect.set)
        if var_spec is None:
            raise InterpreterFaultError(f"effect sets undeclared state variable {effect.set!r}")
        # Explicit re-typing against the *declared* state var type, not celpy's own type
        # (see module docstring: celpy's BoolType is a JSON-serialization trap).
        new_state[effect.set] = _COERCE_BY_TYPE[var_spec.type](raw)
    return new_state


async def _next_event_seq(session: AsyncSession, session_id: uuid.UUID) -> int:
    row = await session.get(SessionRow, session_id)
    assert row is not None
    seq: int = row.next_event_seq
    row.next_event_seq = seq + 1
    return seq


async def _pause_with_fault(tenant_id: uuid.UUID, session_id: uuid.UUID, message: str) -> None:
    """A separate, clean transaction from whatever failed -- the whole point is that a
    fault never leaves the session stuck mid-advance holding no recorded state."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            return
        row.status = "paused"
        event_seq = await _next_event_seq(session, session_id)
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="error",
                payload={"message": message},
            )
        )


@dataclass(frozen=True)
class _TransitionOutcome:
    new_phase_key: str
    terminal: bool


async def _phase_entry_seq(session: AsyncSession, session_id: uuid.UUID) -> int:
    """The event_seq of the transition that entered the current phase, or 0.

    Counting "what this phase produced" needs a floor, and the phase's own entry event is
    the honest one: it is written in the same transaction that set ``current_phase``, so
    nothing can land between them.
    """
    seq = await session.scalar(
        select(SessionEventRow.event_seq)
        .where(
            SessionEventRow.session_id == session_id,
            SessionEventRow.kind == "phase_transition",
        )
        .order_by(SessionEventRow.event_seq.desc())
        .limit(1)
    )
    return int(seq or 0)


async def measure_phase(tenant_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, int]:
    """How much this phase has actually produced, by kind.

    Counts rows written at or after the phase's entry event. Deliberately cheap and
    deliberately dumb: four counts, no interpretation. What they are *worth* is the
    flow author's call, declared in ``requires``.
    """
    from sqlalchemy import func

    from core.actions.effectful import ActionRecordRow
    from core.entities.fsm import EntityStateChangeRow
    from core.entities.storage import EntityRow
    from core.mcp.registry import McpCallRecord
    from core.resolution.records import ResolutionRecordRow

    async with tenant_scope(tenant_id) as session:
        floor = await _phase_entry_seq(session, session_id)
        # Entities carry no event_seq, so they are floored by time instead: the moment
        # the phase was entered.
        entry_at = await session.scalar(
            select(SessionEventRow.created_at).where(
                SessionEventRow.session_id == session_id,
                SessionEventRow.event_seq == floor,
            )
        ) or datetime.min.replace(tzinfo=UTC)

        async def _count(model: Any, seq_col: Any) -> int:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.session_id == session_id, seq_col >= floor)
                )
                or 0
            )

        resolutions = await _count(ResolutionRecordRow, ResolutionRecordRow.event_seq)
        entity_changes = int(
            await session.scalar(
                select(func.count())
                .select_from(EntityStateChangeRow)
                .where(
                    EntityStateChangeRow.session_id == session_id,
                    EntityStateChangeRow.event_seq >= floor,
                )
            )
            or 0
        )
        messages = int(
            await session.scalar(
                select(func.count())
                .select_from(SessionEventRow)
                .where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.kind == "message",
                    SessionEventRow.event_seq >= floor,
                )
            )
            or 0
        )
        entities_touched = int(
            await session.scalar(
                select(func.count())
                .select_from(EntityRow)
                .where(
                    EntityRow.origin_session_id == session_id,
                    EntityRow.updated_at >= entry_at,
                )
            )
            or 0
        )
        tool_calls = int(
            await session.scalar(
                select(func.count())
                .select_from(ActionRecordRow)
                .where(
                    ActionRecordRow.session_id == session_id,
                    ActionRecordRow.event_seq >= floor,
                )
            )
            or 0
        )
        # Read-only remote calls (a web search, a page fetch, a lab query) never reach
        # the effectful ledger above; they are recorded in mcp_call_record only. A beat
        # whose requirement is "the desks must have looked something up" counted zero
        # for every search it made (newsroom sweep, 2026-10-04: two searches recorded,
        # produced=0). Effectful calls are already counted, so only the read-only rows
        # are added, and a call refused by the session cap was never made.
        tool_calls += int(
            await session.scalar(
                select(func.count())
                .select_from(McpCallRecord)
                .where(
                    McpCallRecord.session_id == session_id,
                    McpCallRecord.event_seq >= floor,
                    McpCallRecord.effectful.is_(False),
                    McpCallRecord.outcome != "refused",
                )
            )
            or 0
        )

    return {
        "resolutions": resolutions,
        "messages": messages,
        "tool_calls": tool_calls,
        "entity_changes": entity_changes,
        "entities_touched": entities_touched,
    }


def unmet_requirements(spec: Any, produced: dict[str, int]) -> dict[str, dict[str, int]]:
    """Which declared requirements this phase has not reached, with both numbers."""
    shortfall: dict[str, dict[str, int]] = {}
    for field in ("resolutions", "messages", "tool_calls", "entity_changes", "entities_touched"):
        wanted = int(getattr(spec, field, 0) or 0)
        if wanted and produced.get(field, 0) < wanted:
            shortfall[field] = {"required": wanted, "produced": produced.get(field, 0)}
    return shortfall


async def _enforce_phase_requirements(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    phase_key: str,
    phase: PhaseSpec,
    on_event: OnEvent | None,
) -> str:
    """Decide whether this phase may leave, and record why either way.

    Returns ``"repeat"`` (run the actors again), ``"hold"`` (park for a person) or
    ``"pass"`` (transition normally). Every outcome writes a ``phase_requirement`` event:
    a beat that was let through thin is exactly the thing nobody notices, so it says so
    in the transcript rather than only in a log line.
    """
    spec = phase.requires
    assert spec is not None
    produced = await measure_phase(tenant_id, session_id)
    shortfall = unmet_requirements(spec, produced)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        # Derived, not stored. The scheduler rewrites ``actor_cursor`` wholesale on its
        # next call, so a counter kept there is erased by the very rotation it is meant
        # to bound -- which showed up as a phase repeating until max_steps. The decisions
        # are already in the event log and the phase entry is already the floor, so the
        # count is a question the record can answer.
        floor = await _phase_entry_seq(session, session_id)
        repeats = int(
            await session.scalar(
                select(func.count())
                .select_from(SessionEventRow)
                .where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.kind == "phase_requirement",
                    SessionEventRow.event_seq >= floor,
                    SessionEventRow.payload["decision"].astext == "repeat",
                )
            )
            or 0
        )

        if not shortfall:
            decision = "met"
        elif spec.on_unmet == "warn":
            decision = "passed_unmet"
        elif repeats < spec.max_repeats:
            decision = "repeat"
        elif spec.on_unmet == "hold":
            decision = "hold"
        else:
            decision = "passed_unmet"

        if decision == "repeat":
            # A fresh rotation: the scheduler starts a phase over when the cursor does
            # not name it, so every seat gets another pass at the beat it did not finish.
            row.actor_cursor = {}

        event_seq = await _next_event_seq(session, session_id)
        payload: dict[str, Any] = {
            "phase": phase_key,
            "decision": decision,
            "produced": produced,
            "unmet": shortfall,
            "repeat": repeats + (1 if decision == "repeat" else 0),
            "max_repeats": spec.max_repeats,
        }
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="phase_requirement",
                payload=payload,
            )
        )
        await session.flush()

    if on_event is not None:
        await on_event(event_seq, "phase_requirement", payload)

    return {"repeat": "repeat", "hold": "hold"}.get(decision, "pass")


async def _transition(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    phase_key: str,
    phase: PhaseSpec,
    checkpoint_hook: CheckpointHook | None,
    on_event: OnEvent | None = None,
) -> _TransitionOutcome:
    """One transaction: apply effects, evaluate the target, write the new state +
    phase_transition event. Raises InterpreterFaultError (never partially commits) if the
    target is missing -- the validator should make that impossible for a definition
    that ever passed validation, but this is the backstop, not a trust exercise."""
    payload: dict[str, Any] = {}
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        new_state = apply_effects(phase, row.state, definition.state)
        target = evaluate_gates(phase, new_state)
        event_seq = await _next_event_seq(session, session_id)

        row.state = new_state
        if target is not None and target not in definition.phases:
            raise InterpreterFaultError(
                f"phase {phase_key!r} transitions to undeclared phase {target!r}"
            )
        if target is not None:
            row.current_phase = target

        payload = {"from": phase_key, "to": target, "state": new_state}
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="phase_transition",
                payload=payload,
            )
        )
        await session.flush()

    if on_event is not None:
        await on_event(event_seq, "phase_transition", payload)

    if target is None:
        return _TransitionOutcome(phase_key, terminal=True)

    if checkpoint_hook is not None:
        ctx = InterpreterContext(
            tenant_id, session_id, target, definition.phases[target], new_state
        )
        await checkpoint_hook(ctx)

    return _TransitionOutcome(target, terminal=False)


@idempotent(
    key_fn=lambda *_a, tenant_id, session_id, event_seq, **_k: f"turn:{session_id}:{event_seq}"
)
async def _run_turn_idempotent(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    event_seq: int,
    actor: ActorRef,
    ctx: InterpreterContext,
    execute_turn: ActorTurnExecutor,
) -> dict[str, Any]:
    """The idempotency-keyed unit: a resumed/retried advance must never
    execute the same turn's side effect twice. ``event_seq`` here is *peeked*, not
    claimed, by the caller (``_run_actor_turn``) -- see that function's docstring for why
    that distinction is what makes this key actually stable across a crash-and-retry."""
    result = await execute_turn(actor, ctx)
    return {
        "content_md": result.content_md,
        "already_persisted": result.already_persisted,
        "message_id": str(result.message_id) if result.message_id is not None else None,
    }


async def _run_actor_turn(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    phase_key: str,
    phase: PhaseSpec,
    actor: ActorRef,
    execute_turn: ActorTurnExecutor,
    on_event: OnEvent | None = None,
) -> None:
    """External calls (a real model call) must not run inside a DB transaction/lock
    (the standing principle: "model calls happen outside the row lock").
    That forces this into (at least) two transactions bracketing the external call --
    which creates exactly the idempotency-key trap warns about if the two
    transactions don't share a *stable* key. An earlier version of this function claimed
    (incremented and committed) ``next_event_seq`` in the first transaction, before the
    idempotent call -- so a crash between that claim and the final commit meant a retry
    would peek a *new*, higher ``next_event_seq``, derive a *different* idempotency key,
    and re-run ``execute_turn`` anyway, defeating the entire point.

    Fixed: ``next_event_seq`` is only *peeked* (read, not incremented) before the
    idempotent call, and only actually claimed -- incremented and committed -- in the
    same transaction as the message/event it belongs to. On a crash-and-retry before that
    final commit, ``next_event_seq`` is unchanged, so the retry peeks the *same* value,
    derives the *same* idempotency key, and ``_run_turn_idempotent`` returns the cached
    result instead of re-invoking ``execute_turn``.

    The defensive re-check before the final claim (below) is a documented, deliberate
    limitation, not a hidden one: true concurrent-writer safety is the job (full
    ``SELECT ... FOR UPDATE`` session locking); this function assumes a single advancing
    caller; the session lock itself lives in ``core.process.locking``.
    """
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        event_seq = row.next_event_seq  # peek, not claim
        state = dict(row.state)

    ctx = InterpreterContext(tenant_id, session_id, phase_key, phase, state, event_seq=event_seq)
    result = await _run_turn_idempotent(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=event_seq,
        actor=actor,
        ctx=ctx,
        execute_turn=execute_turn,
    )

    role = "assistant" if actor.mode != "free" else "user"
    message_id: uuid.UUID | None = (
        uuid.UUID(result["message_id"]) if result["message_id"] is not None else None
    )

    async with tenant_scope(tenant_id) as _authsess:
        author_name = await resolve_author_name(_authsess, tenant_id, actor.principal_id)

    if not result["already_persisted"]:
        async with tenant_scope(tenant_id) as session:
            row = await session.get(SessionRow, session_id)
            assert row is not None
            if row.next_event_seq != event_seq:
                raise InterpreterFaultError(
                    f"session {session_id} next_event_seq advanced from {event_seq} to "
                    f"{row.next_event_seq} between peek and claim -- concurrent advance "
                    f"without the single-writer session lock"
                )
            row.next_event_seq = event_seq + 1
            message = MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                author_principal_id=actor.principal_id,
                role=role,
                content_md=result["content_md"],
            )
            session.add(message)
            await session.flush()  # populate message.id for the durable event payload
            message_id = message.id
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_seq=event_seq,
                    kind="message",
                    payload={
                        "id": str(message_id),
                        "role": role,
                        "content": result["content_md"],
                        "author": author_name,
                    },
                    actor_principal_id=actor.principal_id,
                )
            )
            await session.flush()
    else:
        # the execute_turn adapter already performed its own full commit (e.g.
        # core.agents.runtime.run_agent_turn, given this same event_seq) -- verify it
        # actually claimed the slot it was handed rather than silently trusting it.
        # The counter may legitimately sit further ahead: worker jobs (delegation and
        # review notes) advance next_event_seq concurrently, and a turn's own
        # delegation notes can land before this check runs. So the invariant is "the
        # slot's event exists and the counter passed it", never counter equality.
        async with tenant_scope(tenant_id) as session:
            row = await session.get(SessionRow, session_id)
            assert row is not None
            claimed = await session.scalar(
                select(SessionEventRow.id).where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.event_seq == event_seq,
                )
            )
            if claimed is None or row.next_event_seq < event_seq + 1:
                raise InterpreterFaultError(
                    f"session {session_id}: execute_turn reported already_persisted "
                    f"for event_seq {event_seq} but the slot was never claimed "
                    f"(event exists: {claimed is not None}, "
                    f"next_event_seq: {row.next_event_seq})"
                )

    if on_event is not None:
        await on_event(
            event_seq,
            "message",
            {
                "id": str(message_id) if message_id else None,
                "role": role,
                "content": result["content_md"],
                "author": author_name,
            },
        )


async def submit_human_turn(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    content: str,
    principal_id: uuid.UUID,
    *,
    on_event: OnEvent | None = None,
) -> MessageRow:
    """The HTTP layer's entry point for a free-mode human actor's turn (see
    ``HumanTurnPendingError``) -- the scheduler already durably advanced its cursor past
    this actor when ``advance_session`` raised, so this only needs to write the message
    itself. Same peek-then-claim shape as ``_run_actor_turn``/``core.process.skeleton``:
    ``next_event_seq`` is claimed in the same transaction as the row it belongs to.
    Callers should re-invoke ``advance_session`` immediately after this returns, to let
    the interpreter continue past the now-recorded human turn."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            raise ValueError(f"no session {session_id} in this tenant")
        event_seq = row.next_event_seq
        row.next_event_seq = event_seq + 1

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
        author_name = await resolve_author_name(session, tenant_id, principal_id)
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={
                    "id": str(message.id),
                    "role": "user",
                    "content": content,
                    "author": author_name,
                },
                actor_principal_id=principal_id,
            )
        )
        await session.flush()
        message_id = message.id

    if on_event is not None:
        await on_event(
            event_seq,
            "message",
            {"id": str(message_id), "role": "user", "content": content, "author": author_name},
        )
    return message


async def advance_session(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    *,
    next_actor_fn: NextActorFn,
    execute_turn: ActorTurnExecutor,
    checkpoint_hook: CheckpointHook | None = None,
    on_await: OnAwaitHook | None = None,
    on_event: OnEvent | None = None,
    max_steps: int = _MAX_STEPS_PER_ADVANCE,
) -> AdvanceResult:
    """The interpreter loop. Runs until: a phase's actors are exhausted and
    it has an unsatisfied ``await`` (returns 'awaiting'); a free-mode human actor is next
    but hasn't submitted yet (returns 'awaiting_human' -- see
    ``HumanTurnPendingError``); a transition has no target (returns 'terminal');
    ``max_steps`` is hit (a runaway-loop guard -- callers should treat repeatedly hitting
    this as a bug in the definition or the injected scheduler, not call it in a tight
    retry loop, so this returns 'active' rather than raising); or a fault occurs, in
    which case the session is paused (status='paused') via a *separate* clean
    transaction and 'paused' is returned -- never a stuck lock.
    """
    with _tracer.start_as_current_span("interpreter.advance_session") as span:
        span.set_attribute("pyrrhula.session_id", str(session_id))
        steps = 0
        phase_key = ""
        phase: PhaseSpec | None = None
        try:
            while steps < max_steps:
                steps += 1
                async with tenant_scope(tenant_id) as session:
                    row = await session.get(SessionRow, session_id)
                    if row is None:
                        raise ValueError(f"no session {session_id} in this tenant")
                    # A pause or an archive taken while this loop runs must stop it at
                    # the next step. Nothing re-read the status before: a paused
                    # newsroom session ran four more turns and two phase transitions,
                    # and its terminal transition then overwrote 'paused' with
                    # 'completed' (sweep 2026-10-04).
                    if row.status == "paused" or row.archived_at is not None:
                        return AdvanceResult("paused", steps, row.current_phase, ())
                    phase_key = row.current_phase
                    state = dict(row.state)
                    # The scheduler persists the advanced cursor as a side effect of
                    # choosing the actor, before the turn runs. Kept so a turn that
                    # never happened can put it back.
                    cursor_before = dict(row.actor_cursor or {})

                if phase_key not in definition.phases:
                    raise InterpreterFaultError(f"session is in undeclared phase {phase_key!r}")
                phase = definition.phases[phase_key]
                ctx = InterpreterContext(tenant_id, session_id, phase_key, phase, state)

                actor = await next_actor_fn(ctx)
                if actor is None:
                    if phase.await_field is not None:
                        if on_await is not None:
                            await on_await(ctx)
                        else:
                            async with tenant_scope(tenant_id) as session:
                                row = await session.get(SessionRow, session_id)
                                assert row is not None
                                row.status = "awaiting"
                        return AdvanceResult("awaiting", steps, phase_key, tuple(phase.flags))

                    if phase.requires is not None and phase.requires.is_declared():
                        held = await _enforce_phase_requirements(
                            tenant_id, session_id, phase_key, phase, on_event
                        )
                        if held == "repeat":
                            # The actors go round again. Clearing the cursor is what
                            # makes that mean anything: the rotation resolves afresh, so
                            # every seat gets another pass at the beat it did not finish.
                            continue
                        if held == "hold":
                            async with tenant_scope(tenant_id) as session:
                                row = await session.get(SessionRow, session_id)
                                assert row is not None
                                row.status = "awaiting"
                            return AdvanceResult("awaiting", steps, phase_key, tuple(phase.flags))

                    outcome = await _transition(
                        tenant_id,
                        session_id,
                        definition,
                        phase_key,
                        phase,
                        checkpoint_hook,
                        on_event,
                    )
                    if outcome.terminal:
                        # Persist terminal-ness: the session list can't afford to load
                        # every definition to ask "does this phase have an exit", and a
                        # finished session shown as 'active' is exactly the UI ambiguity
                        # this status column exists to prevent. reopen_session flips it
                        # back to 'active' when a finished flow is continued.
                        async with tenant_scope(tenant_id) as session:
                            row = await session.get(SessionRow, session_id)
                            assert row is not None
                            row.status = "completed"
                        return AdvanceResult("terminal", steps, phase_key, tuple(phase.flags))
                    continue

                try:
                    await _run_actor_turn(
                        tenant_id, session_id, phase_key, phase, actor, execute_turn, on_event
                    )
                except InterpreterFaultError:
                    # The turn never happened, but the cursor already moved past this
                    # actor. Put it back, so a resume offers them the turn again instead
                    # of skipping it: a single-actor opening phase vanished entirely
                    # after a resume (Hägnaryd sweep, 2026-10-04).
                    async with tenant_scope(tenant_id) as session:
                        row = await session.get(SessionRow, session_id)
                        if row is not None:
                            row.actor_cursor = cursor_before
                    raise

            # max_steps was reached. A transition on the *last* iteration updates
            # current_phase via `continue`, without another loop-top re-read -- so
            # phase_key/phase here can be stale by exactly one transition. Re-read fresh
            # rather than reporting the phase this run started its last step in.
            async with tenant_scope(tenant_id) as session:
                row = await session.get(SessionRow, session_id)
                assert row is not None
                final_phase_key = row.current_phase
            final_flags = (
                definition.phases[final_phase_key].flags
                if (final_phase_key in definition.phases)
                else ()
            )
            return AdvanceResult("active", steps, final_phase_key, tuple(final_flags))
        except HumanTurnPendingError:
            # Expected stop, not a fault: the scheduler's cursor has already durably
            # advanced past this human actor (make_scheduler's next_actor persists it as
            # a side effect of being called) -- status stays whatever it already was
            # ('active'), nothing to pause or clean up.
            flags = tuple(phase.flags) if phase is not None else ()
            return AdvanceResult("awaiting_human", steps, phase_key, flags)
        except InterpreterFaultError as exc:
            await _pause_with_fault(tenant_id, session_id, str(exc))
            return AdvanceResult("paused", steps, phase_key, ())
