"""ORM models for ``audit_log``, ``usage_record``, and ``price_table`` —
groups these three under "Audit, usage, reports" and this module mirrors that. Owned here
rather than in ``core.tenancy.models`` because these are cross-cutting concerns, not
tenancy tables — but they register on the same shared ``Base`` metadata so migrations
see them.

``audit_log`` is append-only in practice, not just in intent: the migration revokes
UPDATE and DELETE from ``pyrrhula_app`` on that one table, on top of whatever
``ALTER DEFAULT PRIVILEGES`` granted broadly. "We don't call delete" is not the control;
the missing grant is.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ARRAY, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

# UsageRecordRow.message_id FKs to message.id by string reference -- SQLAlchemy only
# resolves that at mapper-configuration time, which requires MessageRow's module to have
# been imported by *someone* first (the same registration-order fix B1.2/C1.3/C1.6 all
# needed). Importing it here guarantees that regardless of what a caller of this module
# imports.
import core.sessions.models  # noqa: E402, F401
from core.tenancy.models import Base


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    actor_principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    query: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_audit_log_tenant_created", "tenant_id", "created_at"),)


class PriceTableRow(Base):
    """Versioned DATA, never code. Not tenant-scoped — pricing is the
    platform's knowledge of what providers charge, not a tenant's data."""

    __tablename__ = "price_table"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)  # '*' = wildcard fallback
    effective_from: Mapped[date] = mapped_column(nullable=False)
    input_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)


class UsageRecordRow(Base):
    """Written in the SAME transaction as the message it meters  —
    metering that can drift from the thing it meters will drift, and then you cannot
    bill or debug. the walking skeleton is the first, minimal call site; every
    future model call (gate, rerank, embed, report, rewrite) writes one of these too."""

    __tablename__ = "usage_record"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # which reply this generation call's usage belongs to -- nullable (a tool-loop's
    # intermediate provider calls and the final answer all share the one message they
    # together produced, set at commit time; non-generation purposes -- gate/rerank/embed
    # -- have no single owning message and leave this null). SET NULL, not CASCADE:
    # usage_record is a billing record and must survive regardless of the message's own
    # fate, mirroring MessageRow.context_manifest_id's identical reasoning.
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("message.id", ondelete="SET NULL"), nullable=True
    )
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The human who DIRECTLY triggered this spend (assistant drafts/chat) -- basis of
    # per-user daily limits. NULL for autonomous session/worker spend.
    principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 'generation'|'gate'|'rerank'|'embed'|'report'|'rewrite' -- same taxonomy as the
    # ModelProvider egress policy, so cost attribution and egress share one
    # vocabulary rather than inventing a second.
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
