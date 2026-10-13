"""An open floor (``order: hands``): everyone speaks once in seat order, then the floor
goes to whoever the last speaker named, else to raised hands, longest raised first, until
nobody wants it; each speaker is capped by ``max_turns_each``."""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.agents.authoring import get_persona
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolContext
from core.process import floor
from core.process.authoring import create_definition
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.dsl.validator import validate_raw
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    advance_session,
    start_session,
)
from core.process.scheduler import (
    _word_mention_score,
    make_default_candidate_resolver,
    make_scheduler,
)
from core.process.skeleton import create_session
from core.sessions.lifecycle import set_session_roster
from core.tenancy.seed import seed_dev_tenant

_VIS = {
    "knowledge_classes": ["misc"],
    "scopes": ["workspace_public"],
    "entity_fields": "all",
    "secrets": "none",
}
_NAMES = ["Ada", "Bram", "Cyra", "Dina"]


def _flow() -> dict[str, Any]:
    return {
        "name": "Open floor",
        "vocabulary_overlay": "neutral",
        "settings": {"each": {"type": "integer", "default": 3, "min": 1, "max": 9}},
        "participants": {
            "vars": {"status": {"type": "string", "default": "in"}},
            "seat_order": "roster",
        },
        "initial_phase": "talk",
        "phases": {
            "talk": {
                "label_key": "phase.talk",
                "actors": [
                    {
                        "persona_type": "participant",
                        "mode": "generate",
                        "order": "hands",
                        "max_turns_each": {"setting": "each"},
                        "where": "p.status == 'in'",
                    }
                ],
                "visibility": _VIS,
                "on_complete": "done",
            },
            "done": {
                "label_key": "phase.done",
                "actors": [{"persona_type": "supervisor", "mode": "generate"}],
                "visibility": _VIS,
            },
        },
    }


def test_flow_validates_and_the_cap_needs_hands() -> None:
    dsl, issues = validate_raw(_flow())
    assert dsl is not None and issues == []
    flow = _flow()
    flow["phases"]["talk"]["actors"][0]["order"] = "seated"
    _dsl, issues = validate_raw(flow)
    assert "phases.talk.actors[0].max_turns_each" in {i.field_path for i in issues}


def test_a_name_counts_only_as_a_whole_word() -> None:
    assert _word_mention_score("Sage", "read the message again") == -1
    assert _word_mention_score("Sage", "sage, answer me") > 0
    assert _word_mention_score("Bram", "bramble aside") == -1


async def _table(
    flow: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, ProcessDefinitionDSL, dict]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"flr-{uuid.uuid4().hex[:8]}")
    host = await seed_dev_agent(tenant_id, workspace_id, key="host", name="Host")
    players = [
        await seed_dev_agent(
            tenant_id, workspace_id, key=n.lower(), name=n, persona_type="participant"
        )
        for n in _NAMES
    ]
    sess = await create_session(tenant_id, workspace_id, host)
    await set_session_roster(tenant_id, sess.id, host, players)
    row = await create_definition(tenant_id, f"flr-{uuid.uuid4().hex[:6]}", "Open", flow or _flow())
    dsl = ProcessDefinitionDSL.model_validate(row.definition)
    await start_session(tenant_id, sess.id, dsl, row.id, row.version)
    names: dict[uuid.UUID, str] = {}
    for pid, name in zip([host, *players], ["Host", *_NAMES], strict=True):
        persona = await get_persona(tenant_id, pid)
        assert persona is not None
        names[persona.principal_id] = name
    return tenant_id, workspace_id, sess.id, dsl, names


async def _discussion(flow: dict[str, Any] | None = None) -> list[str]:
    """Run the scripted discussion and return who spoke, in order."""
    tenant_id, workspace_id, session_id, dsl, names = await _table(flow)
    spoken: list[str] = []
    # (what they say, whether they raise a hand), per speaker, turn by turn
    script: dict[str, list[tuple[str, bool]]] = {
        "Ada": [("Hello.", True), ("One more thing.", True), ("And a last one.", True)],
        "Bram": [("Hi all.", False), ("I was at home.", False)],
        "Cyra": [("I read the message twice.", False)],
        "Dina": [("Bramble aside, Bram should explain where he was.", False)],
    }

    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        name = names[actor.principal_id]
        if ctx.phase_key != "talk":
            return ActorTurnResult(content_md=f"{name} closes")
        text, raise_it = script[name][sum(s == name for s in spoken)]
        spoken.append(name)
        if raise_it:
            assert await floor.raise_hand(
                tenant_id, session_id, ctx.phase_key, actor.principal_id, ctx.event_seq
            )
        return ActorTurnResult(content_md=text)

    resolver = make_default_candidate_resolver(
        workspace_id,
        persona_candidate_resolver=make_persona_candidate_resolver(tenant_id, workspace_id),
    )
    result = await advance_session(
        tenant_id,
        session_id,
        dsl,
        next_actor_fn=make_scheduler(resolver),
        execute_turn=execute_turn,
    )
    assert result.status == "terminal", result
    return spoken


async def test_the_floor_follows_names_then_hands_until_nobody_wants_it(
    db_available: None,
) -> None:
    # The opening round in seat order; Dina named Bram (not "Bramble"), who answers; then
    # Ada's hand from the opening round; Ada raised again and nobody else wanted the
    # floor, so she follows herself once more -- her third turn is her cap, and the hand
    # she raised in it is never called.
    assert await _discussion() == ["Ada", "Bram", "Cyra", "Dina", "Bram", "Ada", "Ada"]


async def test_without_answer_named_only_hands_move_the_floor(db_available: None) -> None:
    flow = _flow()
    flow["phases"]["talk"]["actors"][0]["answer_named"] = False
    # Dina still names Bram, but Bram never raised a hand, so he is not called.
    assert await _discussion(flow) == ["Ada", "Bram", "Cyra", "Dina", "Ada", "Ada"]


async def test_raise_hand_records_once_per_turn_and_says_what_is_left(
    db_available: None,
) -> None:
    tenant_id, _workspace_id, session_id, dsl, names = await _table()
    ada = next(pid for pid, n in names.items() if n == "Ada")
    spec, handler = floor.make_raise_hand_tool(
        tenant_id=tenant_id,
        session_id=session_id,
        phase_key="talk",
        principal_id=ada,
        turns_left=2,
    )
    assert spec.name == floor.RAISE_HAND_TOOL_NAME and "2 more turns" in spec.description
    ctx = ToolContext(tenant_id=tenant_id, persona_id=ada, session_id=session_id, turn_event_seq=7)
    assert json.loads((await handler({}, ctx)).content) == {"raised": True}
    await handler({}, ctx)  # a resumed turn calling it again changes nothing
    hands = await floor.raised_hands(tenant_id, session_id, "talk", since_seq=0)
    assert [(h.principal_id, h.turn_seq) for h in hands] == [(ada, 7)]
    assert await floor.raised_hands(tenant_id, session_id, "talk", since_seq=8) == []
    none_left, _ = floor.make_raise_hand_tool(
        tenant_id=tenant_id,
        session_id=session_id,
        phase_key="talk",
        principal_id=ada,
        turns_left=0,
    )
    assert "no turns left" in none_left.description

    entry = dsl.phases["talk"].actors[0]
    cursor = {"phase_key": "talk", "entry_index": 0, "entry": {"spoken": {str(ada): 2}}}
    assert floor.floor_entry(dsl.phases["talk"], cursor, "talk") is entry
    assert floor.floor_entry(dsl.phases["talk"], cursor, "done") is None
    assert floor.turns_left(entry, cursor, ada, {"each": 3}) == 1
