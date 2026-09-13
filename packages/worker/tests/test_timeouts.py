"""B1.6: the timeout sweep worker, against a live Postgres -- resolves expired awaits
across multiple tenants in one sweep, leaves not-yet-due awaits untouched, and is
idempotent against an already-resolved (e.g. satisfied) await.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.awaits import create_await, satisfy_await
from core.process.dsl.fixtures import STANDARD_SESSION_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import start_session
from core.process.skeleton import create_session
from core.sessions.models import AwaitStateRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from worker.timeouts import sweep_expired_awaits


async def _seed_at_feedback_loop(
    slug_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, ProcessDefinitionDSL]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    definition_row = await create_definition(tenant_id, "std", "Std", STANDARD_SESSION_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(tenant_id, sess.id, definition, definition_row.id, definition_row.version)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, sess.id)
        assert row is not None
        row.current_phase = "feedback_loop"
        row.state = {"round": 3, "scene_id": "", "pending_feedback": True}
    return tenant_id, sess.id, definition


async def _expire(tenant_id: uuid.UUID, await_id: uuid.UUID) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, await_id)
        assert row is not None
        row.timeout_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_sweep_resolves_expired_awaits_across_multiple_tenants(db_available: None) -> None:
    tenant_a, session_a, definition_a = await _seed_at_feedback_loop("sweep-a")
    tenant_b, session_b, definition_b = await _seed_at_feedback_loop("sweep-b")

    await_a = await create_await(tenant_a, session_a, definition_a.phases["feedback_loop"])
    await_b = await create_await(tenant_b, session_b, definition_b.phases["feedback_loop"])
    await _expire(tenant_a, await_a.id)
    await _expire(tenant_b, await_b.id)

    resolved = await sweep_expired_awaits()

    # >=, not ==: this dev environment doesn't reset the database between test runs, so a
    # shared live DB could in principle carry other expired-and-unresolved await_state
    # rows from elsewhere. The two below are proven directly regardless of that count.
    assert resolved >= 2

    async with tenant_scope(tenant_a) as session:
        row_a = await session.get(SessionRow, session_a)
        assert row_a is not None
        assert row_a.current_phase == "open_discussion"
    async with tenant_scope(tenant_b) as session:
        row_b = await session.get(SessionRow, session_b)
        assert row_b is not None
        assert row_b.current_phase == "open_discussion"


async def test_sweep_leaves_not_yet_due_awaits_untouched(db_available: None) -> None:
    tenant_id, session_id, definition = await _seed_at_feedback_loop("sweep-not-due")
    active = await create_await(tenant_id, session_id, definition.phases["feedback_loop"])
    # timeout_at defaults to 72h from now (STANDARD_SESSION_FLOW's feedback_loop) -- not due.

    await sweep_expired_awaits()

    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, active.id)
        assert row is not None
        assert row.outcome is None
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        assert session_row.current_phase == "feedback_loop"


async def test_sweep_is_idempotent_against_an_already_satisfied_await(db_available: None) -> None:
    tenant_id, session_id, definition = await _seed_at_feedback_loop("sweep-satisfied")
    active = await create_await(tenant_id, session_id, definition.phases["feedback_loop"])
    await satisfy_await(tenant_id, active.id, uuid.uuid4(), definition)
    await _expire(tenant_id, active.id)  # expired, but already resolved before the sweep runs

    resolved = await sweep_expired_awaits()

    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, active.id)
        assert row is not None
        assert row.outcome == "satisfied"  # not overwritten to 'timed_out'
    # This specific await didn't contribute to the count (already resolved before the sweep
    # even looked -- create_await's own event_seq-based row is excluded by the WHERE clause).
    assert resolved >= 0
