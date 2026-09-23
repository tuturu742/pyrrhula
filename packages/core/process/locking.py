"""Session concurrency control (B1.5, plan §5.5): one advancing writer per session,
enforced by a real ``SELECT ... FOR UPDATE`` claim held only for brief critical sections
-- never across a slow external model call, per B1.5's own subtask ("model calls happen
outside the row lock").

**Claim, don't hold.** ``claim_session`` takes the row lock, checks/sets the watchdog
marker (``claimed_at``/``claimed_by``), and commits immediately -- releasing the lock.
The caller then does the slow work (a real model call, later, via B1.7) with *no* DB lock
held at all. ``commit_advance`` re-acquires a fresh, equally brief lock, verifies
``version`` hasn't moved since the claim (optimistic check -- ``SessionConflictError`` if
it has), bumps it, and clears the claim marker. A crash during the slow middle section
holds no lock (Postgres already released it after the claim transaction committed) --
only the marker, which ``claim_session``'s own timeout check treats as abandoned once
``_CLAIM_TIMEOUT_SECONDS`` has passed. This is the watchdog: a timeout on the *claim*, not
on the DB lock, exactly as the subtask specifies.

``advance_session_locked`` wraps ``core.process.interpreter.advance_session`` with this
claim/commit pair, making concurrent callers on the *same* session_id safe: exactly one
wins the claim per call; the rest get ``SessionClaimTimeoutError`` immediately (no
blocking wait, no deadlock) if a live claim already exists, or proceed normally once it's
either released or timed out.

``submit_queued_input`` is the durable primitive for "human input arriving during a model
turn queues instead of racing" -- it appends a ``session_event(kind='message',
payload={..., 'queued': True})`` under its own brief claim/commit pair, safe under
concurrent submission, without needing the caller to be the one currently advancing the
session at all. **What actually *consumes* a queued input** (a scheduler recognising it as
satisfying a `mode: free` human actor's turn) needs the real HTTP submission flow and
agent runtime (B1.7) to exist before it can be wired end-to-end; this module's job is
making sure the input is never lost or corrupted under concurrent access, which is
provable and tested today independent of that later wiring.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select

from core.actions.idempotency import CLAIM_LEASE_SECONDS
from core.observability.otel import get_tracer
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    _MAX_STEPS_PER_ADVANCE,
    ActorTurnExecutor,
    AdvanceResult,
    CheckpointHook,
    NextActorFn,
)
from core.process.interpreter import (
    advance_session as _advance_session,
)
from core.sessions.models import SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope

_tracer = get_tracer(__name__)

# The session claim's watchdog has to outlast the operation claim it protects. At 30
# seconds it did not: a live turn is a model call with a tool loop and routinely runs for
# minutes, so the claim read as abandoned long before the turn finished, a second worker
# took it legitimately, and the two then collided on the turn's idempotency key -- which
# is leased for fifteen minutes and correctly refused. Two guards fighting rather than
# complementing each other, and the visible result was a failed job on a session that was
# advancing perfectly well.
#
# Deriving it from that lease is what keeps them from drifting apart again. A heartbeat
# that refreshed the claim while the turn ran would be better still -- it would let a
# genuinely dead worker be taken over in seconds rather than minutes -- but a claim that
# outlives what it guards is the property that has to hold first.
_CLAIM_TIMEOUT_SECONDS = CLAIM_LEASE_SECONDS


class SessionConflictError(Exception):
    """A stale-version advance attempt lost the race: another advance committed between
    this caller's claim and its commit."""


class SessionClaimTimeoutError(Exception):
    """Another claimant already holds a live (non-expired) claim on this session."""


def _claim_age_seconds(claimed_at: datetime) -> float:
    return (datetime.now(UTC) - claimed_at).total_seconds()


@asynccontextmanager
async def claim_session(
    tenant_id: uuid.UUID, session_id: uuid.UUID, claimant_id: str
) -> AsyncIterator[int]:
    """A brief ``SELECT ... FOR UPDATE`` critical section: check for a live competing
    claim, set this claimant's marker, commit (releasing the lock), and yield the
    ``version`` this claimant observed -- the caller passes that back to
    ``commit_advance`` to detect whether anyone else committed in between.
    """
    with _tracer.start_as_current_span("locking.claim_session") as span:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    select(SessionRow).where(SessionRow.id == session_id).with_for_update()
                )
            ).scalar_one()

            if (
                row.claimed_at is not None
                and _claim_age_seconds(row.claimed_at) < _CLAIM_TIMEOUT_SECONDS
            ):
                span.set_attribute("pyrrhula.claim.rejected", True)
                raise SessionClaimTimeoutError(
                    f"session {session_id} is claimed by {row.claimed_by!r} "
                    f"({_claim_age_seconds(row.claimed_at):.1f}s ago)"
                )

            row.claimed_at = datetime.now(UTC)
            row.claimed_by = claimant_id
            observed_version = row.version

        yield observed_version


_KEEP = object()


async def commit_advance(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    expected_version: int,
    *,
    awaiting: str | None | object = _KEEP,
) -> None:
    """Re-acquires a fresh, brief lock; raises ``SessionConflictError`` if ``version``
    moved since the claim (someone else committed in between -- shouldn't happen given
    ``claim_session``'s own exclusivity, but checked explicitly rather than trusted
    blindly); otherwise bumps ``version`` and clears the claim marker."""
    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one()
        if row.version != expected_version:
            raise SessionConflictError(
                f"session {session_id} version changed from {expected_version} to "
                f"{row.version} between claim and commit"
            )
        row.version = expected_version + 1
        row.claimed_at = None
        row.claimed_by = None
        if awaiting is not _KEEP:
            row.awaiting = awaiting


async def refresh_claim(tenant_id: uuid.UUID, session_id: uuid.UUID, claimant_id: str) -> bool:
    """Push the claim's timestamp forward while its holder is still working.

    The watchdog exists for a worker that died mid-advance, and it cannot tell that from a
    worker that is simply slow. Sizing it to the longest imaginable turn is the wrong
    trade in both directions: too short and a live turn gets taken over (a campaign turn
    on a local 27B model was observed at 14m48s against a 15m timeout), too long and a
    genuinely dead worker holds the session for that whole window.

    A heartbeat separates the two questions. The holder says "still here" every so often,
    so the timeout can be sized to *silence* rather than to the longest turn. Returns
    False when the claim is gone or has been taken by someone else -- the caller has lost
    it and should not pretend otherwise.
    """
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None or row.claimed_by != claimant_id:
            return False
        row.claimed_at = datetime.now(UTC)
        return True


async def release_claim(tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    """Best-effort release without bumping version -- used on the failure path (the
    advance itself raised) so a failed attempt doesn't hold the claim for the full
    timeout window before another caller can try."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is not None:
            row.claimed_at = None
            row.claimed_by = None


async def submit_queued_input(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    principal_id: uuid.UUID,
    content: str,
) -> int:
    """Durably records human input arriving while the session may be mid-advance,
    without requiring the caller to hold (or wait for) the advance claim at all --
    "never lost, never racing the in-flight turn." Returns the event_seq it claimed.
    """
    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one()
        event_seq = row.next_event_seq
        row.next_event_seq = event_seq + 1
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=event_seq,
                kind="message",
                payload={"role": "user", "content": content, "queued": True},
                actor_principal_id=principal_id,
            )
        )
        return event_seq


async def advance_session_locked(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    definition: ProcessDefinitionDSL,
    *,
    claimant_id: str,
    next_actor_fn: NextActorFn,
    execute_turn: ActorTurnExecutor,
    checkpoint_hook: CheckpointHook | None = None,
    max_steps: int = _MAX_STEPS_PER_ADVANCE,
) -> AdvanceResult:
    """The real, concurrency-safe entrypoint: claim -> run the interpreter (whose own
    internal transactions are already short-lived, B1.2 -- the model call inside
    ``execute_turn`` happens with no DB lock held, satisfying "model calls happen outside
    the row lock" without this function needing to release/reacquire around every
    internal step) -> commit (bump version, clear claim) on success, or release (without
    bumping version) on failure, so a failed attempt doesn't block the next caller for
    the full watchdog timeout.
    """
    async with claim_session(tenant_id, session_id, claimant_id) as observed_version:
        try:
            result = await _advance_session(
                tenant_id,
                session_id,
                definition,
                next_actor_fn=next_actor_fn,
                execute_turn=execute_turn,
                checkpoint_hook=checkpoint_hook,
                max_steps=max_steps,
            )
        except Exception:
            await release_claim(tenant_id, session_id)
            raise

    # Every advance commits here, so this is the one place that can record what the
    # session is now waiting for without a second writer drifting out of step with it.
    await commit_advance(
        tenant_id,
        session_id,
        observed_version,
        awaiting="human" if result.status == "awaiting_human" else None,
    )
    return result
