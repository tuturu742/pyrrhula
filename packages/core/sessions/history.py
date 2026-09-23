"""Elapsed-history repopulation for a resumed session (G4.1, plan §5.4, req 20).

A session that has been dormant for a month resumes with a checkpoint's ``state`` but an
empty working context: the model has no idea what happened. This module builds the recap
that fills that gap -- and builds it under three rules that make it honest rather than
merely convenient:

1. **Mechanical facts come from records, never from re-summarised prose.** Resolutions
   (``resolution_record``), entity state changes (``entity_state_change``), and secret
   disclosures (``secret_disclosure_event``) are read from their own tables and injected
   *verbatim* into the summary frame as structured lines. The model is asked to narrate
   the prose around them; it is never asked what a roll came up. A planted message that
   lies about a result cannot change the fact frame, because the fact frame never passed
   through the model at all. (This is INV-7's instinct -- "deterministic results render
   from the record" -- applied to summarisation, and it is the same principle G4.10's
   report pipeline scales up.)

2. **The summary is per-viewer.** Facts are filtered through the *resuming context's*
   principal before any model call: an ``entity_state_change`` is included only when the
   entity's ``scope_key`` is in the viewer's resolved scope set (C1.1, the same resolver
   every retrieval path uses -- there is no second visibility implementation here), and a
   disclosure is included only when the viewer was actually disclosed to, or holds
   ``secret:inspect``. A participant's recap therefore cannot contain a fact they could
   not have seen live.

   **Scope boundary, stated plainly:** *prose* (``message`` rows) is not viewer-filtered,
   because the schema has no per-message visibility to filter on -- a session transcript
   is shared by construction, and every principal in the session saw every message live.
   Inventing a per-message scope here to make the filter look symmetrical would be
   fabricating a distinction the rest of the system does not make. The per-viewer
   difference lives entirely in the fact frame, which is where the per-viewer data
   actually is.

3. **The budget is a reservation, not a truncation.** ``BudgetSpec.history_ratio``
   (§5.2 DSL, G4.1) carves the history slice out of the phase budget *before* retrieval
   runs; ``summarise_history(max_tokens=...)`` then fits the summary to exactly that
   slice. Depth adapts: facts are kept first and narrative gets what remains, because a
   dropped fact is a lie of omission about something that provably happened while a
   thinner narrative is just a thinner narrative.

Model calls are metered as ``purpose='report'`` (CLAUDE.md rule 11) -- summarisation is a
report over the session log, and reusing the taxonomy's existing member is deliberate:
this is not a new purpose.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from core.agents.models import Agent
from core.assembler.context_assembler import HistorySummaryBlock, token_proxy
from core.assembler.visibility import scopes_for
from core.audit.models import UsageRecordRow
from core.entities.fsm import EntityStateChangeRow
from core.entities.storage import EntityRow
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.ports.permission import PermissionService, UnknownActionError
from core.process.dsl.schema import PhaseSpec
from core.resolution.records import ResolutionRecordRow
from core.secrets.models import SecretDisclosureEventRow
from core.sessions.models import SessionEventRow
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Principal
from core.tenancy.scope import tenant_scope

_PURPOSE = "report"
_INSPECT_ACTION = "secret:inspect"

_CHUNK_SYSTEM_PROMPT = (
    "You summarise one phase of an elapsed session transcript into two or three plain "
    "sentences, for a participant returning after a long absence. Summarise only what "
    "the messages say. Never state a numeric result, a state change, or who was told "
    "what -- those are supplied separately from the system's own records and are not "
    "yours to restate."
)
_REDUCE_SYSTEM_PROMPT = (
    "You combine per-phase summaries of an elapsed session into one short continuous "
    "recap, in chronological order. Keep it under the requested length. Never introduce "
    "a numeric result or state change that is not already in the text you were given."
)

FactKind = Literal["resolution", "entity_change", "disclosure"]


class _PhaseSummary(BaseModel):
    summary: str


class _ReducedSummary(BaseModel):
    narrative: str


@dataclass(frozen=True)
class MechanicalFact:
    """One record-derived fact, rendered from its own row. ``fields`` is copied verbatim
    out of the record -- nothing here is ever model-generated, which is exactly what makes
    ``test_summary_takes_mechanical_facts_from_records_not_prose`` provable rather than
    hopeful."""

    kind: FactKind
    # ``None`` for a between-sessions change (G4.2): it happened while no session was
    # running, so there is no event_seq to place it at. Rendered as ``seq=-`` and sorted
    # ahead of everything in-session, which is where it actually belongs -- it is part of
    # what changed *before* the resumed turn.
    event_seq: int | None
    record_id: str
    fields: dict[str, object]

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (self.event_seq if self.event_seq is not None else -1, self.kind, self.record_id)

    def render(self) -> str:
        rendered_fields = " ".join(f"{k}={v}" for k, v in sorted(self.fields.items()))
        seq = self.event_seq if self.event_seq is not None else "-"
        return f"- {self.kind} seq={seq} id={self.record_id} {rendered_fields}"


@dataclass(frozen=True)
class HistorySummary:
    from_event_seq: int
    to_event_seq: int
    facts: tuple[MechanicalFact, ...]
    narrative: str
    rendered_text: str
    token_count: int
    content_hash: str

    def to_block(self) -> HistorySummaryBlock:
        """The shape ``ContextAssembler.assemble(history_summary=...)`` takes. The
        assembler deliberately knows nothing about facts vs narrative -- it places one
        block of text and records its provenance; see that module's docstring."""
        return HistorySummaryBlock(
            rendered_text=self.rendered_text,
            token_count=self.token_count,
            from_event_seq=self.from_event_seq,
            to_event_seq=self.to_event_seq,
            content_hash=self.content_hash,
        )


async def _resolution_facts(
    tenant_id: uuid.UUID, session_id: uuid.UUID, from_seq: int, to_seq: int
) -> list[MechanicalFact]:
    """Resolutions are visible to every principal in the session by design (INV-7: the
    record *is* the shared truth about what happened; a result nobody may see is a result
    that should never have been rolled in the open). No scope filter applies."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(ResolutionRecordRow)
                .where(
                    ResolutionRecordRow.session_id == session_id,
                    ResolutionRecordRow.event_seq >= from_seq,
                    ResolutionRecordRow.event_seq <= to_seq,
                )
                .order_by(ResolutionRecordRow.event_seq)
            )
        ).scalars()
        return [
            MechanicalFact(
                kind="resolution",
                event_seq=row.event_seq,
                record_id=str(row.id),
                fields={
                    "tool": row.tool_key,
                    "expression": row.expression,
                    "total": row.total,
                    "outcome": row.outcome,
                },
            )
            for row in rows
        ]


async def _entity_change_facts(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    scope_keys: frozenset[str],
    from_seq: int,
    to_seq: int,
    between_sessions_since: datetime | None,
) -> list[MechanicalFact]:
    """Pushed down as a SQL predicate on the entity's own ``scope_key`` (INV-4 discipline:
    the join filters, the Python does not post-filter). An empty scope set yields nothing
    without a query at all -- ``IN ()`` is not a thing worth generating.

    ``between_sessions_since`` (G4.2) widens the selection to the *out-of-session* changes
    -- schedule effects fired by a clock advance, a facilitator's direct edit -- that
    happened while nobody was in a session. They carry ``session_id IS NULL`` and no
    ``event_seq`` by construction, so no event-range predicate could ever have picked them
    up; without this arm, "what changed since last session" would be exactly the part of
    the recap that silently went missing."""
    if not scope_keys:
        return []

    in_session = and_(
        EntityStateChangeRow.session_id == session_id,
        EntityStateChangeRow.event_seq >= from_seq,
        EntityStateChangeRow.event_seq <= to_seq,
    )
    selector = in_session
    if between_sessions_since is not None:
        selector = or_(
            in_session,
            and_(
                EntityStateChangeRow.session_id.is_(None),
                EntityStateChangeRow.created_at >= between_sessions_since,
            ),
        )

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(EntityStateChangeRow, EntityRow.key)
                .join(EntityRow, EntityRow.id == EntityStateChangeRow.entity_id)
                .where(
                    selector,
                    EntityRow.workspace_id == workspace_id,
                    EntityRow.scope_key.in_(scope_keys),
                )
                .order_by(EntityStateChangeRow.created_at)
            )
        ).all()
        return [
            MechanicalFact(
                kind="entity_change",
                event_seq=change.event_seq,
                record_id=str(change.id),
                fields={
                    "entity": entity_key,
                    "field": change.field_path,
                    "from": change.old_value,
                    "to": change.new_value,
                    "cause": change.cause,
                    "between_sessions": change.session_id is None,
                },
            )
            for change, entity_key in rows
        ]


async def _disclosure_facts(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer_id: uuid.UUID,
    from_seq: int,
    to_seq: int,
    *,
    permission_service: PermissionService,
) -> list[MechanicalFact]:
    """A disclosure is in a viewer's recap only if that viewer was actually told
    (``disclosed_to.principal_ids``), or holds ``secret:inspect`` -- the same action the
    overseer read path gates on (E2.10), not a second notion of "senior enough".

    The *fact* of a disclosure is all that lands here: which secret, in what mode, to how
    many principals. Never ``secret.content`` -- this module has no read path to it (INV-1
    keeps ``core.secrets.repo`` out of here entirely), which is the structural version of
    that promise rather than a remembered one."""
    try:
        may_inspect = await permission_service.check(
            tenant_id, viewer_id, _INSPECT_ACTION, "workspace", workspace_id
        )
    except UnknownActionError:
        may_inspect = False

    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(SecretDisclosureEventRow)
                    .where(
                        SecretDisclosureEventRow.session_id == session_id,
                        SecretDisclosureEventRow.event_seq >= from_seq,
                        SecretDisclosureEventRow.event_seq <= to_seq,
                    )
                    .order_by(SecretDisclosureEventRow.event_seq)
                )
            ).scalars()
        )

    facts: list[MechanicalFact] = []
    for row in rows:
        told = row.disclosed_to.get("principal_ids", [])
        told_ids = told if isinstance(told, list) else []
        if not may_inspect and str(viewer_id) not in told_ids:
            continue
        facts.append(
            MechanicalFact(
                kind="disclosure",
                event_seq=row.event_seq,
                record_id=str(row.id),
                fields={
                    "secret": str(row.secret_id),
                    "mode": row.mode,
                    "told_count": len(told_ids),
                },
            )
        )
    return facts


async def collect_visible_facts(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
    *,
    from_event_seq: int,
    to_event_seq: int,
    permission_service: PermissionService,
    between_sessions_since: datetime | None = None,
) -> tuple[MechanicalFact, ...]:
    """The fact frame for one viewer, in event order. Visibility is resolved once, through
    ``core.assembler.visibility.scopes_for`` -- the *same* resolver the assembler and
    (from G4.5) the exporter use. There is deliberately no "history visibility" of its own
    to drift out of step with it.

    ``between_sessions_since`` (G4.2) additionally pulls in out-of-session entity changes
    -- schedule effects, direct edits -- made after that instant, under the same scope
    filter. Pass the previous session's end for the "what changed while you were away"
    half of a resume recap; leave it ``None`` to summarise a session's own span only."""
    scope_set = await scopes_for(tenant_id, viewer.id, workspace_id, phase.visibility, session_id)
    facts = (
        await _resolution_facts(tenant_id, session_id, from_event_seq, to_event_seq)
        + await _entity_change_facts(
            tenant_id,
            workspace_id,
            session_id,
            frozenset(scope_set),
            from_event_seq,
            to_event_seq,
            between_sessions_since,
        )
        + await _disclosure_facts(
            tenant_id,
            workspace_id,
            session_id,
            viewer.id,
            from_event_seq,
            to_event_seq,
            permission_service=permission_service,
        )
    )
    return tuple(sorted(facts, key=lambda f: f.sort_key))


async def collect_prose_by_phase(
    tenant_id: uuid.UUID, session_id: uuid.UUID, *, from_event_seq: int, to_event_seq: int
) -> list[tuple[str, list[str]]]:
    """Walks the session log forward, tracking the current phase from ``phase_transition``
    events and attributing each ``message`` event to it -- the log is the source of truth
    (§5.4), so the chunk boundaries come from the log rather than from a phase column the
    ``message`` table doesn't have. Returns ``[(phase_key, ["<speaker>: text", ...]), ...]``
    in chronological order, one entry per contiguous run of a phase.

    The speaker is part of the text because a summary of a table has to say who did what.
    Joining the prose without it hands the summariser a monologue assembled from six
    people and asks it to narrate the session: it cannot attribute an action to whoever
    took it, so the returning summary says "the party" and "someone" where the record
    knows the name. The log already carries the author on every ``message`` event.
    """
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(SessionEventRow)
                    .where(
                        SessionEventRow.session_id == session_id,
                        SessionEventRow.event_seq >= from_event_seq,
                        SessionEventRow.event_seq <= to_event_seq,
                    )
                    .order_by(SessionEventRow.event_seq)
                )
            ).scalars()
        )

    groups: list[tuple[str, list[str]]] = []
    current_phase = "unknown"
    for row in rows:
        if row.kind == "phase_transition":
            to_phase = row.payload.get("to")
            if isinstance(to_phase, str):
                current_phase = to_phase
            continue
        if row.kind != "message":
            continue
        content = row.payload.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        author = row.payload.get("author")
        if isinstance(author, str) and author.strip():
            content = f"{author.strip()}: {content}"
        phase_of_event = row.payload.get("phase")
        phase_key = phase_of_event if isinstance(phase_of_event, str) else current_phase
        if groups and groups[-1][0] == phase_key:
            groups[-1][1].append(content)
        else:
            groups.append((phase_key, [content]))
    return groups


def _render(from_seq: int, to_seq: int, facts: Sequence[MechanicalFact], narrative: str) -> str:
    lines = [f'<history_summary from="{from_seq}" to="{to_seq}">']
    if facts:
        lines.append("[recorded facts]")
        lines.extend(fact.render() for fact in facts)
    if narrative:
        lines.append("[narrative]")
        lines.append(narrative)
    lines.append("</history_summary>")
    return "\n".join(lines)


def _fit_to_budget(
    from_seq: int, to_seq: int, facts: Sequence[MechanicalFact], narrative: str, max_tokens: int
) -> tuple[tuple[MechanicalFact, ...], str, str, int]:
    """Facts first, narrative second (see module docstring rule 3), both trimmed newest-
    kept until the whole rendered block fits ``max_tokens`` under the *same* token proxy
    the assembler will charge it with. Deterministic -- the same inputs always trim to the
    same block, which is what makes the summary's hash stable enough to replay against."""
    kept_facts = list(facts)
    words = narrative.split()

    while words:
        rendered = _render(from_seq, to_seq, kept_facts, " ".join(words))
        if token_proxy(rendered) <= max_tokens:
            return tuple(kept_facts), " ".join(words), rendered, token_proxy(rendered)
        words = words[: len(words) - max(1, len(words) // 8)]

    while kept_facts:
        rendered = _render(from_seq, to_seq, kept_facts, "")
        if token_proxy(rendered) <= max_tokens:
            return tuple(kept_facts), "", rendered, token_proxy(rendered)
        kept_facts = kept_facts[1:]  # drop oldest first -- the newest state is what resumes

    rendered = _render(from_seq, to_seq, (), "")
    return (), "", rendered, token_proxy(rendered)


async def _meter(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent: Agent,
    rows: Sequence[tuple[int, int, int]],
) -> None:
    """One ``usage_record`` per model call, ``purpose='report'`` (rule 11). Written in one
    transaction with nothing else pending -- summarisation produces no message row to be
    atomic *with*, so "the same transaction as the thing it meters" degenerates here to
    "the same transaction as its own sibling rows", and saying so beats implying an
    atomicity that isn't there."""
    async with tenant_scope(tenant_id) as session:
        for prompt_tokens, completion_tokens, latency_ms in rows:
            session.add(
                UsageRecordRow(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    agent_id=agent.id,
                    provider=agent.provider,
                    model=agent.model,
                    purpose=_PURPOSE,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                )
            )


async def summarise_history(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
    *,
    from_event_seq: int,
    to_event_seq: int,
    max_tokens: int,
    agent: Agent,
    provider: ModelProvider,
    # The connection's decrypted key. Summarising history is a model call like any
    # other; omitting this made it fail with "Missing Anthropic API Key" on every
    # Anthropic-backed persona, and because a summary is an enrichment the failure was
    # a warning nobody read -- so long sessions quietly lost their history instead.
    api_key: str | None = None,
    permission_service: PermissionService,
    between_sessions_since: datetime | None = None,
) -> HistorySummary:
    """Map-reduce over the elapsed range: chunk prose by phase -> summarise each chunk ->
    reduce to one narrative -> render with the record-derived fact frame in front of it.

    With no prose in range, no model call is made at all: a range containing only
    mechanical facts summarises to the fact frame alone, which is complete and correct,
    and spending a model call to narrate nothing would be waste dressed up as diligence.
    """
    facts = await collect_visible_facts(
        tenant_id,
        workspace_id,
        session_id,
        viewer,
        phase,
        from_event_seq=from_event_seq,
        to_event_seq=to_event_seq,
        permission_service=permission_service,
        between_sessions_since=between_sessions_since,
    )
    groups = await collect_prose_by_phase(
        tenant_id, session_id, from_event_seq=from_event_seq, to_event_seq=to_event_seq
    )

    model_string = f"{agent.provider}/{agent.model}"
    metering: list[tuple[int, int, int]] = []
    narrative = ""

    if groups:
        phase_summaries: list[str] = []
        for phase_key, messages in groups:
            req = GenerationRequest(
                egress_policy=await load_egress_policy(tenant_id),
                model=model_string,
                messages=[
                    {"role": "system", "content": _CHUNK_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Phase {phase_key}:\n" + "\n".join(messages)},
                ],
                purpose=_PURPOSE,
                max_tokens=max(64, max_tokens),
                api_base=agent.api_base,
                api_key=api_key,
                params=dict(agent.params or {}),
            )
            start = time.monotonic()
            chunk_result = await provider.generate_structured(req, _PhaseSummary)
            metering.append(
                (
                    sum(
                        provider.count_tokens(str(m.get("content") or ""), model_string)
                        for m in req.messages
                    ),
                    provider.count_tokens(chunk_result.summary, model_string),
                    int((time.monotonic() - start) * 1000),
                )
            )
            phase_summaries.append(f"{phase_key}: {chunk_result.summary}")

        reduce_req = GenerationRequest(
            egress_policy=await load_egress_policy(tenant_id),
            model=model_string,
            messages=[
                {"role": "system", "content": _REDUCE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Combine into at most {max_tokens} words:\n" + "\n".join(phase_summaries)
                    ),
                },
            ],
            purpose=_PURPOSE,
            max_tokens=max(64, max_tokens),
            api_base=agent.api_base,
            api_key=api_key,
            params=dict(agent.params or {}),
        )
        start = time.monotonic()
        reduced = await provider.generate_structured(reduce_req, _ReducedSummary)
        metering.append(
            (
                sum(
                    provider.count_tokens(str(m.get("content") or ""), model_string)
                    for m in reduce_req.messages
                ),
                provider.count_tokens(reduced.narrative, model_string),
                int((time.monotonic() - start) * 1000),
            )
        )
        narrative = reduced.narrative

    if metering:
        await _meter(tenant_id, workspace_id, agent, metering)

    kept_facts, kept_narrative, rendered, token_count = _fit_to_budget(
        from_event_seq, to_event_seq, facts, narrative, max_tokens
    )
    return HistorySummary(
        from_event_seq=from_event_seq,
        to_event_seq=to_event_seq,
        facts=kept_facts,
        narrative=kept_narrative,
        rendered_text=rendered,
        token_count=token_count,
        content_hash=hashlib.sha256(rendered.encode()).hexdigest(),
    )
