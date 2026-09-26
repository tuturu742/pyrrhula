"""its own isolation + acceptance tests: the workspace clock, scheduled entity
effects, and the between-sessions change feed.

Lives under ``tests/isolation/`` because `entity_schedule` is a new tenant-scoped RLS
table and CLAUDE.md rule 4 requires its negative test to land in the same PR -- and
because these tests need the ``two_tenants`` fixture, which only exists here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.permission.role_permission import RolePermissionService
from core.agents.authoring import create_agent
from core.agents.seed import seed_dev_agent
from core.assembler.visibility import seed_default_scopes
from core.entities.repo import save_schema
from core.entities.schedule import (
    ClockPermissionDeniedError,
    ClockRewindError,
    advance_clock,
    apply_due_schedules,
    create_schedule,
    due_ticks,
    get_clock,
    list_change_feed,
)
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ModelT
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.sessions.history import summarise_history
from core.tenancy.models import Principal, Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()

_PHASE = PhaseSpec(
    label_key="turn",
    actors=[ActorSpec(persona_type="supervisor", mode="generate")],
    visibility=VisibilitySpec(
        knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
    ),
    budget=BudgetSpec(ratio={}, max_tokens=400, history_ratio=0.5),
)


@dataclass
class _SilentProvider:
    """No prose in range means no model call; this double exists to prove that, and
    fails loudly if the summariser ever calls a model it didn't need to."""

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        raise NotImplementedError
        yield  # pragma: no cover -- makes this an async generator for the Protocol

    async def generate_structured(self, req: GenerationRequest, schema: type[ModelT]) -> ModelT:
        return schema.model_validate(dict.fromkeys(schema.model_fields, ""))

    def count_tokens(self, text_: str, model: str) -> int:
        return max(len(text_.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=False, supports_json_mode=True, supports_prompt_caching=False
        )


async def _workspace_of(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _facilitator(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name="clock-keeper")
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role="facilitator",
            )
        )
        return principal.id


async def _participant(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name="player")
        session.add(principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role="participant",
            )
        )
        return principal.id


async def _entity_with_counter(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, scope_key: str = "workspace_public"
) -> uuid.UUID:
    definition = EntitySchemaDefinition(fields=[FieldDef(key="counter", type="integer")])
    schema_row = await save_schema(
        tenant_id, workspace_id, f"ticker-{uuid.uuid4().hex[:6]}", 1, definition
    )
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"ticker-{uuid.uuid4().hex[:8]}",
        name="Ticker",
        scope_key=scope_key,
        data={"counter": 0},
    )
    return entity.id


# ── isolation (CLAUDE.md rule 4: new RLS table, negative test in the same PR) ────────


async def test_entity_schedule_filter_omission(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        workspace_id = await _workspace_of(tenant_id)
        await seed_default_scopes(tenant_id, workspace_id)
        entity_id = await _entity_with_counter(tenant_id, workspace_id)
        await create_schedule(
            tenant_id, workspace_id, entity_id, "tick", "every", 5, {"counter": 1}
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM entity_schedule"))).all()

    assert {row[0] for row in rows} == {tenant_a}


# ── due_ticks: pure, so exhaustively testable without a database ────────────────────


def test_due_ticks_is_half_open_on_the_left_and_closed_on_the_right() -> None:
    # 'at' fires exactly once, on the advance that crosses it.
    assert due_ticks("at", 5, 0, 4) == []
    assert due_ticks("at", 5, 0, 5) == [5]
    assert due_ticks("at", 5, 5, 10) == []  # already crossed; never re-offered
    # 'every' fires on each multiple inside the range.
    assert due_ticks("every", 3, 0, 10) == [3, 6, 9]
    assert due_ticks("every", 3, 3, 9) == [6, 9]
    assert due_ticks("every", 3, 9, 9) == []
    with pytest.raises(ValueError, match="unknown schedule kind"):
        due_ticks("whenever", 1, 0, 1)


# ── acceptance criteria ─────────────────────────────────────────────────────────────


async def test_scheduled_effect_applies_exactly_once_per_tick(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator_id = await _facilitator(tenant_id, workspace_id)
    entity_id = await _entity_with_counter(tenant_id, workspace_id)
    schedule = await create_schedule(
        tenant_id, workspace_id, entity_id, "upkeep", "at", 3, {"counter": 42}
    )

    advance = await advance_clock(
        tenant_id, workspace_id, facilitator_id, 5, permission_service=_PERMISSIONS
    )
    assert (advance.from_clock, advance.to_clock) == (0, 5)

    applied = await apply_due_schedules(
        tenant_id,
        workspace_id,
        facilitator_id,
        advance.from_clock,
        advance.to_clock,
        permission_service=_PERMISSIONS,
    )
    assert [(e.schedule_key, e.tick) for e in applied] == [("upkeep", 3)]

    # The worker retries the same range -- at-least-once delivery meeting rule 8's
    # idempotency key. The effect must not land twice.
    replayed = await apply_due_schedules(
        tenant_id,
        workspace_id,
        facilitator_id,
        advance.from_clock,
        advance.to_clock,
        permission_service=_PERMISSIONS,
    )
    assert [(e.schedule_key, e.tick) for e in replayed] == [("upkeep", 3)]

    async with tenant_scope(tenant_id) as session:
        change_rows = (
            await session.execute(
                text(
                    "SELECT cause, cause_ref, session_id, new_value FROM entity_state_change "
                    "WHERE entity_id = :entity_id"
                ),
                {"entity_id": entity_id},
            )
        ).all()

    assert len(change_rows) == 1, (
        f"the scheduled effect applied {len(change_rows)} times, not exactly once"
    )
    cause, cause_ref, session_id, new_value = change_rows[0]
    assert cause == "fsm"
    assert cause_ref == f"schedule:{schedule.id}:3"
    assert session_id is None  # out-of-session by construction
    assert new_value == 42


async def test_workspace_clock_only_advances_explicitly(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator_id = await _facilitator(tenant_id, workspace_id)
    participant_id = await _participant(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)

    assert await get_clock(tenant_id, workspace_id) == 0

    # Reading the clock, listing the change feed, and starting a session are all reads
    # as far as the timeline is concerned.
    await get_clock(tenant_id, workspace_id)
    await list_change_feed(tenant_id, workspace_id, frozenset({"workspace_public"}))
    await create_session(tenant_id, workspace_id, persona_id)
    assert await get_clock(tenant_id, workspace_id) == 0, (
        "the clock moved as a side effect of a read or a session start"
    )

    # Only the explicit, permission-gated act moves it.
    with pytest.raises(ClockPermissionDeniedError):
        await advance_clock(
            tenant_id, workspace_id, participant_id, 1, permission_service=_PERMISSIONS
        )
    assert await get_clock(tenant_id, workspace_id) == 0

    await advance_clock(tenant_id, workspace_id, facilitator_id, 7, permission_service=_PERMISSIONS)
    assert await get_clock(tenant_id, workspace_id) == 7

    # And it only ever moves forward.
    with pytest.raises(ClockRewindError):
        await advance_clock(
            tenant_id, workspace_id, facilitator_id, 2, permission_service=_PERMISSIONS
        )
    assert await get_clock(tenant_id, workspace_id) == 7


async def test_between_session_changes_surface_in_next_resume_summary(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _tenant_b = two_tenants
    workspace_id = await _workspace_of(tenant_id)
    await seed_default_scopes(tenant_id, workspace_id)
    facilitator_id = await _facilitator(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    entity_id = await _entity_with_counter(tenant_id, workspace_id)
    schedule = await create_schedule(
        tenant_id, workspace_id, entity_id, "aging", "at", 2, {"counter": 99}
    )

    # The previous session ends...
    last_session_ended_at = datetime.now(UTC)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    # ...time passes with nobody in a session, and a schedule fires.
    advance = await advance_clock(
        tenant_id, workspace_id, facilitator_id, 4, permission_service=_PERMISSIONS
    )
    await apply_due_schedules(
        tenant_id,
        workspace_id,
        facilitator_id,
        advance.from_clock,
        advance.to_clock,
        permission_service=_PERMISSIONS,
    )

    # It shows up in the change feed as a record with schedule provenance...
    feed = await list_change_feed(
        tenant_id,
        workspace_id,
        frozenset({"workspace_public"}),
        since=last_session_ended_at,
        out_of_session_only=True,
    )
    assert [(row.cause, row.cause_ref, row.in_session) for row in feed] == [
        ("fsm", f"schedule:{schedule.id}:2", False)
    ]

    # ...and in the next resume's summary as a record-injected fact.
    async with tenant_scope(tenant_id) as session:
        viewer = await session.get(Principal, facilitator_id)
        assert viewer is not None
        session.expunge(viewer)
    profile = await create_agent(
        tenant_id, f"g42-{uuid.uuid4().hex[:6]}", "echo", "echo-1", encryptor=IdentityEncryptor()
    )

    summary = await summarise_history(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _PHASE,
        from_event_seq=0,
        to_event_seq=10,
        max_tokens=200,
        agent=profile,
        provider=_SilentProvider(),
        permission_service=_PERMISSIONS,
        between_sessions_since=last_session_ended_at,
    )

    between = [f for f in summary.facts if f.fields.get("between_sessions") is True]
    assert len(between) == 1
    assert between[0].kind == "entity_change"
    assert between[0].event_seq is None
    assert between[0].fields["cause"] == "fsm"
    assert between[0].fields["to"] == 99
    assert "between_sessions=True" in summary.rendered_text

    # Without the between-sessions window, the same call sees nothing -- proving the
    # facts arrive through that arm and not incidentally through the event-range one.
    without = await summarise_history(
        tenant_id,
        workspace_id,
        sess.id,
        viewer,
        _PHASE,
        from_event_seq=0,
        to_event_seq=10,
        max_tokens=200,
        agent=profile,
        provider=_SilentProvider(),
        permission_service=_PERMISSIONS,
    )
    assert without.facts == ()
