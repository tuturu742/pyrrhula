"""``process_definition`` ORM model (plan §12.2, B1.1). Each row is one immutable version
of one process definition -- "publish = new immutable version" (B1.1's subtask wording) is
an authoring-layer convention (``authoring.py`` always INSERTs, never UPDATEs an existing
row's ``definition``), not a grant-level restriction; see the migration's docstring for why
this table isn't marked append-only (‡) the way ``knowledge_source_version`` is.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class ProcessDefinitionRow(Base):
    __tablename__ = "process_definition"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = a tenant-level template, not tied to one workspace (see migration docstring
    # for the partial-unique-index shape this implies).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The validated DSL content (core.process.dsl.schema.ProcessDefinitionDSL.model_dump()).
    definition: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # list[{field_path, message}] when validation failed; NULL/empty when it passed.
    validation_errors: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft-delete (migration c4f2a7e1b9d3): NULL = live, a timestamp = archived (hidden from
    # the definition list + launch picker). Sessions already running it are unaffected.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Two partial unique indexes, not one plain UniqueConstraint(workspace_id, key,
        # version): Postgres treats every NULL as distinct, so a bare constraint would let
        # multiple tenant-template (workspace_id IS NULL) rows share a key/version freely.
        # See the migration's docstring for the full reasoning.
        Index(
            "uq_process_definition_workspace_key_version",
            "workspace_id",
            "key",
            "version",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_process_definition_tenant_template_key_version",
            "tenant_id",
            "key",
            "version",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
    )
