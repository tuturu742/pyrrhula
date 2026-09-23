"""A status track reacts to the record it guards, so a change has to be written to it.

``resolve_and_apply`` takes ``set_fields`` and a ``machine_key``/``trigger``, and applies
them in that order: fields first, then the transition, so the guard reads what just
happened. Sending the trigger alone is accepted and does nothing useful -- the guard reads
the unchanged field, finds nothing has changed, and the call answers
``{"transitioned": false, "new_state": "<the state it was already in>"}``.

That answer is true, and it is not what the caller meant, which is what makes it
expensive. Observed on a live session: two resolutions ran against one entity's own
record, each reducing a tracked resource past the guard's threshold, and the entity
finished unchanged and in its initial state. Every component was correct. The trigger had
simply been sent without the new value.

Both halves are pinned because the difference between them is invisible at the call site
and permanent in the record.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import EntityRow, create_entity
from core.process.skeleton import create_session
from core.resolution.consequence import resolve_and_apply
from core.resolution.rule_system import MINIMAL_D20_SYSTEM, RuleSystemDefinition, create_rule_system
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PERMISSIONS = RolePermissionService()

_HEALTH = {
    "key": "health",
    "initial": "healthy",
    "states": [
        {"key": "healthy", "label_key": "status.healthy"},
        {"key": "bloodied", "label_key": "status.bloodied"},
    ],
    # The shape a shipped schema uses: the guard reads the field, so the field has to
    # move before the trigger means anything.
    "transitions": [
        {
            "from": "healthy",
            "to": "bloodied",
            "trigger": "damage_taken",
            "guard": "fields.hit_points <= fields.max_hit_points / 2",
        }
    ],
}


async def _wounded_subject(slug: str):  # noqa: ANN201
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    row = await create_rule_system(tenant_id, MINIMAL_D20_SYSTEM)
    system, system_id = RuleSystemDefinition.from_row(row), row.id

    async with tenant_scope(tenant_id) as session:
        principal_id = (
            await session.execute(text("SELECT id FROM principal LIMIT 1"))
        ).scalar_one()
        held = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if held is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    role="facilitator",
                )
            )

    definition = EntitySchemaDefinition(
        fields=[
            FieldDef(key="hit_points", type="integer"),
            FieldDef(key="max_hit_points", type="integer"),
            # The stock system resolves its modifier from this; the guard ignores it.
            FieldDef(key="dexterity", type="integer"),
        ],
        state_machines=[_HEALTH],
    )
    schema_row = await save_schema(
        tenant_id, workspace_id, f"subject-{uuid.uuid4().hex[:6]}", 1, definition
    )
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"subject-{uuid.uuid4().hex[:8]}",
        name="Bram",
        scope_key="workspace_public",
        data={"hit_points": 5, "max_hit_points": 5, "dexterity": 10},
    )
    return tenant_id, workspace_id, principal_id, sess.id, entity.id, system, system_id


async def _read(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> tuple[int, str]:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(EntityRow, entity_id)
        assert row is not None
        return int(row.data["hit_points"]), str(row.fsm_states["health"])


async def test_the_trigger_alone_leaves_the_character_unhurt(db_available: None) -> None:
    (
        tenant_id,
        workspace_id,
        principal_id,
        session_id,
        entity_id,
        system,
        system_id,
    ) = await _wounded_subject("consequence-trigger-only")

    result = await resolve_and_apply(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        session_id=session_id,
        event_seq=1,
        tool_key="resolve_and_apply",
        actor_entity_id=entity_id,
        expression="1d6",
        check_type=sorted(system.check_types)[0],
        machine_key="health",
        trigger="damage_taken",
        set_fields={},
        actor_fields={"dexterity": 10, "hit_points": 5, "max_hit_points": 5},
        rule_system=system,
        rule_system_id=system_id,
        legal_check_types=None,
        permission_service=_PERMISSIONS,
    )

    assert result["transitioned"] is False
    assert result["new_state"] == "healthy"
    assert await _read(tenant_id, entity_id) == (5, "healthy"), (
        "the resolution is recorded and the record is untouched -- which is the failure "
        "this test exists to make visible, not a bug in the function"
    )


async def test_setting_the_total_with_the_trigger_moves_the_track(db_available: None) -> None:
    (
        tenant_id,
        workspace_id,
        principal_id,
        session_id,
        entity_id,
        system,
        system_id,
    ) = await _wounded_subject("consequence-with-fields")

    result = await resolve_and_apply(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        principal_id=principal_id,
        session_id=session_id,
        event_seq=1,
        tool_key="resolve_and_apply",
        actor_entity_id=entity_id,
        expression="1d6",
        check_type=sorted(system.check_types)[0],
        machine_key="health",
        trigger="damage_taken",
        # Seven against a starting five: the new total, sent with the trigger.
        set_fields={"hit_points": -2},
        actor_fields={"dexterity": 10, "hit_points": 5, "max_hit_points": 5},
        rule_system=system,
        rule_system_id=system_id,
        legal_check_types=None,
        permission_service=_PERMISSIONS,
    )

    assert result["transitioned"] is True
    assert result["new_state"] == "bloodied"
    assert await _read(tenant_id, entity_id) == (-2, "bloodied")
