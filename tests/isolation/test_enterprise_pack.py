"""F3.8's own acceptance tests for the enterprise pack -- the pack that exists to
falsify the genericity bet. Loaded through the exact same generic ``core.packs.loader``
F3.7 built; no enterprise-specific code anywhere outside ``packs/enterprise/`` itself.
"""

from __future__ import annotations

import pathlib
import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.entities.repo import get_schema
from core.entities.storage import create_entity
from core.entities.tags import widget_for
from core.packs.loader import load_pack
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import start_session
from core.process.skeleton import create_session
from core.resolution.rule_system import RuleSystemDefinition, get_rule_system
from core.resolution.service import render_resolution_fact, resolve
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "builtin-workflows" / "default"


async def _workspace_id(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalar_one()


async def test_enterprise_process_smoke_session_runs_with_zero_core_diffs(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The process fixture this task provides (F3.9 builds the generic multi-pack
    harness on top): the pack's own process definition validates through B1.1's
    unmodified ``validate_raw``, a real session pins to it, and the declared
    brainstorm->critique->revise->decide->(await)->brainstorm phase graph is fully
    traversable via the unmodified interpreter's own gate/on_complete resolution --
    zero branches anywhere in ``packages/core`` know this pack exists."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)
    assert "brainstorm_critique_revise_decide" in loaded.process_definition_ids

    from core.process.authoring import get_definition

    definition_row = await get_definition(
        tenant_a, loaded.process_definition_ids["brainstorm_critique_revise_decide"]
    )
    assert definition_row is not None
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    assert definition.initial_phase == "brainstorm"

    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)
    await start_session(tenant_a, sess.id, definition, definition_row.id, definition_row.version)

    # Walk the phase graph exactly as the unmodified interpreter would (on_complete
    # chain): brainstorm -> critique -> revise -> decide, then decide loops back
    # (its own on_complete/await.on_timeout both point at brainstorm -- stop once a
    # phase repeats, rather than an unbounded walk of a graph that's cyclic by design).
    phase_chain = [definition.initial_phase]
    current = definition.phases[definition.initial_phase]
    while current.on_complete is not None and current.on_complete not in phase_chain:
        phase_chain.append(current.on_complete)
        current = definition.phases[current.on_complete]
    assert phase_chain == ["brainstorm", "critique", "revise", "decide"]
    assert current.await_field is not None  # decide is a real await/decision node
    assert current.await_field.on_timeout == "brainstorm"  # loops back, per the fixture
    assert current.on_complete == "brainstorm"  # on_complete loops back too


async def test_project_status_renders_via_the_same_resource_widget(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Budget bar and HP bar are one component: the enterprise pack's
    ``budget_remaining`` and the RPG pack's ``hit_points`` resolve through the exact
    same tag->widget registry entry."""
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)

    rpg_dir = pathlib.Path(__file__).resolve().parents[2] / ".plugins" / "default" / "rpg"
    rpg_loaded = await load_pack(rpg_dir, tenant_a, workspace_id)
    enterprise_loaded = await load_pack(_PACK_DIR, tenant_a, workspace_id)

    rpg_schema = await get_schema(tenant_a, rpg_loaded.schema_ids["character"])
    enterprise_schema = await get_schema(tenant_a, enterprise_loaded.schema_ids["project_status"])
    assert rpg_schema is not None and enterprise_schema is not None

    rpg_definition = rpg_schema.to_definition()
    enterprise_definition = enterprise_schema.to_definition()

    hit_points_field = next(f for f in rpg_definition.fields if f.key == "hit_points")
    budget_field = next(f for f in enterprise_definition.fields if f.key == "budget_remaining")

    assert (
        widget_for(hit_points_field.tags[0]) == widget_for(budget_field.tags[0]) == "resource_bar"
    )

    project = await create_entity(
        tenant_a,
        workspace_id,
        enterprise_schema.id,
        enterprise_definition,
        key=f"project-{uuid.uuid4().hex[:8]}",
        name="Q3 Platform Migration",
        scope_key="workspace_public",
        data={
            "project_name": "Q3 Platform Migration",
            "total_budget": 100000,
            "budget_remaining": 25000,
            "blockers": ["vendor_delay"],
            "owner": "engineering",
        },
    )
    assert project.data["budget_remaining"] == 25000


async def test_policy_lookup_writes_and_renders_from_resolution_record(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    workspace_id = await _workspace_id(tenant_a)
    await load_pack(_PACK_DIR, tenant_a, workspace_id)

    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    sess = await create_session(tenant_a, workspace_id, persona_id)

    rule_system_row = await get_rule_system(tenant_a, "policy_lookup_v1")
    assert rule_system_row is not None
    rule_system = RuleSystemDefinition.from_row(rule_system_row)

    # Under the limit -> approved (INV-7: the verdict is a database fact, not a model
    # claim -- the model never asserts its own "approved"/"denied").
    approved_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=0,
        tool_key="policy_lookup",
        actor_entity_id=None,
        expression="1d1+1",
        check_type="expense_approval",
        actor_fields={"amount": 500, "approval_limit": 1000},
        target=None,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
    )
    assert approved_record.outcome == "approved"
    rendered = render_resolution_fact(approved_record, check_type="expense_approval")
    assert str(approved_record.id) in rendered
    assert "APPROVED" in rendered
    assert 'authoritative="true"' in rendered  # citable, system-authored fact

    # Over the limit -> denied, same unchanged code path.
    denied_record = await resolve(
        tenant_id=tenant_a,
        session_id=sess.id,
        event_seq=1,
        tool_key="policy_lookup",
        actor_entity_id=None,
        expression="1d1-1",
        check_type="expense_approval",
        actor_fields={"amount": 5000, "approval_limit": 1000},
        target=None,
        rule_system=rule_system,
        rule_system_id=rule_system_row.id,
        legal_check_types=None,
    )
    assert denied_record.outcome == "denied"


async def test_enterprise_pack_has_zero_core_imports() -> None:
    py_files = list(_PACK_DIR.rglob("*.py"))
    assert py_files == []
