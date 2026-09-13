"""G4.1 acceptance criteria for the resume path itself: the history budget is respected
end to end and the resumed turn replays from its manifest (INV-10 across a resume), and a
definition edited while the session slept does not silently apply.
"""

from __future__ import annotations

import copy
import uuid

from core.agents.seed import seed_dev_agent
from core.assembler.context_assembler import HistorySummaryBlock, assemble, token_proxy
from core.assembler.manifest import write_context_manifest
from core.assembler.visibility import seed_default_scopes
from core.process.authoring import create_definition
from core.process.checkpoints import (
    restore_session_from_checkpoint,
    upgrade_session_definition,
    write_checkpoint,
)
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.schema import (
    ActorSpec,
    BudgetSpec,
    PhaseSpec,
    ProcessDefinitionDSL,
    VisibilitySpec,
)
from core.process.interpreter import start_session
from core.process.skeleton import create_session
from core.sessions.models import MessageRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_HISTORY_RATIO = 0.5
_MAX_TOKENS = 400
_SLICE = int(_MAX_TOKENS * _HISTORY_RATIO)

_PHASE = PhaseSpec(
    label_key="turn",
    actors=[ActorSpec(persona_type="supervisor", mode="generate")],
    visibility=VisibilitySpec(
        knowledge_classes=[], scopes=["workspace_public"], entity_fields="all", secrets="none"
    ),
    budget=BudgetSpec(ratio={}, max_tokens=_MAX_TOKENS, history_ratio=_HISTORY_RATIO),
)


async def test_resumed_turn_stays_in_history_budget_and_replays(db_available: None) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"resume-budget-{uuid.uuid4().hex[:8]}"
    )
    await seed_default_scopes(tenant_id, workspace_id)
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    async with tenant_scope(tenant_id) as session:
        viewer = Principal(tenant_id=tenant_id, kind="human", display_name="resumer")
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
        # A long elapsed transcript: far more than the history slice could ever hold
        # verbatim, which is the entire reason a summary exists.
        for seq in range(40):
            session.add(
                MessageRow(
                    tenant_id=tenant_id,
                    session_id=sess.id,
                    event_seq=seq,
                    author_principal_id=viewer_id,
                    role="user",
                    content_md=f"elapsed turn {seq} " + "filler " * 20,
                )
            )

    # A summary sized to the slice, standing in for events 0..39; the assembler must place
    # it *and* keep the whole history section (summary + any post-summary raw messages)
    # inside the declared slice.
    summary_text = '<history_summary from="0" to="39">\n' + ("word " * 60) + "\n</history_summary>"
    block = HistorySummaryBlock(
        rendered_text=summary_text,
        token_count=token_proxy(summary_text),
        from_event_seq=0,
        to_event_seq=39,
        content_hash="a" * 64,
    )
    assert block.token_count <= _SLICE

    assemble_kwargs = dict(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        session_id=sess.id,
        query_text="what now",
        query_embedding=[0.0] * 8,
        history_max_tokens=_PHASE.history_slice_tokens(),
        history_summary=block,
    )

    manifest = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]

    assert _PHASE.history_slice_tokens() == _SLICE
    assert manifest.token_counts["history"] <= _SLICE, (
        "the resumed turn's history section overran the phase's declared history slice"
    )
    assert summary_text in manifest.rendered_context
    # The summary covers events 0..39, so no message in that range is *also* included
    # verbatim -- double-charging the same history against the budget is the bug this
    # guards.
    assert "elapsed turn 39" not in manifest.rendered_context

    manifest_row = await write_context_manifest(
        tenant_id, sess.id, 40, viewer_id, _PHASE.label_key, manifest
    )
    assert manifest_row.history_summary_from_seq == 0
    assert manifest_row.history_summary_to_seq == 39
    assert manifest_row.history_summary_hash == block.content_hash

    replayed = await assemble(viewer, _PHASE, **assemble_kwargs)  # type: ignore[arg-type]
    assert replayed.content_hash == manifest_row.rendered_hash


async def test_resume_uses_the_pinned_definition_version_not_the_latest(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"resume-pin-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    v1_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    v1 = ProcessDefinitionDSL.model_validate(v1_row.definition)
    await start_session(tenant_id, sess.id, v1, v1_row.id, v1_row.version)
    await write_checkpoint(tenant_id, sess.id, workspace_id)

    # The session sleeps; someone publishes a second version of the same definition key.
    edited = copy.deepcopy(MINIMAL_MVP_FLOW)
    edited["name"] = "MVP (edited while the session slept)"
    v2_row = await create_definition(tenant_id, "mvp", "MVP v2", edited)
    assert v2_row.version == v1_row.version + 1

    restored = await restore_session_from_checkpoint(tenant_id, sess.id)
    assert restored.process_definition_id == v1_row.id
    assert restored.process_definition_version == v1_row.version
    assert restored.definition is not None
    assert restored.definition.name == v1.name, (
        "resume picked up a definition published after the checkpoint -- the pin did not hold"
    )
    assert restored.phase == v1.initial_phase

    # Upgrading is available, but only as an explicit act.
    upgraded = await upgrade_session_definition(tenant_id, sess.id, v2_row.id)
    assert upgraded.process_definition_version == v2_row.version
    assert upgraded.definition is not None
    assert upgraded.definition.name == "MVP (edited while the session slept)"


async def test_restore_carries_checkpoint_pins_without_rewinding_live_state(
    db_available: None,
) -> None:
    """The pins a resumed turn reads *against* come from the checkpoint; live entity rows
    are never rolled back to match it (that operation is ``fork_session``, deliberately a
    different verb). Guards the one line of ``restore_session_from_checkpoint`` where a
    well-meaning future edit would turn a restore into data loss."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"resume-pins-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)

    v1_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    v1 = ProcessDefinitionDSL.model_validate(v1_row.definition)
    await start_session(tenant_id, sess.id, v1, v1_row.id, v1_row.version)
    checkpoint = await write_checkpoint(tenant_id, sess.id, workspace_id)

    restored = await restore_session_from_checkpoint(tenant_id, sess.id)
    assert restored.entity_versions == checkpoint.entity_versions
    assert restored.knowledge_version_pins == checkpoint.knowledge_version_pins
    assert restored.resumed_from_event_seq == checkpoint.event_seq
    assert restored.actor_cursor == checkpoint.actor_cursor

    del persona_id
