"""INV-10 for F3.6: a turn with injected entities replays byte-identical from the
manifest + pinned entity versions. Mirrors ``test_context_manifest_replay.py``'s shape
(re-running ``assemble()`` with the same recorded inputs reproduces ``rendered_hash``
exactly) -- the property INV-10 needs, extended to cover the entity-injection step.
"""

from __future__ import annotations

import uuid

from adapters.permission.role_permission import RolePermissionService
from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.assembler.manifest import write_context_manifest
from core.entities.injection import render_entity_state
from core.entities.mutation import mutate
from core.entities.repo import save_schema
from core.entities.schema import EntitySchemaDefinition, FieldDef
from core.entities.storage import create_entity
from core.process.dsl.schema import ActorSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_PERMISSIONS = RolePermissionService()

_PHASE = PhaseSpec(
    label_key="turn",
    actors=[ActorSpec(persona_type="supervisor", mode="generate")],
    visibility=VisibilitySpec(
        knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
    ),
    budget=None,
)


async def test_entity_injection_replays_identically_from_pinned_versions(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"entity-replay-{uuid.uuid4().hex[:8]}"
    )
    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="viewer")
        session.add(viewer)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=viewer.id,
                role="facilitator",
            )
        )
        viewer_id = viewer.id

    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    session_id = sess.id

    definition = EntitySchemaDefinition(fields=[FieldDef(key="hit_points", type="integer")])
    schema_row = await save_schema(tenant_id, workspace_id, "replay-npc", 1, definition)
    entity = await create_entity(
        tenant_id,
        workspace_id,
        schema_row.id,
        definition,
        key=f"replay-npc-{uuid.uuid4().hex[:8]}",
        name="Replay NPC",
        scope_key="workspace_public",
        data={"hit_points": 12},
    )

    assemble_kwargs = dict(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_id,
        query_text="anything",
        query_embedding=[0.0] * 8,
        history_max_tokens=0,
        entity_state_renderer=render_entity_state,
    )

    # Turn 0: assemble, persist the manifest, then replay with the same recorded
    # inputs (nothing mutated in between) -- the INV-10 property itself.
    original = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]
    manifest_row = await write_context_manifest(
        tenant_id, session_id, 0, viewer_id, _PHASE.label_key, original
    )
    assert manifest_row.rendered_hash == original.content_hash
    assert manifest_row.entity_versions == {str(entity.id): 1}

    replayed = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]
    assert replayed.content_hash == manifest_row.rendered_hash
    assert replayed.entity_versions == original.entity_versions

    # Turn 1: mutate the entity (a real version bump through F3.5's mutation service),
    # then assemble+replay again -- the hash changes (real state changed) but each
    # turn's own manifest+immediate-replay still matches, and entity_versions reflects
    # the new version.
    await mutate(
        viewer_id,
        tenant_id,
        workspace_id,
        entity.id,
        {"hit_points": 3},
        "human",
        None,
        f"replay-mutate-{uuid.uuid4()}",
        permission_service=_PERMISSIONS,
    )

    turn_1 = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]
    manifest_row_1 = await write_context_manifest(
        tenant_id, session_id, 1, viewer_id, _PHASE.label_key, turn_1
    )
    assert manifest_row_1.entity_versions == {str(entity.id): 2}
    assert turn_1.content_hash != original.content_hash  # real state changed

    replayed_1 = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]
    assert replayed_1.content_hash == manifest_row_1.rendered_hash
