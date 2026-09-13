"""B1.3 acceptance criteria for the turn scheduler, against a live Postgres: table-driven
per-mode behavior (declared/initiative/free), initiative ties, mid-phase actor removal,
and cursor persistence surviving kill/resume mid-rotation.
"""

from __future__ import annotations

import uuid

from core.agents.authoring import get_persona
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.schema import ActorSpec, PhaseSpec, ProcessDefinitionDSL, VisibilitySpec
from core.process.interpreter import (
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    start_session,
)
from core.process.scheduler import (
    Candidate,
    make_default_candidate_resolver,
    make_scheduler,
)
from core.process.skeleton import create_session
from core.tenancy.models import Membership, Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_VISIBILITY = VisibilitySpec(
    knowledge_classes=["rules"],
    scopes=["workspace_public"],
    entity_fields="all",
    secrets="none",
)


def _phase(actors: list[ActorSpec]) -> PhaseSpec:
    return PhaseSpec(label_key="phase.test", actors=actors, visibility=_VISIBILITY)


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, sess.id


def _ctx(tenant_id: uuid.UUID, session_id: uuid.UUID, phase: PhaseSpec) -> InterpreterContext:
    return InterpreterContext(tenant_id, session_id, "test_phase", phase, {})


def _static_resolver(candidates: list[Candidate]):  # noqa: ANN201
    async def resolve(_spec: ActorSpec, _ctx: InterpreterContext) -> list[Candidate]:
        return candidates

    return resolve


# ── declared order ──────────────────────────────────────────────────────────────────


async def test_declared_order_gives_turns_in_list_order(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-declared")
    p1, p2, p3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = [Candidate(p1), Candidate(p2), Candidate(p3)]
    spec = ActorSpec(persona_type="participant", mode="generate", order="declared", max_turns=3)
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver(candidates))

    seen = []
    for _ in range(4):
        actor = await scheduler(_ctx(tenant_id, session_id, phase))
        seen.append(actor.principal_id if actor else None)

    assert seen == [p1, p2, p3, None]


async def test_declared_order_respects_max_turns_cap(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-declared-cap")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    spec = ActorSpec(persona_type="participant", mode="generate", order="declared", max_turns=1)
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver([Candidate(p1), Candidate(p2)]))

    first = await scheduler(_ctx(tenant_id, session_id, phase))
    second = await scheduler(_ctx(tenant_id, session_id, phase))

    assert first is not None and first.principal_id == p1
    assert second is None


# ── initiative order ─────────────────────────────────────────────────────────────────


async def test_initiative_order_sorts_descending(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-initiative")
    low, high, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = [
        Candidate(low, initiative=5.0),
        Candidate(high, initiative=20.0),
        Candidate(mid, initiative=12.0),
    ]
    spec = ActorSpec(
        order="initiative", from_field='entity_field("initiative")', mode="generate", max_turns=3
    )
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver(candidates))

    order = []
    for _ in range(3):
        actor = await scheduler(_ctx(tenant_id, session_id, phase))
        order.append(actor.principal_id)

    assert order == [high, mid, low]


async def test_initiative_ties_break_by_declared_order(db_available: None) -> None:
    """Stable tiebreak: two candidates with equal initiative keep the order they were
    returned in by the resolver (their "declared" position), not an arbitrary one."""
    tenant_id, _workspace_id, session_id = await _setup("sched-initiative-tie")
    first_declared, second_declared = uuid.uuid4(), uuid.uuid4()
    candidates = [
        Candidate(first_declared, initiative=10.0),
        Candidate(second_declared, initiative=10.0),
    ]
    spec = ActorSpec(
        order="initiative", from_field='entity_field("initiative")', mode="generate", max_turns=2
    )
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver(candidates))

    order = []
    for _ in range(2):
        actor = await scheduler(_ctx(tenant_id, session_id, phase))
        order.append(actor.principal_id)

    assert order == [first_declared, second_declared]


# ── free order ───────────────────────────────────────────────────────────────────────


async def test_free_order_picks_deterministically_among_eligible(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-free")
    p1, p2 = sorted([uuid.uuid4(), uuid.uuid4()])
    spec = ActorSpec(any_of=["human_participant"], mode="free", order="free", max_turns=2)
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver([Candidate(p1), Candidate(p2)]))

    first = await scheduler(_ctx(tenant_id, session_id, phase))
    assert first is not None and first.principal_id == p1  # sorted-by-id, deterministic


async def test_free_order_respects_max_turns_cap(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-free-cap")
    p1 = uuid.uuid4()
    spec = ActorSpec(any_of=["human_participant"], mode="free", order="free", max_turns=2)
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver([Candidate(p1)]))

    results = [await scheduler(_ctx(tenant_id, session_id, phase)) for _ in range(3)]

    assert [r is not None for r in results] == [True, True, False]


async def test_no_eligible_candidates_returns_none(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-empty")
    spec = ActorSpec(any_of=["human_participant"], mode="free", order="free", max_turns=5)
    phase = _phase([spec])
    scheduler = make_scheduler(_static_resolver([]))

    actor = await scheduler(_ctx(tenant_id, session_id, phase))

    assert actor is None


# ── mid-phase actor removal ─────────────────────────────────────────────────────────


async def test_mid_phase_removal_is_skipped_without_a_turn(db_available: None) -> None:
    """A candidate present in the resolved declared order but absent from a later fresh
    resolve (removed from the eligible pool mid-phase) is skipped, not given a turn and
    not counted against another candidate's turn."""
    tenant_id, _workspace_id, session_id = await _setup("sched-removal")
    p1, p2, p3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    phase = _phase(
        [ActorSpec(persona_type="participant", mode="generate", order="declared", max_turns=3)]
    )

    call_count = 0
    full_pool = [Candidate(p1), Candidate(p2), Candidate(p3)]

    async def resolver(_spec: ActorSpec, _ctx: InterpreterContext) -> list[Candidate]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return full_pool  # resolved_order is fixed from this first call: [p1, p2, p3]
        return [c for c in full_pool if c.principal_id != p2]  # p2 removed thereafter

    scheduler = make_scheduler(resolver)

    seen = []
    for _ in range(4):
        actor = await scheduler(_ctx(tenant_id, session_id, phase))
        seen.append(actor.principal_id if actor else None)

    assert seen == [p1, p3, None, None]


# ── cursor survives kill/resume mid-rotation ────────────────────────────────────────


async def test_cursor_survives_simulated_kill_and_resume_mid_rotation(db_available: None) -> None:
    """A fresh `make_scheduler(...)` closure (simulating a new process after a crash) reads
    the SAME persisted session.actor_cursor and continues exactly where the old one left
    off -- no skipped turn, no repeated turn."""
    tenant_id, _workspace_id, session_id = await _setup("sched-resume")
    p1, p2, p3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    candidates = [Candidate(p1), Candidate(p2), Candidate(p3)]
    spec = ActorSpec(persona_type="participant", mode="generate", order="declared", max_turns=3)
    phase = _phase([spec])

    scheduler_before_crash = make_scheduler(_static_resolver(candidates))
    first = await scheduler_before_crash(_ctx(tenant_id, session_id, phase))
    assert first is not None and first.principal_id == p1

    # "Crash": a brand-new closure, no in-memory state carried over -- only the DB cursor.
    scheduler_after_resume = make_scheduler(_static_resolver(candidates))
    second = await scheduler_after_resume(_ctx(tenant_id, session_id, phase))
    third = await scheduler_after_resume(_ctx(tenant_id, session_id, phase))
    fourth = await scheduler_after_resume(_ctx(tenant_id, session_id, phase))

    assert second is not None and second.principal_id == p2
    assert third is not None and third.principal_id == p3
    assert fourth is None


async def test_cursor_resets_on_entering_a_different_phase(db_available: None) -> None:
    tenant_id, _workspace_id, session_id = await _setup("sched-phase-reset")
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    spec = ActorSpec(persona_type="participant", mode="generate", order="declared", max_turns=2)
    phase_a = PhaseSpec(label_key="a", actors=[spec], visibility=_VISIBILITY)
    phase_b = PhaseSpec(label_key="b", actors=[spec], visibility=_VISIBILITY)
    scheduler = make_scheduler(_static_resolver([Candidate(p1), Candidate(p2)]))

    ctx_a = InterpreterContext(tenant_id, session_id, "phase_a", phase_a, {})
    ctx_b = InterpreterContext(tenant_id, session_id, "phase_b", phase_b, {})

    first_in_a = await scheduler(ctx_a)
    first_in_b = await scheduler(ctx_b)  # new phase -- cursor resets, starts from p1 again

    assert first_in_a is not None and first_in_a.principal_id == p1
    assert first_in_b is not None and first_in_b.principal_id == p1


# ── make_default_candidate_resolver: real workspace_membership, injected agents ────


async def test_default_resolver_resolves_human_participants_from_real_membership(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("sched-default-human")
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name="Extra")
        session.add(principal)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, principal_id=principal.id, role="participant"))
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal.id,
                role="participant",
            )
        )
        extra_principal_id = principal.id

    resolver = make_default_candidate_resolver(workspace_id)
    spec = ActorSpec(human_participant="all", mode="free")
    phase = _phase([spec])
    candidates = await resolver(spec, _ctx(tenant_id, session_id, phase))

    assert extra_principal_id in {c.principal_id for c in candidates}


async def test_default_resolver_delegates_agent_tokens_to_injected_resolver(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("sched-default-agent")
    agent_principal_id = uuid.uuid4()
    injected_calls = 0

    async def agent_resolver(_spec: ActorSpec, _ctx: InterpreterContext) -> list[Candidate]:
        nonlocal injected_calls
        injected_calls += 1
        return [Candidate(agent_principal_id)]

    resolver = make_default_candidate_resolver(
        workspace_id, persona_candidate_resolver=agent_resolver
    )
    spec = ActorSpec(persona_type="supervisor", mode="generate")
    phase = _phase([spec])
    candidates = await resolver(spec, _ctx(tenant_id, session_id, phase))

    assert injected_calls == 1
    assert [c.principal_id for c in candidates] == [agent_principal_id]


async def test_default_resolver_returns_empty_for_agent_tokens_without_injected_resolver(
    db_available: None,
) -> None:
    tenant_id, workspace_id, session_id = await _setup("sched-default-noagent")
    resolver = make_default_candidate_resolver(workspace_id)
    spec = ActorSpec(persona_type="supervisor", mode="generate")
    phase = _phase([spec])

    candidates = await resolver(spec, _ctx(tenant_id, session_id, phase))

    assert candidates == []


async def test_default_resolver_with_real_agent_resolver_mixes_human_and_agent_any_of(
    db_available: None,
) -> None:
    """B1.8: a mixed any_of list (['human_participant', 'participant_agent']) exercised
    through the *real* persona_candidate_resolver (core.agents.scheduling), not a stub --
    both a real workspace member and a real agent must show up together, since B1.3 and
    B1.8's own resolvers were previously only ever tested independently."""
    tenant_id, workspace_id, session_id = await _setup("sched-mixed-anyof")
    participant_agent_id = await seed_dev_agent(
        tenant_id, workspace_id, key="participant-agent", persona_type="participant"
    )
    participant_agent = await get_persona(tenant_id, participant_agent_id)
    assert participant_agent is not None

    async with tenant_scope(tenant_id) as session:
        human_principal = Principal(tenant_id=tenant_id, kind="human", display_name="Human")
        session.add(human_principal)
        await session.flush()
        session.add(
            Membership(tenant_id=tenant_id, principal_id=human_principal.id, role="participant")
        )
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=human_principal.id,
                role="participant",
            )
        )
        human_principal_id = human_principal.id

    resolver = make_default_candidate_resolver(
        workspace_id,
        persona_candidate_resolver=make_persona_candidate_resolver(tenant_id, workspace_id),
    )
    spec = ActorSpec(any_of=["human_participant", "participant_agent"], mode="free")
    phase = _phase([spec])
    candidates = await resolver(spec, _ctx(tenant_id, session_id, phase))

    assert {c.principal_id for c in candidates} == {
        human_principal_id,
        participant_agent.principal_id,
    }


# ── composes with B1.2's interpreter as designed ────────────────────────────────────


async def test_scheduler_plugs_directly_into_advance_session_as_next_actor_fn(
    db_available: None,
) -> None:
    """make_scheduler(...) is a real NextActorFn (B1.2's exact injection point) -- proves
    the two modules compose, not just that each independently passes its own tests."""
    tenant_id, _workspace_id, session_id = await _setup("sched-interp-compose")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)
    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    facilitator_principal_id = uuid.uuid4()
    scheduler = make_scheduler(_static_resolver([Candidate(facilitator_principal_id)]))

    async def execute_turn(actor, ctx: InterpreterContext) -> ActorTurnResult:  # noqa: ANN001
        return ActorTurnResult(content_md=f"{actor.principal_id} spoke in {ctx.phase_key}")

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=execute_turn,
        max_steps=1,
    )

    assert result.status == "active"
    assert result.steps_taken == 1
