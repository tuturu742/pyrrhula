"""Roles inside a team, private checks, run-offs and per-phase participant effects.

A small hidden-team flow in neutral words: ``side`` is dealt ``covert`` to two of four,
then ``role`` is dealt ``leader`` to one of the covert and ``seer`` to one of the open
(a deal narrowed by ``where``). The seer privately checks someone (``reveal: voters`` and
``disclose``); everyone then votes, a tie marks its candidates (``on_tied``) for a
run-off, and each round's marks are cleared by ``participant_effects``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.agents.authoring import get_persona
from core.agents.scheduling import make_persona_candidate_resolver
from core.agents.seed import seed_dev_agent
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
from core.sessions.models import SessionEventRow
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_VIS = {
    "knowledge_classes": ["misc"],
    "scopes": ["workspace_public"],
    "entity_fields": "all",
    "secrets": "none",
}
_IN = "p.status == 'in'"
_ANY_TIED = "size(participants.filter(p, p.tied)) > 0"


def _actors(where: str) -> list[dict[str, Any]]:
    return [{"persona_type": "participant", "mode": "generate", "order": "seated", "where": where}]


def _flow() -> dict[str, Any]:
    return {
        "name": "Checks",
        "vocabulary_overlay": "neutral",
        "participants": {
            "vars": {
                "side": {"type": "string", "default": "open"},
                "role": {"type": "string", "default": "none"},
                "status": {"type": "string", "default": "in"},
                "mark": {"type": "boolean", "default": False},
                "struck": {"type": "boolean", "default": False},
                "tied": {"type": "boolean", "default": False},
            },
            "deal": [
                {"var": "side", "value": "covert", "count": 2},
                {"var": "role", "value": "leader", "count": 1, "where": "p.side == 'covert'"},
                {"var": "role", "value": "seer", "count": 1, "where": "p.side == 'open'"},
            ],
            "hidden": ["side", "role", "mark", "struck"],
            "team_var": "side",
            "team_values": ["covert"],
        },
        "initial_phase": "check",
        "phases": {
            "check": {
                "label_key": "phase.check",
                "actors": _actors("p.role == 'seer' && " + _IN),
                "visibility": _VIS,
                "audience": {"members": "false"},
                "ballot": {
                    "targets": "p.id != voter.id",
                    "reveal": "voters",
                    "disclose": {"covert": "p.side == 'covert'"},
                    "on_chosen": [{"set": "mark", "to": "true"}],
                },
                "on_complete": "vote",
            },
            "vote": {
                "label_key": "phase.vote",
                "actors": _actors(_IN),
                "visibility": _VIS,
                "participant_effects": [{"set": "tied", "to": "false"}],
                "ballot": {
                    "targets": "p.id != voter.id && p.status == 'in'",
                    "tie": "none",
                    "on_tied": [{"set": "tied", "to": "true"}],
                    "on_chosen": [{"set": "status", "to": "'out'"}],
                },
                "gates": [{"when": _ANY_TIED, "to": "runoff"}, {"else": True, "to": "done"}],
            },
            "runoff": {
                "label_key": "phase.runoff",
                "actors": _actors(_IN),
                "visibility": _VIS,
                "participant_effects": [{"set": "tied", "to": "false", "where": "p.tied"}],
                "ballot": {
                    "targets": "p.id != voter.id && p.tied",
                    "on_chosen": [
                        {"set": "status", "to": "'out'"},
                        {"set": "struck", "to": "true"},
                    ],
                },
                "on_complete": "done",
            },
            "done": {
                "label_key": "phase.done",
                "actors": [{"persona_type": "supervisor", "mode": "generate"}],
                "visibility": _VIS,
            },
        },
    }


def test_flow_validates() -> None:
    dsl, issues = validate_raw(_flow())
    assert dsl is not None and issues == []


def test_validator_refuses_a_voter_where_the_engine_binds_none() -> None:
    flow = _flow()
    flow["phases"]["check"]["ballot"]["disclose"]["mine"] = "voter.side == 'open'"
    flow["phases"]["vote"]["participant_effects"][0]["set"] = "nope"
    flow["participants"]["deal"][1]["where"] = "p.no_such_var"
    _dsl, issues = validate_raw(flow)
    paths = {i.field_path for i in issues}
    assert "phases.check.ballot.disclose.mine" in paths
    assert "phases.vote.participant_effects[0].set" in paths
    assert "participants.deal[1].where" in paths


async def _game() -> tuple[
    uuid.UUID, uuid.UUID, uuid.UUID, ProcessDefinitionDSL, dict[uuid.UUID, str]
]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"chk-{uuid.uuid4().hex[:8]}")
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
    row = await create_definition(tenant_id, f"chk-{uuid.uuid4().hex[:6]}", "Checks", _flow())
    dsl = ProcessDefinitionDSL.model_validate(row.definition)
    await start_session(tenant_id, sess.id, dsl, row.id, row.version)
    by_principal: dict[uuid.UUID, str] = {}
    for pid, name in zip([host, *players], ["Host", *names], strict=True):
        persona = await get_persona(tenant_id, pid)
        assert persona is not None
        by_principal[persona.principal_id] = name
    return tenant_id, workspace_id, sess.id, dsl, by_principal


async def test_a_role_is_dealt_inside_its_team(db_available: None) -> None:
    for _ in range(3):  # different sessions draw differently; the rule holds for each
        tenant_id, _ws, session_id, _dsl, _names = await _game()
        views = await parts.participant_views(tenant_id, session_id)
        leaders = [v for v in views if v.vars["role"] == "leader"]
        seers = [v for v in views if v.vars["role"] == "seer"]
        assert len(leaders) == 1 and leaders[0].vars["side"] == "covert"
        assert len(seers) == 1 and seers[0].vars["side"] == "open"
        assert sum(v.vars["side"] == "covert" for v in views) == 2


async def test_a_private_check_a_runoff_and_round_marks(db_available: None) -> None:
    tenant_id, workspace_id, session_id, dsl, names = await _game()
    assert dsl.participants is not None
    views = await parts.participant_views(tenant_id, session_id)
    by_seat = sorted(views, key=lambda v: v.seat or 0)
    seer = next(v for v in views if v.vars["role"] == "seer")
    checked = next(v for v in views if v.vars["side"] == "covert")
    x, y = by_seat[0], by_seat[1]

    def pick(phase: str, voter: parts.ParticipantView) -> parts.ParticipantView:
        if phase == "check":
            return checked
        if phase == "vote":  # x and y vote for each other, the rest split: a 2-2 tie
            if voter.session_persona_id == x.session_persona_id:
                return y
            if voter.session_persona_id == y.session_persona_id:
                return x
            return x if voter.seat == 3 else y
        # run-off: y votes x, x votes y, the rest x
        return y if voter.session_persona_id == x.session_persona_id else x

    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        if ctx.phase.ballot is not None:
            now = await parts.participant_views(tenant_id, session_id)
            voter = next(v for v in now if v.principal_id == actor.principal_id)
            async with tenant_scope(tenant_id) as session:
                key = parts.poll_key(ctx.phase_key, await _phase_entry_seq(session, session_id))
            choice = pick(ctx.phase_key, voter)
            if ctx.phase_key == "runoff":
                tied = {v.session_persona_id for v in now if v.vars["tied"]}
                assert tied == {x.session_persona_id, y.session_persona_id}
            assert await parts.cast_ballot(
                tenant_id, session_id, key, voter.session_persona_id, choice.session_persona_id
            )
        return ActorTurnResult(content_md=f"{names[actor.principal_id]} speaks")

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
    check, vote, runoff = results
    # The check: found by the engine, from the record, addressed to the seer alone.
    assert check["disclosed"] == {"covert": True}
    assert check["audience"] == [str(seer.principal_id)]
    # The tie marked its two candidates; the run-off cleared them and chose x.
    assert vote["chosen"] is None and sorted(vote["tied"]) == sorted([x.name, y.name])
    assert runoff["chosen"] == x.name
    final = {v.name: v.vars for v in await parts.participant_views(tenant_id, session_id)}
    assert not any(v["tied"] for v in final.values())
    assert final[x.name]["status"] == "out"

    others = [v for v in views if v.session_persona_id != seer.session_persona_id]
    seer_block = parts.render_participants_block(
        dsl.participants, views, seer.principal_id, viewer_is_supervisor=False, results=results
    )
    assert f"check: {checked.name} was chosen" in seer_block and "covert = yes" in seer_block
    for other in others:
        block = parts.render_participants_block(
            dsl.participants, views, other.principal_id, viewer_is_supervisor=False, results=results
        )
        assert "check:" not in block and "covert = " not in block
    host_block = parts.render_participants_block(
        dsl.participants, views, uuid.uuid4(), viewer_is_supervisor=True, results=results
    )
    assert "check:" not in host_block  # not even the facilitator's model reads a private check

    # The run-off's change to the hidden ``struck`` reaches x itself and the supervisor, not
    # the rest of the table; the public change to ``status`` reaches everyone.
    line = next(r for r in results if r["phase"] == "runoff")
    bystander = next(v for v in views if v.session_persona_id not in (x.session_persona_id,))
    public = parts.render_ballot_result(
        line, viewer_is_supervisor=False, viewer_principal_id=bystander.principal_id
    )
    assert "status in -> out" in public and "struck" not in public
    own = parts.render_ballot_result(
        line, viewer_is_supervisor=False, viewer_principal_id=x.principal_id
    )
    assert "struck False -> True" in own
    assert "struck False -> True" in parts.render_ballot_result(line, viewer_is_supervisor=True)
