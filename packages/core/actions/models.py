"""ORM model for ``completed_operation`` — idempotency. Resume-from-checkpoint
re-executes work; a node that made a paid API call before an interrupt point charges the
tenant's key twice on resume unless every side-effecting operation checks here first.

``status`` is the claim mechanism (§core.actions.idempotency): 'in_progress' is what a
concurrent duplicate call sees and waits out, not just a cache of finished results.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class CompletedOperationRow(Base):
    __tablename__ = "completed_operation"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
