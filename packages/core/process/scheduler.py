"""The turn scheduler: who acts next within a phase, in
what order, until when. Produces a ``NextActorFn`` (the exact injection point) backed
by a real, persisted cursor (``session.actor_cursor``) that survives kill/resume without
skipping or double-acting an actor.

**Candidate resolution is split between real and injected, by what actually exists.**
``human_participant``/``any_of``'s human-based tokens resolve against a real
``workspace_membership`` query -- that table exists. Persona-role-based tokens
(``persona_type``, ``any_of``'s ``"<role>_agent"`` tokens) and initiative order's entity-
field lookups are **injected**, not queried here: this module must not reach into the
persona or entity tables itself. That mirrors the interpreter's own injection of
``next_actor_fn`` for the identical reason, one layer down: this module's
``CandidateResolver`` is the seam that real persona-role queries and real
``entity_field`` lookups (which live elsewhere
list) plug real implementations into.

``order`` (declared/initiative/free) picks the scheduling algorithm; ``mode``
(free/generate/generate_as) is orthogonal -- it says whether the resulting turn is
human-typed or model-generated, and this module never inspects it beyond passing it
through on the returned ``ActorRef``. Do not confuse the two fields; the DSL schema itself
 keeps them separate for exactly this reason.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select

from core.process import floor
from core.process import participants as parts
from core.process.dsl.schema import ActorSpec
from core.process.interpreter import ActorRef, InterpreterContext, NextActorFn
from core.sessions.models import SessionRow
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_HUMAN_TOKENS = frozenset({"human_participant", "human_overseer"})


@dataclass(frozen=True)
class Candidate:
    principal_id: uuid.UUID
    initiative: float | None = None
    # Display name, filled by the persona resolver. "addressed" and "reactive" ordering
    # read it: matching who the previous speaker named requires knowing what the
    # candidates are called.
    name: str | None = None
    # The persona's chattiness axis value (0-100), when its behaviour profile sets
    # one. "reactive" ordering weights unprompted turns by it; everything else ignores it.
    chattiness: float | None = None


# Resolves ONE actor spec entry into its currently eligible concrete candidates -- called
# fresh on every scheduling decision (not cached beyond one call), so membership/entity
# changes mid-phase are always reflected (this is what makes "mid-phase actor removal"
# work: a candidate absent from a fresh resolve is simply never offered a turn again).
CandidateResolver = Callable[[ActorSpec, InterpreterContext], Awaitable[list[Candidate]]]


async def _resolve_human_candidates(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[Candidate]:
    async with tenant_scope(tenant_id) as session:
        # human_participant/human_overseer actors resolve to *human* members only -- agent
        # personas may legitimately hold a workspace role too (so they can act on entities),
        # and must never be offered a human free-mode turn. Filtering on principal.kind keeps
        # that grant from leaking into human-actor scheduling.
        rows = (
            await session.execute(
                select(WorkspaceMembership.principal_id)
                .join(Principal, Principal.id == WorkspaceMembership.principal_id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    Principal.kind == "human",
                )
                .order_by(WorkspaceMembership.principal_id)
            )
        ).scalars()
        return [Candidate(principal_id=pid) for pid in rows]


def make_default_candidate_resolver(
    workspace_id: uuid.UUID,
    *,
    persona_candidate_resolver: CandidateResolver | None = None,
) -> CandidateResolver:
    """The resolver this module can build on its own: humans via ``workspace_membership``,
    agents via an injected fallback (``None`` = no agent candidates -- correct for a
    phase with no agent actors; the real ``persona_type`` query is injected by the
    caller). Initiative values are always ``None`` from this resolver;
    a caller wanting real initiative order must supply its own resolver that also injects
    entity-field lookups -- see module docstring."""

    async def resolve(spec: ActorSpec, ctx: InterpreterContext) -> list[Candidate]:
        candidates: list[Candidate] = []
        if spec.human_participant == "all":
            candidates.extend(await _resolve_human_candidates(ctx.tenant_id, workspace_id))
        elif spec.any_of is not None:
            if any(token in _HUMAN_TOKENS for token in spec.any_of):
                candidates.extend(await _resolve_human_candidates(ctx.tenant_id, workspace_id))
            if persona_candidate_resolver is not None and any(
                token not in _HUMAN_TOKENS for token in spec.any_of
            ):
                candidates.extend(await persona_candidate_resolver(spec, ctx))
        elif (
            spec.persona_type is not None or spec.order == "initiative"
        ) and persona_candidate_resolver is not None:
            candidates.extend(await persona_candidate_resolver(spec, ctx))
        return candidates

    return resolve


@dataclass(frozen=True)
class _EntryCursor:
    resolved_order: list[str] | None  # principal_id strings, fixed for declared/initiative
    turn_index: int
    turns_taken: int
    # "hands" only: the event clock when the floor opened (raised hands before it belong
    # to an earlier visit of the phase), turns given per actor, and the clock when each
    # actor's latest turn was issued -- a hand raised at or after it is still up.
    since_seq: int | None = None
    spoken: dict[str, int] | None = None
    granted: dict[str, int] | None = None

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "resolved_order": self.resolved_order,
            "turn_index": self.turn_index,
            "turns_taken": self.turns_taken,
        }
        if self.since_seq is not None:
            data.update(since_seq=self.since_seq, spoken=self.spoken, granted=self.granted)
        return data

    @staticmethod
    def from_json(data: dict[str, object] | None) -> _EntryCursor:
        if not data:
            return _EntryCursor(resolved_order=None, turn_index=0, turns_taken=0)
        return _EntryCursor(
            resolved_order=data.get("resolved_order"),  # type: ignore[arg-type]
            turn_index=data.get("turn_index", 0),  # type: ignore[arg-type]
            turns_taken=data.get("turns_taken", 0),  # type: ignore[arg-type]
            since_seq=data.get("since_seq"),  # type: ignore[arg-type]
            spoken=data.get("spoken"),  # type: ignore[arg-type]
            granted=data.get("granted"),  # type: ignore[arg-type]
        )


def _cap(spec: ActorSpec, settings: dict[str, object]) -> int | None:
    """The most turns this entry gives. ``seated`` is bounded by its own walk (seats x
    laps) and ``hands`` by its per-actor cap, so neither has one unless it is written;
    every other order defaults to 1."""
    cap = parts.setting_number(spec.max_turns, settings)
    if spec.order in ("seated", "hands"):
        return cap
    return cap or 1


async def _participant_filter(
    spec: ActorSpec, ctx: InterpreterContext, fresh: list[Candidate]
) -> tuple[list[Candidate], dict[str, parts.ParticipantView]]:
    """Apply ``where`` to the candidates and return the participant views by principal
    id (seat order needs them). A no-op, with no query, for entries that use neither."""
    if spec.where is None and spec.order not in ("seated", "hands"):
        return fresh, {}
    views = await parts.participant_views(ctx.tenant_id, ctx.session_id)
    by_id = {str(v.principal_id): v for v in views}
    if spec.where is None:
        return fresh, by_id
    base = parts.activation(state=ctx.state, settings=ctx.settings, views=views)
    kept = [
        c
        for c in fresh
        if (view := by_id.get(str(c.principal_id))) is not None
        and parts.matches(spec.where, {**base, "p": view.cel()})
    ]
    return kept, by_id


def _seated_order(
    spec: ActorSpec,
    ctx: InterpreterContext,
    fresh: list[Candidate],
    by_id: dict[str, parts.ParticipantView],
) -> list[str]:
    """Seat order, starting ``rotate_by`` seats on, walked ``laps`` times. Resolved once
    at phase entry from everyone selected -- a participant removed mid-phase is
    skipped at its turn by the eligibility check, like declared order."""
    seated = sorted(
        (c for c in fresh if str(c.principal_id) in by_id),
        key=lambda c: by_id[str(c.principal_id)].seat or 0,
    )
    order = [str(c.principal_id) for c in seated]
    if order and spec.rotate_by is not None:
        bindings = parts.activation(
            state=ctx.state, settings=ctx.settings, views=list(by_id.values())
        )
        shift = int(parts.eval_cel(spec.rotate_by, bindings)) % len(order)
        order = order[shift:] + order[:shift]
    laps = parts.setting_number(spec.laps, ctx.settings) or 1
    return order * laps


async def _last_assistant_text(tenant_id: uuid.UUID, session_id: uuid.UUID) -> str:
    """The most recent generated turn's text -- the utterance that may name an addressee.
    Split out so tests can monkeypatch it instead of standing up a transcript."""
    text, _author = await _last_assistant_turn(tenant_id, session_id)
    return text


async def _last_assistant_turn(
    tenant_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str, uuid.UUID | None]:
    """The most recent generated turn's text and its author -- "reactive" ordering needs
    both: the text may name the next speaker, and the author must not follow themselves."""
    from core.sessions.models import MessageRow

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                select(MessageRow.content_md, MessageRow.author_principal_id)
                .where(MessageRow.session_id == session_id, MessageRow.role == "assistant")
                .order_by(MessageRow.event_seq.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return "", None
    return row[0] or "", row[1]


def _mention_score(name: str | None, lowered_text: str) -> int:
    """Where (if anywhere) this candidate is named in the text. Ranked by where the
    match ENDS, with a full-name bonus, so "Viktor Wallmark" beats Elin Wallmark's bare
    surname hit at the same position; -1 when not named. 3+ character words only, so
    initials and particles never trigger."""
    lname = (name or "").strip().lower()
    if not lname or not lowered_text:
        return -1
    score = -1
    full_at = lowered_text.rfind(lname)
    if full_at >= 0:
        score = (full_at + len(lname)) * 2 + 1
    for word in lname.split():
        if len(word) < 3:
            continue
        word_at = lowered_text.rfind(word)
        if word_at >= 0:
            score = max(score, (word_at + len(word)) * 2)
    return score


def _word_mention_score(name: str | None, lowered_text: str) -> int:
    """Like _mention_score, ranked by the last whole-word mention: "Sage" is not named
    by "message". -1 when not named."""
    score = -1
    for word in (name or "").strip().lower().split():
        if len(word) < 3:
            continue
        for match in re.finditer(rf"\b{re.escape(word)}\b", lowered_text):
            score = max(score, match.end() * 2)
    return score


def _reactive_pick(
    candidates: list[Candidate],
    text: str,
    previous_author: uuid.UUID | None,
    seed: str,
) -> Candidate | None:
    """One reactive turn: the person the last message names goes next; nobody named, a
    deterministic chattiness-weighted pick. The previous speaker never follows
    themselves while anyone else is eligible -- being pushed toward IS how they get the
    floor back, one turn later.

    Determinism matters (INV-10): the "random" pick is sha256(seed) over cumulative
    chattiness weights, so a replay makes the identical choice. Weight 0 personas never
    speak unprompted but still answer when named.
    """
    if not candidates:
        return None
    pool = [c for c in candidates if c.principal_id != previous_author] or candidates

    lowered = text.lower()
    best: Candidate | None = None
    best_score = -1
    for candidate in pool:
        score = _mention_score(candidate.name, lowered)
        if score > best_score:
            best_score, best = score, candidate
    if best is not None and best_score >= 0:
        return best

    weights = [max(0.0, c.chattiness if c.chattiness is not None else 50.0) for c in pool]
    total = sum(weights)
    if total <= 0:
        return pool[0]
    digest = hashlib.sha256(seed.encode()).digest()
    point = (int.from_bytes(digest[:8], "big") / 2**64) * total
    running = 0.0
    for candidate, weight in zip(pool, weights, strict=True):
        running += weight
        if point < running:
            return candidate
    return pool[-1]


def _addressed_first(candidates: list[Candidate], text: str) -> list[Candidate]:
    """Reorder so the candidate the text names last comes first; declared order otherwise.
    Deterministic string matching, no model call -- see _mention_score for the ranking.
    Nobody named -> declared order untouched."""
    lowered = text.lower()
    if not lowered:
        return candidates
    best_index: int | None = None
    best_score = -1
    for index, candidate in enumerate(candidates):
        score = _mention_score(candidate.name, lowered)
        if score > best_score:
            best_score, best_index = score, index
    if best_index is None or best_score < 0:
        return candidates
    chosen = candidates[best_index]
    return [chosen, *(c for i, c in enumerate(candidates) if i != best_index)]


async def _next_on_floor(
    spec: ActorSpec,
    cursor: _EntryCursor,
    ctx: InterpreterContext,
    fresh: list[Candidate],
    by_id: dict[str, parts.ParticipantView],
    seq_now: int,
) -> tuple[ActorRef | None, _EntryCursor]:
    """One "hands" turn. First everyone once, in seat order (declared order when the flow
    has no participants); then whoever the last message names, else the hand raised
    longest ago; nobody named and no hand up ends the entry. The previous speaker never
    follows themselves while anyone else wants the floor, and an actor who has had
    ``max_turns_each`` turns is never offered another."""
    each = parts.setting_number(spec.max_turns_each, ctx.settings)
    since = cursor.since_seq if cursor.since_seq is not None else seq_now
    spoken = dict(cursor.spoken or {})
    granted = dict(cursor.granted or {})
    eligible = {str(c.principal_id): c for c in fresh}

    def has_turns(pid: str) -> bool:
        return each is None or spoken.get(pid, 0) < each

    def give(pid: str, resolved: list[str], index: int) -> tuple[ActorRef, _EntryCursor]:
        spoken[pid] = spoken.get(pid, 0) + 1
        granted[pid] = seq_now
        return ActorRef(principal_id=uuid.UUID(pid), mode=spec.mode), _EntryCursor(
            resolved, index, cursor.turns_taken + 1, since, spoken, granted
        )

    resolved = cursor.resolved_order
    if resolved is None:
        if by_id:
            resolved = _seated_order(spec, ctx, fresh, by_id)
        else:
            resolved = [str(c.principal_id) for c in fresh]
    index = cursor.turn_index
    while index < len(resolved):
        pid = resolved[index]
        index += 1
        if pid in eligible and has_turns(pid):
            return give(pid, resolved, index)

    # The opening round is over: the floor follows the conversation.
    text, previous_author = await _last_assistant_turn(ctx.tenant_id, ctx.session_id)
    previous = str(previous_author) if previous_author is not None else None
    lowered = text.lower()
    named: str | None = None
    best = -1
    for pid, candidate in eligible.items() if spec.answer_named else ():
        if pid == previous or not has_turns(pid):
            continue
        score = _word_mention_score(candidate.name, lowered)
        if score > best:
            best, named = score, pid
    if named is not None and best >= 0:
        return give(named, resolved, index)

    waiting: dict[str, int] = {}
    for hand in await floor.raised_hands(ctx.tenant_id, ctx.session_id, ctx.phase_key, since):
        pid = str(hand.principal_id)
        if hand.turn_seq >= granted.get(pid, since) and pid not in waiting:
            waiting[pid] = hand.turn_seq
    queue = [
        pid
        for pid, _seq in sorted(waiting.items(), key=lambda item: item[1])
        if pid in eligible and has_turns(pid)
    ]
    others = [pid for pid in queue if pid != previous]
    if others or queue:
        return give((others or queue)[0], resolved, index)
    return None, _EntryCursor(resolved, index, cursor.turns_taken, since, spoken, granted)


async def _next_from_entry(
    spec: ActorSpec,
    cursor: _EntryCursor,
    ctx: InterpreterContext,
    resolve_candidates: CandidateResolver,
    seq_now: int = 0,
) -> tuple[ActorRef | None, _EntryCursor]:
    """Returns (actor or None if this entry is exhausted, updated cursor). ``seq_now`` is
    the session's event clock at this decision -- the "hands" floor ties raised hands to
    the turns they were raised in by it."""
    cap = _cap(spec, ctx.settings)
    if cap is not None and cursor.turns_taken >= cap:
        return None, cursor

    selected = await resolve_candidates(spec, ctx)
    fresh, by_id = await _participant_filter(spec, ctx, selected)
    eligible_ids = {str(c.principal_id) for c in fresh}

    if spec.order == "hands":
        return await _next_on_floor(spec, cursor, ctx, fresh, by_id, seq_now)

    if spec.order == "reactive":
        text, previous_author = await _last_assistant_turn(ctx.tenant_id, ctx.session_id)
        seed = f"{ctx.session_id}:{ctx.event_seq}:{cursor.turns_taken}"
        chosen = _reactive_pick(fresh, text, previous_author, seed)
        if chosen is None:
            return None, cursor
        new_cursor = _EntryCursor(None, 0, cursor.turns_taken + 1)
        return ActorRef(principal_id=chosen.principal_id, mode=spec.mode), new_cursor

    if spec.order == "free":
        # Deterministic pick among currently-eligible candidates -- no fixed order is
        # cached, so a change in the eligible pool is picked up on the very next call.
        # (A human free-mode candidate being "selected" here only matters once the
        # await/satisfaction gates whether the interpreter actually waits for their
        # input -- this module only decides eligibility + rotation, not readiness.)
        ordered = sorted(fresh, key=lambda c: str(c.principal_id))
        if not ordered:
            return None, cursor
        chosen = ordered[0]
        new_cursor = _EntryCursor(None, 0, cursor.turns_taken + 1)
        return ActorRef(principal_id=chosen.principal_id, mode=spec.mode), new_cursor

    # declared / initiative: resolve the order once, then walk it, skipping anyone no
    # longer eligible (mid-phase removal) without giving them a turn.
    if cursor.resolved_order is None:
        if spec.order == "initiative":
            indexed = list(enumerate(fresh))
            indexed.sort(key=lambda pair: (-(pair[1].initiative or 0.0), pair[0]))
            ordered_candidates = [c for _i, c in indexed]
        elif spec.order == "addressed":
            # Whoever the previous speaker named answers first; the rest keep declared
            # order. Resolved once at phase entry like declared order, so one question
            # gets one addressee -- a phase that presses a new person re-enters and
            # re-resolves.
            ordered_candidates = _addressed_first(
                fresh, await _last_assistant_text(ctx.tenant_id, ctx.session_id)
            )
        elif spec.order == "seated":
            ordered_candidates = []
        else:
            ordered_candidates = fresh
        resolved_order = (
            _seated_order(spec, ctx, fresh, by_id)
            if spec.order == "seated"
            else [str(c.principal_id) for c in ordered_candidates]
        )
    else:
        resolved_order = cursor.resolved_order

    turn_index = cursor.turn_index
    while turn_index < len(resolved_order):
        pid_str = resolved_order[turn_index]
        if pid_str in eligible_ids:
            new_cursor = _EntryCursor(resolved_order, turn_index + 1, cursor.turns_taken + 1)
            return ActorRef(principal_id=uuid.UUID(pid_str), mode=spec.mode), new_cursor
        turn_index += 1  # mid-phase removal: skip without a turn

    return None, _EntryCursor(resolved_order, turn_index, cursor.turns_taken)


def make_scheduler(resolve_candidates: CandidateResolver) -> NextActorFn:
    """Returns a ``NextActorFn`` (the exact injection point) backed by a persisted
    cursor. Safe to call repeatedly across separate ``advance_session`` invocations --
    kill/resume mid-rotation reads the same cursor back and continues exactly where it
    left off, never re-offering a turn already given or skipping the next one."""

    async def next_actor(ctx: InterpreterContext) -> ActorRef | None:
        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            raw_cursor = row.actor_cursor
            seq_now = row.next_event_seq

        # The cursor is persisted before the turn it was advanced for runs. If nothing has
        # been written to the session since (next_event_seq unchanged), that turn never
        # landed -- the process died mid-generation, or the turn faulted -- and the actor
        # must be offered again rather than skipped. Observed as a lost referee turn after
        # a pod was replaced under a session (karsh-vale sweep, 2026-10-04).
        previous_cursor = raw_cursor.get("previous")
        if (
            raw_cursor.get("phase_key") == ctx.phase_key
            and raw_cursor.get("issued_at_seq") == seq_now
            and isinstance(previous_cursor, dict)
        ):
            raw_cursor = {"phase_key": ctx.phase_key, **previous_cursor}

        if raw_cursor.get("phase_key") != ctx.phase_key:
            entry_index = 0
            entry_cursor = _EntryCursor(None, 0, 0)
        else:
            entry_index = raw_cursor.get("entry_index", 0)  # type: ignore[assignment]
            entry_cursor = _EntryCursor.from_json(raw_cursor.get("entry"))  # type: ignore[arg-type]
        previous = {"entry_index": entry_index, "entry": entry_cursor.to_json()}

        actors = ctx.phase.actors
        actor: ActorRef | None = None
        while entry_index < len(actors):
            spec = actors[entry_index]
            actor, entry_cursor = await _next_from_entry(
                spec, entry_cursor, ctx, resolve_candidates, seq_now
            )
            if actor is not None:
                break
            entry_index += 1
            entry_cursor = _EntryCursor(None, 0, 0)

        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            row.actor_cursor = {
                "phase_key": ctx.phase_key,
                "entry_index": entry_index,
                "entry": entry_cursor.to_json(),
                # Where this advance started from, and the session's event clock at the
                # time: the pair that lets the next call tell "the turn happened" from
                # "the turn was only issued".
                "previous": previous,
                "issued_at_seq": seq_now,
            }

        return actor

    return next_actor
