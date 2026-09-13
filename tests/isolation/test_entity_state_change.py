"""F3.2's own isolation + acceptance tests: guarded FSM transitions, the append-only
grant on ``entity_state_change``, and static FSM validation (reachability, dangling
transitions) hooked into F3.1's schema save path.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from core.entities.fsm import (
    EntityStateChangeRow,
    StateMachineDef,
    evaluate_and_record_transition,
)
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.entities.validation import SchemaValidationError
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

_MINIMAL_ENTITY_DEFINITION = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])

# Built via model_validate from raw dicts (the "from" alias key), not Python kwargs --
# this is also how pack content is actually authored (JSON), not constructed in Python.
_HEALTH_MACHINE = StateMachineDef.model_validate(
    {
        "key": "health",
        "states": [
            {"key": "healthy", "label_key": "status.healthy"},
            {"key": "bloodied", "label_key": "status.bloodied"},
            {"key": "unconscious", "label_key": "status.unconscious"},
            {"key": "dead", "label_key": "status.dead"},
        ],
        "initial": "healthy",
        "transitions": [
            {
                "from": "healthy",
                "to": "bloodied",
                "trigger": "damage_taken",
                "guard": "fields.hit_points <= 10",
            },
            {
                "from": "bloodied",
                "to": "unconscious",
                "trigger": "damage_taken",
                "guard": "fields.hit_points <= 0",
            },
        ],
    }
)

_TICKET_MACHINE = StateMachineDef.model_validate(
    {
        "key": "lifecycle",
        "states": [
            {"key": "draft", "label_key": "status.draft"},
            {"key": "in_review", "label_key": "status.in_review"},
            {"key": "approved", "label_key": "status.approved"},
        ],
        "initial": "draft",
        "transitions": [
            {"from": "draft", "to": "in_review", "trigger": "submit"},
            {"from": "in_review", "to": "approved", "trigger": "approve"},
        ],
    }
)


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _change_rows_for(
    tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> list[EntityStateChangeRow]:
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            select(EntityStateChangeRow).where(EntityStateChangeRow.entity_id == entity_id)
        )
        return list(result.scalars())


async def _make_entity(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID:
    """A real ``entity`` row -- ``entity_state_change.entity_id`` FKs to it (added by
    F3.3's migration once the ``entity`` table existed to FK to)."""
    schema_row = await save_schema(
        tenant_id, workspace_id, f"fixture-{uuid.uuid4().hex[:8]}", 1, _MINIMAL_ENTITY_DEFINITION
    )
    row = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        _MINIMAL_ENTITY_DEFINITION,
        key=f"fixture-{uuid.uuid4().hex[:8]}",
        name="Fixture Entity",
        scope_key="workspace_public",
        data={"name": "x"},
    )
    return row.id


_PROGRESSION_MACHINE = StateMachineDef.model_validate(
    {
        "key": "rank",
        "states": [
            {"key": "novice", "label_key": "rank.novice"},
            {"key": "veteran", "label_key": "rank.veteran"},
        ],
        "initial": "novice",
        "transitions": [
            {
                "from": "novice",
                "to": "veteran",
                # "on_change:<derived field>" is a plain trigger-name convention, not a
                # special interpreter feature -- whatever computes the new derived value
                # (F3.5's mutation service) calls the exact same
                # evaluate_and_record_transition with this trigger string. No
                # "progression engine" exists anywhere in this module.
                "trigger": "on_change:level",
                "guard": "fields.level >= 5",
            }
        ],
    }
)


async def test_progression_pattern_is_a_trigger_convention_not_a_special_engine(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    entity_id = await _make_entity(tenant_a, workspace_id)

    # Derived `level` hasn't crossed the threshold yet.
    result = await evaluate_and_record_transition(
        tenant_a,
        entity_id,
        None,
        None,
        _PROGRESSION_MACHINE,
        "novice",
        "on_change:level",
        {"level": 3},
    )
    assert result is None

    # Derived `level` crosses the threshold -- same call, same function, new value.
    result = await evaluate_and_record_transition(
        tenant_a,
        entity_id,
        None,
        None,
        _PROGRESSION_MACHINE,
        "novice",
        "on_change:level",
        {"level": 5},
    )
    assert result == "veteran"


async def test_guard_refusal_writes_nothing_and_success_writes_one_change_row(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    entity_id = await _make_entity(tenant_a, workspace_id)

    # False guard: hit_points is still high, the damage_taken trigger fires but the
    # transition's own guard refuses it.
    result = await evaluate_and_record_transition(
        tenant_a,
        entity_id,
        None,
        None,
        _HEALTH_MACHINE,
        "healthy",
        "damage_taken",
        {"hit_points": 20},
    )
    assert result is None
    assert await _change_rows_for(tenant_a, entity_id) == []

    # True guard: same trigger, lower hit_points -> transitions and writes exactly one row.
    result = await evaluate_and_record_transition(
        tenant_a,
        entity_id,
        None,
        None,
        _HEALTH_MACHINE,
        "healthy",
        "damage_taken",
        {"hit_points": 5},
    )
    assert result == "bloodied"
    rows = await _change_rows_for(tenant_a, entity_id)
    assert len(rows) == 1
    assert rows[0].cause == "fsm"
    assert rows[0].old_value == "healthy"
    assert rows[0].new_value == "bloodied"


async def test_health_and_ticket_lifecycles_share_the_interpreter_path(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """No domain branches: the exact same ``evaluate_and_record_transition`` call
    drives both an HP-style and a ticket-style FSM."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    health_entity = await _make_entity(tenant_a, workspace_id)
    health_result = await evaluate_and_record_transition(
        tenant_a,
        health_entity,
        None,
        None,
        _HEALTH_MACHINE,
        "healthy",
        "damage_taken",
        {"hit_points": 1},
    )

    ticket_entity = await _make_entity(tenant_a, workspace_id)
    ticket_result = await evaluate_and_record_transition(
        tenant_a, ticket_entity, None, None, _TICKET_MACHINE, "draft", "submit", {}
    )

    assert health_result == "bloodied"
    assert ticket_result == "in_review"
    health_rows = await _change_rows_for(tenant_a, health_entity)
    ticket_rows = await _change_rows_for(tenant_a, ticket_entity)
    assert [r.field_path for r in health_rows] == ["fsm_states.health"]
    assert [r.field_path for r in ticket_rows] == ["fsm_states.lifecycle"]


async def test_entity_state_change_is_append_only_as_grants(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    entity_id = await _make_entity(tenant_a, workspace_id)
    await evaluate_and_record_transition(
        tenant_a, entity_id, None, None, _TICKET_MACHINE, "draft", "submit", {}
    )
    rows = await _change_rows_for(tenant_a, entity_id)
    row_id = rows[0].id

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("UPDATE entity_state_change SET cause = 'human' WHERE id = :id"),
                {"id": row_id},
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("DELETE FROM entity_state_change WHERE id = :id"), {"id": row_id}
            )


async def test_unreachable_state_and_dangling_target_fail_validation(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    # Dangling transition target, in isolation: reachability is deliberately skipped for
    # a machine that already has a dangling target (same ordering core.process.dsl
    # .validator uses -- one bad edge shouldn't also spam "unreachable" downstream of it).
    dangling_machine = StateMachineDef.model_validate(
        {
            "key": "dangling",
            "states": [{"key": "start", "label_key": "x"}],
            "initial": "start",
            "transitions": [{"from": "start", "to": "nowhere", "trigger": "go"}],
        }
    )
    dangling_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")], state_machines=[dangling_machine]
    )
    try:
        await save_schema(tenant_a, workspace_id, "dangling-machine-schema", 1, dangling_definition)
        raise AssertionError("expected SchemaValidationError")
    except SchemaValidationError as exc:
        messages = [f"{i.field_path}: {i.message}" for i in exc.issues]
        assert any("nowhere" in m for m in messages)

    # Unreachable state, in isolation: no dangling target this time, so reachability
    # actually runs and names the orphaned state.
    unreachable_machine = StateMachineDef.model_validate(
        {
            "key": "unreachable",
            "states": [{"key": "start", "label_key": "x"}, {"key": "orphan", "label_key": "x"}],
            "initial": "start",
            "transitions": [],
        }
    )
    unreachable_definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")], state_machines=[unreachable_machine]
    )
    try:
        await save_schema(
            tenant_a, workspace_id, "unreachable-machine-schema", 1, unreachable_definition
        )
        raise AssertionError("expected SchemaValidationError")
    except SchemaValidationError as exc:
        messages = [f"{i.field_path}: {i.message}" for i in exc.issues]
        assert any("orphan" in m for m in messages)
