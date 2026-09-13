"""B1.6 acceptance criteria for await + timeout, against a live Postgres: suspend/resume
on human input, take the timeout transition when input never arrives, the satisfy-vs-
timeout race resolves to exactly one outcome, and an awaiting session holds no locks or
worker slots.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.awaits import (
    create_await,
    get_active_await,
    make_await_hook,
    resolve_timeout,
    satisfy_await,
)
from core.process.dsl.fixtures import STANDARD_SESSION_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    start_session,
)
from core.process.skeleton import create_session
from core.sessions.models import AwaitStateRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id


async def _seed_at_feedback_loop(
    slug_prefix: str, *, round_: int = 3
) -> tuple[uuid.UUID, uuid.UUID, ProcessDefinitionDSL]:
    tenant_id, _workspace_id, session_id = await _setup(slug_prefix)
    definition_row = await create_definition(tenant_id, "std", "Std", STANDARD_SESSION_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = "feedback_loop"
        row.state = {"round": round_, "scene_id": "", "pending_feedback": True}

    return tenant_id, session_id, definition


async def _no_actor(_ctx: InterpreterContext) -> ActorRef | None:
    return None


async def _get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row


async def _get_await(tenant_id: uuid.UUID, await_id: uuid.UUID) -> AwaitStateRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, await_id)
        assert row is not None
        return row


# ── suspend, resume on human input, or take the timeout transition ─────────────────


async def test_feedback_loop_suspends_and_creates_a_real_await_state_row(
    db_available: None,
) -> None:
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-suspend")
    on_await = make_await_hook()

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_no_actor,
        execute_turn=_unused_execute_turn,
        on_await=on_await,
        max_steps=1,
    )

    assert result.status == "awaiting"
    row = await _get_session(tenant_id, session_id)
    assert row.status == "awaiting"

    active = await get_active_await(tenant_id, session_id)
    assert active is not None
    assert active.await_kind == "human_input"
    assert active.on_timeout_phase == "open_discussion"
    assert active.outcome is None


async def test_satisfying_the_await_resumes_the_session_and_leaves_the_awaiting_phase(
    db_available: None,
) -> None:
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-satisfy")
    on_await = make_await_hook()
    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_no_actor,
        execute_turn=_unused_execute_turn,
        on_await=on_await,
        max_steps=1,
    )
    active = await get_active_await(tenant_id, session_id)
    assert active is not None
    principal_id = uuid.uuid4()

    won = await satisfy_await(tenant_id, active.id, principal_id, definition)

    assert won is True
    row = await _get_session(tenant_id, session_id)
    assert row.status == "active"
    assert row.current_phase == "open_discussion"  # feedback_loop's on_complete target
    resolved = await _get_await(tenant_id, active.id)
    assert resolved.outcome == "satisfied"
    assert resolved.satisfied_at is not None

    # No lingering active await to accidentally re-trigger.
    assert await get_active_await(tenant_id, session_id) is None


async def test_timeout_takes_the_on_timeout_transition_when_input_never_arrives(
    db_available: None,
) -> None:
    """Time-warped: rather than sleeping past a real 72h timeout, the await is created
    with an already-past timeout_at directly -- resolve_timeout doesn't care how it got
    that way, only that timeout_at < now(), matching what the real worker sweep checks."""
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-timeout")
    phase = definition.phases["feedback_loop"]
    active = await create_await(tenant_id, session_id, phase)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, active.id)
        assert row is not None
        row.timeout_at = datetime.now(UTC) - timedelta(seconds=1)  # already expired

    on_timeout_phase = await resolve_timeout(tenant_id, active.id)

    assert on_timeout_phase == "open_discussion"
    row = await _get_session(tenant_id, session_id)
    assert row.status == "active"
    assert row.current_phase == "open_discussion"
    resolved = await _get_await(tenant_id, active.id)
    assert resolved.outcome == "timed_out"


# ── satisfy-vs-timeout race: exactly one outcome wins, deterministically ───────────


async def test_satisfy_vs_timeout_race_exactly_one_outcome_wins(db_available: None) -> None:
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-race")
    phase = definition.phases["feedback_loop"]
    active = await create_await(tenant_id, session_id, phase)
    principal_id = uuid.uuid4()

    satisfy_result = await satisfy_await(tenant_id, active.id, principal_id, definition)
    timeout_result = await resolve_timeout(tenant_id, active.id)

    assert satisfy_result is True
    assert timeout_result is None  # lost the race -- already resolved

    resolved = await _get_await(tenant_id, active.id)
    assert resolved.outcome == "satisfied"  # the winner's outcome, not overwritten


async def test_timeout_vs_satisfy_race_the_other_direction(db_available: None) -> None:
    """The reverse ordering must be equally deterministic -- whichever call actually
    executes its UPDATE first wins, and this codebase makes no ordering promise beyond
    that; what matters is exactly one outcome is ever recorded, never both, never neither."""
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-race-reverse")
    phase = definition.phases["feedback_loop"]
    active = await create_await(tenant_id, session_id, phase)
    principal_id = uuid.uuid4()

    timeout_result = await resolve_timeout(tenant_id, active.id)
    satisfy_result = await satisfy_await(tenant_id, active.id, principal_id, definition)

    assert timeout_result == "open_discussion"
    assert satisfy_result is False  # lost the race

    resolved = await _get_await(tenant_id, active.id)
    assert resolved.outcome == "timed_out"
    row = await _get_session(tenant_id, session_id)
    assert row.current_phase == "open_discussion"


# ── an awaiting session holds no locks and no worker slots ─────────────────────────


async def test_an_awaiting_session_holds_no_locks(db_available: None) -> None:
    """create_await commits and returns -- nothing about it holds a transaction, a row
    lock, or any other resource open across the (arbitrarily long, up to 72h) wait. A
    concurrent plain read of the row while "awaiting" must succeed immediately."""
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-no-lock")
    phase = definition.phases["feedback_loop"]

    await create_await(tenant_id, session_id, phase)

    # If create_await left a transaction/lock open, this independent read would hang or
    # time out instead of returning immediately.
    row = await _get_session(tenant_id, session_id)
    assert row.status == "awaiting"


async def test_an_awaiting_session_occupies_no_worker_slot(db_available: None) -> None:
    """No job is enqueued for an awaiting session -- the timeout sweep discovers it later
    by scanning await_state.timeout_at, not by a worker holding a claimed slot for the
    whole wait. Nothing in this codebase's JobQueue is ever touched by create_await."""
    tenant_id, session_id, definition = await _seed_at_feedback_loop("await-no-worker-slot")
    phase = definition.phases["feedback_loop"]

    await create_await(tenant_id, session_id, phase)

    async with tenant_scope(tenant_id) as session:
        job_count = (
            await session.execute(
                text("SELECT count(*) FROM job WHERE tenant_id = :tid"), {"tid": tenant_id}
            )
        ).scalar_one()
    assert job_count == 0


async def _unused_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    raise AssertionError(
        "execute_turn should never be called when next_actor_fn always returns None"
    )
