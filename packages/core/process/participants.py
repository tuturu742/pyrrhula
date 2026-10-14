"""Participants: per-participant variables, seats, a secret deal, sealed ballots and
private audiences, for flows whose roster plays roles.

Everything here is generic. A flow declares ``participants:`` (typed variables, which of
them are hidden, which are dealt by draw) and phases declare ``ballot:``, ``picks:`` and
``audience:``; this module knows nothing about what the values *mean* -- "mafia",
"alive" and "town" are pack words a flow writes, never words this module writes.

Who knows what is decided here and enforced by exclusion, the same way secrets are: a
hidden variable is rendered into a viewer's context only if the viewer may see it (its
own, a teammate's, the supervisor's view of everyone, or one made public by the reveal
flag). A message outside a reader's audience never enters that reader's history. Nothing
is "described and then withheld by instruction".

Randomness (the deal, the seat order, a tied vote) is seeded with HMAC over the session's
own ``roll_secret`` -- the same session secret the randomizer uses -- so the draw is
reproducible for an audit once the secret is revealed, and unpredictable to anyone
reading the code and the session id before then.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets as _secrets
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import celpy
from celpy.adapter import json_to_cel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.agents.models import Persona
from core.agents.tools import ToolContext, ToolHandler, ToolResult
from core.ports.model_provider import ToolSpec
from core.process.dsl.schema import (
    BallotSpec,
    ParticipantEffectSpec,
    ParticipantsSpec,
    PickSpec,
    ProcessDefinitionDSL,
    SettingRef,
    SettingSpec,
)
from core.process.models import ProcessDefinitionRow
from core.sessions.models import (
    BallotRow,
    ParticipantStateChangeRow,
    SessionEventRow,
    SessionPersonaRow,
    SessionRow,
)
from core.tenancy.scope import tenant_scope

_cel_env = celpy.Environment()

_COERCE: dict[str, Any] = {"integer": int, "number": float, "string": str, "boolean": bool}


class ParticipantError(Exception):
    """A participants declaration cannot be applied to this session (too few seats for a
    deal, a setting outside its range, a CEL failure)."""


# ── settings ────────────────────────────────────────────────────────────────────────


def resolve_settings(
    declared: Mapping[str, SettingSpec], overrides: Mapping[str, object] | None
) -> dict[str, object]:
    """The definition's setting defaults merged with a session's overrides, each checked
    against its declared type and range. An unknown override is refused, not ignored: a
    typo would otherwise run the experiment with the default and record that as the
    choice."""
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise ParticipantError(f"unknown settings: {', '.join(unknown)}")
    resolved: dict[str, object] = {}
    for name, spec in declared.items():
        raw = overrides.get(name, spec.default)
        try:
            value = _COERCE[spec.type](raw)
        except (TypeError, ValueError) as exc:
            raise ParticipantError(f"setting {name!r}: {raw!r} is not a {spec.type}") from exc
        if spec.type in ("integer", "number"):
            if spec.min is not None and float(value) < spec.min:
                raise ParticipantError(f"setting {name!r}: {value} is below {spec.min}")
            if spec.max is not None and float(value) > spec.max:
                raise ParticipantError(f"setting {name!r}: {value} is above {spec.max}")
        resolved[name] = value
    return resolved


def setting_number(value: int | SettingRef | None, settings: Mapping[str, object]) -> int | None:
    """An int written in place, or one read from a setting."""
    if value is None:
        return None
    if isinstance(value, SettingRef):
        raw = settings.get(value.setting)
        if raw is None:
            raise ParticipantError(f"unknown setting {value.setting!r}")
        return int(cast(Any, raw))
    return int(value)


# ── seeds ───────────────────────────────────────────────────────────────────────────


def _ensure_roll_secret(row: SessionRow) -> str:
    if row.roll_secret is None:
        row.roll_secret = _secrets.token_hex(32)
    return row.roll_secret


def seeded_rng(row: SessionRow, purpose: str) -> random.Random:
    digest = hmac.new(
        _ensure_roll_secret(row).encode(), f"{row.id}|{purpose}".encode(), hashlib.sha256
    ).digest()
    return random.Random(int.from_bytes(digest, "big"))


# ── views ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParticipantView:
    """One roster participant as the engine sees it -- every variable, hidden or not.
    Never rendered to a model whole; ``visible_vars`` decides what a viewer gets."""

    session_persona_id: uuid.UUID
    persona_id: uuid.UUID
    principal_id: uuid.UUID
    name: str
    seat: int | None
    vars: dict[str, Any] = field(default_factory=dict)

    def cel(self) -> dict[str, Any]:
        """The ``p`` a CEL expression sees: identity, seat and every variable."""
        return {
            "id": str(self.principal_id),
            "name": self.name,
            "seat": self.seat or 0,
            **self.vars,
        }


async def load_views(session: AsyncSession, session_id: uuid.UUID) -> list[ParticipantView]:
    """The session's non-supervisor roster, in seat order (creation order for seatless)."""
    rows = (
        await session.execute(
            select(SessionPersonaRow, Persona.name, Persona.principal_id)
            .join(Persona, Persona.id == SessionPersonaRow.persona_id)
            .where(
                SessionPersonaRow.session_id == session_id,
                SessionPersonaRow.is_supervisor.is_(False),
            )
            .order_by(SessionPersonaRow.seat.nulls_last(), SessionPersonaRow.created_at)
        )
    ).all()
    return [
        ParticipantView(
            session_persona_id=sp.id,
            persona_id=sp.persona_id,
            principal_id=principal_id,
            name=name,
            seat=sp.seat,
            vars=dict(sp.vars or {}),
        )
        for sp, name, principal_id in rows
    ]


async def participant_views(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[ParticipantView]:
    async with tenant_scope(tenant_id) as session:
        return await load_views(session, session_id)


async def supervisor_principals(session: AsyncSession, session_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(Persona.principal_id)
            .join(SessionPersonaRow, SessionPersonaRow.persona_id == Persona.id)
            .where(
                SessionPersonaRow.session_id == session_id,
                SessionPersonaRow.is_supervisor.is_(True),
            )
        )
    ).scalars()
    return list(rows)


async def load_session_definition(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> ProcessDefinitionDSL | None:
    """The definition this session is pinned to, or None (skeleton sessions)."""
    async with tenant_scope(tenant_id) as session:
        row = await session.get(SessionRow, session_id)
        if row is None or row.process_definition_id is None:
            return None
        definition_row = await session.get(ProcessDefinitionRow, row.process_definition_id)
        if definition_row is None:
            return None
        return ProcessDefinitionDSL.model_validate(definition_row.definition)


# ── CEL ─────────────────────────────────────────────────────────────────────────────


def activation(
    *,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
    views: Sequence[ParticipantView],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "state": dict(state),
        "settings": dict(settings),
        "participants": [v.cel() for v in views],
        **extra,
    }


def eval_cel(expression: str, bindings: Mapping[str, Any]) -> Any:
    try:
        program = _cel_env.program(_cel_env.compile(expression))
        return program.evaluate(json_to_cel(dict(bindings)))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 -- any CEL failure is a participants error
        raise ParticipantError(f"CEL expression {expression!r} failed: {exc}") from exc


def matches(expression: str | None, bindings: Mapping[str, Any]) -> bool:
    return True if expression is None else bool(eval_cel(expression, bindings))


# ── setup: seats and the deal ───────────────────────────────────────────────────────


async def setup_participants(
    session: AsyncSession, row: SessionRow, spec: ParticipantsSpec, settings: Mapping[str, object]
) -> dict[str, Any]:
    """Seat the roster and deal the declared values, inside the caller's transaction
    (``start_session``). Returns the public summary for the transcript: seats and how
    many of each dealt value -- never who got what."""
    views = await load_views(session, row.id)
    if not views:
        raise ParticipantError("the session declares participants but has none on its roster")
    sp_rows = {
        sp.id: sp
        for sp in (
            await session.execute(
                select(SessionPersonaRow).where(
                    SessionPersonaRow.id.in_([v.session_persona_id for v in views])
                )
            )
        ).scalars()
    }

    order = list(views)
    if spec.seat_order == "drawn":
        seeded_rng(row, "seats").shuffle(order)
    for seat, view in enumerate(order, start=1):
        sp_rows[view.session_persona_id].seat = seat

    defaults = {name: _COERCE[v.type](v.default) for name, v in spec.vars.items()}
    current = {view.session_persona_id: dict(defaults) for view in views}

    dealt_summary: list[dict[str, Any]] = []
    undealt = [
        v.session_persona_id
        for v in sorted(order, key=lambda v: sp_rows[v.session_persona_id].seat or 0)
    ]
    seated = sorted(order, key=lambda v: sp_rows[v.session_persona_id].seat or 0)
    dealt_vars: dict[uuid.UUID, set[str]] = {v.session_persona_id: set() for v in views}
    for index, deal in enumerate(spec.deal):
        count = setting_number(deal.count, settings) or 0
        if deal.where is None:
            pool = list(undealt)
        else:
            # A role inside a team: anyone ``where`` admits who has not been dealt this
            # same var, judged on the values dealt so far.
            now = [
                ParticipantView(
                    session_persona_id=v.session_persona_id,
                    persona_id=v.persona_id,
                    principal_id=v.principal_id,
                    name=v.name,
                    seat=sp_rows[v.session_persona_id].seat,
                    vars=current[v.session_persona_id],
                )
                for v in seated
            ]
            base = activation(state=dict(row.state or {}), settings=settings, views=now)
            pool = [
                v.session_persona_id
                for v in now
                if deal.var not in dealt_vars[v.session_persona_id]
                and matches(deal.where, {**base, "p": v.cel()})
            ]
        if count > len(pool):
            raise ParticipantError(
                f"deal {deal.var}={deal.value!r} needs {count} participants; {len(pool)} left"
            )
        chosen = seeded_rng(row, f"deal:{index}").sample(pool, count)
        coerced = _COERCE[spec.vars[deal.var].type](deal.value)
        for sp_id in chosen:
            current[sp_id][deal.var] = coerced
            dealt_vars[sp_id].add(deal.var)
            if sp_id in undealt:
                undealt.remove(sp_id)
        dealt_summary.append({"var": deal.var, "value": coerced, "count": count})

    for sp_id, values in current.items():
        sp_rows[sp_id].vars = values
        for name, value in values.items():
            session.add(
                ParticipantStateChangeRow(
                    tenant_id=row.tenant_id,
                    session_id=row.id,
                    session_persona_id=sp_id,
                    var=name,
                    old_value=None,
                    new_value=value,
                    cause="setup",
                )
            )
    await session.flush()
    return {
        "seats": [
            {"seat": sp_rows[v.session_persona_id].seat, "name": v.name}
            for v in sorted(order, key=lambda v: sp_rows[v.session_persona_id].seat or 0)
        ],
        "dealt": dealt_summary,
    }


# ── who sees what ───────────────────────────────────────────────────────────────────


def visible_vars(
    spec: ParticipantsSpec,
    subject: ParticipantView,
    viewer: ParticipantView | None,
    *,
    viewer_is_supervisor: bool,
) -> dict[str, Any]:
    """The variables of ``subject`` that ``viewer`` may know. Non-hidden ones always;
    hidden ones only to the subject itself, the supervisor, a teammate, or anyone once the
    subject's reveal flag is set."""
    revealed = bool(spec.reveal_var and subject.vars.get(spec.reveal_var))
    out: dict[str, Any] = {}
    for name in spec.vars:
        if name not in spec.hidden:
            out[name] = subject.vars.get(name)
            continue
        if viewer_is_supervisor or revealed:
            out[name] = subject.vars.get(name)
            continue
        if viewer is not None and viewer.session_persona_id == subject.session_persona_id:
            out[name] = subject.vars.get(name)
            continue
        if viewer is not None and spec.team_var is not None:
            team = viewer.vars.get(spec.team_var)
            if team in spec.team_values and subject.vars.get(spec.team_var) == team:
                out[name] = subject.vars.get(name)
    return out


def render_participants_block(
    spec: ParticipantsSpec,
    views: Sequence[ParticipantView],
    viewer_principal_id: uuid.UUID,
    *,
    viewer_is_supervisor: bool,
    results: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The table as this viewer may know it, and the votes closed so far, rendered from
    the record (INV-7)."""
    viewer = next((v for v in views if v.principal_id == viewer_principal_id), None)
    shown_vars = [n for n in spec.vars if not (spec.reveal_var and n == spec.reveal_var)]
    lines = ["Participants, in seat order:"]
    for view in views:
        known = visible_vars(spec, view, viewer, viewer_is_supervisor=viewer_is_supervisor)
        facts = ", ".join(f"{name}: {known[name]}" for name in shown_vars if name in known)
        marker = " (you)" if viewer is not None and view.principal_id == viewer.principal_id else ""
        seat = f"{view.seat}. " if view.seat is not None else "- "
        lines.append(f"{seat}{view.name}{marker}" + (f" — {facts}" if facts else ""))
    if viewer is not None and spec.hidden:
        hidden_own = {n: viewer.vars.get(n) for n in spec.hidden if n != spec.reveal_var}
        lines.append(
            "Only you (and anyone a rule above lets see it) know your hidden values: "
            + ", ".join(f"{k} = {v}" for k, v in hidden_own.items())
            + "."
        )
    if viewer_is_supervisor and spec.hidden:
        lines.append("You see every participant's hidden values; the participants do not.")
    readable = [r for r in results if result_readable(r, viewer_principal_id)]
    if readable:
        lines.append("")
        lines.append("Vote results so far, oldest first:")
        lines.extend(
            render_ballot_result(
                r,
                viewer_is_supervisor=viewer_is_supervisor,
                viewer_principal_id=viewer_principal_id,
            )
            for r in readable
        )
    return "\n".join(lines)


def result_readable(payload: Mapping[str, Any], viewer_principal_id: uuid.UUID | None) -> bool:
    """Whether this viewer may read this closed vote at all. A ``reveal: voters`` result
    reaches its voters only -- not the supervisor, whose model could only leak it."""
    if payload.get("reveal") != "voters":
        return True
    return visible_to(payload.get("audience") or [], viewer_principal_id)


def render_ballot_result(
    payload: Mapping[str, Any],
    *,
    viewer_is_supervisor: bool,
    viewer_principal_id: uuid.UUID | None = None,
) -> str:
    """One closed vote as this viewer may read it: who was chosen, what changed and what
    the vote disclosed; each ballot too when the vote reveals votes (the supervisor
    always sees them). A change to a hidden var is shown only to whoever may know it:
    the supervisor, the participant changed, and the voters of a private vote. Callers
    check ``result_readable`` first."""
    phase = payload.get("phase", "vote")
    chosen = payload.get("chosen")
    private = payload.get("reveal") == "voters"
    if chosen and payload.get("options") is not None:
        counts = payload.get("counts") or {}
        tally_text = ", ".join(f"{o} {counts.get(o, 0)}" for o in payload["options"])
        line = f"- {phase}: {chosen!r} won ({tally_text})"
    elif chosen:
        shown = [
            c
            for c in payload.get("changes") or []
            if not c.get("hidden")
            or viewer_is_supervisor
            or private
            or (viewer_principal_id is not None and c.get("principal") == str(viewer_principal_id))
        ]
        changes = ", ".join(f"{c['var']} {c['old']} -> {c['new']}" for c in shown)
        line = f"- {phase}: {chosen} was chosen" + (f" ({changes})" if changes else "")
        facts = payload.get("disclosed") or {}
        if facts:
            line += ". Found: " + ", ".join(f"{k} = {_fact(v)}" for k, v in facts.items())
    elif payload.get("short"):
        need = {"majority": "more than half of the votes", "unanimous": "every vote"}.get(
            payload.get("threshold", ""), "enough votes"
        )
        line = f"- {phase}: no choice won {need}; nothing was chosen"
    elif payload.get("tied"):
        line = f"- {phase}: tie between {', '.join(payload['tied'])}; nobody was chosen"
    else:
        line = f"- {phase}: no votes were cast; nobody was chosen"
    if private:
        line += " (private: only the voters of this vote read this)"
    if payload.get("reveal", "votes") in ("votes", "voters") or viewer_is_supervisor:
        votes = payload.get("votes") or {}
        cast = "; ".join(f"{voter} -> {choice or 'abstained'}" for voter, choice in votes.items())
        abstained = payload.get("abstained") or []
        if abstained:
            cast = "; ".join(filter(None, [cast, f"no ballot: {', '.join(abstained)}"]))
        if cast:
            line += f". Ballots: {cast}"
    return line


def _fact(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


async def ballot_results(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every closed vote's recorded result, oldest first -- the record, not anyone's
    retelling of it (INV-7)."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(SessionEventRow.payload)
                .where(
                    SessionEventRow.session_id == session_id,
                    SessionEventRow.kind == "ballot_result",
                )
                .order_by(SessionEventRow.event_seq)
            )
        ).scalars()
        return [dict(r) for r in rows]


# ── effects ─────────────────────────────────────────────────────────────────────────


async def apply_participant_effects(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    spec: ParticipantsSpec,
    targets: Iterable[ParticipantView],
    effects: Sequence[ParticipantEffectSpec],
    bindings: Mapping[str, Any],
    cause: str,
    cause_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Apply each effect to each target, typed against the declared var, recording every
    change. In the caller's transaction."""
    changes: list[dict[str, Any]] = []
    for target in targets:
        sp = await session.get(SessionPersonaRow, target.session_persona_id)
        assert sp is not None
        values = dict(sp.vars or {})
        for effect in effects:
            var_spec = spec.vars.get(effect.set)
            if var_spec is None:
                raise ParticipantError(f"effect sets undeclared participant var {effect.set!r}")
            raw = eval_cel(effect.to, {**bindings, "p": {**target.cel(), **values}})
            new = _COERCE[var_spec.type](raw)
            old = values.get(effect.set)
            if old == new:
                continue
            values[effect.set] = new
            session.add(
                ParticipantStateChangeRow(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    session_persona_id=target.session_persona_id,
                    var=effect.set,
                    old_value=old,
                    new_value=new,
                    cause=cause,
                    cause_ref=cause_ref,
                )
            )
            changes.append(
                {
                    "name": target.name,
                    "principal": str(target.principal_id),
                    "var": effect.set,
                    "old": old,
                    "new": new,
                    # A hidden var's change is no more public than the var itself.
                    "hidden": effect.set in spec.hidden,
                }
            )
        sp.vars = values
    await session.flush()
    return changes


# ── audiences ───────────────────────────────────────────────────────────────────────


def visible_to(audience: Sequence[uuid.UUID | str] | None, viewer: uuid.UUID | None) -> bool:
    """Whether a message with this audience reaches this viewer. NULL audience = everyone;
    a private message reaches no anonymous reader."""
    if audience is None:
        return True
    if viewer is None:
        return False
    return str(viewer) in {str(a) for a in audience}


async def audience_for(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    members: str,
    author_principal_id: uuid.UUID,
    *,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> list[uuid.UUID]:
    """The principals who may read what the author says in a phase with ``audience``:
    every participant matching ``members``, the author, and the supervisor(s)."""
    async with tenant_scope(tenant_id) as session:
        views = await load_views(session, session_id)
        supervisors = await supervisor_principals(session, session_id)
    author = next((v for v in views if v.principal_id == author_principal_id), None)
    base = activation(state=state, settings=settings, views=views)
    base["author"] = author.cel() if author is not None else {"id": str(author_principal_id)}
    out = {author_principal_id, *supervisors}
    for view in views:
        if matches(members, {**base, "p": view.cel()}):
            out.add(view.principal_id)
    return sorted(out, key=str)


# ── ballots ─────────────────────────────────────────────────────────────────────────


def ballot_targets(
    ballot: BallotSpec,
    views: Sequence[ParticipantView],
    voter: ParticipantView,
    *,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> list[ParticipantView]:
    base = activation(state=state, settings=settings, views=views, voter=voter.cel())
    return [v for v in views if matches(ballot.targets, {**base, "p": v.cel()})]


async def cast_ballot(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    poll_key: str,
    voter_sp_id: uuid.UUID,
    choice_sp_id: uuid.UUID | None,
    choice_option: str | None = None,
) -> bool:
    """Record one ballot -- for a participant, or for one of a vote's ``options``.
    Returns False when this voter already voted in this poll -- the first ballot stands,
    so a resumed turn cannot vote twice."""
    async with tenant_scope(tenant_id) as session:
        result = await session.execute(
            pg_insert(BallotRow)
            .values(
                tenant_id=tenant_id,
                session_id=session_id,
                poll_key=poll_key,
                voter_session_persona_id=voter_sp_id,
                choice_session_persona_id=choice_sp_id,
                choice_option=choice_option,
            )
            .on_conflict_do_nothing(constraint="uq_ballot_voter")
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Tally:
    counts: dict[str, int]
    chosen: ParticipantView | None
    tied: list[str]
    abstained: list[str]
    voters: dict[str, str | None]
    # The winning option of a vote between ``options``.
    chosen_option: str | None = None
    # The leader fell short of the ballot's threshold, so nobody was chosen.
    short: bool = False


def _meets(threshold: str, top: int, expected: int) -> bool:
    """Whether ``top`` votes carry a vote that expected ``expected`` ballots."""
    if threshold == "majority":
        return top * 2 > expected
    if threshold == "unanimous":
        return expected > 0 and top == expected
    return True


async def tally(
    session: AsyncSession,
    row: SessionRow,
    poll_key: str,
    ballot: BallotSpec,
    views: Sequence[ParticipantView],
    eligible_voters: Sequence[ParticipantView],
) -> Tally:
    """Count the poll's ballots, in the caller's transaction. Most votes wins, if they
    reach the ballot's ``threshold`` of the expected voters (every voter the phase
    admits; the ballots cast when it admits nobody by name); a tie elects nobody, or is
    settled by a recorded draw (``tie: draw``). A voter with no ballot abstained."""
    by_sp = {v.session_persona_id: v for v in views}
    ballots = (
        await session.execute(
            select(BallotRow).where(BallotRow.session_id == row.id, BallotRow.poll_key == poll_key)
        )
    ).scalars()
    voters: dict[str, str | None] = {}
    # Keyed by a participant's name, or by the option itself in a vote between options.
    counts: dict[str, int] = {}
    seats: dict[str, int] = {}
    for b in ballots:
        voter = by_sp.get(b.voter_session_persona_id)
        if voter is None:
            continue
        if ballot.options is not None:
            key = b.choice_option if b.choice_option in ballot.options else None
            if key is not None:
                seats[key] = ballot.options.index(key)
        else:
            choice = by_sp.get(b.choice_session_persona_id) if b.choice_session_persona_id else None
            key = choice.name if choice is not None else None
            if choice is not None:
                seats[key] = choice.seat or 0  # type: ignore[index]
        voters[voter.name] = key
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    abstained = [v.name for v in eligible_voters if voters.get(v.name) is None]
    expected = len(eligible_voters) or len(voters)
    leader: str | None = None
    tied: list[str] = []
    short = False
    if counts:
        top = max(counts.values())
        leaders = sorted((k for k, n in counts.items() if n == top), key=lambda k: seats[k])
        if not _meets(ballot.threshold, top, expected):
            short = True
        elif len(leaders) == 1:
            leader = leaders[0]
        else:
            tied = leaders
            if ballot.tie == "draw":
                leader = seeded_rng(row, f"tie:{poll_key}").choice(leaders)
    by_name = {v.name: v for v in views}
    is_option = ballot.options is not None
    return Tally(
        counts=counts,
        chosen=None if is_option or leader is None else by_name[leader],
        tied=tied,
        abstained=abstained,
        voters=voters,
        chosen_option=leader if is_option else None,
        short=short,
    )


def poll_key(phase_key: str, phase_entry_seq: int) -> str:
    return f"{phase_key}:{phase_entry_seq}"


BALLOT_TOOL_NAME = "ballot"


_BALLOT_AFTER = {
    "votes": "then all votes are shown together",
    "outcome": "then only the result is shown, never who voted for whom",
    "voters": "then the result is shown only to those who voted in it",
}


def make_ballot_tool(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    key: str,
    voter: ParticipantView,
    targets: Sequence[ParticipantView],
    reveal: str = "votes",
    options: Sequence[str] | None = None,
) -> tuple[ToolSpec, ToolHandler]:
    """The ``ballot`` tool for one voter's turn: its ``choice`` is an enum of the names
    this voter may vote for right now, rebuilt every turn -- or of the vote's
    ``options``. The first ballot counts; a second call is answered, not recorded."""
    by_name: dict[str, ParticipantView | None] = (
        {o: None for o in options} if options is not None else {t.name: t for t in targets}
    )
    what = (
        "the option you vote for"
        if options is not None
        else ("the name of the participant you vote for")
    )
    spec = ToolSpec(
        name=BALLOT_TOOL_NAME,
        description=(
            "Cast your vote. It is sealed: nobody sees it until everyone has voted, "
            f"{_BALLOT_AFTER.get(reveal, _BALLOT_AFTER['votes'])}. You vote once; your first "
            "ballot counts."
        ),
        parameters={
            "type": "object",
            "properties": {
                "choice": {
                    "type": "string",
                    "enum": sorted(by_name),
                    "description": what,
                }
            },
            "required": ["choice"],
        },
    )

    async def handler(arguments: dict[str, object], _ctx: ToolContext) -> ToolResult:
        name = str(arguments.get("choice") or "").strip()
        if name not in by_name:
            return ToolResult(json.dumps({"error": "not a valid choice", "valid": sorted(by_name)}))
        target = by_name[name]
        recorded = await cast_ballot(
            tenant_id,
            session_id,
            key,
            voter.session_persona_id,
            target.session_persona_id if target is not None else None,
            choice_option=name if target is None else None,
        )
        if not recorded:
            return ToolResult(json.dumps({"error": "you have already voted in this vote"}))
        return ToolResult(json.dumps({"recorded": True, "choice": name}))

    return spec, handler


# ── picks ───────────────────────────────────────────────────────────────────────────

PICK_CAUSE = "pick"


def pick_available(
    pick: PickSpec,
    views: Sequence[ParticipantView],
    actor: ParticipantView,
    *,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> bool:
    base = activation(state=state, settings=settings, views=views, actor=actor.cel())
    return matches(pick.available, {**base, "p": actor.cel()})


def pick_targets(
    pick: PickSpec,
    views: Sequence[ParticipantView],
    actor: ParticipantView,
    *,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> list[ParticipantView]:
    base = activation(state=state, settings=settings, views=views, actor=actor.cel())
    return [v for v in views if matches(pick.targets, {**base, "p": v.cel()})]


def make_pick_tool(
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    spec: ParticipantsSpec,
    pick: PickSpec,
    actor: ParticipantView,
    state: Mapping[str, Any],
    settings: Mapping[str, Any],
    targets: Sequence[ParticipantView],
) -> tuple[ToolSpec, ToolHandler]:
    """One pick's tool for one actor's turn: ``choice`` is an enum of who this actor may
    pick right now. The pick applies its effects at once, in one transaction, each change
    recorded with cause ``pick`` and a ``cause_ref`` naming the tool and the turn -- which
    also makes it one pick per tool per turn: a second call in the same turn, or the same
    call re-run by a resumed turn, is answered, not applied again."""
    names = sorted(t.name for t in targets)
    tool_spec = ToolSpec(
        name=pick.tool,
        description=pick.description + " Once per turn.",
        parameters={
            "type": "object",
            "properties": {
                "choice": {
                    "type": "string",
                    "enum": names,
                    "description": "the name of the participant you pick",
                }
            },
            "required": ["choice"],
        },
    )

    async def handler(arguments: dict[str, object], ctx: ToolContext) -> ToolResult:
        name = str(arguments.get("choice") or "").strip()
        if name not in names:
            return ToolResult(json.dumps({"error": "not a valid choice", "valid": names}))
        if ctx.turn_event_seq is None:
            return ToolResult(json.dumps({"error": "no turn to pick in"}))
        cause_ref = f"{pick.tool}:{ctx.turn_event_seq}"
        async with tenant_scope(tenant_id) as session:
            done = (
                await session.execute(
                    select(ParticipantStateChangeRow.id)
                    .where(
                        ParticipantStateChangeRow.session_id == session_id,
                        ParticipantStateChangeRow.cause == PICK_CAUSE,
                        ParticipantStateChangeRow.cause_ref == cause_ref,
                    )
                    .limit(1)
                )
            ).first()
            if done is not None:
                return ToolResult(json.dumps({"error": "you have already used this in your turn"}))
            # Decided again on the record as it is now, not as the turn found it.
            views = await load_views(session, session_id)
            me = next((v for v in views if v.session_persona_id == actor.session_persona_id), None)
            if me is None or not pick_available(pick, views, me, state=state, settings=settings):
                return ToolResult(json.dumps({"error": "you cannot do this now"}))
            now = pick_targets(pick, views, me, state=state, settings=settings)
            target = next((v for v in now if v.name == name), None)
            if target is None:
                valid = sorted(v.name for v in now)
                return ToolResult(json.dumps({"error": "not a valid choice", "valid": valid}))
            bindings = activation(state=state, settings=settings, views=views, actor=me.cel())
            await apply_participant_effects(
                session,
                tenant_id=tenant_id,
                session_id=session_id,
                spec=spec,
                targets=[target],
                effects=pick.on_picked,
                bindings=bindings,
                cause=PICK_CAUSE,
                cause_ref=cause_ref,
            )
            if pick.on_picker:
                views = await load_views(session, session_id)
                me = next(v for v in views if v.session_persona_id == actor.session_persona_id)
                await apply_participant_effects(
                    session,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    spec=spec,
                    targets=[me],
                    effects=pick.on_picker,
                    bindings=activation(
                        state=state, settings=settings, views=views, actor=me.cel()
                    ),
                    cause=PICK_CAUSE,
                    cause_ref=cause_ref,
                )
        return ToolResult(json.dumps({"picked": name}), end_turn=pick.end_turn)

    return tool_spec, handler
