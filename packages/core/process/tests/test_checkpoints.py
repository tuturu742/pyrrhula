"""B1.4 acceptance criteria for checkpoints, against a live Postgres: checkpoint-per-
transition, state reconstruction from checkpoint+tail across 100 randomised turns, and
fork isolation. The append-only grant test lives in
``tests/isolation/test_append_only_grants.py`` (registered there, not duplicated here).
"""

from __future__ import annotations

import random
import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.knowledge.authoring import (
    EntryFields,
    attach_source_to_workspace,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.process.authoring import create_definition
from core.process.checkpoints import fork_session, make_checkpoint_hook, reconstruct_state
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    start_session,
)
from core.process.scheduler import Candidate, make_scheduler
from core.process.skeleton import create_session
from core.sessions.models import CheckpointRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id


def _single_candidate_scheduler(principal_id: uuid.UUID):  # noqa: ANN201
    async def resolver(_spec, _ctx: InterpreterContext) -> list[Candidate]:  # noqa: ANN001
        return [Candidate(principal_id)]

    return make_scheduler(resolver)


async def _random_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    return ActorTurnResult(content_md=f"{ctx.phase_key}-{random.randint(0, 1_000_000)}")


async def _get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row


# ── checkpoint written at every phase transition ────────────────────────────────────


async def test_checkpoint_written_at_every_phase_transition(db_available: None) -> None:
    tenant_id, workspace_id, session_id = await _setup("ckpt-per-transition")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    principal_id = uuid.uuid4()
    scheduler = _single_candidate_scheduler(principal_id)
    checkpoint_hook = make_checkpoint_hook(workspace_id)

    # 6 steps = 3 turns + 3 transitions (one full lap: arbiter_narrate -> player_act ->
    # resolve -> arbiter_narrate).
    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=6,
    )

    async with tenant_scope(tenant_id) as session:
        checkpoints = (
            (
                await session.execute(
                    select(CheckpointRow)
                    .where(CheckpointRow.session_id == session_id)
                    .order_by(CheckpointRow.event_seq)
                )
            )
            .scalars()
            .all()
        )

    # 3 transitions in 6 steps -> exactly 3 checkpoints, not one per turn.
    assert len(checkpoints) == 3
    assert [c.phase for c in checkpoints] == ["player_act", "resolve", "arbiter_narrate"]
    assert checkpoints[-1].state == {"round": 1}


async def test_checkpoint_has_real_knowledge_version_pins_and_empty_entity_versions(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("ckpt-pins")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    await upsert_draft_entry(
        tenant_id,
        source.id,
        "entry-a",
        EntryFields(title="E", body_md="b", class_="rules", scope_key="workspace_public"),
    )
    version = await publish_version(tenant_id, source.id)
    await attach_source_to_workspace(
        tenant_id, workspace_id, source.id, scope_key="workspace_public"
    )

    principal_id = uuid.uuid4()
    scheduler = _single_candidate_scheduler(principal_id)
    checkpoint_hook = make_checkpoint_hook(workspace_id)

    # max_steps=2: one actor turn (no checkpoint -- only transitions write one), then
    # one transition (writes the checkpoint this test inspects).
    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=2,
    )

    async with tenant_scope(tenant_id) as session:
        checkpoint = await session.scalar(
            select(CheckpointRow).where(CheckpointRow.session_id == session_id).limit(1)
        )
    assert checkpoint is not None
    assert checkpoint.knowledge_version_pins == {str(source.id): str(version.id)}
    assert checkpoint.entity_versions == {}


# ── resume: reconstruction from checkpoint + tail (100 randomised turns) ───────────


async def test_state_reconstructed_from_checkpoint_and_tail_matches_live_state_across_100_turns(
    db_available: None,
) -> None:
    random.seed(20260718)  # deterministic test, not a flaky one
    tenant_id, workspace_id, session_id = await _setup("ckpt-reconstruct")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    principal_id = uuid.uuid4()
    scheduler = _single_candidate_scheduler(principal_id)
    checkpoint_hook = make_checkpoint_hook(workspace_id)

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=100,
    )

    live = await _get_session(tenant_id, session_id)
    reconstructed_phase, reconstructed_state = await reconstruct_state(tenant_id, session_id)

    assert reconstructed_phase == live.current_phase
    assert reconstructed_state == live.state
    # Sanity: 100 steps of a 2-step-per-lap-turn, 1-transition cycle produced real,
    # non-trivial state -- this isn't accidentally passing because nothing happened.
    assert live.state["round"] > 0


async def test_reconstruct_state_raises_for_a_session_with_no_checkpoint_yet(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, session_id = await _setup("ckpt-none")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    try:
        await reconstruct_state(tenant_id, session_id)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "no checkpoint" in str(exc)


# ── fork: parent continues, child diverges, no shared mutable state ────────────────


async def test_fork_at_a_checkpoint_diverges_without_touching_the_parent(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("ckpt-fork")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    principal_id = uuid.uuid4()
    scheduler = _single_candidate_scheduler(principal_id)
    checkpoint_hook = make_checkpoint_hook(workspace_id)

    # One full lap -> round becomes 1, three checkpoints recorded.
    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=6,
    )

    async with tenant_scope(tenant_id) as session:
        fork_point = await session.scalar(
            select(CheckpointRow)
            .where(CheckpointRow.session_id == session_id)
            .order_by(CheckpointRow.event_seq)
            .limit(1)
        )
    assert fork_point is not None
    assert fork_point.phase == "player_act"
    assert fork_point.state == {"round": 0}

    child = await fork_session(tenant_id, fork_point.id)

    assert child.id != session_id
    assert child.current_phase == "player_act"
    assert child.state == {"round": 0}
    assert child.forked_from_checkpoint_id == fork_point.id

    # Parent continues independently: give it more turns; the child must not move.
    parent_scheduler = _single_candidate_scheduler(principal_id)
    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=parent_scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=6,
    )
    parent_after = await _get_session(tenant_id, session_id)
    child_after = await _get_session(tenant_id, child.id)

    assert parent_after.state != child_after.state  # parent moved on (round advanced further)
    assert child_after.state == {"round": 0}  # child untouched by the parent's continuation

    # Child diverges independently too, without affecting the parent.
    child_scheduler = _single_candidate_scheduler(principal_id)
    await advance_session(
        tenant_id,
        child.id,
        definition,
        next_actor_fn=child_scheduler,
        execute_turn=_random_execute_turn,
        checkpoint_hook=checkpoint_hook,
        max_steps=2,
    )
    parent_final = await _get_session(tenant_id, session_id)
    child_final = await _get_session(tenant_id, child.id)

    assert parent_final.state == parent_after.state  # untouched by the child's own advance
    # child: player_act (1 turn, max_turns=1) -> transition -> resolve, in 2 steps.
    assert child_final.current_phase == "resolve"
