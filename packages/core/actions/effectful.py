"""`EffectfulAction` (CLAUDE.md rule 8).

A tool call that changes the world outside Pyrrhula -- posting a message, opening a pull
request, booking something -- cannot simply be retried. Resume-from-checkpoint re-executes
work by design, so every such call carries an idempotency key
``(session_id, event_seq, attempt_target)`` and leaves an `action_record` saying what it
did.

**The record is the answer to "did I already do this?"** Not the external world: asking a
remote system whether it saw your request needs the request to have been identifiable,
which is the thing you were trying to establish. So the record is written *before* the
call (`dispatched`, outcome NULL) and completed after. A crash in between leaves a
dispatched-but-unresolved row, which is exactly the state `reconcile` exists to resolve --
and resolving it is a *lookup*, never a re-dispatch.

**Outcome is claimed once, atomically**, the same conditional-UPDATE shape
`await_state.outcome` uses: `UPDATE ... WHERE outcome IS NULL`. Two workers racing to
complete the same action means one wins and one learns it lost, with no locking beyond
what Postgres already gives.

**Returned content is data.** `core.mcp.client` wraps every result in the injection
envelope before it reaches a model, and nothing a tool returns is ever consulted when
deciding what may be called next. That separation is the whole reason a tool's *response*
cannot authorise a further call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base
from core.tenancy.scope import tenant_scope


def idempotency_key(session_id: uuid.UUID, event_seq: int, attempt_target: str) -> str:
    """Rule 8's key, in one place. Every effectful path derives its key here so two of them
    can never disagree about what "the same operation" means."""
    return f"{session_id}:{event_seq}:{attempt_target}"


class ActionRecordRow(Base):
    """Append-only in the DELETE direction (rule 5 as far as it can apply): an effectful
    call that happened cannot un-happen, so DELETE is revoked. UPDATE stays, because
    completing a dispatched action is the one write this row exists to receive."""

    __tablename__ = "action_record"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_target: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    server_key: Mapped[str] = mapped_column(String(63), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(127), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('completed', 'failed', 'reconciled')",
            name="ck_action_record_outcome",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_action_record_tenant_idempotency"
        ),
        Index("ix_action_record_session", "session_id"),
    )


@dataclass(frozen=True)
class EffectfulAction:
    """One effectful call, identified rather than described: the key is what makes it the
    *same* call across a restart."""

    tenant_id: uuid.UUID
    session_id: uuid.UUID
    event_seq: int
    attempt_target: str
    server_key: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return idempotency_key(self.session_id, self.event_seq, self.attempt_target)


@dataclass(frozen=True)
class ClaimResult:
    """``fresh`` means this caller owns the dispatch. ``existing`` carries the row either
    way, so a loser can read the winner's outcome instead of guessing at it."""

    fresh: bool
    record: ActionRecordRow


class ActionAlreadyDispatchedError(Exception):
    """An action was dispatched but never completed, and this caller found it that way.
    Not an error condition so much as the state a crash leaves behind -- callers resolve it
    with ``reconcile``, which is why this carries the record."""

    def __init__(self, record: ActionRecordRow) -> None:
        self.record = record
        super().__init__(
            f"action {record.idempotency_key} was dispatched at {record.dispatched_at} and "
            "never completed; reconcile before dispatching again"
        )


async def claim(action: EffectfulAction) -> ClaimResult:
    """Writes the dispatch record, or reports the one already there.

    Written *before* the external call, deliberately. The alternative -- record after --
    leaves a window where the call happened and nothing knows it, which is the exact
    window a crash finds."""
    async with tenant_scope(action.tenant_id) as session:
        existing = await session.scalar(
            select(ActionRecordRow).where(
                ActionRecordRow.tenant_id == action.tenant_id,
                ActionRecordRow.idempotency_key == action.key,
            )
        )
        if existing is not None:
            session.expunge(existing)
            return ClaimResult(fresh=False, record=existing)

        row = ActionRecordRow(
            tenant_id=action.tenant_id,
            session_id=action.session_id,
            event_seq=action.event_seq,
            attempt_target=action.attempt_target,
            idempotency_key=action.key,
            server_key=action.server_key,
            tool_name=action.tool_name,
            arguments=dict(action.arguments),
            dispatched_at=datetime.now(UTC),
        )
        session.add(row)
        await session.flush()
        session.expunge(row)
        return ClaimResult(fresh=True, record=row)


async def complete(tenant_id: uuid.UUID, key: str, outcome: str, result: dict[str, Any]) -> bool:
    """Claims the outcome atomically. ``False`` means someone else got there first, which
    is a real and expected result under concurrency, not a failure."""
    if outcome not in ("completed", "failed", "reconciled"):
        raise ValueError(f"unknown action outcome {outcome!r}")
    async with tenant_scope(tenant_id) as session:
        claimed = await session.execute(
            update(ActionRecordRow)
            .where(
                ActionRecordRow.tenant_id == tenant_id,
                ActionRecordRow.idempotency_key == key,
                ActionRecordRow.outcome.is_(None),
            )
            .values(outcome=outcome, result=result, completed_at=datetime.now(UTC))
            .returning(ActionRecordRow.id)
        )
        return claimed.first() is not None


async def get_record(tenant_id: uuid.UUID, key: str) -> ActionRecordRow | None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(ActionRecordRow).where(
                ActionRecordRow.tenant_id == tenant_id,
                ActionRecordRow.idempotency_key == key,
            )
        )
        if row is not None:
            session.expunge(row)
        return row


async def pending_actions(tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[ActionRecordRow]:
    """Dispatched-but-unresolved actions for one session -- what a resume has to reconcile
    before it may take another step."""
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (
                await session.execute(
                    select(ActionRecordRow)
                    .where(
                        ActionRecordRow.session_id == session_id,
                        ActionRecordRow.outcome.is_(None),
                    )
                    .order_by(ActionRecordRow.event_seq)
                )
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows
