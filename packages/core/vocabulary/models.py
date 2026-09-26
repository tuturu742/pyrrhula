"""``vocabulary_overlay``: the label-
resolution layer that lets the same domain-neutral engine present as an RPG tool
(``rpg_v1``) or an enterprise tool (``enterprise_v1``) with zero code changes -- nothing
in the schema is ever renamed, only how the UI *labels* it.

``tenant_id`` nullable: ``NULL`` is a system overlay (shipped, available to every
tenant); a real UUID is a tenant-authored custom overlay (the column exists so the
overlay packs have somewhere to write to without a schema change). Two partial unique
indexes, not one plain constraint, for the same
reason ``ProcessDefinitionRow``'s migration documents: Postgres treats every NULL as
distinct, so a bare ``UNIQUE(tenant_id, key)`` would let multiple system overlays share
a key freely.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class VocabularyOverlayRow(Base):
    __tablename__ = "vocabulary_overlay"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # NULL = system overlay, visible to every tenant (see module docstring).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # {label_key: display_string, ...} -- the glossary, keyed by label_key.
    labels: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "uq_vocabulary_overlay_system_key",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
        Index(
            "uq_vocabulary_overlay_tenant_key",
            "tenant_id",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )
