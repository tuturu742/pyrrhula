"""Waking a session whose delegated work has finished.

A phase that hands work to coding agents and then falls straight through to the next
phase produces an incoherent flow: the review phase reviews nothing, the merge phase
finds an empty queue, and both report that truthfully while the branches are still being
built minutes later. The session says it is finished before the work it asked for exists.

So the dispatching phase declares ``await: delegated_work`` and the interpreter parks it.
This is what unparks it: after any job finishes, if the session that job belonged to has
no jobs left outstanding, its await is satisfied and the flow continues into phases that
now have something real to look at.

**"No jobs left" is the whole definition of done here**, deliberately, rather than
counting the batch. Delegation fans out -- a review job per pull request, a rework job per
verdict, another review after each rework -- so any count taken at dispatch is wrong by
the time it matters. Asking "is anything still queued or running for this session?" stays
correct however the fan-out goes.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import func, select

from adapters.queue.postgres.models import JobRow
from core.agents.models import Persona
from core.process.authoring import get_definition
from core.process.awaits import satisfy_await
from core.process.dsl.validator import validate_raw
from core.sessions.models import AwaitStateRow, SessionRow
from core.tenancy.scope import tenant_scope, unscoped_session
from worker.job_queue_factory import get_job_queue

log = structlog.get_logger()

# The job that is finishing right now is still 'claimed' when this runs, so it has to be
# excluded or a session would never look idle.
_OUTSTANDING = ("pending", "claimed")


async def _outstanding_jobs(session_id: str, excluding: uuid.UUID) -> int:
    async with unscoped_session() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(JobRow)
                .where(
                    JobRow.payload["session_id"].astext == session_id,
                    JobRow.status.in_(_OUTSTANDING),
                    JobRow.id != excluding,
                )
            )
            or 0
        )


async def wake_if_work_is_done(
    tenant_id: uuid.UUID, session_id: uuid.UUID, finished_job_id: uuid.UUID
) -> bool:
    """Satisfy the session's open ``delegated_work`` await, if its work is finished.

    Returns whether this call actually woke the session. False covers every ordinary
    case: the session is not waiting, it is waiting on a person, other jobs are still
    running, or a timeout got there first.
    """
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None or row.status != "awaiting" or row.process_definition_id is None:
            return False
        definition_id = row.process_definition_id
        phase_key = row.current_phase
        await_row = (
            await session.execute(
                select(AwaitStateRow)
                .where(
                    AwaitStateRow.session_id == session_id,
                    AwaitStateRow.outcome.is_(None),
                )
                .order_by(AwaitStateRow.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if await_row is None:
            return False
        await_id = await_row.id

    definition_row = await get_definition(tenant_id, definition_id)
    if definition_row is None:
        return False
    dsl, issues = validate_raw(definition_row.definition)
    if dsl is None or issues:
        return False
    phase = dsl.phases.get(phase_key)
    if phase is None or phase.await_field is None:
        return False
    if phase.await_field.type != "delegated_work":
        # Waiting on a person. A finished job says nothing about whether they have acted.
        return False

    if await _outstanding_jobs(str(session_id), finished_job_id):
        return False

    # The await is recorded as satisfied BY someone. The session's own primary persona
    # is the honest answer: it is the actor that dispatched the work whose completion is
    # being reported, so the transcript reads as that actor's turn resuming.
    actor = await _session_principal(tenant_id, session_id)
    if actor is None:
        return False

    woke = await satisfy_await(tenant_id, await_id, actor, dsl)
    if not woke:
        return False  # a timeout won the race, which is a real outcome, not an error
    log.info("session_wake.delegated_work_finished", session_id=str(session_id), phase=phase_key)
    # `satisfy_await` moves the session to its next phase and marks it active; it does
    # not run it. Without this the flow would be correct and stopped -- parked one phase
    # further on with nobody driving, which is the failure mode this whole change exists
    # to remove.
    await get_job_queue().enqueue(
        tenant_id,
        "advance_session",
        {"tenant_id": str(tenant_id), "session_id": str(session_id)},
    )
    return True


async def _session_principal(tenant_id: uuid.UUID, session_id: uuid.UUID) -> uuid.UUID | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None:
            return None
        persona = await session.get(Persona, row.persona_id)
        return persona.principal_id if persona is not None else None
