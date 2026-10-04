"""Acceptance criteria for the process interpreter, against a live Postgres."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.agents.models import Persona
from core.agents.seed import seed_dev_agent
from core.process.authoring import create_definition
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW, STANDARD_SESSION_FLOW
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    HumanTurnPendingError,
    InterpreterContext,
    _run_actor_turn,
    _run_turn_idempotent,
    advance_session,
    start_session,
)
from core.process.skeleton import create_session
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    async with tenant_scope(tenant_id) as session:
        agent_row = await session.get(Persona, persona_id)
        assert agent_row is not None
        principal_id = agent_row.principal_id
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return tenant_id, workspace_id, principal_id, sess.id


class _ScriptedScheduler:
    """Test double for the real scheduler: gives each phase visit exactly
    ``max(actor.max_turns or 1)`` turns, resetting the moment the interpreter has moved
    to a different phase key -- so a cyclic flow's repeat visits each get their own
    allocation, matching what a real per-visit cursor would do."""

    def __init__(self, principal_id: uuid.UUID) -> None:
        self._principal_id = principal_id
        self._last_phase_key: str | None = None
        self._turns_this_visit = 0

    async def __call__(self, ctx: InterpreterContext) -> ActorRef | None:
        if ctx.phase_key != self._last_phase_key:
            self._last_phase_key = ctx.phase_key
            self._turns_this_visit = 0
        if not ctx.phase.actors:
            return None
        max_turns = max((a.max_turns or 1) for a in ctx.phase.actors)
        if self._turns_this_visit >= max_turns:
            return None
        self._turns_this_visit += 1
        mode = ctx.phase.actors[0].mode
        return ActorRef(principal_id=self._principal_id, mode=mode)


async def _stub_execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
    return ActorTurnResult(content_md=f"stub turn in {ctx.phase_key}")


async def _get_events(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[SessionEventRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SessionEventRow)
                .where(SessionEventRow.session_id == session_id)
                .order_by(SessionEventRow.event_seq)
            )
        ).scalars()
        return list(rows)


async def _get_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        return row


# ── golden test: 3-phase MVP end to end ─────────────────────────────────────────────────


async def test_mvp_flow_runs_a_scripted_session_and_produces_the_expected_event_sequence(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-mvp")
    definition_row = await create_definition(tenant_id, "mvp", "MVP", MINIMAL_MVP_FLOW)
    definition = ProcessDefinitionDSL.model_validate(definition_row.definition)

    await start_session(
        tenant_id, session_id, definition, definition_row.id, definition_row.version
    )

    scheduler = _ScriptedScheduler(principal_id)
    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_stub_execute_turn,
        max_steps=6,
    )

    # arbiter_narrate(turn) -> transition(player_act) -> player_act(turn) ->
    # transition(resolve) -> resolve(turn) -> transition(arbiter_narrate, round=1)
    assert result.status == "active"
    assert result.steps_taken == 6
    assert result.final_phase == "arbiter_narrate"

    events = await _get_events(tenant_id, session_id)
    assert [e.kind for e in events] == [
        "message",
        "phase_transition",
        "message",
        "phase_transition",
        "message",
        "phase_transition",
    ]
    assert [e.event_seq for e in events] == [0, 1, 2, 3, 4, 5]
    assert events[1].payload == {
        "from": "arbiter_narrate",
        "to": "player_act",
        "state": {"round": 0},
    }
    assert events[3].payload == {"from": "player_act", "to": "resolve", "state": {"round": 0}}
    assert events[5].payload == {
        "from": "resolve",
        "to": "arbiter_narrate",
        "state": {"round": 1},
    }

    row = await _get_session(tenant_id, session_id)
    assert row.current_phase == "arbiter_narrate"
    assert row.state == {"round": 1}

    async with tenant_scope(tenant_id) as session:
        messages = (
            (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.session_id == session_id)
                    .order_by(MessageRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
    assert [m.content_md for m in messages] == [
        "stub turn in arbiter_narrate",
        "stub turn in player_act",
        "stub turn in resolve",
    ]


# ── CEL gate branching (Standard Session Flow's resolution phase) ──────────────────────


async def test_resolution_phase_branches_to_feedback_loop_when_round_becomes_a_multiple_of_three(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-gate-fb")
    definition = ProcessDefinitionDSL.model_validate(STANDARD_SESSION_FLOW)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = "resolution"
        row.state = {"round": 5, "scene_id": "", "pending_feedback": False}

    async def no_actor(_ctx: InterpreterContext) -> ActorRef | None:
        return None

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=no_actor,
        execute_turn=_stub_execute_turn,
        max_steps=1,
    )

    assert result.final_phase == "feedback_loop"
    row = await _get_session(tenant_id, session_id)
    assert row.current_phase == "feedback_loop"
    assert row.state["round"] == 6
    assert row.state["pending_feedback"] is True
    assert isinstance(row.state["pending_feedback"], bool)  # not celpy's int-subclass BoolType


async def test_resolution_phase_branches_to_open_discussion_otherwise(db_available: None) -> None:
    tenant_id, _workspace_id, _principal_id, session_id = await _setup("interp-gate-od")
    definition = ProcessDefinitionDSL.model_validate(STANDARD_SESSION_FLOW)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = "resolution"
        row.state = {"round": 6, "scene_id": "", "pending_feedback": False}

    async def no_actor(_ctx: InterpreterContext) -> ActorRef | None:
        return None

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=no_actor,
        execute_turn=_stub_execute_turn,
        max_steps=1,
    )

    assert result.final_phase == "open_discussion"
    row = await _get_session(tenant_id, session_id)
    assert row.state["round"] == 7
    assert row.state["pending_feedback"] is False


# ── await yields without holding anything ────────────────────────────────────────────


async def test_phase_with_unsatisfied_await_yields_awaiting_status(db_available: None) -> None:
    tenant_id, _workspace_id, _principal_id, session_id = await _setup("interp-await")
    definition = ProcessDefinitionDSL.model_validate(STANDARD_SESSION_FLOW)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = "feedback_loop"
        row.state = {"round": 3, "scene_id": "", "pending_feedback": True}

    async def no_actor(_ctx: InterpreterContext) -> ActorRef | None:
        return None

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=no_actor,
        execute_turn=_stub_execute_turn,
        max_steps=5,
    )

    assert result.status == "awaiting"
    assert result.steps_taken == 1
    row = await _get_session(tenant_id, session_id)
    assert row.status == "awaiting"
    assert row.current_phase == "feedback_loop"  # never transitioned


# ── fault handling: pause, never a stuck lock ───────────────────────────────────────


async def test_interpreter_fault_pauses_the_session_with_a_diagnostic_event(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, _principal_id, session_id = await _setup("interp-fault")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = "not_a_declared_phase"

    async def no_actor(_ctx: InterpreterContext) -> ActorRef | None:
        return None

    result = await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=no_actor,
        execute_turn=_stub_execute_turn,
    )

    assert result.status == "paused"
    row = await _get_session(tenant_id, session_id)
    assert row.status == "paused"
    events = await _get_events(tenant_id, session_id)
    assert events[-1].kind == "error"
    assert "not_a_declared_phase" in events[-1].payload["message"]


# ── resume without duplicate side effects (idempotency) ─────────────────────


async def test_resumed_turn_with_the_same_peeked_event_seq_does_not_reexecute_the_side_effect(
    db_available: None,
) -> None:
    """Directly proves the mechanism the fix in _run_actor_turn's docstring describes:
    the idempotency key must be derived from a *peeked*, not claimed, event_seq, or a
    crash-and-retry silently re-executes the side effect under a fresh key."""
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-idem-unit")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    phase = definition.phases["arbiter_narrate"]
    actor = ActorRef(principal_id=principal_id, mode="generate")
    ctx = InterpreterContext(tenant_id, session_id, "arbiter_narrate", phase, {})

    call_count = 0

    async def counting_execute_turn(_actor: ActorRef, _ctx: InterpreterContext) -> ActorTurnResult:
        nonlocal call_count
        call_count += 1
        return ActorTurnResult(content_md="hello")

    first = await _run_turn_idempotent(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        actor=actor,
        ctx=ctx,
        execute_turn=counting_execute_turn,
    )
    second = await _run_turn_idempotent(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        actor=actor,
        ctx=ctx,
        execute_turn=counting_execute_turn,
    )

    assert call_count == 1
    expected = {"content_md": "hello", "already_persisted": False, "message_id": None}
    assert first == second == expected


async def test_run_actor_turn_resumes_after_the_side_effect_but_before_the_final_commit(
    db_available: None,
) -> None:
    """Simulates the exact crash window the fix closes: the idempotent side effect (e.g.
    a real model call) already completed successfully, but the transaction that would
    have committed its message/event/next_event_seq never ran (the process died first).
    A resumed _run_actor_turn call must reuse the cached result, not call execute_turn
    again, and must still correctly commit exactly one message."""
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-idem-resume")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    phase = definition.phases["arbiter_narrate"]
    actor = ActorRef(principal_id=principal_id, mode="generate")
    ctx = InterpreterContext(tenant_id, session_id, "arbiter_narrate", phase, {})

    call_count = 0

    async def counting_execute_turn(_actor: ActorRef, _ctx: InterpreterContext) -> ActorTurnResult:
        nonlocal call_count
        call_count += 1
        return ActorTurnResult(content_md="hello")

    # The "crashed first attempt": the side effect ran and its own transaction committed,
    # but _run_actor_turn's wrapping commit (message/event/next_event_seq) never happened.
    await _run_turn_idempotent(
        tenant_id=tenant_id,
        session_id=session_id,
        event_seq=0,
        actor=actor,
        ctx=ctx,
        execute_turn=counting_execute_turn,
    )
    assert call_count == 1

    # "Resume": next_event_seq is still 0 (nothing committed it), so this peeks the same
    # value, derives the same idempotency key, and must not re-invoke execute_turn.
    await _run_actor_turn(
        tenant_id, session_id, "arbiter_narrate", phase, actor, counting_execute_turn
    )

    assert call_count == 1
    async with tenant_scope(tenant_id) as session:
        messages = (
            (await session.execute(select(MessageRow).where(MessageRow.session_id == session_id)))
            .scalars()
            .all()
        )
    assert len(messages) == 1
    assert messages[0].content_md == "hello"
    row = await _get_session(tenant_id, session_id)
    assert row.next_event_seq == 1


# ── HumanTurnPendingError / already_persisted / on_event ─────────────────────────


async def test_human_turn_pending_error_yields_awaiting_human_without_pausing(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-human-pending")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = definition.initial_phase
        row.state = {name: spec.default for name, spec in definition.state.items()}

    scheduler = _ScriptedScheduler(principal_id)

    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        if actor.mode == "free":
            raise HumanTurnPendingError()
        return ActorTurnResult(content_md=f"turn in {ctx.phase_key}")

    result = await advance_session(
        tenant_id, session_id, definition, next_actor_fn=scheduler, execute_turn=execute_turn
    )

    assert result.status == "awaiting_human"
    assert result.final_phase == "player_act"
    row = await _get_session(tenant_id, session_id)
    assert row.status == "active"  # never paused/faulted -- this is an expected stop


async def test_already_persisted_result_skips_the_duplicate_write(db_available: None) -> None:
    """Simulates an execute_turn adapter that -- like core.process.live_session's real
    one, wrapping core.agents.runtime.run_agent_turn -- performs its own full commit
    (message + claiming the peeked event_seq slot) and reports already_persisted=True.
    _run_actor_turn must not write a second MessageRow for the same turn."""
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-already-persisted")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    phase = definition.phases["arbiter_narrate"]
    actor = ActorRef(principal_id=principal_id, mode="generate")

    async def execute_turn(_actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        async with tenant_scope(tenant_id) as session:
            row = await session.get(SessionRow, session_id)
            assert row is not None
            row.next_event_seq = ctx.event_seq + 1
            message = MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=ctx.event_seq,
                author_principal_id=principal_id,
                role="assistant",
                content_md="already committed by the adapter",
            )
            session.add(message)
            await session.flush()
            message_id = message.id
            # The real adapter claims the slot with a session_event too -- and the
            # interpreter's already_persisted verification checks for exactly that.
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_seq=ctx.event_seq,
                    kind="message",
                    payload={
                        "id": str(message_id),
                        "role": "assistant",
                        "content": "already committed by the adapter",
                    },
                    actor_principal_id=principal_id,
                )
            )
        return ActorTurnResult(
            content_md="already committed by the adapter",
            already_persisted=True,
            message_id=message_id,
        )

    await _run_actor_turn(tenant_id, session_id, "arbiter_narrate", phase, actor, execute_turn)

    async with tenant_scope(tenant_id) as session:
        messages = (
            (await session.execute(select(MessageRow).where(MessageRow.session_id == session_id)))
            .scalars()
            .all()
        )
    assert len(messages) == 1  # no second MessageRow written for the same turn
    assert messages[0].content_md == "already committed by the adapter"


async def test_already_persisted_tolerates_concurrent_note_writers(
    db_available: None,
) -> None:
    """Observed live: the turn's own delegation enqueues worker jobs whose transcript
    notes land (and advance next_event_seq well past event_seq+1) before the
    interpreter's already_persisted verification runs. The turn claimed its slot, so
    that is a success, never a fault."""
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-note-race")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    phase = definition.phases["arbiter_narrate"]
    actor = ActorRef(principal_id=principal_id, mode="generate")

    async def execute_turn(_actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        async with tenant_scope(tenant_id) as session:
            row = await session.get(SessionRow, session_id)
            assert row is not None
            message = MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=ctx.event_seq,
                author_principal_id=principal_id,
                role="assistant",
                content_md="turn content",
            )
            session.add(message)
            await session.flush()
            message_id = message.id
            session.add(
                SessionEventRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_seq=ctx.event_seq,
                    kind="message",
                    payload={"id": str(message_id), "role": "assistant", "content": "turn content"},
                    actor_principal_id=principal_id,
                )
            )
            # Worker-posted notes race in behind the turn's commit: seqs +1..+4, with
            # the counter bumped past them (max() rule) -- 5 where the old check
            # demanded exactly event_seq + 1.
            for offset in range(1, 5):
                session.add(
                    SessionEventRow(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_seq=ctx.event_seq + offset,
                        kind="note",
                        payload={"content": f"note {offset}"},
                        actor_principal_id=principal_id,
                    )
                )
            row.next_event_seq = ctx.event_seq + 5
        return ActorTurnResult(
            content_md="turn content", already_persisted=True, message_id=message_id
        )

    # Must not raise InterpreterFaultError.
    await _run_actor_turn(tenant_id, session_id, "arbiter_narrate", phase, actor, execute_turn)


async def test_on_event_fires_for_phase_transition_and_message(db_available: None) -> None:
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-on-event")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.current_phase = definition.initial_phase
        row.state = {name: spec.default for name, spec in definition.state.items()}

    scheduler = _ScriptedScheduler(principal_id)
    published: list[tuple[int, str, dict[str, object]]] = []

    async def on_event(event_seq: int, kind: str, payload: dict[str, object]) -> None:
        published.append((event_seq, kind, payload))

    await advance_session(
        tenant_id,
        session_id,
        definition,
        next_actor_fn=scheduler,
        execute_turn=_stub_execute_turn,
        on_event=on_event,
    )

    kinds = [kind for _seq, kind, _payload in published]
    assert "message" in kinds
    assert "phase_transition" in kinds
    message_payload = next(p for _s, k, p in published if k == "message")
    assert message_payload["role"] == "assistant"
    assert "id" in message_payload


async def test_a_paused_session_stops_advancing_at_the_next_step(db_available: None) -> None:
    """A pause taken while the loop runs must stop it. The status was never re-read: a
    paused newsroom session ran four more turns and its terminal transition then
    overwrote 'paused' with 'completed' (sweep 2026-10-04)."""
    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-paused")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.status = "paused"

    async def eager(_ctx: InterpreterContext) -> ActorRef | None:
        return ActorRef(principal_id=principal_id, mode="generate")

    result = await advance_session(
        tenant_id, session_id, definition, next_actor_fn=eager, execute_turn=_stub_execute_turn
    )

    assert result.status == "paused"
    assert not [e for e in await _get_events(tenant_id, session_id) if e.kind == "message"]
    assert (await _get_session(tenant_id, session_id)).status == "paused"


async def test_a_faulted_turn_puts_the_actor_cursor_back(db_available: None) -> None:
    """The scheduler persists the advanced cursor before the turn runs. A turn that then
    faults left it advanced, so a resume skipped that actor: the Hägnaryd inspector's
    whole opening phase vanished after a resume (sweep 2026-10-04)."""
    from core.process.interpreter import InterpreterFaultError

    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-cursor")
    definition = ProcessDefinitionDSL.model_validate(MINIMAL_MVP_FLOW)
    before = {"phase_key": "opening", "entry_index": 0, "entry": None}

    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        row.actor_cursor = dict(before)

    async def advancing(ctx: InterpreterContext) -> ActorRef | None:
        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            row.actor_cursor = {"phase_key": ctx.phase_key, "entry_index": 1, "entry": None}
        return ActorRef(principal_id=principal_id, mode="generate")

    async def faulting(_actor: ActorRef, _ctx: InterpreterContext) -> ActorTurnResult:
        raise InterpreterFaultError("the model returned an empty generation")

    result = await advance_session(
        tenant_id, session_id, definition, next_actor_fn=advancing, execute_turn=faulting
    )

    assert result.status == "paused"
    assert (await _get_session(tenant_id, session_id)).actor_cursor == before


async def test_a_personas_own_tool_results_stay_in_its_history(db_available: None) -> None:
    """A remote tool's answer used to exist only inside the turn that fetched it: the
    Hägnaryd inspector spent her second lab request repeating the first and told the
    table the radio had produced nothing (sweep 2026-10-04). Her own results now ride
    along in her history, at the seq they arrived; nobody else's do."""
    from core.process.live_session import _load_conversation

    tenant_id, _workspace_id, principal_id, session_id = await _setup("interp-tools")
    async with tenant_scope(tenant_id) as session:
        persona_name = await session.scalar(
            select(Persona.name).where(Persona.principal_id == principal_id)
        )
        assert persona_name
        session.add(
            MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=1,
                author_principal_id=principal_id,
                role="assistant",
                content_md="Lab, recover the speech file.",
            )
        )
        session.add(
            SessionEventRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=2,
                kind="tool_call",
                payload={
                    "server_key": "evidence",
                    "tool_name": "evidence_check",
                    "arguments": {"request": "recover_speech_file"},
                    "author": persona_name,
                    "outcome": "completed",
                    "result": "Page six names the heir.",
                },
            )
        )
        session.add(
            MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=3,
                author_principal_id=principal_id,
                role="assistant",
                content_md="Noted.",
            )
        )

    own = await _load_conversation(tenant_id, session_id, viewer_principal_id=principal_id)
    assert [m["content"][:12] for m in own] == ["Lab, recover", "[Result of y", "Noted."]
    assert "Page six names the heir." in own[1]["content"]
    assert own[1]["role"] == "user"

    someone_else = await _load_conversation(tenant_id, session_id, viewer_principal_id=uuid.uuid4())
    assert not any("Page six" in m["content"] for m in someone_else)
