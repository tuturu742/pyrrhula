"""Async / play-by-post notification pipeline.

A play-by-post game and a week-long enterprise review cycle are the same mechanism: an
``await_state`` with a long timeout and humans who need to be told it is their turn. This
module owns *when* and *to whom*; ``core.ports.notifier`` owns *how*, and the channel is
an adapter (email, chat webhook, push) that core never names.

**Exactly once is a constraint, not a convention.** Every notification carries a
``dedupe_key`` (``await_opened:<id>``, ``await_reminder:<id>``, ``digest:<principal>:
<window>``) with a UNIQUE index behind it. ``record_notification`` returns ``None`` on a
duplicate rather than raising: a second sweep tick discovering it has nothing to do is the
system working, not an error. The adapter is called only for a row this process actually
won, so at-least-once job delivery becomes exactly-once notification.

**A digest is a small report, so the report rule applies**:
its event selection runs through the *recipient's* visibility before anything is rendered,
by reusing ``core.sessions.history.collect_visible_facts`` rather than re-deriving what a
principal may see. A digest that scrubbed after selecting would be one more surface where
a scrub could be forgotten; there is no scrub here because there is nothing to scrub.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from core.ports.notifier import Notification, Notifier
from core.ports.permission import PermissionService
from core.process.dsl.schema import PhaseSpec
from core.sessions.history import MechanicalFact, collect_visible_facts
from core.sessions.models import AwaitStateRow, SessionRow
from core.tenancy.models import Base, Principal, WorkspaceMembership
from core.tenancy.roles import roles_satisfying
from core.tenancy.scope import tenant_scope

# Which ``any_of`` tokens (DSL ) name a *human*. An agent actor needs no email.
_HUMAN_TOKENS = frozenset({"human_participant", "human_overseer"})
_TOKEN_ROLES = {"human_participant": "participant", "human_overseer": "overseer"}


class NotificationRow(Base):
    """The durable record of what was sent. Not append-only: ``sent_at`` is stamped after
    the adapter returns, so "recorded but not yet delivered" is a state the row can be in
    -- which is the state a crash between the two leaves behind, and the one a retry can
    recognise and finish."""

    __tablename__ = "notification"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=True
    )
    await_state_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("await_state.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('await_opened', 'await_reminder', 'digest')", name="ck_notification_kind"
        ),
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_notification_tenant_dedupe"),
        Index("ix_notification_principal", "principal_id"),
    )


async def record_notification(
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    kind: str,
    dedupe_key: str,
    subject: str,
    body_md: str,
    *,
    session_id: uuid.UUID | None = None,
    await_state_id: uuid.UUID | None = None,
) -> NotificationRow | None:
    """Claims the right to send. ``None`` means someone already claimed it -- the normal
    outcome of a retried job, not a failure. The UNIQUE constraint is what decides, so two
    workers racing the same await produce one notification without coordinating."""
    async with tenant_scope(tenant_id) as session:
        row = NotificationRow(
            tenant_id=tenant_id,
            principal_id=principal_id,
            session_id=session_id,
            await_state_id=await_state_id,
            kind=kind,
            dedupe_key=dedupe_key,
            subject=subject,
            body_md=body_md,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return None
        session.expunge(row)
        return row


async def mark_sent(tenant_id: uuid.UUID, notification_id: uuid.UUID) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.get(NotificationRow, notification_id)
        if row is not None:
            row.sent_at = datetime.now(UTC)


async def _deliver(
    tenant_id: uuid.UUID, row: NotificationRow, notifier: Notifier
) -> NotificationRow:
    await notifier.send(
        Notification(
            tenant_id=tenant_id,
            principal_id=row.principal_id,
            kind=row.kind,
            subject=row.subject,
            body_md=row.body_md,
            dedupe_key=row.dedupe_key,
        )
    )
    await mark_sent(tenant_id, row.id)
    return row


async def human_recipients(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, expected_from: dict[str, object]
) -> list[uuid.UUID]:
    """Who the await was waiting on, restricted to humans. Reads the actor specs
    ``create_await`` recorded on ``await_state.expected_from`` -- descriptive of who was
    eligible when the await opened, which is exactly the question "who should be told"
    asks. An agent actor is skipped: agents do not read email, and a notification to one
    would be a row nobody ever acts on.

    ``persona_type``-selected actors resolve to agents and are therefore always skipped;
    ``human_participant: all`` and the ``human_*`` ``any_of`` tokens resolve to workspace
    memberships."""
    specs = expected_from.get("phase_actors", [])
    if not isinstance(specs, list):
        return []

    wanted_roles: set[str] = set()
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("human_participant") == "all":
            wanted_roles.add("participant")
        any_of = spec.get("any_of")
        if isinstance(any_of, list):
            for token in any_of:
                if token in _HUMAN_TOKENS:
                    wanted_roles.add(_TOKEN_ROLES[str(token)])
    if not wanted_roles:
        return []

    # Expand each wanted role to every stored role that satisfies it, so a steward -- who
    # is a human overseer for notification purposes -- is not missed by an ``in
    # ('overseer',)`` filter. Pushed down as the SQL predicate, not post-filtered.
    stored_roles = {r for wanted in wanted_roles for r in roles_satisfying(wanted)}

    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(WorkspaceMembership.principal_id)
                    .join(Principal, Principal.id == WorkspaceMembership.principal_id)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.role.in_(stored_roles),
                        Principal.kind != "agent",
                    )
                    .order_by(WorkspaceMembership.principal_id)
                )
            ).scalars()
        )
    return rows


async def notify_await_opened(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    await_state_id: uuid.UUID,
    *,
    notifier: Notifier,
) -> list[NotificationRow]:
    """One "it's your turn" per eligible human, ever. Called by the worker after an await
    is created -- not inside ``create_await``, which runs in the interpreter's transaction
    and has no business making a network call."""
    async with tenant_scope(tenant_id) as session:
        await_row = await session.get(AwaitStateRow, await_state_id)
        if await_row is None:
            raise ValueError(f"no await_state {await_state_id} in this tenant")
        session_row = await session.get(SessionRow, await_row.session_id)
        assert session_row is not None
        session_id = await_row.session_id
        expected_from = dict(await_row.expected_from)
        current_phase = session_row.current_phase

    sent: list[NotificationRow] = []
    for principal_id in await human_recipients(tenant_id, workspace_id, expected_from):
        row = await record_notification(
            tenant_id,
            principal_id,
            "await_opened",
            f"await_opened:{await_state_id}",
            "It's your turn",
            f"Session {session_id} is waiting on you in phase `{current_phase}`.",
            session_id=session_id,
            await_state_id=await_state_id,
        )
        if row is not None:
            sent.append(await _deliver(tenant_id, row, notifier))
    return sent


async def notify_await_reminder(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    await_state_id: uuid.UUID,
    *,
    notifier: Notifier,
) -> list[NotificationRow]:
    """The single nudge, at the phase's (or definition's) configured ``reminder_at``. A
    different ``dedupe_key`` from the opening notification, so the two never suppress each
    other -- and the same key on every retry, so the nudge never doubles."""
    async with tenant_scope(tenant_id) as session:
        await_row = await session.get(AwaitStateRow, await_state_id)
        if await_row is None:
            raise ValueError(f"no await_state {await_state_id} in this tenant")
        session_id = await_row.session_id
        expected_from = dict(await_row.expected_from)
        timeout_at = await_row.timeout_at

    sent: list[NotificationRow] = []
    for principal_id in await human_recipients(tenant_id, workspace_id, expected_from):
        row = await record_notification(
            tenant_id,
            principal_id,
            "await_reminder",
            f"await_reminder:{await_state_id}",
            "Still waiting on you",
            f"Session {session_id} is still waiting on you. It moves on at "
            f"{timeout_at:%Y-%m-%d %H:%M} UTC.",
            session_id=session_id,
            await_state_id=await_state_id,
        )
        if row is not None:
            sent.append(await _deliver(tenant_id, row, notifier))
    return sent


@dataclass(frozen=True)
class PendingAwait:
    await_state_id: uuid.UUID
    session_id: uuid.UUID
    phase: str
    timeout_at: datetime


@dataclass(frozen=True)
class Digest:
    principal_id: uuid.UUID
    pending: tuple[PendingAwait, ...]
    facts: tuple[MechanicalFact, ...]
    body_md: str


async def _pending_awaits_for(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID
) -> list[PendingAwait]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(AwaitStateRow, SessionRow.current_phase)
                .join(SessionRow, SessionRow.id == AwaitStateRow.session_id)
                .where(
                    AwaitStateRow.outcome.is_(None),
                    SessionRow.workspace_id == workspace_id,
                )
                .order_by(AwaitStateRow.timeout_at)
            )
        ).all()
        pending = [
            (
                PendingAwait(
                    await_state_id=row.id,
                    session_id=row.session_id,
                    phase=str(phase),
                    timeout_at=row.timeout_at,
                ),
                dict(row.expected_from),
            )
            for row, phase in rows
        ]

    out: list[PendingAwait] = []
    for entry, expected_from in pending:
        if principal_id in await human_recipients(tenant_id, workspace_id, expected_from):
            out.append(entry)
    return out


async def build_digest(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    viewer: Principal,
    phase: PhaseSpec,
    *,
    session_id: uuid.UUID,
    from_event_seq: int,
    to_event_seq: int,
    permission_service: PermissionService,
    between_sessions_since: datetime | None = None,
) -> Digest:
    """The recipient's own digest: what they are holding up, and what has happened that
    they may see. The second half goes through ``collect_visible_facts`` -- the
    visibility-filtered fact frame -- so a digest is filtered by the same resolver as a
    resumed turn's context and an export. Never present beats scrubbed after."""
    pending = await _pending_awaits_for(tenant_id, workspace_id, viewer.id)
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

    lines = ["## Waiting on you"]
    if pending:
        lines.extend(
            f"- session {p.session_id} (phase `{p.phase}`), until {p.timeout_at:%Y-%m-%d %H:%M} UTC"
            for p in pending
        )
    else:
        lines.append("- nothing right now")
    lines.append("")
    lines.append("## Since you last looked")
    if facts:
        lines.extend(fact.render() for fact in facts)
    else:
        lines.append("- nothing you can see has changed")

    return Digest(
        principal_id=viewer.id,
        pending=tuple(pending),
        facts=facts,
        body_md="\n".join(lines),
    )


async def send_digest(
    tenant_id: uuid.UUID, digest: Digest, window_key: str, *, notifier: Notifier
) -> NotificationRow | None:
    """One digest per principal per window. ``window_key`` is the caller's periodicity
    (e.g. an ISO date for daily) -- keeping it out of this function means "daily" is a
    scheduling decision, not something baked into the notification layer."""
    row = await record_notification(
        tenant_id,
        digest.principal_id,
        "digest",
        f"digest:{digest.principal_id}:{window_key}",
        "Your session digest",
        digest.body_md,
    )
    if row is None:
        return None
    return await _deliver(tenant_id, row, notifier)
