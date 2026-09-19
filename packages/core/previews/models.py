"""Preview environments: a built artifact, running, with a link a human can open.

Delegated work ends at an artifact (`game-web.tar.gz`); a preview is that artifact
actually *served* by a container so a person can try it before anyone calls the work
done. The row is the source of truth for lifecycle and sharing -- the engine remains the
source of truth for execution -- and carries the internal address the API proxy talks to.

The (tenant, name) unique constraint plus a deterministic name is what makes starting a
preview idempotent (CLAUDE.md rule 8): re-running converges on one container per repo
instead of leaking one per attempt. This follows ``core.exec_envs`` rather than the
``completed_operation`` ledger, because the engine-side name is already the natural key.

Statuses: ``starting`` (the worker is provisioning), ``running`` (serving; the only
status the public proxy will serve), ``failed`` (provisioning or the container died),
``stopped`` (a human stopped it), ``expired`` (past ``expires_at``; the reaper tore the
container down).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base

ACTIVE_STATUSES = ("starting", "running")


class PreviewEnvironmentRow(Base):
    __tablename__ = "preview_environment"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    # Engine-side name (container name / job label / ECS startedBy) AND, on socket
    # engines, the container's DNS alias. Deterministic: pyr-prev-<repo8>.
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Engine-side handle for teardown/status; equals ``name`` for every adapter today.
    ref: Mapped[str] = mapped_column(String(120), nullable=False, default="", server_default="")
    engine_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    image: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    # Reachable from the api process only; never handed to a browser.
    internal_url: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="starting", server_default="starting"
    )
    # DB-level FKs live in the migration; not declared here so this module maps
    # standalone without importing the process/repo models.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    repo_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    artifact_name: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default=""
    )
    # The git ref this preview was built from. Distinct from ``ref`` above, which is the
    # engine's own handle for the container -- an unfortunate collision of a short word,
    # and the reason this one is spelled out.
    git_ref: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    created_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by_label: Mapped[str] = mapped_column(
        String(120), nullable=False, default="", server_default=""
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_preview_environment_name"),)
