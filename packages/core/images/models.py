"""ORM rows for the image tables. ``image_registry`` is deployment-level: no tenant, no RLS,
written only through the admin API (see the migration's docstring)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, func
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
