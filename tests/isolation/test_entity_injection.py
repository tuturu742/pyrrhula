"""F3.6's own acceptance tests: private-field visibility and "injected, not retrieved"
against a live ``assemble()`` call. INV-10 replay coverage lives in
``tests/replay/test_entity_injection_replay.py``.
"""

from __future__ import annotations

import uuid

from core.assembler.context_assembler import assemble
from core.entities.injection import render_entity_state
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.process.dsl.schema import ActorSpec, PhaseSpec, VisibilitySpec
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _phase(scopes: list[str]) -> PhaseSpec:
    return PhaseSpec(
        label_key="turn",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=scopes, entity_fields="all", secrets="none"
        ),
        budget=None,
    )


async def _make_viewer(tenant_id: uuid.UUID, workspace_id: uuid.UUID, role: str) -> Principal:
    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name=role)
        session.add(viewer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id, workspace_id=workspace_id, principal_id=viewer.id, role=role
            )
        )
        await session.refresh(viewer)
        return viewer


async def test_private_field_absent_for_participant_present_for_authorized_viewer() -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"entity-inject-vis-{uuid.uuid4().hex[:8]}"
    )
    participant = await _make_viewer(tenant_id, workspace_id, "participant")
    facilitator = await _make_viewer(tenant_id, workspace_id, "facilitator")

    definition = EntitySchemaDefinition(
        fields=[
            FieldDef(key="name", type="string"),
            FieldDef(
                key="secret_motive",
                type="string",
                tags=["private"],
                scope_key="facilitator_only",
            ),
        ]
    )
    schema_row = await save_schema(tenant_id, workspace_id, "npc-schema", 1, definition)
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"npc-{uuid.uuid4().hex[:8]}",
        name="Suspicious NPC",
        scope_key="workspace_public",
        data={"name": "The Innkeeper", "secret_motive": "is secretly a spy"},
    )

    phase = _phase(["workspace_public", "facilitator_only"])
    session_id = uuid.uuid4()

    participant_block = await render_entity_state(
        tenant_id, workspace_id, session_id, participant, phase
    )
    facilitator_block = await render_entity_state(
        tenant_id, workspace_id, session_id, facilitator, phase
    )

    assert "secret_motive" not in participant_block.rendered_text
    assert "is secretly a spy" not in participant_block.rendered_text
    assert "The Innkeeper" in participant_block.rendered_text  # non-private field still shows

    assert "secret_motive" in facilitator_block.rendered_text
    assert "is secretly a spy" in facilitator_block.rendered_text

    assert facilitator_block.entity_versions == {str(entity.id): 1}
    assert participant_block.entity_versions == {str(entity.id): 1}


async def test_entity_state_is_injected_not_retrieved() -> None:
    """No knowledge sources exist at all in this workspace -- if entity text reaches
    the rendered context, it did so through the injection step, not the (empty, proven
    empty by ``manifest.entries == ()``) retrieval/bucket-fill pipeline."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"entity-inject-notretr-{uuid.uuid4().hex[:8]}"
    )
    viewer = await _make_viewer(tenant_id, workspace_id, "facilitator")

    definition = EntitySchemaDefinition(fields=[FieldDef(key="name", type="string")])
    schema_row = await save_schema(tenant_id, workspace_id, "marker-schema", 1, definition)
    marker = "ZzZ-DISTINCTIVE-ENTITY-MARKER-ZzZ"
    await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"marker-{uuid.uuid4().hex[:8]}",
        name=marker,
        scope_key="workspace_public",
        data={"name": marker},
    )

    phase = _phase(["workspace_public"])
    manifest = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=uuid.uuid4(),
        query_text="anything",
        query_embedding=[0.0] * 8,
        history_max_tokens=0,
        entity_state_renderer=render_entity_state,
    )

    assert manifest.entries == ()  # no knowledge chunks -- nothing to retrieve at all
    assert marker in manifest.rendered_context  # yet the entity's text is present
    assert marker in manifest.stable_prefix  # injected into the stable section (F3.6)
    assert manifest.entity_versions  # recorded on the manifest
