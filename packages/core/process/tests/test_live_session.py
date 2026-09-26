"""its own acceptance criteria, and the concrete proof for
``tasks/phase-1/EXIT-GATE.md`` item 1: a real, non-stubbed ``advance_session``/
``run_agent_turn`` call chain runs a session end to end through the real interpreter,
scheduler, tool loop, and context assembler -- not just a direct-call-only test of one
piece in isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select

from core.agents.models import Persona
from core.agents.seed import seed_dev_agent
from core.assembler.models import ContextManifestRow
from core.ports.model_provider import Capabilities, Chunk, GenerationRequest, ToolCall
from core.process.authoring import create_definition
from core.process.dsl.fixtures import MINIMAL_MVP_FLOW
from core.process.dsl.validator import validate_raw
from core.process.interpreter import start_session, submit_human_turn
from core.process.live_session import run_process_definition_session
from core.process.skeleton import create_session
from core.resolution.records import ResolutionRecordRow
from core.sessions.models import MessageRow, SessionEventRow, SessionRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


@dataclass(frozen=True)
class _ScriptedTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass
class _ScriptedProvider:
    turns: list[_ScriptedTurn]
    call_count: int = field(default=0, init=False)

    async def generate(self, req: GenerationRequest) -> AsyncIterator[Chunk]:
        del req
        self.call_count += 1
        turn = self.turns.pop(0)
        finish_reason = "tool_calls" if turn.tool_calls else "stop"
        yield Chunk(text=turn.text, finish_reason=finish_reason, tool_calls=turn.tool_calls)

    async def generate_structured(self, req: GenerationRequest, schema: type) -> object:  # type: ignore[type-arg]
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return max(len(text.split()), 1)

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(
            supports_tools=True, supports_json_mode=False, supports_prompt_caching=False
        )


def _stub_embedding_provider():  # noqa: ANN201
    from adapters.embedding.stub.provider import StubEmbeddingProvider

    return StubEmbeddingProvider(dimension=1024)


def _flow_with_randomizer_on_resolve() -> dict[str, object]:
    """MINIMAL_MVP_FLOW, plus a randomizer tool on the resolve phase -- the shipped
    fixture itself declares no tools, but the exit gate's own slice wants a live
    randomizer call proven through this exact flow shape."""
    flow = deepcopy(MINIMAL_MVP_FLOW)
    phases = flow["phases"]
    assert isinstance(phases, dict)
    resolve_phase = phases["resolve"]
    assert isinstance(resolve_phase, dict)
    resolve_phase["tools"] = ["randomizer"]
    return flow


async def test_minimal_mvp_flow_runs_two_rounds_through_the_real_interpreter_and_tool_loop(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"live-session-{uuid.uuid4().hex[:8]}"
    )
    persona_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", persona_type="supervisor"
    )

    async with tenant_scope(tenant_id) as session:
        # The human participant is a separate principal from the facilitator agent.
        human_principal = Principal(tenant_id=tenant_id, kind="human", display_name="Player")
        session.add(human_principal)
        await session.flush()
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=human_principal.id,
                role="participant",
            )
        )
        human_principal_id = human_principal.id

    sess = await create_session(tenant_id, workspace_id, persona_id)

    flow = _flow_with_randomizer_on_resolve()
    definition_row = await create_definition(tenant_id, "mvp-live", "MVP Live", flow)
    dsl, issues = validate_raw(flow)
    assert dsl is not None and not issues, issues

    await start_session(tenant_id, sess.id, dsl, definition_row.id, definition_row.version)

    provider = _ScriptedProvider(
        turns=[
            _ScriptedTurn(text="The room is dark and quiet."),  # round 1: arbiter_narrate
            _ScriptedTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_1",
                        name="randomizer",
                        arguments={"expression": "1d20+2", "check_type": "stealth", "target": 10},
                    ),
                ),
            ),  # round 1: resolve, tool-call leg
            _ScriptedTurn(text="You slip past unnoticed."),  # round 1: resolve, final leg
            _ScriptedTurn(text="A new room, lit by torchlight."),  # round 2: arbiter_narrate
        ]
    )

    def model_provider_factory(_name: str):  # noqa: ANN202
        return provider

    embedding_provider = _stub_embedding_provider()

    result1 = await run_process_definition_session(
        tenant_id,
        sess.id,
        dsl,
        model_provider_factory=model_provider_factory,
        embedding_provider=embedding_provider,
    )
    assert result1.status == "awaiting_human"
    assert result1.final_phase == "player_act"

    await submit_human_turn(tenant_id, sess.id, "I search the room.", human_principal_id)

    result2 = await run_process_definition_session(
        tenant_id,
        sess.id,
        dsl,
        model_provider_factory=model_provider_factory,
        embedding_provider=embedding_provider,
    )
    assert result2.status == "awaiting_human"
    assert result2.final_phase == "player_act"
    assert provider.call_count == 4  # every scripted turn was actually consumed

    async with tenant_scope(tenant_id) as session:
        messages = (
            (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.session_id == sess.id)
                    .order_by(MessageRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
        transitions = (
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == sess.id,
                        SessionEventRow.kind == "phase_transition",
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            )
            .scalars()
            .all()
        )
        final_session_row = await session.get(SessionRow, sess.id)
        assert final_session_row is not None

    # Real MessageRows, correct roles: arbiter (assistant) -> human (user) -> arbiter's
    # resolve reply (assistant) -> arbiter's round-2 narration (assistant).
    assert [m.role for m in messages] == ["assistant", "user", "assistant", "assistant"]
    assert messages[0].content_md == "The room is dark and quiet."
    assert messages[1].content_md == "I search the room."
    assert messages[2].content_md == "You slip past unnoticed."
    assert messages[3].content_md == "A new room, lit by torchlight."

    # A real phase_transition sequence: arbiter_narrate -> player_act -> resolve ->
    # arbiter_narrate (round 2's own transition into player_act hasn't happened yet --
    # the second run_process_definition_session call stopped at player_act itself).
    transition_pairs = [(t.payload["from"], t.payload["to"]) for t in transitions]
    assert transition_pairs == [
        ("arbiter_narrate", "player_act"),
        ("player_act", "resolve"),
        ("resolve", "arbiter_narrate"),
        ("arbiter_narrate", "player_act"),
    ]

    # The resolve phase's CEL effect (`state.round + 1`) actually ran.
    assert final_session_row.state["round"] == 1
    assert final_session_row.current_phase == "player_act"

    # randomizer, reached through the real tool loop (not a direct resolve() call),
    # wrote a real ResolutionRecord, and the resolve-phase reply is correlated to it.
    resolve_message = messages[2]
    assert len(resolve_message.resolution_record_ids) == 1
    async with tenant_scope(tenant_id) as session:
        record = await session.get(
            ResolutionRecordRow, uuid.UUID(resolve_message.resolution_record_ids[0])
        )
        assert record is not None
        assert record.expression == "1d20+2"
        assert record.outcome in ("success", "failure")

        manifest_row = (
            await session.get(ContextManifestRow, resolve_message.context_manifest_id)
            if resolve_message.context_manifest_id
            else None
        )
        # The context assembler actually ran for this turn -- a manifest was
        # written and linked, not skipped.
        assert manifest_row is not None


async def test_the_scheduler_path_threads_on_event_into_the_turn(
    monkeypatch: pytest.MonkeyPatch, db_available: None
) -> None:
    """The typing cue and the rich completed-message mirror live inside
    run_one_persona_turn and hang off on_event. _make_execute_turn -- the factory behind
    every autonomous process-definition turn -- dropped it, so live viewers streamed every
    turn with no cue: the UI could not say who was speaking and labelled each in-flight
    reply with the facilitator fallback ("Arbiter" under the rpg overlay), which read as
    one agent answering for the whole cast. Found by capturing the wire order of a real
    turn: chunk, message, transition -- and no typing event at all.
    """
    import uuid as _uuid

    from core.agents.seed import seed_dev_agent
    from core.process import live_session as ls
    from core.process.dsl.schema import ActorSpec, PhaseSpec, VisibilitySpec
    from core.process.interpreter import ActorRef, InterpreterContext
    from core.tenancy.seed import seed_dev_tenant

    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"onevent-{_uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(tenant_id, workspace_id, key="p", persona_type="supervisor")
    from core.agents.authoring import get_persona

    persona = await get_persona(tenant_id, persona_id)
    assert persona is not None

    seen: dict[str, object] = {}

    async def fake_turn(**kwargs):  # noqa: ANN003, ANN202
        seen.update(kwargs)
        raise RuntimeError("stop here -- wiring is what this test is about")

    monkeypatch.setattr(ls, "run_one_persona_turn", fake_turn)

    async def sentinel_on_event(seq, kind, payload):  # noqa: ANN001, ANN202
        pass

    execute_turn = ls._make_execute_turn(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        model_provider_factory=lambda _p: None,
        embedding_provider=None,  # type: ignore[arg-type]
        rule_system=None,  # type: ignore[arg-type]
        rule_system_id=_uuid.uuid4(),
        on_chunk=None,
        on_event=sentinel_on_event,
    )
    phase = PhaseSpec(
        label_key="x",
        actors=[ActorSpec(persona_type="supervisor", mode="generate")],
        visibility=VisibilitySpec(
            knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
        ),
    )
    ctx = InterpreterContext(tenant_id, _uuid.uuid4(), "x", phase, {}, event_seq=0)

    with pytest.raises(RuntimeError, match="stop here"):
        await execute_turn(ActorRef(principal_id=persona.principal_id, mode="generate"), ctx)

    assert seen.get("on_event") is sentinel_on_event, (
        "the factory has to hand on_event through to the turn"
    )


def test_phase_remote_allowlist_gates_by_phase() -> None:
    """Remote MCP tools used to reach every persona in every phase; the phase now
    declares who gets what. None keeps legacy allow-all, [] shuts the phase off, a
    list is an allowlist -- which is how a mystery's forensic oracle reaches the
    investigator's phases and never a suspect's."""
    from core.process.dsl.schema import ActorSpec, PhaseSpec, VisibilitySpec
    from core.process.live_session import _phase_remote_allowlist

    def phase(remote_tools):  # noqa: ANN001, ANN202
        return PhaseSpec(
            label_key="x",
            actors=[ActorSpec(persona_type="supervisor", mode="generate")],
            visibility=VisibilitySpec(
                knowledge_classes=[], scopes=[], entity_fields=[], secrets="none"
            ),
            remote_tools=remote_tools,
        )

    assert _phase_remote_allowlist(phase(None)) is None, "legacy allow-all"
    assert _phase_remote_allowlist(phase([])) == []
    assert _phase_remote_allowlist(phase(["evidence_check"])) == ["evidence_check"]


async def test_a_personas_own_brief_reaches_its_turn(db_available: None) -> None:
    """The persona's written prose must be in front of the model that speaks as it.

    It was not. `assemble()` takes the viewer, the phase, knowledge, secrets, history and
    behaviour-axis directives, and `persona_md` reached a model in exactly two places in
    this codebase -- both in the delegation reviewer. A live turn got the persona's NAME
    and nothing else, so a cast's character came only from its knowledge entries, its
    phase prompt and its axes.

    That is survivable where the character lives in the lore, which is why it went
    unnoticed for so long. It is not survivable where the brief carries a FACT: a
    newsroom editor told the names of its two reporters addressed two others it had
    invented, and the obvious response was to rewrite a brief the model had never seen.
    """
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(slug=f"brief-{uuid.uuid4().hex[:8]}")
    persona_id = await seed_dev_agent(
        tenant_id, workspace_id, key="facilitator", persona_type="supervisor"
    )

    brief = "You are Marit Halvorsen. Your two reporters are Aksel Rygg and Nadia Brekke."
    async with tenant_scope(tenant_id) as session:
        row = await session.get(Persona, persona_id)
        row.persona_md = brief

    sess = await create_session(tenant_id, workspace_id, persona_id)
    flow = _flow_with_randomizer_on_resolve()
    definition_row = await create_definition(tenant_id, "brief-flow", "Brief", flow)
    dsl, issues = validate_raw(flow)
    assert dsl is not None and not issues, issues
    await start_session(tenant_id, sess.id, dsl, definition_row.id, definition_row.version)

    seen: list[GenerationRequest] = []

    class _Capturing(_ScriptedProvider):
        async def generate(self, req: GenerationRequest):  # type: ignore[override]
            seen.append(req)
            async for chunk in super().generate(req):
                yield chunk

    provider = _Capturing(turns=[_ScriptedTurn(text="Beats assigned.")])
    await run_process_definition_session(
        tenant_id,
        sess.id,
        dsl,
        model_provider_factory=lambda _p: provider,
        embedding_provider=_stub_embedding_provider(),
    )

    assert seen, "no generation happened"
    system_text = "\n".join(
        str(m.get("content") or "") for m in seen[0].messages if m.get("role") == "system"
    )
    assert "Aksel Rygg" in system_text, "the persona's own brief never reached the model"
    assert "Nadia Brekke" in system_text
