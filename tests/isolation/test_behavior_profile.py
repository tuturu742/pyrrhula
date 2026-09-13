"""E2.3's acceptance criteria: the stakes:high validation rule, behavior_profile's
append-only version history (T0.4 shape: isolation + the grant is asserted, not assumed),
and the ContextManifest.behavior_profile_version wiring's replay-relevant property --
a pinned version resolves to the value in effect *at that time*, not whatever the agent's
profile has since become.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import ContextManifest
from core.assembler.manifest import write_context_manifest
from core.behavior.fixtures import (
    INVALID_HIGH_STAKES_WITHOUT_GATE,
    RPG_AXIS_PACK,
    RPG_AXIS_PACK_ID,
    SECRET_DISCLOSURE_PROPENSITY,
)
from core.behavior.repo import (
    create_axis_definition,
    create_behavior_profile,
    get_behavior_profile_version,
    get_current_behavior_profile,
)
from core.behavior.validation import (
    AxisDefinitionSchema,
    AxisValidationError,
    validate_axis_definition,
)
from core.process.skeleton import create_session
from core.tenancy.scope import tenant_scope


async def _seed_rpg_axes(tenant_id: uuid.UUID) -> None:
    """Profiles are now VALIDATED against the pack's axis definitions (the sliders'
    write path refuses unknown keys / out-of-range values), so tests must load the
    axes first, exactly as a real pack load does."""
    for definition in RPG_AXIS_PACK:
        await create_axis_definition(tenant_id, AxisDefinitionSchema.model_validate(definition))


def test_high_stakes_axis_without_gate_binding_fails_validation() -> None:
    definition = AxisDefinitionSchema.model_validate(INVALID_HIGH_STAKES_WITHOUT_GATE)
    with pytest.raises(AxisValidationError, match="malice"):
        validate_axis_definition(definition)


def test_high_stakes_axis_with_gate_binding_passes_validation() -> None:
    definition = AxisDefinitionSchema.model_validate(SECRET_DISCLOSURE_PROPENSITY)
    validate_axis_definition(definition)  # must not raise


def test_unknown_binding_kind_is_rejected() -> None:
    """``kind`` isn't constrained by the pydantic schema itself (``extra="allow"`` on
    ``BindingSchema``) -- the taxonomy check is ``validate_axis_definition``'s job, so
    parsing succeeds and the rejection happens one layer up."""
    bad = {**SECRET_DISCLOSURE_PROPENSITY, "bindings": [{"kind": "mind_control"}]}
    definition = AxisDefinitionSchema.model_validate(bad)
    with pytest.raises(AxisValidationError, match="mind_control"):
        validate_axis_definition(definition)


async def test_axis_definitions_upsert_by_pack_and_key(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    for definition_dict in RPG_AXIS_PACK:
        await create_axis_definition(tenant_a, AxisDefinitionSchema.model_validate(definition_dict))

    async with tenant_scope(tenant_a) as session:
        rows = (
            await session.execute(
                text("SELECT key FROM axis_definition WHERE tenant_id = :t AND pack_id = :p"),
                {"t": tenant_a, "p": RPG_AXIS_PACK_ID},
            )
        ).all()
    assert {row[0] for row in rows} == {d["key"] for d in RPG_AXIS_PACK}


async def test_axis_definition_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        await create_axis_definition(
            tenant_id, AxisDefinitionSchema.model_validate(SECRET_DISCLOSURE_PROPENSITY)
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM axis_definition"))).all()
    assert {row[0] for row in rows} == {tenant_a}


def test_axis_labels_resolve_through_vocabulary_overlay() -> None:
    """No literal axis nouns appear in core: every fixture's label_key is an opaque
    ``namespace.key`` overlay pointer, never resolved English text -- resolution is the
    UI's job (``useLabel()``/``label()``), same as every other user-facing noun."""
    for definition_dict in RPG_AXIS_PACK:
        label_key = definition_dict["label_key"]
        assert isinstance(label_key, str)
        assert label_key.startswith("axis.")
        assert label_key == label_key.lower()

    # Round-trips unresolved: create_axis_definition never rewrites label_key.
    definition = AxisDefinitionSchema.model_validate(SECRET_DISCLOSURE_PROPENSITY)
    assert definition.label_key == "axis.secret_disclosure_propensity"


async def test_behavior_profile_versions_are_append_only(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    await _seed_rpg_axes(tenant_a)

    v1 = await create_behavior_profile(
        tenant_a, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 40}
    )
    v2 = await create_behavior_profile(
        tenant_a, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 70}
    )
    assert v1.version == 1
    assert v2.version == 2

    current = await get_current_behavior_profile(tenant_a, persona_id)
    assert current is not None
    assert current.version == 2
    assert current.axis_values == {"talkativeness": 70}

    # The old version is still readable, unaffected by the new one -- immutable history.
    pinned_v1 = await get_behavior_profile_version(tenant_a, persona_id, 1)
    assert pinned_v1 is not None
    assert pinned_v1.axis_values == {"talkativeness": 40}

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("UPDATE behavior_profile SET axis_values = '{}' WHERE id = :id"),
                {"id": v1.id},
            )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(tenant_a) as session:
            await session.execute(
                text("DELETE FROM behavior_profile WHERE id = :id"), {"id": v1.id}
            )


async def test_behavior_profile_filter_omission(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        async with tenant_scope(tenant_id) as session:
            workspace_id = (
                await session.execute(text("SELECT id FROM workspace LIMIT 1"))
            ).scalar_one()
        persona_id = await seed_dev_agent(tenant_id, workspace_id)
        await _seed_rpg_axes(tenant_id)
        await create_behavior_profile(
            tenant_id, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 50}
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM behavior_profile"))).all()
    assert {row[0] for row in rows} == {tenant_a}


async def test_manifest_records_behavior_profile_version_and_replays_identically(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    persona_id = await seed_dev_agent(tenant_a, workspace_id)
    await _seed_rpg_axes(tenant_a)
    sess = await create_session(tenant_a, workspace_id, persona_id)

    profile_v1 = await create_behavior_profile(
        tenant_a, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 40}
    )

    def _manifest(content_hash: str) -> ContextManifest:
        return ContextManifest(
            id=uuid.uuid4(),
            tenant_id=tenant_a,
            session_id=sess.id,
            rendered_context="x",
            stable_prefix="",
            volatile_suffix="x",
            entries=(),
            redactions=(),
            resolution_ids=(),
            token_counts={},
            content_hash=content_hash,
        )

    current = await get_current_behavior_profile(tenant_a, persona_id)
    assert current is not None
    row_1 = await write_context_manifest(
        tenant_a,
        sess.id,
        0,
        owner_id,
        "test_phase",
        _manifest("a" * 64),
        behavior_profile_version=current.version,
    )
    assert row_1.behavior_profile_version == profile_v1.version == 1

    # The agent is re-tuned to a new version *after* the manifest above was written.
    await create_behavior_profile(tenant_a, persona_id, RPG_AXIS_PACK_ID, {"talkativeness": 90})

    # Replaying with the version *pinned on the already-written manifest* -- not
    # whatever get_current_behavior_profile would return now -- reproduces the same
    # recorded version, the actual INV-10-relevant claim: a manifest is a historical
    # fact, immune to the agent's profile moving on afterward.
    pinned = await get_behavior_profile_version(
        tenant_a, persona_id, row_1.behavior_profile_version
    )
    assert pinned is not None
    assert pinned.version == 1
    assert pinned.axis_values == {"talkativeness": 40}

    row_2 = await write_context_manifest(
        tenant_a,
        sess.id,
        1,
        owner_id,
        "test_phase",
        _manifest("b" * 64),
        behavior_profile_version=pinned.version,
    )
    assert row_2.behavior_profile_version == row_1.behavior_profile_version
