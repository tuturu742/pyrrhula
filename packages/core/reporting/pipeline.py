"""The report pipeline.

Same two rules as the history summariser, at report scale -- and deliberately the *same
implementation* of both, because a second copy is a second thing to get wrong:

**Visibility first, never scrubbed after.** The event stream is filtered for
`(generated_for_principal, audience_mode)` before any model call, by reusing
`core.sessions.history.collect_visible_facts`. A participant recap is generated from a
context that never contained the villain's secret. Not scrubbed after -- never present.

**Structured facts come from records.** Resolutions, entity changes, and disclosures enter
as frames read straight from their tables; the model narrates *around* them. A recap that
misremembers who died is worse than no recap, and there is no reason to let a model
paraphrase a number sitting in a table.

`redactions[]` renders as **visible stubs** ("[3 events not visible to you]"). Silent
omission is worse than acknowledged omission: it lets a reader mistake an incomplete recap
for a complete one, and that mistake is the whole failure mode reporting has.

Provenance (`source_event_range`, `source_manifest_ids`, `fact_frame_hash`) is what makes a
report checkable: given the same range, the frame rebuilds identically, and the hash says
so without anyone re-reading the prose.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.agents.models import Agent
from core.assembler.models import ContextManifestRow
from core.audit.models import UsageRecordRow
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.ports.permission import PermissionService
from core.process.dsl.schema import PhaseSpec
from core.reporting.templates import ReportTemplate
from core.sessions.history import MechanicalFact, collect_prose_by_phase, collect_visible_facts
from core.tenancy.egress import load_egress_policy
from core.tenancy.models import Base, Principal
from core.tenancy.scope import tenant_scope

_PURPOSE = "report"

_CHUNK_SYSTEM_PROMPT = (
    "You summarise one phase of a session transcript into two or three plain sentences for "
    "a reader who was not present. Summarise only what the messages say. Never state a "
    "numeric result, a state change, or who was told what -- those are supplied separately "
    "from the system's own records."
)
_REDUCE_SYSTEM_PROMPT = (
    "You combine per-phase summaries into one continuous narrative, in chronological "
    "order. Never introduce a numeric result, outcome, or state change that is not already "
    "in the text you were given."
)


class _PhaseSummary(BaseModel):
    summary: str


class _Narrative(BaseModel):
    narrative: str


class ReportRow(Base):
    """'s `report`. ``generated_for_principal_id`` and ``audience_mode`` are NOT NULL
    together: a report that could not say who it was for is a report whose visibility
    nobody can decide afterwards."""

    __tablename__ = "report"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    template_key: Mapped[str] = mapped_column(String(63), nullable=False)
    audience_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    generated_for_principal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    source_event_from: Mapped[int] = mapped_column(Integer, nullable=False)
    source_event_to: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    redactions: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    artifacts: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    fact_frame_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "audience_mode IN ('participant', 'overseer', 'sanitised')",
            name="ck_report_audience_mode",
        ),
        Index("ix_report_session", "session_id"),
        Index("ix_report_target", "generated_for_principal_id"),
    )


@dataclass(frozen=True)
class FactFrame:
    """The structured half of a report: facts read from records, never through a model.
    ``content_hash`` is over the rendered frame, so "the same range rebuilds the same
    frame" is checkable without re-reading prose."""

    facts: tuple[MechanicalFact, ...]
    rendered: str
    content_hash: str


def build_fact_frame(facts: tuple[MechanicalFact, ...], kinds: list[str]) -> FactFrame:
    selected = tuple(f for f in facts if not kinds or f.kind in kinds)
    rendered = "\n".join(f.render() for f in selected)
    return FactFrame(
        facts=selected,
        rendered=rendered,
        content_hash=hashlib.sha256(rendered.encode()).hexdigest(),
    )


def render_redaction_stub(count: int) -> str:
    """The draft notice's wording. One function so every renderer -- markdown here, PDF
    and EPUB elsewhere -- emits the identical string, and a format that quietly dropped it would be
    visibly different rather than plausibly different."""
    noun = "event" if count == 1 else "events"
    return f"> _[{count} {noun} not visible to you]_"


@dataclass(frozen=True)
class ReportResult:
    report_id: uuid.UUID
    content_md: str
    fact_frame: FactFrame
    redactions: list[dict[str, object]]


async def generate_report(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
    template: ReportTemplate,
    *,
    from_event_seq: int,
    to_event_seq: int,
    agent: Agent,
    provider: ModelProvider,
    permission_service: PermissionService,
    # The connection's decrypted key. A report is a model call like any other and was
    # the one that went out without one: every deployment whose provider needs a key got
    # an authentication error from a connection that worked perfectly for turns, and the
    # sealed credential sat unread two fields away.
    api_key: str | None = None,
) -> ReportResult:
    """Runs the template's pipeline and writes the `report` row.

    ``phase`` supplies the visibility spec the filter resolves against -- passed in rather
    than derived here, for the same reason ``assemble()`` takes it: the caller knows which
    phase's rules apply, and a reporting module guessing would be a second place that
    decides visibility."""
    facts = await collect_visible_facts(
        tenant_id,
        workspace_id,
        session_id,
        viewer,
        phase,
        from_event_seq=from_event_seq,
        to_event_seq=to_event_seq,
        permission_service=permission_service,
    )

    fact_step = next(s for s in template.pipeline if s.kind == "fact_frame")
    frame = build_fact_frame(facts, fact_step.fact_kinds)

    total_events, visible_prose = await _prose_and_counts(
        tenant_id, session_id, from_event_seq, to_event_seq, template.audience_mode
    )

    narrative = ""
    metering: list[tuple[int, int, int]] = []
    if visible_prose and any(s.kind in ("chunk_summarise", "reduce") for s in template.pipeline):
        narrative = await _summarise(
            tenant_id, template, visible_prose, agent, provider, metering, api_key
        )
    if metering:
        await _meter(tenant_id, workspace_id, agent, metering)

    hidden = max(0, total_events - sum(len(msgs) for _, msgs in visible_prose))
    redactions: list[dict[str, object]] = []
    if hidden:
        redactions.append({"type": "event", "count": hidden, "reason": "not visible to you"})

    content_md = _render_markdown(template, frame, narrative, hidden)
    manifest_ids = await _manifest_ids(tenant_id, session_id, from_event_seq, to_event_seq)

    async with tenant_scope(tenant_id) as session:
        row = ReportRow(
            tenant_id=tenant_id,
            session_id=session_id,
            template_key=template.key,
            audience_mode=template.audience_mode,
            generated_for_principal_id=viewer.id,
            source_event_from=from_event_seq,
            source_event_to=to_event_seq,
            source_manifest_ids=manifest_ids,
            content_md=content_md,
            redactions=redactions,
            fact_frame_hash=frame.content_hash,
        )
        session.add(row)
        await session.flush()
        report_id = row.id

    return ReportResult(
        report_id=report_id,
        content_md=content_md,
        fact_frame=frame,
        redactions=redactions,
    )


async def _prose_and_counts(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    from_seq: int,
    to_seq: int,
    audience_mode: str,
) -> tuple[int, list[tuple[str, list[str]]]]:
    """``(total_message_count, prose_visible_to_this_audience)``.

    A **sanitised** report gets no prose at all: prose is where a model's paraphrase of a
    concealed fact would live, and a report meant to be safe to hand anyone should be
    facts and nothing else. The difference between the two counts is what the redaction
    stub reports, so the stub is arithmetic rather than an estimate."""
    groups = await collect_prose_by_phase(
        tenant_id, session_id, from_event_seq=from_seq, to_event_seq=to_seq
    )
    total = sum(len(msgs) for _, msgs in groups)
    if audience_mode == "sanitised":
        return total, []
    return total, groups


async def _summarise(
    tenant_id: uuid.UUID,
    template: ReportTemplate,
    groups: list[tuple[str, list[str]]],
    agent: Agent,
    provider: ModelProvider,
    metering: list[tuple[int, int, int]],
    api_key: str | None = None,
) -> str:
    """Map-reduce, with the template's own budgets. ``purpose='report'`` on every call, so
    a tenant whose egress policy pins reporting to local models gets that enforced inside
    the ``ModelProvider`` port rather than remembered here."""
    model_string = f"{agent.provider}/{agent.model}"
    chunk_step = next((s for s in template.pipeline if s.kind == "chunk_summarise"), None)
    reduce_step = next((s for s in template.pipeline if s.kind == "reduce"), None)

    summaries: list[str] = []
    if chunk_step is not None:
        for phase_key, messages in groups:
            summaries.append(
                f"{phase_key}: "
                + await _call(
                    tenant_id,
                    provider,
                    model_string,
                    agent,
                    _CHUNK_SYSTEM_PROMPT,
                    f"Phase {phase_key}:\n" + "\n".join(messages),
                    chunk_step.max_tokens,
                    _PhaseSummary,
                    metering,
                    api_key,
                )
            )
    else:
        summaries = [f"{phase_key}: " + "\n".join(messages) for phase_key, messages in groups]

    if reduce_step is None:
        return "\n".join(summaries)
    return await _call(
        tenant_id,
        provider,
        model_string,
        agent,
        _REDUCE_SYSTEM_PROMPT,
        f"Combine into at most {reduce_step.max_tokens} words:\n" + "\n".join(summaries),
        reduce_step.max_tokens,
        _Narrative,
        metering,
        api_key,
    )


async def _call(
    tenant_id: uuid.UUID,
    provider: ModelProvider,
    model_string: str,
    agent: Agent,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    schema: type[_PhaseSummary] | type[_Narrative],
    metering: list[tuple[int, int, int]],
    api_key: str | None = None,
) -> str:
    req = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        purpose=_PURPOSE,
        max_tokens=max(64, max_tokens),
        api_base=agent.api_base,
        api_key=api_key,
        params=dict(agent.params or {}),
    )
    start = time.monotonic()
    result = await provider.generate_structured(req, schema)
    text = result.summary if isinstance(result, _PhaseSummary) else result.narrative
    metering.append(
        (
            sum(
                provider.count_tokens(str(m.get("content") or ""), model_string)
                for m in req.messages
            ),
            provider.count_tokens(text, model_string),
            int((time.monotonic() - start) * 1000),
        )
    )
    return text


async def _meter(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    agent: Agent,
    rows: list[tuple[int, int, int]],
) -> None:
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


def _render_markdown(
    template: ReportTemplate, frame: FactFrame, narrative: str, hidden_count: int
) -> str:
    """Facts first, narrative second, stub last. The order is the argument: a reader who
    stops after the first section has read the part that is true by construction."""
    parts = [f"# {template.label_key}", "", "## Recorded facts"]
    parts.append(frame.rendered if frame.rendered else "_No recorded facts in this range._")
    if narrative:
        parts.extend(["", "## Narrative", narrative])
    if hidden_count:
        parts.extend(["", render_redaction_stub(hidden_count)])
    return "\n".join(parts)


async def _manifest_ids(
    tenant_id: uuid.UUID, session_id: uuid.UUID, from_seq: int, to_seq: int
) -> list[uuid.UUID]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(ContextManifestRow.id)
                .where(
                    ContextManifestRow.session_id == session_id,
                    ContextManifestRow.event_seq >= from_seq,
                    ContextManifestRow.event_seq <= to_seq,
                )
                .order_by(ContextManifestRow.event_seq)
            )
        ).scalars()
        return list(rows)


async def get_report(tenant_id: uuid.UUID, report_id: uuid.UUID) -> ReportRow | None:
    async with tenant_scope(tenant_id) as session:
        return await session.get(ReportRow, report_id)


async def mark_reviewed(
    tenant_id: uuid.UUID, report_id: uuid.UUID, reviewer_principal_id: uuid.UUID
) -> ReportRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(ReportRow, report_id)
        if row is None:
            raise ValueError(f"no report {report_id} in this tenant")
        row.reviewed_by = reviewer_principal_id
        row.reviewed_at = datetime.now(UTC)
        await session.flush()
        session.expunge(row)
        return row


async def regenerate_fact_frame(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    report: ReportRow,
    viewer: Principal,
    phase: PhaseSpec,
    template: ReportTemplate,
    *,
    permission_service: PermissionService,
) -> FactFrame:
    """Rebuilds the frame from the report's own recorded provenance. The acceptance
    criterion is that this is byte-identical to the original -- which is only true because
    the frame comes from records, and is exactly the property that makes it worth
    recording provenance at all."""
    facts = await collect_visible_facts(
        tenant_id,
        workspace_id,
        report.session_id,
        viewer,
        phase,
        from_event_seq=report.source_event_from,
        to_event_seq=report.source_event_to,
        permission_service=permission_service,
    )
    fact_step = next(s for s in template.pipeline if s.kind == "fact_frame")
    return build_fact_frame(facts, fact_step.fact_kinds)


async def list_reports_for_session(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[ReportRow]:
    """Newest first -- the UI's "which reports exist for this session" read; the POST
    endpoint returns only a job id, so without this a generated report was findable
    only by guessing."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(ReportRow)
                .where(ReportRow.session_id == session_id)
                .order_by(ReportRow.created_at.desc())
            )
        ).scalars()
        return list(rows)
