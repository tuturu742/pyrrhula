"""Between-session state: the workspace clock and scheduled entity effects.

Workspaces are not only alive during sessions. Time passes between game nights; a ticket
ages between review cycles; a work item is unblocked while nobody is in a session. Three
pieces make that real, and each is deliberately narrow:

**The clock is explicit.** ``workspace.clock_value`` moves only when someone calls
``advance_clock`` -- never as a side effect of reading the workspace, starting a session,
or the wall-clock ticking over. This is the difference between "the world state changed
because the facilitator advanced the timeline" and "the world state changed because
somebody opened a page", and only the first is something a session can be resumed against.
It is also why the clock is an integer rather than a timestamp: the unit is whatever the
workspace's overlay says (a day, an iteration, a review cycle), and core has no business
deciding that a fictional month is 30 real days.

**Schedules are declarative data, not code** (CLAUDE.md rule 10). An ``entity_schedule``
row says *when* (``at clock >= X`` or ``every N``) and *what* (a field-change map handed
straight to the ``mutate``). There is no expression to evaluate and nothing user-
supplied to execute.

**Exactly-once is rule 8's mechanism, not a bespoke one.** Every application derives its
idempotency key from ``(schedule_id, tick)``, so a worker retry, a re-run of an overlapping
clock range, or two workers racing the same advance all collapse onto the same
``completed_operation`` row. Nothing here tracks a "last applied" cursor that could drift
out of step with what actually happened; the record of what happened is
``entity_state_change``, as it is for every other mutation.

Applied effects land with ``cause='fsm'`` and ``cause_ref='schedule:<id>:<tick>'``, and
``session_id = NULL`` -- the out-of-session mutation surface the mutation service
supports (``entity_state_change.session_id`` is nullable precisely for this).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.entities.fsm import EntityStateChangeRow
from core.entities.mutation import mutate
from core.entities.storage import EntityRow
from core.ports.permission import PermissionService
from core.tenancy.models import Base, Workspace
from core.tenancy.scope import tenant_scope

_ADVANCE_CLOCK_ACTION = "workspace:advance_clock"


class ClockRewindError(Exception):
    """The clock only moves forward. Rewinding it would make already-applied schedule
    effects re-eligible with the same ``(schedule, tick)`` keys they already consumed --
    they would no-op, so the world would silently disagree with the clock. Forking a
    session from a checkpoint is the supported way to revisit an earlier state."""


class ClockPermissionDeniedError(Exception):
    pass


class EntityScheduleRow(Base):
    """A declarative schedule entry. Not append-only: ``enabled`` is a real toggle (a
    facilitator turns a recurring effect off), so the app role keeps UPDATE here.
    Exactly-once application does not depend on this row's immutability -- see the module
    docstring."""

    __tablename__ = "entity_schedule"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("entity.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    # 'at' -- fires once, on the first tick at or after ``threshold``.
    # 'every' -- fires on every tick that is a multiple of ``threshold``.
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    changes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('at', 'every')", name="ck_entity_schedule_kind"),
        CheckConstraint("threshold >= 1", name="ck_entity_schedule_threshold_positive"),
        UniqueConstraint("workspace_id", "key", name="uq_entity_schedule_workspace_key"),
        Index("ix_entity_schedule_workspace", "workspace_id"),
    )


@dataclass(frozen=True)
class ClockAdvance:
    """The range a clock advance opened. Hand it to ``apply_due_schedules`` (the worker
    does) -- re-driving the same range is a no-op for anything already applied, which is
    what makes the two-step safe to retry rather than merely unlikely to be retried."""

    from_clock: int
    to_clock: int


@dataclass(frozen=True)
class AppliedEffect:
    schedule_id: uuid.UUID
    schedule_key: str
    entity_id: uuid.UUID
    tick: int
    cause_ref: str
    entity_version: int


def due_ticks(kind: str, threshold: int, from_clock: int, to_clock: int) -> list[int]:
    """Which ticks in ``(from_clock, to_clock]`` this schedule fires on. Half-open on the
    left on purpose: a tick the clock has already passed was already offered to this
    schedule on the advance that reached it, and offering it again is exactly the
    double-application the idempotency key would then have to catch -- better not to
    generate it. Pure and total; every branch is unit-testable without a database."""
    if threshold < 1 or to_clock <= from_clock:
        return []
    if kind == "at":
        return [threshold] if from_clock < threshold <= to_clock else []
    if kind == "every":
        first = (from_clock // threshold + 1) * threshold
        return list(range(first, to_clock + 1, threshold))
    raise ValueError(f"unknown schedule kind {kind!r}; expected 'at' or 'every'")


async def create_schedule(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    key: str,
    kind: str,
    threshold: int,
    changes: Mapping[str, object],
    *,
    created_by: uuid.UUID | None = None,
) -> EntityScheduleRow:
    if kind not in ("at", "every"):
        raise ValueError(f"unknown schedule kind {kind!r}; expected 'at' or 'every'")
    if threshold < 1:
        raise ValueError(f"schedule threshold must be >= 1, got {threshold!r}")
    async with tenant_scope(tenant_id) as session:
        row = EntityScheduleRow(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            entity_id=entity_id,
            key=key,
            kind=kind,
            threshold=threshold,
            changes=dict(changes),
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        session.expunge(row)
        return row


async def get_clock(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    """A pure read. It has no write path at all -- which is the point of the acceptance
    criterion "the clock never advances as a side effect of reads"."""
    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"no workspace {workspace_id} in this tenant")
        return workspace.clock_value


async def apply_due_schedules(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    from_clock: int,
    to_clock: int,
    *,
    permission_service: PermissionService,
) -> list[AppliedEffect]:
    """Applies every enabled schedule's due effects across ``(from_clock, to_clock]``,
    each through the ``mutate`` with its own ``(schedule, tick)`` idempotency key.

    Separate from ``advance_clock`` so a crashed advance can be re-driven by the worker
    over the same range without re-advancing the clock: replaying a range is a no-op by
    construction, which is what makes the retry safe rather than merely unlikely to
    happen twice."""
    async with tenant_scope(tenant_id) as session:
        schedules = list(
            (
                await session.execute(
                    select(EntityScheduleRow)
                    .where(
                        EntityScheduleRow.workspace_id == workspace_id,
                        EntityScheduleRow.enabled.is_(True),
                    )
                    .order_by(EntityScheduleRow.key)
                )
            ).scalars()
        )
        for row in schedules:
            session.expunge(row)

    applied: list[AppliedEffect] = []
    for schedule in schedules:
        for tick in due_ticks(schedule.kind, schedule.threshold, from_clock, to_clock):
            cause_ref = f"schedule:{schedule.id}:{tick}"
            result = await mutate(
                principal_id,
                tenant_id,
                workspace_id,
                schedule.entity_id,
                schedule.changes,
                "fsm",
                cause_ref,
                cause_ref,
                permission_service=permission_service,
                session_id=None,
                event_seq=None,
            )
            applied.append(
                AppliedEffect(
                    schedule_id=schedule.id,
                    schedule_key=schedule.key,
                    entity_id=schedule.entity_id,
                    tick=tick,
                    cause_ref=cause_ref,
                    entity_version=int(result["version"]),
                )
            )
    return applied


async def advance_clock(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    to_value: int,
    *,
    permission_service: PermissionService,
) -> ClockAdvance:
    """The one and only way ``workspace.clock_value`` moves. Permission-gated on
    ``workspace:advance_clock`` through the port (CLAUDE.md rule 12 -- never inline role
    logic) and forward-only.

    It moves the clock and *only* the clock. Applying the schedules the move made due is
    ``apply_due_schedules`` over the returned range, driven by the worker
    (``worker.schedules``) -- deliberately a second step, for two reasons: a clock advance
    across a long gap can fan out into a great many mutations that no HTTP request should
    hold open, and a crash between the two leaves a workspace whose clock is correct and
    whose effects are merely pending, which re-driving the range fixes exactly (every
    application is idempotent on ``(schedule, tick)``). The other order -- effects first --
    would leave effects applied at a clock value that never officially happened, which
    nothing can reconcile."""
    if not await permission_service.check(
        tenant_id, principal_id, _ADVANCE_CLOCK_ACTION, "workspace", workspace_id
    ):
        raise ClockPermissionDeniedError(
            f"principal {principal_id} may not advance the clock of workspace {workspace_id}"
        )

    async with tenant_scope(tenant_id) as session:
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"no workspace {workspace_id} in this tenant")
        from_clock = workspace.clock_value
        if to_value < from_clock:
            raise ClockRewindError(
                f"workspace {workspace_id} clock is at {from_clock}; refusing to rewind it "
                f"to {to_value}"
            )
        workspace.clock_value = to_value
        workspace.clock_advanced_at = datetime.now(UTC)

    return ClockAdvance(from_clock=from_clock, to_clock=to_value)


@dataclass(frozen=True)
class ChangeFeedRow:
    """One row of the between-sessions change feed -- a *record*, not prose. The overseer
    UI renders these directly; nothing here is summarised or narrated (that is the job,
    on the way into a resumed session's context, and the at report scale)."""

    change_id: uuid.UUID
    entity_id: uuid.UUID
    field_path: str
    old_value: object | None
    new_value: object | None
    cause: str
    cause_ref: str | None
    in_session: bool
    created_at: datetime


async def list_change_feed(
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    scope_keys: frozenset[str],
    *,
    since: datetime | None = None,
    out_of_session_only: bool = False,
    limit: int = 200,
) -> list[ChangeFeedRow]:
    """ "What changed since last session" as a query over the change log, filtered by the
    viewer's resolved scope set pushed down onto the entity's own ``scope_key`` (INV-4
    discipline -- the join filters; nothing is dropped in Python afterwards). ``scope_keys``
    is required and defaultless for the same reason every other scoped query's is."""
    if not scope_keys:
        return []
    async with tenant_scope(tenant_id) as session:
        stmt = (
            select(EntityStateChangeRow)
            .join(EntityRow, EntityRow.id == EntityStateChangeRow.entity_id)
            .where(
                EntityRow.workspace_id == workspace_id,
                EntityRow.scope_key.in_(scope_keys),
            )
        )
        if since is not None:
            stmt = stmt.where(EntityStateChangeRow.created_at >= since)
        if out_of_session_only:
            stmt = stmt.where(EntityStateChangeRow.session_id.is_(None))
        rows = list(
            (
                await session.execute(
                    stmt.order_by(EntityStateChangeRow.created_at.desc()).limit(limit)
                )
            ).scalars()
        )

    return [
        ChangeFeedRow(
            change_id=row.id,
            entity_id=row.entity_id,
            field_path=row.field_path,
            old_value=row.old_value,
            new_value=row.new_value,
            cause=row.cause,
            cause_ref=row.cause_ref,
            in_session=row.session_id is not None,
            created_at=row.created_at,
        )
        for row in rows
    ]
