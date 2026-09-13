"""Workflow (#2, moddable per the tenant-authoring feature): a named bundle of a vocabulary
overlay + persona-type display names + label overrides + featured flows + capabilities.

``tenant_id`` is nullable, the ``vocabulary_overlay`` pattern exactly: NULL = a global system
template (rpg / swdev / enterprise -- read-only clone sources; the RLS policy's WITH CHECK
makes them structurally unwritable through the app role), non-NULL = a tenant's own workflow.
A tenant selects one by key (tenant.settings["workflow_key"]); tenant rows shadow a
same-keyed global on lookup.

Capabilities: global rows carry raw ``{"mcp_servers": [...]}`` provisioned by
``core.workflows.service.apply_workflow_capabilities``. Tenant-authored rows never write raw
MCP configs -- their tool surface is ``{"repo_access": bool}``, interpreted at session
creation (per-repo git servers registered on the workspace allowlist), keeping the MCP
allowlist the one egress control.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class WorkflowRow(Base):
    __tablename__ = "workflow"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # NULL = global system template; non-NULL = tenant-authored (RLS-scoped).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The vocabulary overlay this workflow applies (by key). NULL = the system default.
    overlay_key: Mapped[str | None] = mapped_column(String(63), nullable=True)
    # {persona_type: display_name} -- e.g. {"supervisor": "Lead", "participant": "Engineer"}.
    persona_type_labels: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    # {label_key: display} -- merged over the base overlay's labels into a materialized
    # tenant overlay (`wf-<key>`) when this workflow is selected.
    label_overrides: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Process-definition keys the launch picker features first for this workflow.
    featured_process_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Global rows: {"mcp_servers": [...]}. Tenant rows: {"repo_access": bool}.
    capabilities: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "uq_workflow_system_key",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
        Index(
            "uq_workflow_tenant_key",
            "tenant_id",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )
