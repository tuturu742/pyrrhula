"""its own acceptance tests: concurrent-transition serialisation, idempotent replay,
atomic rollback on a planted effect failure, and permission-before-locking.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, text

from adapters.permission.role_permission import RolePermissionService
from core.entities.fsm import EntityStateChangeRow, StateMachineDef
from core.entities.mutation import PermissionDeniedError, mutate, transition
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import EntityRow, create_entity
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PERMISSIONS = RolePermissionService()


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _grant(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is not None:
            return
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id, workspace_id=workspace_id, principal_id=principal_id, role=role
            )
        )


async def _principal_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(text("SELECT id FROM principal LIMIT 1"))
        principal_id: uuid.UUID = result.scalar_one()
        return principal_id


async def _entity_row(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> EntityRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(EntityRow, entity_id)
        assert row is not None
        return row


_ADVANCE_MACHINE_JSON = {
    "key": "progress",
    "states": [
        {"key": "a", "label_key": "x"},
        {"key": "b", "label_key": "x"},
        {"key": "c", "label_key": "x"},
    ],
    "initial": "a",
    "transitions": [
        {"from": "a", "to": "b", "trigger": "advance"},
        {"from": "b", "to": "c", "trigger": "advance"},
    ],
}


async def test_concurrent_transitions_serialise_without_lost_updates(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    principal_id = await _principal_id(tenant_a)
    await _grant(tenant_a, workspace_id, principal_id, "facilitator")

    machine = StateMachineDef.model_validate(_ADVANCE_MACHINE_JSON)
    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")], state_machines=[machine]
    )
    schema_row = await save_schema(tenant_a, workspace_id, "advance-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"advance-entity-{uuid.uuid4().hex[:8]}",
        name="Advancer",
        scope_key="workspace_public",
        data={"name": "x"},
    )

    results = await asyncio.gather(
        transition(
            principal_id,
            tenant_a,
            workspace_id,
            entity_row.id,
            "progress",
            "advance",
            f"concurrent-1-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        ),
        transition(
            principal_id,
            tenant_a,
            workspace_id,
            entity_row.id,
            "progress",
            "advance",
            f"concurrent-2-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        ),
    )

    assert all(r["transitioned"] for r in results)
    versions = sorted(r["version"] for r in results)
    assert versions == [2, 3]  # strictly monotonic, no lost update

    final = await _entity_row(tenant_a, entity_row.id)
    assert final.version == 3
    assert final.fsm_states["progress"] == "c"

    async with tenant_scope(tenant_a) as session:
        rows = (
            await session.execute(
                select(EntityStateChangeRow).where(EntityStateChangeRow.entity_id == entity_row.id)
            )
        ).scalars()
        change_rows = list(rows)
    assert len(change_rows) == 2
    assert {(r.old_value, r.new_value) for r in change_rows} == {("a", "b"), ("b", "c")}


async def test_replayed_mutation_is_a_noop_returning_recorded_result(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    principal_id = await _principal_id(tenant_a)
    await _grant(tenant_a, workspace_id, principal_id, "facilitator")

    definition = EntitySchemaDefinition(fields=[FieldDef(key="count", type="integer")])
    schema_row = await save_schema(tenant_a, workspace_id, "replay-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"replay-entity-{uuid.uuid4().hex[:8]}",
        name="Replayed",
        scope_key="workspace_public",
        data={"count": 0},
    )

    key = f"replay-key-{uuid.uuid4()}"
    first = await mutate(
        principal_id,
        tenant_a,
        workspace_id,
        entity_row.id,
        {"count": 1},
        "human",
        None,
        key,
        permission_service=_PERMISSIONS,
    )
    second = await mutate(
        principal_id,
        tenant_a,
        workspace_id,
        entity_row.id,
        {"count": 1},
        "human",
        None,
        key,
        permission_service=_PERMISSIONS,
    )

    assert first == second
    final = await _entity_row(tenant_a, entity_row.id)
    assert final.version == 2  # bumped exactly once, not twice


async def test_failed_effect_rolls_back_the_whole_transition(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    principal_id = await _principal_id(tenant_a)
    await _grant(tenant_a, workspace_id, principal_id, "facilitator")

    # "zero_field" is always 0 in real data; the effect's CEL divides by it -- passes
    # the dummy-value compile-check (dummy zero_field=1, 1/1=1, no error) but fails
    # for real at transition time.
    machine = StateMachineDef.model_validate(
        {
            "key": "risky",
            "states": [{"key": "start", "label_key": "x"}, {"key": "end", "label_key": "x"}],
            "initial": "start",
            "transitions": [
                {
                    "from": "start",
                    "to": "end",
                    "trigger": "go",
                    "effects": [
                        {"kind": "set_field", "field": "score", "value": "1 / fields.zero_field"}
                    ],
                }
            ],
        }
    )
    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="zero_field", type="integer"), FieldDef(key="score", type="integer")],
        state_machines=[machine],
    )
    schema_row = await save_schema(tenant_a, workspace_id, "risky-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"risky-entity-{uuid.uuid4().hex[:8]}",
        name="Risky",
        scope_key="workspace_public",
        data={"zero_field": 0, "score": 0},
    )

    with pytest.raises(Exception):  # noqa: B017 -- CELValidationError wraps the real ZeroDivisionError
        await transition(
            principal_id,
            tenant_a,
            workspace_id,
            entity_row.id,
            "risky",
            "go",
            f"risky-key-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        )

    final = await _entity_row(tenant_a, entity_row.id)
    assert final.version == 1  # untouched
    assert final.fsm_states == {"risky": "start"}  # still where it started, never advanced
    assert final.data == {"zero_field": 0, "score": 0}

    async with tenant_scope(tenant_a) as session:
        rows = (
            await session.execute(
                select(EntityStateChangeRow).where(EntityStateChangeRow.entity_id == entity_row.id)
            )
        ).scalars()
        assert list(rows) == []


async def test_permission_denied_before_locking(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    principal_id = await _principal_id(tenant_a)
    # Deliberately no WorkspaceMembership grant at all -- no role, no entity:mutate.

    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])
    schema_row = await save_schema(tenant_a, workspace_id, "denied-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"denied-entity-{uuid.uuid4().hex[:8]}",
        name="Denied",
        scope_key="workspace_public",
        data={"name": "x"},
    )

    with pytest.raises(PermissionDeniedError):
        await mutate(
            principal_id,
            tenant_a,
            workspace_id,
            entity_row.id,
            {"name": "y"},
            "human",
            None,
            f"denied-key-{uuid.uuid4()}",
            permission_service=_PERMISSIONS,
        )

    final = await _entity_row(tenant_a, entity_row.id)
    assert final.version == 1
    assert final.data == {"name": "x"}


async def test_a_new_entity_is_already_in_every_machine_s_initial_state(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``fsm_states`` used to start empty, with every reader coalescing a missing key to
    ``machine.initial``. That reads as equivalent and is not: the state was never written
    down, so anything listing entities *by* their states -- the session Characters panel,
    the work board's status column -- saw a row with no states at all and skipped it. A
    character with a ``health`` machine is ``healthy`` from the moment it exists.
    """
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    machine = StateMachineDef.model_validate(_ADVANCE_MACHINE_JSON)
    definition = EntitySchemaDefinition(
        fields=[FieldDef(key="name", type="string")], state_machines=[machine]
    )
    schema_row = await save_schema(tenant_a, workspace_id, "seeded-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"seeded-entity-{uuid.uuid4().hex[:8]}",
        name="Seeded",
        scope_key="workspace_public",
        data={"name": "x"},
    )

    assert entity_row.fsm_states == {"progress": "a"}
    assert (await _entity_row(tenant_a, entity_row.id)).fsm_states == {"progress": "a"}


async def test_a_schema_without_machines_still_creates_an_entity(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])
    schema_row = await save_schema(tenant_a, workspace_id, "machineless-schema", 1, definition)
    entity_row = await create_entity(
        tenant_a,
        workspace_id,
        schema_row.id,
        definition,
        key=f"machineless-{uuid.uuid4().hex[:8]}",
        name="Plain",
        scope_key="workspace_public",
        data={"name": "x"},
    )

    assert entity_row.fsm_states == {}
