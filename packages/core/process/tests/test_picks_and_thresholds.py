"""Picks, vote thresholds and votes between options.

The flow here is in neutral words: participants ``put_forward`` one another while they
speak (a pick), the table votes only between those put forward, and a vote between the
options ``yes`` and ``no`` decides whether everyone left in a tie goes ``out``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select

from core.agents.authoring import get_persona
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.seed import seed_dev_agent
from core.agents.tools import ToolContext
from core.process import participants as parts
from core.process.authoring import create_definition
from core.process.dsl.schema import ProcessDefinitionDSL
from core.process.dsl.validator import validate_raw
from core.process.interpreter import (
    ActorRef,
    ActorTurnResult,
    InterpreterContext,
    _phase_entry_seq,
    advance_session,
    start_session,
)
from core.process.scheduler import make_default_candidate_resolver, make_scheduler
from core.process.skeleton import create_session
from core.sessions.lifecycle import set_session_roster
from core.sessions.models import ParticipantStateChangeRow, SessionEventRow, SessionRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_VIS = {
    "knowledge_classes": ["misc"],
    "scopes": ["workspace_public"],
    "entity_fields": "all",
    "secrets": "none",
}
_IN = "p.status == 'in'"
_PUT_FORWARD = "size(participants.filter(p, p.put_forward))"
_SEATED_IN = {
    "persona_type": "participant",
    "mode": "generate",
    "order": "seated",
    "where": _IN,
}


def _flow(threshold: str = "plurality") -> dict[str, Any]:
    return {
        "name": "Put forward",
        "vocabulary_overlay": "neutral",
        "state": {"settle": {"type": "string", "default": ""}},
        "participants": {
            "vars": {
                "status": {"type": "string", "default": "in"},
                "put_forward": {"type": "boolean", "default": False},
                "put_forward_by": {"type": "string", "default": ""},
                "has_picked": {"type": "boolean", "default": False},
                "tied": {"type": "boolean", "default": False},
            },
        },
        "initial_phase": "talk",
        "phases": {
            "talk": {
                "label_key": "phase.talk",
                "actors": [_SEATED_IN],
                "visibility": _VIS,
                "picks": [
                    {
                        "tool": "put_forward",
                        "description": "Put one participant forward for the vote.",
                        "targets": "p.id != actor.id && p.status == 'in' && !p.put_forward",
                        "available": "!actor.has_picked",
                        "on_picked": [
                            {"set": "put_forward", "to": "true"},
                            {"set": "put_forward_by", "to": "actor.name"},
                        ],
                        "on_picker": [{"set": "has_picked", "to": "true"}],
                        "end_turn": True,
                    }
                ],
                "gates": [
                    {"when": f"{_PUT_FORWARD} == 0", "to": "done"},
                    {"else": True, "to": "vote"},
                ],
            },
            "vote": {
                "label_key": "phase.vote",
                "actors": [_SEATED_IN],
                "visibility": _VIS,
                "audience": {"members": "false"},
                "ballot": {
                    "targets": "p.put_forward && p.status == 'in'",
                    "threshold": threshold,
                    "on_chosen": [{"set": "status", "to": "'out'"}],
                    "on_tied": [{"set": "tied", "to": "true"}],
                },
                "gates": [
                    {"when": "size(participants.filter(p, p.tied)) > 0", "to": "settle"},
                    {"else": True, "to": "done"},
                ],
            },
            "settle": {
                "label_key": "phase.settle",
                "actors": [_SEATED_IN],
                "visibility": _VIS,
                "audience": {"members": "false"},
                "ballot": {"options": ["yes", "no"], "record_as": "settle"},
                "gates": [
                    {"when": "state.settle == 'yes'", "to": "all_out"},
                    {"else": True, "to": "done"},
                ],
            },
            "all_out": {
                "label_key": "phase.all_out",
                "actors": [{"persona_type": "supervisor", "mode": "generate"}],
                "visibility": _VIS,
                "participant_effects": [{"set": "status", "to": "'out'", "where": "p.tied"}],
                "on_complete": "done",
            },
            "done": {
                "label_key": "phase.done",
                "actors": [{"persona_type": "supervisor", "mode": "generate"}],
                "visibility": _VIS,
            },
        },
    }


# ── the declaration ─────────────────────────────────────────────────────────────────


def test_flow_validates() -> None:
    dsl, issues = validate_raw(_flow())
    assert dsl is not None and issues == []


def test_an_option_vote_needs_a_string_state_var_and_no_participant_fields() -> None:
    flow = _flow()
    flow["phases"]["settle"]["ballot"]["record_as"] = "nope"
    _dsl, issues = validate_raw(flow)
    assert any(i.field_path == "phases.settle.ballot.record_as" for i in issues)

    flow = _flow()
    flow["phases"]["settle"]["ballot"]["on_chosen"] = [{"set": "status", "to": "'out'"}]
    dsl, issues = validate_raw(flow)
    assert dsl is None and any("vote between options" in i.message for i in issues)

    flow = _flow()
    del flow["phases"]["settle"]["ballot"]["record_as"]
    dsl, issues = validate_raw(flow)
    assert dsl is None and any("record_as" in i.message for i in issues)

    flow = _flow()
    flow["phases"]["settle"]["ballot"]["options"] = ["yes", "yes"]
    dsl, issues = validate_raw(flow)
    assert dsl is None and any("distinct" in i.message for i in issues)


def test_a_pick_is_checked_like_every_other_expression() -> None:
    flow = _flow()
    pick = flow["phases"]["talk"]["picks"][0]
    pick["targets"] = "p.no_such_var"
    pick["on_picker"] = [{"set": "undeclared", "to": "true"}]
    _dsl, issues = validate_raw(flow)
    paths = {i.field_path for i in issues}
    assert "phases.talk.picks[0].targets" in paths
    assert "phases.talk.picks[0].on_picker[0].set" in paths

    for name in ("ballot", "raise_hand", "Put-Forward"):
        flow = _flow()
        flow["phases"]["talk"]["picks"][0]["tool"] = name
        dsl, _issues = validate_raw(flow)
        assert dsl is None, name

    flow = _flow()
    flow["phases"]["talk"]["picks"].append(dict(flow["phases"]["talk"]["picks"][0]))
    _dsl, issues = validate_raw(flow)
    assert any(i.field_path == "phases.talk.picks" for i in issues)


def test_a_threshold_carries_only_what_it_says() -> None:
    assert parts._meets("plurality", 1, 9)
    assert parts._meets("majority", 3, 5) and not parts._meets("majority", 2, 4)
    assert parts._meets("unanimous", 3, 3) and not parts._meets("unanimous", 2, 3)
    assert not parts._meets("unanimous", 0, 0)


# ── the record ──────────────────────────────────────────────────────────────────────


async def _game(
    threshold: str = "plurality",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, ProcessDefinitionDSL, dict[uuid.UUID, str]]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"picks-{uuid.uuid4().hex[:8]}")
    host = await seed_dev_agent(tenant_id, workspace_id, key="host", name="Host")
    names = ["Ada", "Bo", "Cy", "Di"]
    players = [
        await seed_dev_agent(
            tenant_id, workspace_id, key=n.lower(), name=n, persona_type="participant"
        )
        for n in names
    ]
    sess = await create_session(tenant_id, workspace_id, host)
    await set_session_roster(tenant_id, sess.id, host, players)
    row = await create_definition(tenant_id, f"pf-{uuid.uuid4().hex[:6]}", "PF", _flow(threshold))
    dsl = ProcessDefinitionDSL.model_validate(row.definition)
    await start_session(tenant_id, sess.id, dsl, row.id, row.version)
    by_principal: dict[uuid.UUID, str] = {}
    for pid, name in zip([host, *players], ["Host", *names], strict=True):
        persona = await get_persona(tenant_id, pid)
        assert persona is not None
        by_principal[persona.principal_id] = name
    return tenant_id, workspace_id, sess.id, dsl, by_principal


def _ctx(tenant_id: uuid.UUID, session_id: uuid.UUID, seq: int) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id, persona_id=uuid.uuid4(), session_id=session_id, turn_event_seq=seq
    )


async def _pick_tool(
    tenant_id: uuid.UUID, session_id: uuid.UUID, dsl: ProcessDefinitionDSL, name: str
) -> Any:
    """The put_forward tool as the turn would build it for ``name`` -- or None when the
    turn would not offer it."""
    assert dsl.participants is not None
    pick = dsl.phases["talk"].picks[0]
    views = await parts.participant_views(tenant_id, session_id)
    me = next(v for v in views if v.name == name)
    if not parts.pick_available(pick, views, me, state={}, settings={}):
        return None
    targets = parts.pick_targets(pick, views, me, state={}, settings={})
    return parts.make_pick_tool(
        tenant_id=tenant_id,
        session_id=session_id,
        spec=dsl.participants,
        pick=pick,
        actor=me,
        state={},
        settings={},
        targets=targets,
    )


async def test_a_pick_changes_the_record_once(db_available: None) -> None:
    tenant_id, _ws, session_id, dsl, _names = await _game()
    tool = await _pick_tool(tenant_id, session_id, dsl, "Ada")
    assert tool is not None
    spec, handler = tool
    assert spec.name == "put_forward"
    assert spec.parameters["properties"]["choice"]["enum"] == ["Bo", "Cy", "Di"]  # not herself

    refused = await handler({"choice": "Zed"}, _ctx(tenant_id, session_id, 50))
    assert "not a valid choice" in refused.content

    done = await handler({"choice": "Cy"}, _ctx(tenant_id, session_id, 50))
    assert json.loads(done.content) == {"picked": "Cy"} and done.end_turn
    # the same call again in the same turn -- a second pick, or a resumed turn -- is
    # answered, not applied
    again = await handler({"choice": "Di"}, _ctx(tenant_id, session_id, 50))
    assert "already" in again.content

    views = {v.name: v.vars for v in await parts.participant_views(tenant_id, session_id)}
    assert views["Cy"]["put_forward"] is True and views["Cy"]["put_forward_by"] == "Ada"
    assert views["Ada"]["has_picked"] is True
    assert views["Di"]["put_forward"] is False
    async with tenant_scope(tenant_id) as session:
        causes = (
            await session.execute(
                select(ParticipantStateChangeRow.cause, ParticipantStateChangeRow.cause_ref).where(
                    ParticipantStateChangeRow.session_id == session_id,
                    ParticipantStateChangeRow.cause == parts.PICK_CAUSE,
                )
            )
        ).all()
    assert sorted(causes) == [("pick", "put_forward:50")] * 3

    # `available` reads the var the pick set: Ada is not offered the tool again, and the
    # one already put forward is no longer a target for anyone
    assert await _pick_tool(tenant_id, session_id, dsl, "Ada") is None
    bo = await _pick_tool(tenant_id, session_id, dsl, "Bo")
    assert bo is not None and bo[0].parameters["properties"]["choice"]["enum"] == ["Ada", "Di"]
    # a tool built earlier in the turn still decides on the record as it is now
    stale = await handler({"choice": "Cy"}, _ctx(tenant_id, session_id, 51))
    assert "cannot do this now" in stale.content

    # the table sees who was put forward, and by whom: a public var
    assert dsl.participants is not None
    block = parts.render_participants_block(
        dsl.participants,
        await parts.participant_views(tenant_id, session_id),
        uuid.uuid4(),
        viewer_is_supervisor=False,
    )
    assert "Cy — status: in, put_forward: True, put_forward_by: Ada" in block


async def _play(
    threshold: str,
    picks: dict[str, str],
    votes: dict[str, str],
    settle: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    """Run the flow to its end: in ``talk`` each name in ``picks`` puts its value forward
    through the real tool; in each vote every participant votes as told through the real
    ballot tool. Returns the session state, the final vars and the phases entered."""
    tenant_id, workspace_id, session_id, dsl, names = await _game(threshold)
    phases: list[str] = []
    seq = iter(range(1000, 2000))

    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        name = names[actor.principal_id]
        if not phases or phases[-1] != ctx.phase_key:
            phases.append(ctx.phase_key)
        views = await parts.participant_views(tenant_id, session_id)
        me = next((v for v in views if v.principal_id == actor.principal_id), None)
        if ctx.phase_key == "talk" and name in picks:
            tool = await _pick_tool(tenant_id, session_id, dsl, name)
            assert tool is not None
            await tool[1]({"choice": picks[name]}, _ctx(tenant_id, session_id, next(seq)))
        ballot = ctx.phase.ballot
        if ballot is not None and me is not None:
            told = (settle or {}) if ballot.options is not None else votes
            if name in told:
                async with tenant_scope(tenant_id) as session:
                    key = parts.poll_key(ctx.phase_key, await _phase_entry_seq(session, session_id))
                targets = (
                    []
                    if ballot.options is not None
                    else parts.ballot_targets(ballot, views, me, state={}, settings={})
                )
                _spec, handler = parts.make_ballot_tool(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    key=key,
                    voter=me,
                    targets=targets,
                    options=ballot.options,
                )
                cast = await handler({"choice": told[name]}, _ctx(tenant_id, session_id, 0))
                assert json.loads(cast.content).get("recorded"), cast.content
        return ActorTurnResult(content_md=f"{name} speaks")

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
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        state = dict(row.state or {})
        results = [
            e.payload
            for e in (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.kind == "ballot_result",
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            ).scalars()
        ]
    final = {v.name: v.vars for v in await parts.participant_views(tenant_id, session_id)}
    return {"state": state, "results": results}, final, phases


async def test_nobody_put_forward_means_no_vote(db_available: None) -> None:
    record, final, phases = await _play("plurality", picks={}, votes={})
    assert phases == ["talk", "done"]
    assert record["results"] == []
    assert all(v["status"] == "in" for v in final.values())


async def test_only_those_put_forward_can_be_voted_for(db_available: None) -> None:
    record, final, phases = await _play(
        "plurality",
        picks={"Ada": "Bo", "Cy": "Di"},
        votes={"Ada": "Bo", "Bo": "Di", "Cy": "Bo", "Di": "Bo"},
    )
    assert phases == ["talk", "vote", "done"]
    (vote,) = record["results"]
    assert vote["chosen"] == "Bo" and vote["counts"] == {"Bo": 3, "Di": 1}
    assert "threshold" not in vote  # plurality, the default, is not written
    assert final["Bo"]["status"] == "out"


async def test_a_majority_short_of_half_chooses_nobody(db_available: None) -> None:
    # Two of four is not more than half: nobody is chosen, and it is not a tie either.
    record, final, phases = await _play(
        "majority",
        picks={"Ada": "Bo", "Cy": "Di"},
        votes={"Ada": "Bo", "Cy": "Bo", "Bo": "Di"},
    )
    (vote,) = record["results"]
    assert vote["chosen"] is None and vote["short"] is True and vote["tied"] == []
    assert vote["threshold"] == "majority" and vote["abstained"] == ["Di"]
    assert phases == ["talk", "vote", "done"]
    assert all(v["status"] == "in" for v in final.values())
    line = parts.render_ballot_result(vote, viewer_is_supervisor=False)
    assert "no choice won more than half of the votes" in line


async def test_unanimous_needs_every_expected_voter(db_available: None) -> None:
    record, final, _phases = await _play(
        "unanimous",
        picks={"Ada": "Bo"},
        votes={"Ada": "Bo", "Bo": "Bo", "Cy": "Bo", "Di": "Bo"},
    )
    assert record["results"][0]["chosen"] == "Bo" and final["Bo"]["status"] == "out"

    record, final, _phases = await _play(
        "unanimous",
        picks={"Ada": "Bo"},
        votes={"Ada": "Bo", "Bo": "Bo", "Cy": "Bo"},  # Di abstains
    )
    assert record["results"][0]["short"] is True and final["Bo"]["status"] == "in"


async def test_a_vote_between_options_writes_state_and_steers_the_gates(
    db_available: None,
) -> None:
    tied = {"Ada": "Bo", "Bo": "Di", "Cy": "Bo", "Di": "Di"}
    record, final, phases = await _play(
        "plurality",
        picks={"Ada": "Bo", "Cy": "Di"},
        votes=tied,
        settle={"Ada": "yes", "Bo": "no", "Cy": "yes", "Di": "yes"},
    )
    assert phases == ["talk", "vote", "settle", "all_out", "done"]
    settle = record["results"][1]
    assert settle["chosen"] == "yes" and settle["options"] == ["yes", "no"]
    assert settle["counts"] == {"yes": 3, "no": 1}
    assert settle["votes"]["Bo"] == "no"
    assert record["state"]["settle"] == "yes"
    assert final["Bo"]["status"] == "out" and final["Di"]["status"] == "out"
    assert final["Ada"]["status"] == "in"
    line = parts.render_ballot_result(settle, viewer_is_supervisor=False)
    assert line.startswith("- settle: 'yes' won (yes 3, no 1)")

    # an even split between the options is a tie: nothing won, state says so
    record, final, phases = await _play(
        "plurality",
        picks={"Ada": "Bo", "Cy": "Di"},
        votes=tied,
        settle={"Ada": "yes", "Bo": "no", "Cy": "no", "Di": "yes"},
    )
    assert phases == ["talk", "vote", "settle", "done"]
    assert record["state"]["settle"] == "" and record["results"][1]["tied"] == ["yes", "no"]
    assert final["Bo"]["status"] == "in" and final["Di"]["status"] == "in"
