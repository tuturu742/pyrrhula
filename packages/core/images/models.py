"""ORM rows for the image tables.

``image_registry`` is deployment-level: no tenant, no RLS, written only through the admin
API. ``image_definition`` and ``image_build`` are tenant-scoped behind FORCE RLS (see the
migrations' docstrings)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class ImageRegistryRow(Base):
    __tablename__ = "image_registry"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    pull_host: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    path_prefix: Mapped[str] = mapped_column(String(120), nullable=False, default="pyrrhula")
    path_style: Mapped[str] = mapped_column(String(8), nullable=False, default="nested")
    insecure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # A provider_credential row on ADMIN_TENANT_ID: read access, used to verify digests and
    # handed to engines to pull. Never a tenant's, never returned by the API.
    credential_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    credential_username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    k8s_pull_secret: Mapped[str] = mapped_column(String(253), nullable=False, default="")
    supports_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    public_by_default_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


ACTIVE_BUILD_STATUSES = ("queued", "submitted", "building", "verifying", "smoke_testing")
TERMINAL_BUILD_STATUSES = ("ready", "failed", "cancelled", "superseded", "deleted")


class ImageDefinitionRow(Base):
    __tablename__ = "image_definition"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    # Also the runtime key a repo selects once a build of it is ready.
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    dockerfile: Mapped[str] = mapped_column(Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="built")
    harness_key: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    current_build_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImageBuildRow(Base):
    __tablename__ = "image_build"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("image_definition.id", ondelete="CASCADE"), nullable=False
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    builder_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    registry_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dockerfile: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # What the builder was told to push (built) or what the bundle named (imported).
    target_ref: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    external_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    external_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    # What the builder said it produced; never used by itself (see ``digest``).
    reported_digest: Mapped[str] = mapped_column(String(71), nullable=False, default="")
    # What the registry itself answered. The only digest anything runs.
    digest: Mapped[str] = mapped_column(String(71), nullable=False, default="")
    pinned_ref: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # A bundle's word that a harness is inside: a claim, recorded only as one.
    harness_claim: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Set only when the smoke test proved the claim.
    baked_harness: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    smoke: Mapped[str] = mapped_column(Text, nullable=False, default="")
    log_tail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
