"""G4.3's isolation + acceptance tests: notification/reminder/timeout firing exactly once
each, visibility-filtered digests, and a long await surviving a process restart.

Lives under ``tests/isolation/`` because `notification` is a new tenant-scoped RLS table
and CLAUDE.md rule 4 requires its negative test in the same PR, and because these tests
need the ``two_tenants`` fixture.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from adapters.notifier.recording import RecordingNotifier
from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.entities.mutation import mutate
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.process.authoring import create_definition
from core.process.awaits import create_await, get_active_await, satisfy_await
from core.process.dsl.fixtures import STANDARD_SESSION_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import start_session
from core.process.skeleton import create_session
from core.sessions.models import AwaitStateRow, SessionRow
from core.sessions.notifications import (
    NotificationRow,
    build_digest,
    notify_await_opened,
    send_digest,
)
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import dispose_engine, tenant_scope
from worker.timeouts import sweep_due_reminders_for_tenant, sweep_expired_awaits_for_tenant

_PERMISSIONS = RolePermissionService()
_AWAIT_PHASE_KEY = "feedback_loop"


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _member(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str, name: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=name)
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role=role,
            )
        )
        await session.flush()
        session.expunge(principal)
        return principal


async def _session_at_await_phase(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, ProcessDefinitionDSL]:
    """A session pinned to STANDARD_SESSION_FLOW and parked in its human-await phase.
    Set directly rather than driven through the interpreter: the phases between the
    initial one and `feedback_loop` exercise scheduler/turn-taking machinery that has
    nothing to do with what G4.3 is about, and walking them would make these tests fail
    for reasons unrelated to their own claims."""
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    definition_row = await create_definition(
        tenant_id, f"std-{uuid.uuid4().hex[:6]}", "Std", STANDARD_SESSION_FLOW
    )
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(tenant_id, sess.id, definition, definition_row.id, definition_row.version)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, sess.id)
        assert row is not None
        row.current_phase = _AWAIT_PHASE_KEY
    return sess.id, definition


async def _backdate(tenant_id: uuid.UUID, await_id: uuid.UUID, **fields: datetime) -> None:
    """Move an await's clock into the past. Sweeps are time-driven, and a test that slept
    long enough to prove it would be a test nobody runs."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(AwaitStateRow, await_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)


# ── isolation (CLAUDE.md rule 4) ────────────────────────────────────────────────────


async def test_notification_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        workspace_id = await _workspace_of(tenant_id)
        principal = await _member(tenant_id, workspace_id, "participant", "recipient")
        session_id, definition = await _session_at_await_phase(tenant_id, workspace_id)
        phase = definition.phases[_AWAIT_PHASE_KEY]
        await_row = await create_await(tenant_id, session_id, phase)
        await notify_await_opened(
            tenant_id, workspace_id, await_row.id, notifier=RecordingNotifier()
        )
        assert principal is not None

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM notification"))).all()

    assert {row[0] for row in rows} == {tenant_a}


# ── acceptance criteria ─────────────────────────────────────────────────────────────


async def test_await_notification_reminder_and_timeout_fire_once_each(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await _member(tenant_id, workspace_id, "participant", "player")
    session_id, definition = await _session_at_await_phase(tenant_id, workspace_id)
    phase = definition.phases[_AWAIT_PHASE_KEY]

    # The definition's pacing default reaches the row: the phase's own await declares no
    # reminder_at, so it inherits `pacing.reminder_at` = 24h into a 72h window.
    reminder_after = definition.reminder_duration_for(phase)
    assert reminder_after == "24h"
    await_row = await create_await(tenant_id, session_id, phase, reminder_after=reminder_after)
    assert await_row.reminder_at is not None
    assert await_row.reminder_at < await_row.timeout_at

    notifier = RecordingNotifier()

    # 1. Opening notification -- and the retry that a job queue's at-least-once delivery
    #    makes inevitable.
    first = await notify_await_opened(tenant_id, workspace_id, await_row.id, notifier=notifier)
    retried = await notify_await_opened(tenant_id, workspace_id, await_row.id, notifier=notifier)
    assert len(first) == 1
    assert retried == []

    # 2. Reminder, at the configured fraction -- swept twice, sent once.
    await _backdate(tenant_id, await_row.id, reminder_at=datetime.now(UTC) - timedelta(minutes=1))
    assert await sweep_due_reminders_for_tenant(tenant_id, notifier) == 1
    assert await sweep_due_reminders_for_tenant(tenant_id, notifier) == 0

    # 3. Timeout -- swept twice, fires once (resolve_timeout's atomic UPDATE).
    await _backdate(tenant_id, await_row.id, timeout_at=datetime.now(UTC) - timedelta(minutes=1))
    assert await sweep_expired_awaits_for_tenant(tenant_id) == 1
    assert await sweep_expired_awaits_for_tenant(tenant_id) == 0

    async with tenant_scope(tenant_id) as session:
        kinds = sorted((await session.execute(select(NotificationRow.kind))).scalars().all())
        resolved = await session.get(AwaitStateRow, await_row.id)
        assert resolved is not None
        outcome = resolved.outcome
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        landed_in = session_row.current_phase

    assert kinds == ["await_opened", "await_reminder"]
    assert [n.kind for n in notifier.sent] == ["await_opened", "await_reminder"]
    assert outcome == "timed_out"
    assert landed_in == phase.await_field.on_timeout  # type: ignore[union-attr]


async def test_digest_is_visibility_filtered_per_recipient(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator = await _member(tenant_id, workspace_id, "facilitator", "director")
    participant = await _member(tenant_id, workspace_id, "participant", "player")
    session_id, definition = await _session_at_await_phase(tenant_id, workspace_id)
    phase = definition.phases["facilitator_frame"]  # sees both scopes; the viewer decides

    schema_definition = EntitySchemaDefinition(fields=[FieldDef(key="counter", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "digest-thing", 1, schema_definition)
    entities = {}
    for label, scope_key in (("public", "workspace_public"), ("hidden", "facilitator_only")):
        entity = await create_entity(
            tenant_id,
            workspace_id,
            schema_row.id,
            schema_definition,
            key=f"{label}-{uuid.uuid4().hex[:8]}",
            name=label,
            scope_key=scope_key,
            data={"counter": 0},
        )
        entities[label] = entity
        await mutate(
            facilitator.id,
            tenant_id,
            workspace_id,
            entity.id,
            {"counter": 3},
            "human",
            None,
            f"digest-{entity.id}",
            permission_service=_PERMISSIONS,
            session_id=session_id,
            event_seq=1,
        )

    notifier = RecordingNotifier()
    bodies = {}
    for viewer in (facilitator, participant):
        digest = await build_digest(
            tenant_id,
            workspace_id,
            viewer,
            phase,
            session_id=session_id,
            from_event_seq=0,
            to_event_seq=10,
            permission_service=_PERMISSIONS,
        )
        assert await send_digest(tenant_id, digest, "2026-08-14", notifier=notifier) is not None
        # One digest per principal per window, no matter how often the job re-runs.
        assert await send_digest(tenant_id, digest, "2026-08-14", notifier=notifier) is None
        bodies[viewer.id] = digest.body_md

    assert entities["public"].key in bodies[facilitator.id]
    assert entities["hidden"].key in bodies[facilitator.id]
    assert entities["public"].key in bodies[participant.id]
    assert entities["hidden"].key not in bodies[participant.id], (
        "a participant's digest contained a facilitator_only entity's change -- the digest "
        "is not visibility-filtered"
    )
    assert len(notifier.sent) == 2


async def test_long_await_survives_restart_and_resumes_on_reply(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    responder = await _member(tenant_id, workspace_id, "participant", "slow-replier")
    session_id, definition = await _session_at_await_phase(tenant_id, workspace_id)
    phase = definition.phases[_AWAIT_PHASE_KEY]

    await_row = await create_await(tenant_id, session_id, phase, reminder_after="24h")
    assert (await_row.timeout_at - datetime.now(UTC)) > timedelta(days=2)  # a week-long await
    await_id = await_row.id

    # The process restarts. Every connection, every bit of in-memory state, is gone --
    # `await_state` is a row, so the await is not.
    await dispose_engine()

    async with tenant_scope(tenant_id) as session:
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None
        assert session_row.status == "awaiting"

    recovered = await get_active_await(tenant_id, session_id)
    assert recovered is not None
    assert recovered.id == await_id

    # The human finally replies.
    assert await satisfy_await(tenant_id, await_id, responder.id, definition) is True

    async with tenant_scope(tenant_id) as session:
        resolved = await session.get(AwaitStateRow, await_id)
        assert resolved is not None
        session_row = await session.get(SessionRow, session_id)
        assert session_row is not None

        assert resolved.outcome == "satisfied"
        assert session_row.status == "active"
        # `on_complete`, not `on_timeout` -- a satisfied await takes the phase's real exit.
        assert session_row.current_phase == phase.on_complete

    # A timeout sweep arriving afterwards finds nothing to do: a satisfied await never
    # times out, which is the transactional guarantee the whole await module is built on.
    await _backdate(tenant_id, await_id, timeout_at=datetime.now(UTC) - timedelta(minutes=1))
    assert await sweep_expired_awaits_for_tenant(tenant_id) == 0
