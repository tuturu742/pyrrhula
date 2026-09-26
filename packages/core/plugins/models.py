"""Plugin repositories: operator-registered git repositories that provide workflows.

A plugin repository is pure declarative content (CLAUDE.md rules 9 and 10): ``plugin.json`` at the
root plus one directory per workflow containing ``workflow.json`` (the template row's
fields) and the pack content the generic loader understands (schemas/, processes/,
rule_systems/, tools/, axes/, overlay/, seed/). Nothing in a plugin ever executes.

The table is deliberately **not tenant-scoped** (like ``tenant`` and ``job``): plugin
repositories are deployment-level configuration managed by the platform operator
through the admin console, and their workflows become NULL-tenant system templates.
All writes go through ``admin_registry_session`` -- the app role cannot write global
rows at all (RLS WITH CHECK).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class PluginRepositoryRow(Base):
    __tablename__ = "plugin_repository"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(String(1023), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    # 'baked' = the default plugin, whose pinned content ships inside the image
    # (/app/packs) so first boot never needs the network; 'git' = cloned at sync time.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="git", server_default="git"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Which workflow keys this repository owns -- the cross-repo uniqueness ledger.
    workflow_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowManifest(BaseModel):
    """``<workflow>/workflow.json`` -- exactly the global template row's fields."""

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=255)
    overlay_key: str | None = None
    persona_type_labels: dict[str, str] = Field(default_factory=dict)
    label_overrides: dict[str, str] = Field(default_factory=dict)
    featured_process_keys: list[str] = Field(default_factory=list)
    capabilities: dict[str, object] = Field(default_factory=dict)


class PluginManifest(BaseModel):
    """``plugin.json`` at the repository root."""

    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    workflows: list[str] = Field(min_length=1)
