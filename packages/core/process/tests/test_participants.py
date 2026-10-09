"""Participants, seats, a secret deal, sealed ballots and private audiences.

The flow here is a small hidden-team game in neutral words: a ``side`` var dealt
``covert`` to a minority, discussion in seat order among those still ``in``, a vote that
puts the most-voted participant ``out`` and reveals its side, and gates over the roster.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
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
from core.process.live_session import _load_conversation, _load_conversation_with_boundary
from core.process.scheduler import make_default_candidate_resolver, make_scheduler
from core.process.skeleton import create_session
from core.sessions.lifecycle import set_session_roster
from core.sessions.models import (
    BallotRow,
    MessageRow,
    ParticipantStateChangeRow,
    SessionEventRow,
    SessionPersonaRow,
    SessionRow,
)
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

_VIS = {
    "knowledge_classes": ["misc"],
    "scopes": ["workspace_public"],
    "entity_fields": "all",
    "secrets": "none",
}
_IN = "p.status == 'in'"
_COVERT_IN = "size(participants.filter(p, p.side == 'covert' && p.status == 'in'))"
_OPEN_IN = "size(participants.filter(p, p.side == 'open' && p.status == 'in'))"


def _flow() -> dict[str, Any]:
    return {
        "name": "Hidden team",
        "vocabulary_overlay": "neutral",
        "settings": {
            "covert_count": {"type": "integer", "default": 1, "min": 1, "max": 3},
            "talk_laps": {"type": "integer", "default": 1, "min": 1, "max": 5},
        },
        "participants": {
            "vars": {
                "side": {"type": "string", "default": "open"},
                "status": {"type": "string", "default": "in"},
                "revealed": {"type": "boolean", "default": False},
            },
            "deal": [{"var": "side", "value": "covert", "count": {"setting": "covert_count"}}],
            "hidden": ["side"],
            "team_var": "side",
            "team_values": ["covert"],
            "reveal_var": "revealed",
        },
        "initial_phase": "talk",
        "phases": {
            "talk": {
                "label_key": "phase.talk",
                "actors": [
                    {
                        "persona_type": "participant",
                        "mode": "generate",
                        "order": "seated",
                        "where": _IN,
                        "laps": {"setting": "talk_laps"},
                    }
                ],
                "visibility": _VIS,
                "on_complete": "vote",
            },
            "vote": {
                "label_key": "phase.vote",
                "actors": [
                    {
                        "persona_type": "participant",
                        "mode": "generate",
                        "order": "seated",
                        "where": _IN,
                    }
                ],
                "visibility": _VIS,
                "audience": {"members": "false"},
                "ballot": {
                    "targets": "p.id != voter.id && p.status == 'in'",
                    "tie": "none",
                    "on_chosen": [
                        {"set": "status", "to": "'out'"},
                        {"set": "revealed", "to": "true"},
                    ],
                },
                "gates": [
                    {"when": f"{_COVERT_IN} == 0", "to": "done"},
                    {"when": f"{_COVERT_IN} >= {_OPEN_IN}", "to": "done"},
                    {"else": True, "to": "talk"},
                ],
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


def test_validator_names_an_unknown_setting_and_a_bad_participant_expression() -> None:
    flow = _flow()
    flow["phases"]["talk"]["actors"][0]["laps"] = {"setting": "nope"}
    flow["phases"]["vote"]["ballot"]["targets"] = "p.no_such_var == 1"
    _dsl, issues = validate_raw(flow)
    paths = {i.field_path for i in issues}
    assert "phases.talk.actors[0].laps" in paths
    assert "phases.vote.ballot.targets" in paths


def test_settings_merge_and_refuse() -> None:
    dsl = ProcessDefinitionDSL.model_validate(_flow())
    assert parts.resolve_settings(dsl.settings, None) == {"covert_count": 1, "talk_laps": 1}
    assert parts.resolve_settings(dsl.settings, {"talk_laps": "3"})["talk_laps"] == 3
    with pytest.raises(parts.ParticipantError, match="unknown"):
        parts.resolve_settings(dsl.settings, {"talk_lap": 3})
    with pytest.raises(parts.ParticipantError, match="above"):
        parts.resolve_settings(dsl.settings, {"covert_count": 9})


async def _game(
    settings: dict[str, object] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, ProcessDefinitionDSL, dict[uuid.UUID, str]]:
    tenant_id, _owner, workspace_id = await seed_dev_tenant(slug=f"parts-{uuid.uuid4().hex[:8]}")
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
    row = await create_definition(tenant_id, f"ht-{uuid.uuid4().hex[:6]}", "HT", _flow())
    dsl = ProcessDefinitionDSL.model_validate(row.definition)
    await start_session(tenant_id, sess.id, dsl, row.id, row.version, settings_overrides=settings)
    by_principal: dict[uuid.UUID, str] = {}
    for pid, name in zip([host, *players], ["Host", *names], strict=True):
        persona = await get_persona(tenant_id, pid)
        assert persona is not None
        by_principal[persona.principal_id] = name
    return tenant_id, workspace_id, sess.id, dsl, by_principal


async def test_start_seats_everyone_and_deals_in_secret(db_available: None) -> None:
    tenant_id, _ws, session_id, _dsl, _names = await _game({"covert_count": 1})
    views = await parts.participant_views(tenant_id, session_id)
    assert sorted(v.seat for v in views) == [1, 2, 3, 4]
    assert sum(v.vars["side"] == "covert" for v in views) == 1
    assert all(v.vars["status"] == "in" and v.vars["revealed"] is False for v in views)
    async with tenant_scope(tenant_id) as session:
        seated = (
            await session.execute(
                select(SessionEventRow.payload).where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.kind == "participants_seated",
                )
            )
        ).scalar_one()
        recorded = (
            await session.execute(
                select(ParticipantStateChangeRow).where(
                    ParticipantStateChangeRow.session_id == session_id
                )
            )
        ).scalars()
        assert len(list(recorded)) == 12  # 4 participants x 3 vars
        row = await session.get(SessionRow, session_id)
        assert row is not None and row.settings == {"covert_count": 1, "talk_laps": 1}
    # The public record says how many were dealt, never to whom.
    assert seated["dealt"] == [{"var": "side", "value": "covert", "count": 1}]
    assert "Ada" not in str(seated["dealt"])


async def test_hidden_values_reach_only_who_may_know(db_available: None) -> None:
    tenant_id, _ws, session_id, dsl, _names = await _game({"covert_count": 2})
    assert dsl.participants is not None
    views = await parts.participant_views(tenant_id, session_id)
    covert = [v for v in views if v.vars["side"] == "covert"]
    open_ = [v for v in views if v.vars["side"] == "open"]

    open_block = parts.render_participants_block(
        dsl.participants, views, open_[0].principal_id, viewer_is_supervisor=False
    )
    assert "covert" not in open_block  # not even the word: exclusion, not instruction
    assert "side: open" in open_block.splitlines()[[v.name for v in views].index(open_[0].name) + 1]

    covert_block = parts.render_participants_block(
        dsl.participants, views, covert[0].principal_id, viewer_is_supervisor=False
    )
    assert covert_block.count("side: covert") == 2  # itself and its teammate
    assert "side: open" not in covert_block

    host_block = parts.render_participants_block(
        dsl.participants, views, uuid.uuid4(), viewer_is_supervisor=True
    )
    assert host_block.count("side: covert") == 2 and host_block.count("side: open") == 2


async def test_a_full_game_votes_reveals_and_ends(db_available: None) -> None:
    """Every vote goes against the covert participant, so the open side wins on the first
    vote; the ballot result, the reveal and the final outcome are all recorded."""
    tenant_id, workspace_id, session_id, dsl, names = await _game({"covert_count": 1})
    views = await parts.participant_views(tenant_id, session_id)
    covert = next(v for v in views if v.vars["side"] == "covert")
    spoke: list[tuple[str, str]] = []

    async def execute_turn(actor: ActorRef, ctx: InterpreterContext) -> ActorTurnResult:
        name = names[actor.principal_id]
        spoke.append((ctx.phase_key, name))
        if ctx.phase.ballot is not None:
            now = await parts.participant_views(tenant_id, session_id)
            voter = next(v for v in now if v.principal_id == actor.principal_id)
            async with tenant_scope(tenant_id) as session:
                key = parts.poll_key(ctx.phase_key, await _phase_entry_seq(session, session_id))
            target = (
                covert
                if voter.session_persona_id != covert.session_persona_id
                else next(v for v in now if v.session_persona_id != covert.session_persona_id)
            )
            assert await parts.cast_ballot(
                tenant_id, session_id, key, voter.session_persona_id, target.session_persona_id
            )
            # a second ballot in the same vote is refused, not counted
            assert not await parts.cast_ballot(
                tenant_id, session_id, key, voter.session_persona_id, None
            )
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

    seat_order = [v.name for v in sorted(views, key=lambda v: v.seat or 0)]
    assert [n for phase, n in spoke if phase == "talk"] == seat_order
    assert [n for phase, n in spoke if phase == "vote"] == seat_order
    assert [n for phase, n in spoke if phase == "done"] == ["Host"]

    async with tenant_scope(tenant_id) as session:
        events = {
            e.kind: e.payload
            for e in (
                await session.execute(
                    select(SessionEventRow).where(SessionEventRow.session_id == session_id)
                )
            ).scalars()
        }
        ballots = list(
            (
                await session.execute(select(BallotRow).where(BallotRow.session_id == session_id))
            ).scalars()
        )
    assert len(ballots) == 4
    tally = events["ballot_result"]
    assert tally["chosen"] == covert.name and tally["counts"][covert.name] == 3
    assert tally["changes"] == [
        {"name": covert.name, "var": "status", "old": "in", "new": "out"},
        {"name": covert.name, "var": "revealed", "old": False, "new": True},
    ]
    outcome = events["outcome"]
    final = {p["name"]: p["vars"] for p in outcome["participants"]}
    assert final[covert.name] == {"side": "covert", "status": "out", "revealed": True}
    # once revealed, the side is public to everyone
    assert dsl.participants is not None
    after = await parts.participant_views(tenant_id, session_id)
    open_viewer = next(v for v in after if v.vars["side"] == "open")
    block = parts.render_participants_block(
        dsl.participants, after, open_viewer.principal_id, viewer_is_supervisor=False
    )
    assert f"{covert.name} — side: covert" in block

    # Phase markers in the transcript sit exactly where each marked phase began.
    history, _ = await _load_conversation_with_boundary(
        tenant_id,
        session_id,
        open_viewer.principal_id,
        markers={"talk": "Talk opens", "vote": "The vote opens"},
        initial_phase="talk",
    )
    lines = [m["content"] for m in history]
    assert lines[0] == "[Talk opens]"
    vote_at = lines.index("[The vote opens]")
    assert all("speaks" in line for line in lines[1:vote_at])
    assert len(lines[1:vote_at]) == 4


def test_a_marker_reads_state_and_nothing_else() -> None:
    from core.process.markers import render_marker

    assert render_marker("Night {day} falls.", {"day": 2}) == "Night 2 falls."
    assert render_marker("{missing} {day.__class__}", {"day": 2}) == "{missing} {day.__class__}"


async def test_a_private_message_never_reaches_a_non_member(db_available: None) -> None:
    tenant_id, _ws, session_id, _dsl, names = await _game()
    views = await parts.participant_views(tenant_id, session_id)
    author, member, outsider = views[0], views[1], views[2]
    canary = f"CANARY-{uuid.uuid4().hex}"
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        assert row is not None
        seq = row.next_event_seq
        row.next_event_seq = seq + 1
        session.add(
            MessageRow(
                tenant_id=tenant_id,
                session_id=session_id,
                event_seq=seq,
                author_principal_id=author.principal_id,
                role="assistant",
                content_md=canary,
                audience=[author.principal_id, member.principal_id],
            )
        )
    for viewer, sees in ((author, True), (member, True), (outsider, False), (None, False)):
        history = await _load_conversation(
            tenant_id, session_id, viewer.principal_id if viewer else None
        )
        assert (canary in str(history)) is sees, viewer


async def test_where_skips_a_participant_who_is_out(db_available: None) -> None:
    tenant_id, workspace_id, session_id, dsl, names = await _game()
    views = await parts.participant_views(tenant_id, session_id)
    gone = views[1]
    async with tenant_scope(tenant_id) as session:
        sp = await session.get(SessionPersonaRow, gone.session_persona_id)
        assert sp is not None
        sp.vars = {**sp.vars, "status": "out"}
    resolver = make_default_candidate_resolver(
        workspace_id,
        persona_candidate_resolver=make_persona_candidate_resolver(tenant_id, workspace_id),
    )
    next_actor = make_scheduler(resolver)
    ctx = InterpreterContext(
        tenant_id, session_id, "talk", dsl.phases["talk"], {}, settings={"talk_laps": 1}
    )
    order: list[str] = []
    for _ in range(5):
        actor = await next_actor(ctx)
        if actor is None:
            break
        order.append(names[actor.principal_id])
        async with tenant_scope(tenant_id) as session:  # a turn landed
            row = await session.get(SessionRow, session_id)
            assert row is not None
            row.next_event_seq += 1
    assert gone.name not in order and len(order) == 3


def test_an_outcome_only_vote_never_names_its_voters() -> None:
    payload: dict[str, Any] = {
        "phase": "covert_vote",
        "votes": {"Ada": "Bo"},
        "abstained": [],
        "tied": [],
        "chosen": "Bo",
        "changes": [{"name": "Bo", "var": "status", "old": "in", "new": "out"}],
        "reveal": "outcome",
    }
    public = parts.render_ballot_result(payload, viewer_is_supervisor=False)
    assert "Bo was chosen" in public and "Ada" not in public
    assert "Ada -> Bo" in parts.render_ballot_result(payload, viewer_is_supervisor=True)
    payload["reveal"] = "votes"
    assert "Ada -> Bo" in parts.render_ballot_result(payload, viewer_is_supervisor=False)
