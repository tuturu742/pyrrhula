"""ORM model for the v1 (Postgres-backed) JobQueue adapter. Registered on the shared
``core.tenancy.models.Base`` metadata so Alembic picks it up, but owned here rather than
in ``core.tenancy`` because this table's *shape* is specific to this adapter's strategy —
a future Temporal-backed adapter (H5.9) wouldn't have one at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class JobRow(Base):
    """No RLS: see core.ports.job_queue module docstring for why the job queue is
    deliberately not tenant-isolated at the database layer."""

    __tablename__ = "job"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_job_status_created_at", "status", "created_at"),
        Index("ix_job_kind", "kind"),
    )
