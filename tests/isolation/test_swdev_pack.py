"""F3.13's own acceptance tests for the swdev pack -- the third pack, keeping the D5/
INV-7 trust chain and the genericity bet (INV-9) exercised in a domain that is neither a
tabletop RPG nor an enterprise workflow. Loaded through the exact same generic
``core.packs.loader`` F3.7 built; no swdev-specific code anywhere outside
``packs/swdev/`` itself.
"""

from __future__ import annotations

import json
import pathlib
import uuid

import pytest
from sqlalchemy import select, text

from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.behavior.validation import (
    AxisDefinitionSchema,
    AxisValidationError,
    validate_axis_definition,
)
from core.entities.mutation import mutate, transition
from core.entities.repo import get_schema
from core.entities.storage import create_entity
from core.entities.tags import widget_for
from core.packs.loader import load_pack
from core.process.authoring import get_definition
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import evaluate_gates, start_session
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.resolution.service import render_resolution_fact, resolve
from core.tenancy.models import Workspace, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "swdev"
_PERMISSIONS = RolePermissionService()


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def _lifecycle_state(tenant_id: uuid.UUID, entity_id: uuid.UUID) -> str:
    async with tenant_scope(tenant_id) as session:
        return str(
            (
                await session.execute(
                    text("SELECT fsm_states ->> 'lifecycle' FROM entity WHERE id = :id"),
                    {"id": entity_id},
                )
            ).scalar_one()
        )


async def _authorized_principal_id(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> uuid.UUID:
    """Any existing principal in this tenant, granted a role whose permission set
    includes ``entity:mutate`` -- mirrors ``test_entity_mutation.py``'s own
    ``_principal_id``/``_grant`` helpers."""
    async with tenant_scope(tenant_id) as session:
        principal_id: uuid.UUID = (
            await session.execute(text("SELECT id FROM principal LIMIT 1"))
        ).scalar_one()
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is None:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    role="facilitator",
                )
            )
    return principal_id


async def test_merge_guard_is_pure_cel_over_the_generic_interpreter(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A ``pull_request`` whose linked ``build`` is not ``passed`` cannot transition to
    ``approved``; flipping the build to ``passed`` allows it -- both through the
    unmodified FSM interpreter (F3.2/F3.5), no core changes. The cross-entity link
    (``pull_request.build_status`` mirroring the linked ``build`` entity's own FSM
    state) is plain entity data a pack schema declares, not a new core mechanism --
    there is no cross-entity guard primitive in ``core.entities.fsm`` today, so this is
    the honest way to express it without one."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)

    build_schema = await get_schema(tenant_a, loaded.schema_ids["build"])
    pr_schema = await get_schema(tenant_a, loaded.schema_ids["pull_request"])
    assert build_schema is not None and pr_schema is not None

    build = await create_entity(
        tenant_a,
        workspace_id,
        build_schema.id,
        build_schema.to_definition(),
        key=f"build-{uuid.uuid4().hex[:8]}",
        name="CI Build",
        scope_key="workspace_public",
        data={"label": "CI Build #1", "work_item_ref": "work-item-1", "duration_seconds": 120},
    )
    pr = await create_entity(
        tenant_a,
        workspace_id,
        pr_schema.id,
        pr_schema.to_definition(),
        key=f"pr-{uuid.uuid4().hex[:8]}",
        name="Add rate limiting",
        scope_key="workspace_public",
        data={
            "title": "Add rate limiting",
            "work_item_ref": "work-item-1",
            "build_status": "queued",
        },
    )
    principal_id = await _authorized_principal_id(tenant_a, workspace_id)

    opened = await transition(
        principal_id,
        tenant_a,
        workspace_id,
        pr.id,
        "lifecycle",
        "open",
        f"merge-guard-open-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )
    assert opened["transitioned"] is True and opened["new_state"] == "open"

    # Build still queued -- the guard blocks the transition, not a validation error.
    blocked = await transition(
        principal_id,
        tenant_a,
        workspace_id,
        pr.id,
        "lifecycle",
        "approve",
        f"merge-guard-blocked-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )
    assert blocked["transitioned"] is False

    # The build passes (its own FSM, unrelated core code) -- then the PR's mirrored
    # field is updated to reflect it (the sync step a real delegation/webhook wiring,
    # not yet built, would eventually automate -- G4.16).
    await transition(
        principal_id,
        tenant_a,
        workspace_id,
        build.id,
        "lifecycle",
        "start",
        f"merge-guard-build-start-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )
    await transition(
        principal_id,
        tenant_a,
        workspace_id,
        build.id,
        "lifecycle",
        "succeed",
        f"merge-guard-build-pass-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )
    await mutate(
        principal_id,
        tenant_a,
        workspace_id,
        pr.id,
        {"build_status": "passed"},
        "human",
        None,
        f"merge-guard-sync-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )

    approved = await transition(
        principal_id,
        tenant_a,
        workspace_id,
        pr.id,
        "lifecycle",
        "approve",
        f"merge-guard-approved-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )
    assert approved["transitioned"] is True
    assert approved["new_state"] == "approved"


async def test_checklist_verdict_renders_from_resolution_record_not_prose(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``checklist_eval`` writes a ``ResolutionRecord`` (C1.5/C1.6's unmodified
    ``ResolutionService``) and the verdict renders from that record (INV-7) -- a model
    asserting "all checks passed" contrary to the record would be decoration, flagged by
    C1.7's existing check, not something this pack has to reimplement."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    await load_pack(_PACK_DIR, tenant_a, workspace_id)

    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)

    rule_system_row = await get_rule_system(tenant_a, "checklist_v1")
    assert rule_system_row is not None
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    pass_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=0,
        tool_key="checklist_eval",
        actor_entity_id=None,
        expression="1d1+1",
        check_type="definition_of_done",
        actor_fields={"tests_passing": True, "docs_updated": True, "reviewed": True},
        target=None,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
    )
    assert pass_record.outcome == "pass"
    rendered = render_resolution_fact(pass_record, check_type="definition_of_done")
    assert str(pass_record.id) in rendered
    assert "PASS" in rendered
    assert 'authoritative="true"' in rendered

    fail_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=1,
        tool_key="checklist_eval",
        actor_entity_id=None,
        expression="1d1-1",
        check_type="definition_of_done",
        actor_fields={"tests_passing": True, "docs_updated": False, "reviewed": True},
        target=None,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
    )
    assert fail_record.outcome == "fail"


def test_swdev_high_stakes_axis_requires_gate_binding() -> None:
    """``risk_tolerance`` (as shipped) is ``stakes: high`` with a real gate binding --
    E2.3's own rule, exercised on swdev content. Stripping the gate binding from the
    exact shipped definition must fail validation, proving the rule actually fires here
    rather than merely being satisfied by coincidence."""
    raw = json.loads((_PACK_DIR / "axes" / "risk_tolerance.json").read_text())
    shipped = AxisDefinitionSchema.model_validate(raw)
    assert shipped.stakes == "high"
    validate_axis_definition(shipped)  # must not raise -- the shipped file is valid

    raw["bindings"] = [b for b in raw["bindings"] if b["kind"] != "gate"]
    ungated = AxisDefinitionSchema.model_validate(raw)
    with pytest.raises(AxisValidationError, match="risk_tolerance"):
        validate_axis_definition(ungated)


async def test_work_item_renders_via_the_same_resource_widget_as_other_packs(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """``remaining_estimate`` [resource] resolves through the exact same tag->widget
    registry entry as the RPG pack's ``hit_points`` -- the third domain, same widget,
    zero new components (F3.10's own INV-9 discipline)."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    rpg_dir = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "rpg"
    rpg_loaded = await load_pack(rpg_dir, tenant_a, workspace_id)
    swdev_loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)

    rpg_schema = await get_schema(tenant_a, rpg_loaded.schema_ids["character"])
    work_item_schema = await get_schema(tenant_a, swdev_loaded.schema_ids["work_item"])
    assert rpg_schema is not None and work_item_schema is not None

    hit_points_field = next(f for f in rpg_schema.to_definition().fields if f.key == "hit_points")
    estimate_field = next(
        f for f in work_item_schema.to_definition().fields if f.key == "remaining_estimate"
    )
    assert (
        widget_for(hit_points_field.tags[0]) == widget_for(estimate_field.tags[0]) == "resource_bar"
    )


async def test_plan_implement_review_merge_smoke_session_runs_with_zero_core_diffs(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The process validates and boots through the unmodified interpreter -- zero branches
    in ``packages/core`` know this pack exists.

    ``implement`` used to declare an ``await`` as a placeholder for delegation that did not
    exist yet. Delegation is real now (G4.16 landed as the delegate endpoint and its worker
    jobs), so the placeholder is gone: it parked the flow with nothing in the product to
    satisfy it, on the phase where the engineers were meant to act."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)
    assert "plan_implement_review_merge" in loaded.process_definition_ids

    definition_row = await get_definition(
        tenant_a, loaded.process_definition_ids["plan_implement_review_merge"]
    )
    assert definition_row is not None
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    assert definition.initial_phase == "plan"

    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)
    await start_session(tenant_a, sess.id, definition, definition_row.id, definition_row.version)

    # Walk the flow the way the interpreter does once a phase's actors are exhausted --
    # `on_complete` for most phases, but `review` transitions on gates, and reading only
    # `on_complete` is what hid `merge` being unreachable while this chain looked correct.
    phase_chain = [definition.initial_phase]
    phase_key = evaluate_gates(definition.phases[definition.initial_phase], {})
    while phase_key is not None and phase_key not in phase_chain:
        phase_chain.append(phase_key)
        phase_key = evaluate_gates(definition.phases[phase_key], {})
    assert phase_chain == ["plan", "implement", "review", "merge"]
    assert definition.phases["implement"].await_field is None


async def test_swdev_pack_has_zero_core_imports() -> None:
    py_files = list(_PACK_DIR.rglob("*.py"))
    assert py_files == []


async def test_dispatch_walks_a_new_work_item_out_of_the_backlog(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Regression: dispatch fired `submit_for_review` alone.

    That transition is declared `from: in_progress`, while a freshly created work item has
    no stored state and resolves to the machine's initial one, `backlog`. Nothing matched,
    the mutation service correctly reported `transitioned: False`, and the item stayed in
    `backlog` through every delegation, branch, pull request and review run against it --
    so every session kept offering the same finished work, with the panel showing no state
    to explain why.

    The walk is the fix and it has to be idempotent: a second dispatch of an item already
    in review must not push it further or back.
    """
    from core.actions.delegation import DelegationResult, _drive_fsm
    from core.tenancy.models import Principal

    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)
    principal_id = await _authorized_principal_id(tenant_a, workspace_id)

    schema = await get_schema(tenant_a, loaded.schema_ids["work_item"])
    assert schema is not None
    item = await create_entity(
        tenant_a,
        workspace_id,
        schema.id,
        schema.to_definition(),
        key=f"wi-{uuid.uuid4().hex[:8]}",
        name="Write the README",
        scope_key="workspace_public",
        data={"title": "Write the README", "labels": []},
    )
    assert item.fsm_states == {}, "precondition: a new item has no stored state"

    async with tenant_scope(tenant_a) as session:
        viewer = await session.get(Principal, principal_id)
        assert viewer is not None
        session.expunge(viewer)

    walk = ("refine", "start", "submit_for_review")

    result = DelegationResult(action_key="a1", branch="pyr/test-1")
    await _drive_fsm(
        tenant_a,
        workspace_id,
        viewer,
        item.id,
        f"act-{uuid.uuid4().hex[:8]}",
        "lifecycle",
        walk,
        _PERMISSIONS,
        result,
    )

    assert result.transitions == ["ready", "in_progress", "in_review"]
    assert await _lifecycle_state(tenant_a, item.id) == "in_review"

    # Dispatching the same item again applies nothing: every trigger in the walk is
    # illegal from in_review, which is exactly the no-op the guard should produce.
    again = DelegationResult(action_key="a2", branch="pyr/test-1")
    await _drive_fsm(
        tenant_a,
        workspace_id,
        viewer,
        item.id,
        f"act-{uuid.uuid4().hex[:8]}",
        "lifecycle",
        walk,
        _PERMISSIONS,
        again,
    )
    assert again.transitions == []
    assert await _lifecycle_state(tenant_a, item.id) == "in_review"
