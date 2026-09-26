"""The acceptance criteria: banded rendering is total and boundary-exact, no raw axis
number ever appears in a rendered directive, and the rendered directive block is stable
across turns for the same profile version (cache-prefix stability)."""

from __future__ import annotations

import uuid

import pytest

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import assemble
from core.behavior.directives import (
    BandSchema,
    DirectiveBindingError,
    render_directive,
    render_directives_for_profile,
    validate_bands,
)
from core.behavior.fixtures import CHATTINESS, RPG_AXIS_PACK_ID
from core.behavior.models import AxisDefinitionRow
from core.behavior.repo import create_axis_definition, create_behavior_profile
from core.behavior.validation import AxisDefinitionSchema
from core.knowledge.retrieval.tests.conftest import unit_vector
from core.process.dsl.schema import ActorSpec, BudgetSpec, PhaseSpec, VisibilitySpec
from core.process.skeleton import create_session, submit_user_message
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _chattiness_row() -> AxisDefinitionRow:
    """A plain, unsaved ORM instance -- band-rendering logic needs no DB round-trip;
    only the pack-load path (`create_axis_definition`) does."""
    definition = AxisDefinitionSchema.model_validate(CHATTINESS)
    return AxisDefinitionRow(
        pack_id=definition.pack_id,
        key=definition.key,
        label_key=definition.label_key,
        range_min=definition.range_min,
        range_max=definition.range_max,
        stakes=definition.stakes,
        semantics_md=definition.semantics_md,
        bindings=[b.model_dump() for b in definition.bindings],
    )


def test_band_rendering_is_total_and_boundary_exact() -> None:
    axis = _chattiness_row()
    # Boundary-exact: 33 is the top of the first band, 34 the bottom of the second --
    # no value in [0, 100] maps to nothing (total), and edges land in the band that
    # actually declares them, not its neighbour.
    assert (
        render_directive(axis, 0)
        == "You speak rarely, only when you have something substantive to add."
    )
    assert (
        render_directive(axis, 33)
        == "You speak rarely, only when you have something substantive to add."
    )
    assert render_directive(axis, 34) == "You speak about as often as anyone else at the table."
    assert render_directive(axis, 66) == "You speak about as often as anyone else at the table."
    assert (
        render_directive(axis, 67)
        == "You volunteer dialogue and narration freely, often unprompted."
    )
    assert (
        render_directive(axis, 100)
        == "You volunteer dialogue and narration freely, often unprompted."
    )


def test_band_validation_rejects_gaps_and_overlaps() -> None:
    gap = [BandSchema(min=0, max=20, text="a"), BandSchema(min=22, max=100, text="b")]
    with pytest.raises(DirectiveBindingError, match="gap"):
        validate_bands("chattiness", 0, 100, gap)

    overlap = [BandSchema(min=0, max=50, text="a"), BandSchema(min=40, max=100, text="b")]
    with pytest.raises(DirectiveBindingError, match="overlap"):
        validate_bands("chattiness", 0, 100, overlap)

    short_of_range_max = [BandSchema(min=0, max=90, text="a")]
    with pytest.raises(DirectiveBindingError, match="range_max"):
        validate_bands("chattiness", 0, 100, short_of_range_max)


def test_rendered_directive_contains_no_raw_axis_number() -> None:
    axis = _chattiness_row()
    for value in (0, 20, 33, 50, 73, 100):
        rendered = render_directive(axis, value)
        assert rendered is not None
        assert str(value) not in rendered


async def test_directive_bytes_stable_across_turns_for_same_profile_version(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"directive-stable-{uuid.uuid4().hex[:8]}"
    )
    axis_row = await create_axis_definition(
        tenant_id, AxisDefinitionSchema.model_validate(CHATTINESS)
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    profile = await create_behavior_profile(
        tenant_id, persona_id, RPG_AXIS_PACK_ID, {"chattiness": 10}
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
                role="participant",
            )
        )
    session_row = await create_session(tenant_id, workspace_id, persona_id)

    directives_text = render_directives_for_profile([axis_row], profile.axis_values)
    assert directives_text == "You speak rarely, only when you have something substantive to add."

    phase = PhaseSpec(
        label_key="test_phase",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
        ),
        budget=BudgetSpec(ratio={}, max_tokens=100),
    )

    first = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_row.id,
        query_text="first turn",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        behavior_directives_text=directives_text,
    )

    # An unrelated *volatile* part of context changes between turns: a new message
    # enters conversation history, which only ever lands in LayoutSections.volatile.
    await submit_user_message(tenant_id, session_row.id, viewer.id, "a brand new turn happened")

    second = await assemble(
        viewer,
        phase,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=session_row.id,
        query_text="second turn, totally different history state",
        query_embedding=unit_vector(0),
        history_max_tokens=100,
        behavior_directives_text=directives_text,
    )

    assert first.stable_prefix == second.stable_prefix
    assert directives_text in first.stable_prefix
    assert directives_text in second.stable_prefix
