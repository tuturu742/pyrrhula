"""The interrupt primitive (B1.6, plan §5.2 ``await``, §12.7): a phase suspends for human
input with a timeout transition -- the mechanism behind play-by-post pacing and, later,
enterprise approval gates.

**Satisfy-vs-timeout is a race, resolved atomically, by design.** Both
``satisfy_await`` and ``resolve_timeout`` claim the same row via a single
``UPDATE await_state SET outcome = ... WHERE id = :id AND outcome IS NULL RETURNING id``
-- Postgres guarantees only one such UPDATE against the same row can ever see
``outcome IS NULL`` and succeed; the loser's UPDATE affects zero rows, which both
functions treat as "already resolved, I lost" rather than an error. No application-level
locking is needed for this specific race because the atomic, conditional UPDATE already
is the lock.

**`expected_from` is a descriptive record, not an authorization check.** It's populated
from the phase's actor specs at the moment the await is created, for audit/UI purposes
("who was eligible when this await opened"); `satisfy_await` does not itself verify the
calling principal is one of them. Enforcing that is the HTTP layer's job (checking
workspace membership the same way B1.3's scheduler resolves eligible candidates) --
`satisfy_await`'s own job is the atomic race resolution, not full authorization. Flagged
explicitly rather than silently assumed solved.

**The cross-tenant timeout sweep does not touch `await_state`'s RLS at all** (unlike
`job`, which is deliberately not RLS-covered so a worker can claim work for any tenant --
see `core.ports.job_queue`). `await_state` has no comparable justification for a second
named RLS exception, and CLAUDE.md's no-RLS allowlist (`tenant`, `role_permission`,
`job`) is a closed, three-table list, not something to casually extend per-task. Instead,
`sweep_expired_awaits` (packages/worker/timeouts.py) reads the tenant list from
`unscoped_session()` (`tenant` is itself one of the three legitimate exceptions) and
scopes its actual `await_state` query per-tenant via `tenant_scope(tenant_id)` -- O(tenants)
queries per sweep tick, correct and RLS-respecting throughout, at a scaling cost that's a
documented, deliberate trade-off for a per-tenant SaaS system, not a hidden one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from core.process.dsl.schema import (
    AwaitSpec,
    PhaseSpec,
    ProcessDefinitionDSL,
    parse_duration_seconds,
)
from core.process.interpreter import (
    InterpreterContext,
    OnAwaitHook,
    OnEvent,
    apply_effects,
    evaluate_gates,
)
from core.sessions.models import AwaitStateRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope


async def create_await(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    phase: PhaseSpec,
    *,
    reminder_after: str | None = None,
) -> AwaitStateRow:
    """Called when the interpreter yields at an unsatisfied await (B1.2's
    ``on_await`` hook -- see ``core.process.interpreter``). Persists the await and marks
    the session ``status='awaiting'`` in the same transaction. ``event_seq`` is peeked
    (not claimed) from the session's current ``next_event_seq`` -- creating an await
    doesn't itself consume a log slot, only its eventual resolution does (see
    ``satisfy_await``/``resolve_timeout``).

    ``reminder_after`` (G4.3) is the already-resolved pacing duration -- resolved by the
    caller through ``ProcessDefinitionDSL.reminder_duration_for(phase)``, because a
    ``PhaseSpec`` alone cannot see the definition-level ``pacing`` default it might
    inherit. ``None`` means no reminder is due, and the column stays NULL."""
    await_spec: AwaitSpec | None = phase.await_field
    assert await_spec is not None, "create_await called for a phase with no await block"

    now = datetime.now(UTC)
    timeout_at = now + timedelta(seconds=parse_duration_seconds(await_spec.timeout))
    reminder_at = (
        now + timedelta(seconds=parse_duration_seconds(reminder_after))
        if reminder_after is not None
        else None
    )
    expected_from = {
        "phase_actors": [a.model_dump(mode="json", by_alias=True) for a in phase.actors]
    }

    async with tenant_scope(tenant_id) as session:
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        session_row.status = "awaiting"

        row = AwaitStateRow(
            tenant_id=tenant_id,
            session_id=session_id,
            event_seq=session_row.next_event_seq,
            await_kind=await_spec.type,
            expected_from=expected_from,
            timeout_at=timeout_at,
            reminder_at=reminder_at,
            on_timeout_phase=await_spec.on_timeout,
        )
        session.add(row)
        await session.flush()
        return row


def make_await_hook(definition: ProcessDefinitionDSL | None = None) -> OnAwaitHook:
    """B1.2's ``OnAwaitHook`` seam, for real: pass this to
    ``advance_session(on_await=...)`` to persist a real ``await_state`` row instead of
    the documented plain-status-flip default.

    ``definition`` (G4.3) supplies the pacing defaults an individual ``PhaseSpec`` can't
    see. It is optional so every pre-G4.3 caller keeps working unchanged -- an await
    created without it simply has no reminder, which is what those callers already got."""

    async def hook(ctx: InterpreterContext) -> None:
        reminder_after = (
            definition.reminder_duration_for(ctx.phase) if definition is not None else None
        )
        await create_await(ctx.tenant_id, ctx.session_id, ctx.phase, reminder_after=reminder_after)

    return hook


async def get_active_await(tenant_id: uuid.UUID, session_id: uuid.UUID) -> AwaitStateRow | None:
    async with tenant_scope(tenant_id) as session:
        result: AwaitStateRow | None = await session.scalar(
            select(AwaitStateRow)
            .where(AwaitStateRow.session_id == session_id, AwaitStateRow.outcome.is_(None))
            .order_by(AwaitStateRow.event_seq.desc())
            .limit(1)
        )
        return result


async def satisfy_await(
    tenant_id: uuid.UUID,
    await_state_id: uuid.UUID,
    principal_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    *,
    on_event: OnEvent | None = None,
) -> bool:
    """Atomically claims the await for the 'satisfied' outcome, then performs the *same*
    phase transition the interpreter would have performed had actors simply exhausted
    normally (``apply_effects`` + ``evaluate_gates``, using the phase's own
    ``on_complete``/``gates``) -- so the session lands ready to continue from its real
    next phase, not sitting back in the awaiting phase where the next ``advance_session``
    call would just re-enter the same await with no memory that it was ever satisfied.
    This mirrors ``resolve_timeout``'s direct jump to ``on_timeout_phase`` below, making
    both outcomes symmetric, self-contained phase advances -- neither needs the
    interpreter to re-visit the awaiting phase at all.

    Returns ``True`` if this call won the race; ``False`` if it lost -- most likely to a
    timeout that resolved first, a real, expected outcome under the race this function
    exists to handle, not an error.
    """
    published_event_seq: int | None = None
    published_payload: dict[str, object] = {}
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            update(AwaitStateRow)
            .where(AwaitStateRow.id == await_state_id, AwaitStateRow.outcome.is_(None))
            .values(outcome="satisfied", satisfied_at=datetime.now(UTC))
            .returning(AwaitStateRow.session_id, AwaitStateRow.event_seq)
        )
        row = result.first()
        if row is None:
            return False
        session_id, await_event_seq = row

        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        phase = definition.phases[session_row.current_phase]

        new_state = apply_effects(phase, session_row.state, definition.state)
        target = evaluate_gates(phase, new_state)

        session_row.state = new_state
        session_row.status = "active"
        if target is not None:
            session_row.current_phase = target

        event_seq = session_row.next_event_seq
        published_event_seq = event_seq
        published_payload = {
            "outcome": "satisfied",
            "await_event_seq": await_event_seq,
            "to": target,
            "state": new_state,
        }
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="await",
                payload=published_payload,
                actor_principal_id=principal_id,
            )
        )
        session_row.next_event_seq += 1

    if on_event is not None and published_event_seq is not None:
        await on_event(published_event_seq, "await", published_payload)
    return True


async def resolve_timeout(
    tenant_id: uuid.UUID, await_state_id: uuid.UUID, *, on_event: OnEvent | None = None
) -> str | None:
    """Atomically claims the await for the 'timed_out' outcome. Returns the
    ``on_timeout_phase`` to transition to if this call won the race; ``None`` if it lost
    (the await was already satisfied -- "a satisfied await never times out," the
    transactional guarantee this whole module is built around).

    Deliberately does *not* run ``apply_effects``/``evaluate_gates`` the way
    ``satisfy_await`` does: ``on_timeout`` is an unconditional bypass target by
    construction (a plain phase-key string in the DSL, not gates) -- timing out means the
    phase did *not* complete as designed, so it skips the phase's normal exit logic
    entirely rather than evaluating it. This asymmetry is deliberate, not an
    inconsistency: satisfaction means "the phase completed, evaluate its real exit";
    timeout means "give up, take the designated fallback, full stop."
    """
    published_event_seq: int | None = None
    published_payload: dict[str, object] = {}
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            update(AwaitStateRow)
            .where(AwaitStateRow.id == await_state_id, AwaitStateRow.outcome.is_(None))
            .values(outcome="timed_out", satisfied_at=datetime.now(UTC))
            .returning(
                AwaitStateRow.session_id, AwaitStateRow.event_seq, AwaitStateRow.on_timeout_phase
            )
        )
        row = result.first()
        if row is None:
            return None
        session_id, _event_seq, on_timeout_phase = row
        on_timeout_phase = str(on_timeout_phase)

        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        session_row.status = "active"
        session_row.current_phase = on_timeout_phase

        event_seq = session_row.next_event_seq
        published_event_seq = event_seq
        published_payload = {"outcome": "timed_out", "to": on_timeout_phase}
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="await",
                payload=published_payload,
            )
        )
        session_row.next_event_seq += 1

    if on_event is not None and published_event_seq is not None:
        await on_event(published_event_seq, "await", published_payload)
    return on_timeout_phase
