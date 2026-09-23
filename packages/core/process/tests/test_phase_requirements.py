"""A phase may declare what it has to produce before it is allowed to move on.

A phase ends when its actor entries are used up, which says the turns were spent and
nothing about whether the work happened. Observed on a live eight-beat run: one long,
genuinely good encounter ran on while four beats advanced underneath it on turn budget
alone. The beat meant to stage a different opponent was spent on more rounds of the
previous one, the beat meant to pose a problem never posed one, and nothing anywhere
reported a thing -- every phase had done exactly what the flow asked of it, which was to
hand out turns.

``requires`` is counts over what the phase itself recorded, never a predicate over the
world: countable, total, and the same on a replay.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.process.dsl.schema import (
    ActorSpec,
    PhaseCompletionSpec,
    PhaseSpec,
    ProcessDefinitionDSL,
    VisibilitySpec,
)
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    measure_phase,
    start_session,
    unmet_requirements,
)
from core.process.scheduler import Candidate, make_scheduler
from core.process.skeleton import create_session
from core.sessions.models import SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _visibility() -> VisibilitySpec:
    return VisibilitySpec(
        scopes=["workspace_public"],
        knowledge_classes=["misc"],
        entity_fields="all",
        secrets="none",
    )


def _definition(requires: PhaseCompletionSpec | None) -> ProcessDefinitionDSL:
    """Two phases: the first declares the requirement, the second is where it lands."""
    return ProcessDefinitionDSL(
        name="requirement-flow",
        vocabulary_overlay="rpg_v1",
        initial_phase="work",
        phases={
            "work": PhaseSpec(
                label_key="phase.work",
                actors=[ActorSpec(persona_type="participant", mode="generate")],
                visibility=_visibility(),
                requires=requires,
                on_complete="after",
            ),
            "after": PhaseSpec(
                label_key="phase.after",
                actors=[ActorSpec(persona_type="participant", mode="generate")],
                visibility=_visibility(),
            ),
        },
    )


async def _session_on(definition: ProcessDefinitionDSL, slug: str):  # noqa: ANN201
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    from core.process.authoring import create_definition

    row = await create_definition(tenant_id, "req", "Req", definition.model_dump(by_alias=True))
    await start_session(tenant_id, sess.id, definition, row.id, row.version)
    return tenant_id, sess.id


async def _phase_events(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[dict]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.kind == "phase_requirement",
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
        return [dict(r.payload) for r in rows]


async def _current_phase(tenant_id: uuid.UUID, session_id: uuid.UUID) -> str:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row.current_phase


def _scheduler(principal_id: uuid.UUID):  # noqa: ANN201
    async def resolver(_spec, _ctx: InterpreterContext) -> list[Candidate]:  # noqa: ANN001
        return [Candidate(principal_id)]

    return make_scheduler(resolver)


async def _talk(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    return ActorTurnResult(content_md=f"a turn in {ctx.phase_key}")


# ── the shape of the decision, without a database ──────────────────────────────────


def test_a_phase_with_no_requirement_declares_nothing() -> None:
    assert PhaseCompletionSpec().is_declared() is False
    assert PhaseCompletionSpec(resolutions=1).is_declared() is True


def test_unmet_reports_both_numbers() -> None:
    """The shortfall has to name what was wanted and what arrived; 'not finished' with no
    figures is the report that sends somebody to read the transcript by hand."""
    spec = PhaseCompletionSpec(resolutions=2, messages=1)
    unmet = unmet_requirements(spec, {"resolutions": 0, "messages": 3, "tool_calls": 0})
    assert unmet == {"resolutions": {"required": 2, "produced": 0}}


def test_a_requirement_of_zero_is_not_a_requirement() -> None:
    assert unmet_requirements(PhaseCompletionSpec(), {"resolutions": 0, "messages": 0}) == {}


# ── against a live session ─────────────────────────────────────────────────────────


async def test_a_phase_that_met_its_requirement_moves_on(db_available: None) -> None:
    definition = _definition(PhaseCompletionSpec(messages=1))
    tenant_id, session_id = await _session_on(definition, "req-met")

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_scheduler(uuid.uuid4()),
        execute_turn=_talk,
        max_steps=6,
    )

    events = await _phase_events(tenant_id, session_id)
    assert events, "the decision must be recorded even when it is a pass"
    assert events[0]["decision"] == "met"
    assert await _current_phase(tenant_id, session_id) == "after"


async def test_an_unmet_phase_runs_its_actors_again_before_giving_up(
    db_available: None,
) -> None:
    """One resolution is required and the turns produce none, so the phase repeats up to
    its bound and then moves on: a beat nobody can satisfy must not trap the session."""
    definition = _definition(PhaseCompletionSpec(resolutions=1, on_unmet="repeat", max_repeats=2))
    tenant_id, session_id = await _session_on(definition, "req-repeat")

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_scheduler(uuid.uuid4()),
        execute_turn=_talk,
        max_steps=30,
    )

    events = await _phase_events(tenant_id, session_id)
    decisions = [e["decision"] for e in events]
    assert decisions.count("repeat") == 2, decisions
    assert decisions[-1] == "passed_unmet", decisions
    # The shortfall is named in the record, with both numbers.
    assert events[-1]["unmet"]["resolutions"] == {"required": 1, "produced": 0}
    assert await _current_phase(tenant_id, session_id) == "after"


async def test_warn_never_repeats(db_available: None) -> None:
    definition = _definition(PhaseCompletionSpec(resolutions=1, on_unmet="warn"))
    tenant_id, session_id = await _session_on(definition, "req-warn")

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_scheduler(uuid.uuid4()),
        execute_turn=_talk,
        max_steps=10,
    )

    decisions = [e["decision"] for e in await _phase_events(tenant_id, session_id)]
    assert decisions == ["passed_unmet"]
    assert await _current_phase(tenant_id, session_id) == "after"


async def test_hold_parks_the_session_for_a_person(db_available: None) -> None:
    definition = _definition(PhaseCompletionSpec(resolutions=1, on_unmet="hold", max_repeats=0))
    tenant_id, session_id = await _session_on(definition, "req-hold")

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_scheduler(uuid.uuid4()),
        execute_turn=_talk,
        max_steps=10,
    )

    assert result.status == "awaiting"
    assert await _current_phase(tenant_id, session_id) == "work", "it must not have moved on"
    assert [e["decision"] for e in await _phase_events(tenant_id, session_id)] == ["hold"]


async def test_measure_counts_only_what_this_phase_produced(db_available: None) -> None:
    """The floor is the phase's own entry event, so a turn taken in an earlier beat
    cannot satisfy a later one -- which is the whole failure being fixed."""
    definition = _definition(PhaseCompletionSpec(messages=1))
    tenant_id, session_id = await _session_on(definition, "req-floor")

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=_scheduler(uuid.uuid4()),
        execute_turn=_talk,
        max_steps=6,
    )

    # Now in "after": the messages from "work" are behind the phase entry and must not
    # be counted towards it.
    produced = await measure_phase(tenant_id, session_id)
    assert produced["messages"] <= 1, produced


# ── caught at authoring time, not at run time ──────────────────────────────────────


def _one_phase(requires: PhaseCompletionSpec | None, mode: str = "generate"):  # noqa: ANN202
    return ProcessDefinitionDSL(
        name="t",
        vocabulary_overlay="rpg_v1",
        initial_phase="p",
        phases={
            "p": PhaseSpec(
                label_key="x",
                actors=[ActorSpec(persona_type="participant", mode=mode)],
                visibility=_visibility(),
                requires=requires,
            )
        },
    )


def test_a_block_that_requires_nothing_is_a_validation_error() -> None:
    """It reads as a rule to whoever maintains the flow and enforces nothing, which is
    worse than its absence."""
    from core.process.dsl.validator import validate_definition

    issues = validate_definition(_one_phase(PhaseCompletionSpec()))
    assert [i.field_path for i in issues] == ["phases.p.requires"]


def test_requiring_output_from_a_phase_that_cannot_generate_is_refused() -> None:
    """A free-mode phase waits for a person; asking it to produce resolutions can only
    repeat or hold, forever, for a reason nobody reading the flow would guess."""
    from core.process.dsl.validator import validate_definition

    issues = validate_definition(_one_phase(PhaseCompletionSpec(resolutions=1), mode="free"))
    assert any("no generating actor" in i.message for i in issues)


def test_a_satisfiable_requirement_validates_clean() -> None:
    from core.process.dsl.validator import validate_definition

    assert validate_definition(_one_phase(PhaseCompletionSpec(resolutions=1))) == []
