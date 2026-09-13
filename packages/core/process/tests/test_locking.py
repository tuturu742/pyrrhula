"""B1.5 acceptance criteria for session concurrency control, against a live Postgres:
exactly one winner per turn under concurrent advance attempts, no lost queued inputs,
and a slow "model call" doesn't block reads of other sessions.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import ActorRef, ActorTurnResult, InterpreterContext, start_session
from core.process.locking import (
    SessionClaimTimeoutError,
    SessionConflictError,
    advance_session_locked,
    claim_session,
    commit_advance,
    submit_queued_input,
)
from core.process.scheduler import Candidate, make_scheduler
from core.process.skeleton import create_session
from core.sessions.models import SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id


async def _make_session_with_definition(
    slug_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, ProcessDefinitionDSL]:
    tenant_id, _workspace_id, session_id = await _setup(slug_prefix)
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )
    return tenant_id, session_id, definition


def _single_candidate_scheduler(principal_id: uuid.UUID):  # noqa: ANN201
    async def resolver(_spec, _ctx: InterpreterContext) -> list[Candidate]:  # noqa: ANN001
        return [Candidate(principal_id)]

    return make_scheduler(resolver)


async def _instant_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    return ActorTurnResult(content_md=f"turn in {ctx.phase_key}")


async def _get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row


# ── claim/commit primitives ─────────────────────────────────────────────────────────


async def test_claim_then_commit_bumps_version_and_clears_marker(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("lock-claim-commit")

    async with claim_session(tenant_id, session_id, "worker-1") as version:
        assert version == 0
        mid = await _get_session(tenant_id, session_id)
        assert mid.claimed_by == "worker-1"

    await commit_advance(tenant_id, session_id, version)

    after = await _get_session(tenant_id, session_id)
    assert after.version == 1
    assert after.claimed_at is None
    assert after.claimed_by is None


async def test_second_claim_while_live_is_rejected(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("lock-second-claim")

    async with claim_session(tenant_id, session_id, "worker-1"):
        try:
            async with claim_session(tenant_id, session_id, "worker-2"):
                pass
            raise AssertionError("expected SessionClaimTimeoutError")
        except SessionClaimTimeoutError:
            pass


async def test_commit_with_stale_version_raises_conflict(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("lock-stale-commit")

    async with claim_session(tenant_id, session_id, "worker-1") as version:
        pass
    await commit_advance(tenant_id, session_id, version)  # version now 1

    try:
        await commit_advance(tenant_id, session_id, version)  # stale: still 0
        raise AssertionError("expected SessionConflictError")
    except SessionConflictError:
        pass


# ── stress test: exactly one winner per turn, no deadlocks ─────────────────────────


async def test_n_concurrent_advance_attempts_produce_exactly_one_winner(
    db_available: None,
) -> None:
    tenant_id, session_id, definition = await _make_session_with_definition("lock-stress")
    principal_id = uuid.uuid4()

    async def attempt(claimant: str):  # noqa: ANN201
        scheduler = _single_candidate_scheduler(principal_id)
        try:
            return await advance_session_locked(
                tenant_id,
                session_id,
                definition,
                claimant_id=claimant,
                next_actor_fn=scheduler,
                execute_turn=_instant_execute_turn,
                max_steps=1,
            )
        except SessionClaimTimeoutError as exc:
            return exc

    results = await asyncio.gather(*(attempt(f"worker-{i}") for i in range(10)))

    winners = [r for r in results if not isinstance(r, Exception)]
    rejections = [r for r in results if isinstance(r, SessionClaimTimeoutError)]

    assert len(winners) == 1
    assert len(rejections) == 9

    # No deadlock, no corruption: the session ends up with exactly one committed advance.
    final = await _get_session(tenant_id, session_id)
    assert final.version == 1
    assert final.claimed_at is None


async def test_queued_inputs_under_concurrent_submission_are_never_lost_or_duplicated(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, session_id = await _setup("lock-queue-stress")
    principal_id = uuid.uuid4()

    async def submit(i: int) -> int:
        return await submit_queued_input(tenant_id, session_id, principal_id, f"message-{i}")

    event_seqs = await asyncio.gather(*(submit(i) for i in range(20)))

    assert sorted(event_seqs) == list(range(20))  # gapless, no duplicates

    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(SessionEventRow.session_id == session_id)
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 20
    assert all(r.payload["queued"] is True for r in rows)
    assert [r.event_seq for r in rows] == list(range(20))


# ── a provider stall doesn't block reads or other sessions ─────────────────────────


async def test_a_slow_turn_does_not_block_reads_of_the_same_session(db_available: None) -> None:
    """Stands in for "a provider stall (mocked 60s) does not block reads or other
    sessions": scaled down to a real (short) sleep for test speed, but structurally
    identical -- the slow work happens with zero DB lock held, so a concurrent plain read
    of the same row must complete quickly, not wait for the slow call to finish."""
    tenant_id, session_id, definition = await _make_session_with_definition("lock-stall-read")
    principal_id = uuid.uuid4()
    scheduler = _single_candidate_scheduler(principal_id)

    async def stalling_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        await asyncio.sleep(0.4)
        return ActorTurnResult(content_md="slow turn")

    stall_task = asyncio.create_task(
        advance_session_locked(
            tenant_id,
            session_id,
            definition,
            claimant_id="slow-worker",
            next_actor_fn=scheduler,
            execute_turn=stalling_execute_turn,
            max_steps=1,
        )
    )
    await asyncio.sleep(0.05)  # let the stall begin (claim taken, now inside execute_turn)

    read_start = time.monotonic()
    row = await _get_session(tenant_id, session_id)
    read_elapsed = time.monotonic() - read_start

    assert row.claimed_by == "slow-worker"  # confirms we sampled mid-stall, not before/after
    assert read_elapsed < 0.2  # nowhere near the 0.4s stall -- the read was never blocked

    await stall_task  # let it finish so the test doesn't leak a background task


async def test_a_slow_turn_on_one_session_does_not_block_advancing_another(
    db_available: None,
) -> None:
    tenant_id_a, session_id_a, definition_a = await _make_session_with_definition("lock-stall-a")
    tenant_id_b, session_id_b, definition_b = await _make_session_with_definition("lock-stall-b")
    principal_id = uuid.uuid4()

    async def stalling_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        await asyncio.sleep(0.4)
        return ActorTurnResult(content_md="slow turn")

    slow_task = asyncio.create_task(
        advance_session_locked(
            tenant_id_a,
            session_id_a,
            definition_a,
            claimant_id="slow-worker",
            next_actor_fn=_single_candidate_scheduler(principal_id),
            execute_turn=stalling_execute_turn,
            max_steps=1,
        )
    )
    await asyncio.sleep(0.05)

    fast_start = time.monotonic()
    fast_result = await advance_session_locked(
        tenant_id_b,
        session_id_b,
        definition_b,
        claimant_id="fast-worker",
        next_actor_fn=_single_candidate_scheduler(principal_id),
        execute_turn=_instant_execute_turn,
        max_steps=1,
    )
    fast_elapsed = time.monotonic() - fast_start

    assert fast_result.status == "active"
    assert fast_elapsed < 0.2  # session B advanced without waiting on session A's stall

    await slow_task
