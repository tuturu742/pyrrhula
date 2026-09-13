"""The turn scheduler (B1.3, plan §5.2 actors, §5.3): who acts next within a phase, in
what order, until when. Produces a ``NextActorFn`` (B1.2's exact injection point) backed
by a real, persisted cursor (``session.actor_cursor``) that survives kill/resume without
skipping or double-acting an actor.

**Candidate resolution is split between real and injected, by what actually exists.**
``human_participant``/``any_of``'s human-based tokens resolve against a real
``workspace_membership`` query -- that table exists (T0.2). Persona-role-based tokens
(``persona_type``, ``any_of``'s ``"<role>_agent"`` tokens) and initiative order's entity-
field lookups are **injected**, not queried here: ``agent.persona_type`` doesn't exist as a
column until B1.7, and there is no ``entity``/``entity_schema`` table at all in Phase 1 --
neither exists to query. This mirrors B1.2's own injection of ``next_actor_fn`` for the
identical reason (build against schema that exists, wire in the rest when it does), one
layer down: this module's ``CandidateResolver`` is the seam B1.7 (real agent-role queries)
and the entity-schema task (real ``entity_field`` lookups, not yet on any Phase-1 task
list) plug real implementations into.

``order`` (declared/initiative/free) picks the scheduling algorithm; ``mode``
(free/generate/generate_as) is orthogonal -- it says whether the resulting turn is
human-typed or model-generated, and this module never inspects it beyond passing it
through on the returned ``ActorRef``. Do not confuse the two fields; the DSL schema itself
(B1.1) keeps them separate for exactly this reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select

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
    # Display name, filled by the persona resolver. Only "addressed" ordering reads it:
    # matching who the previous speaker named requires knowing what the candidates are
    # called.
    name: str | None = None


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
    """The resolver B1.3 can build for real today: humans via ``workspace_membership``,
    agents via an injected fallback (``None`` = no agent candidates -- correct for a
    phase with no agent actors, a real gap for one that has them, until B1.7 supplies a
    real ``persona_type`` query). Initiative values are always ``None`` from this resolver;
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

    def to_json(self) -> dict[str, object]:
        return {
            "resolved_order": self.resolved_order,
            "turn_index": self.turn_index,
            "turns_taken": self.turns_taken,
        }

    @staticmethod
    def from_json(data: dict[str, object] | None) -> _EntryCursor:
        if not data:
            return _EntryCursor(resolved_order=None, turn_index=0, turns_taken=0)
        return _EntryCursor(
            resolved_order=data.get("resolved_order"),  # type: ignore[arg-type]
            turn_index=data.get("turn_index", 0),  # type: ignore[arg-type]
            turns_taken=data.get("turns_taken", 0),  # type: ignore[arg-type]
        )


def _cap(spec: ActorSpec) -> int:
    return spec.max_turns or 1


async def _last_assistant_text(tenant_id: uuid.UUID, session_id: uuid.UUID) -> str:
    """The most recent generated turn's text -- the utterance that may name an addressee.
    Split out so tests can monkeypatch it instead of standing up a transcript."""
    from core.sessions.models import MessageRow

    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(MessageRow.content_md)
            .where(MessageRow.session_id == session_id, MessageRow.role == "assistant")
            .order_by(MessageRow.event_seq.desc())
            .limit(1)
        )
    return row or ""


def _addressed_first(candidates: list[Candidate], text: str) -> list[Candidate]:
    """Reorder so the candidate the text names last comes first; declared order otherwise.

    Deterministic string matching, no model call: for each candidate, the latest position
    at which its full name or any single word of its name (3+ chars, so initials and
    particles do not trigger) occurs in the text. A full-name match outranks a bare word
    at the same position -- casts share surnames ("Viktor Wallmark" must beat Elin
    Wallmark's bare surname hit. The last-named candidate wins because a question ends by
    naming its addressee far more often than it opens with one. Nobody named -> declared
    order untouched.
    """
    lowered = text.lower()
    if not lowered:
        return candidates
    best_index: int | None = None
    best_score = -1
    for index, candidate in enumerate(candidates):
        name = (candidate.name or "").strip().lower()
        if not name:
            continue
        # Ranked by where the match ENDS, not where it starts: "Viktor Wallmark" ends
        # exactly where Elin Wallmark's bare surname hit ends, and only the end-position
        # tie lets the full-name bonus decide it. Start-position ranking handed that
        # sentence to the wrong sibling.
        score = -1
        full_at = lowered.rfind(name)
        if full_at >= 0:
            score = (full_at + len(name)) * 2 + 1
        for word in name.split():
            if len(word) < 3:
                continue
            word_at = lowered.rfind(word)
            if word_at >= 0:
                score = max(score, (word_at + len(word)) * 2)
        if score > best_score:
            best_score, best_index = score, index
    if best_index is None or best_score < 0:
        return candidates
    chosen = candidates[best_index]
    return [chosen, *(c for i, c in enumerate(candidates) if i != best_index)]


async def _next_from_entry(
    spec: ActorSpec,
    cursor: _EntryCursor,
    ctx: InterpreterContext,
    resolve_candidates: CandidateResolver,
) -> tuple[ActorRef | None, _EntryCursor]:
    """Returns (actor or None if this entry is exhausted, updated cursor)."""
    if cursor.turns_taken >= _cap(spec):
        return None, cursor

    fresh = await resolve_candidates(spec, ctx)
    eligible_ids = {str(c.principal_id) for c in fresh}

    if spec.order == "free":
        # Deterministic pick among currently-eligible candidates -- no fixed order is
        # cached, so a change in the eligible pool is picked up on the very next call.
        # (A human free-mode candidate being "selected" here only matters once B1.6's
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
        else:
            ordered_candidates = fresh
        resolved_order = [str(c.principal_id) for c in ordered_candidates]
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
    """Returns a ``NextActorFn`` (B1.2's exact injection point) backed by a persisted
    cursor. Safe to call repeatedly across separate ``advance_session`` invocations --
    kill/resume mid-rotation reads the same cursor back and continues exactly where it
    left off, never re-offering a turn already given or skipping the next one."""

    async def next_actor(ctx: InterpreterContext) -> ActorRef | None:
        async with tenant_scope(ctx.tenant_id) as session:
            row = await session.get(SessionRow, ctx.session_id)
            assert row is not None
            raw_cursor = row.actor_cursor

        if raw_cursor.get("phase_key") != ctx.phase_key:
            entry_index = 0
            entry_cursor = _EntryCursor(None, 0, 0)
        else:
            entry_index = raw_cursor.get("entry_index", 0)  # type: ignore[assignment]
            entry_cursor = _EntryCursor.from_json(raw_cursor.get("entry"))  # type: ignore[arg-type]

        actors = ctx.phase.actors
        actor: ActorRef | None = None
        while entry_index < len(actors):
            spec = actors[entry_index]
            actor, entry_cursor = await _next_from_entry(
                spec, entry_cursor, ctx, resolve_candidates
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
            }

        return actor

    return next_actor
