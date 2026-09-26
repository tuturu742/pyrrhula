"""``scope`` : named visibility compartments within a workspace.
Every ``KnowledgeEntry``/``Entity``/``EntityField`` carries a ``scope_key`` (already true
for ``knowledge_entry``/``knowledge_chunk`` since A1.1); this table is what gives those
free-text keys real membership semantics.

``members`` JSONB shape (the plan's own snippet just says "principal/role refs" --
this is the concrete shape this codebase uses, documented here since it's the one place
both the writer (``seed_default_scopes``) and reader (``scopes_for``) must agree on it):

  kind='public' -- members unused (``{}``); granted to anyone with a workspace
                             role at all (see ``visibility.py``).
  kind='role' -- {"roles": ["facilitator", ...]} -- workspace/agent role names
                             that qualify. Matched against ``WorkspaceMembership.role``
                             (human/service principals) or ``Persona.persona_type`` (agents).
  kind='group'|'private' -- {"principal_ids": ["<uuid>", ...]} -- explicit principal
                             membership, agents included via their own principal id.

The ``agent_private:<principal_id>`` convention (per-principal compartment, groundwork for
Phase 2 secrets) needs no ``scope`` row at all -- ``scopes_for`` grants it structurally to
its own owner, see that module's docstring.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class ScopeRow(Base):
    __tablename__ = "scope"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    members: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_scope_workspace_key"),
        CheckConstraint("kind IN ('public', 'role', 'group', 'private')", name="ck_scope_kind"),
        Index("ix_scope_workspace", "workspace_id"),
    )


class ContextManifestRow(Base):
    """The persisted record of exactly what a model saw and why (INV-10). One row per
    ``(session_id, event_seq)`` -- the same event_seq the
    message it renders context for is stamped with. Append-only (‡): the migration
    revokes UPDATE/DELETE, matching ``checkpoint``/``session_event``.

    ``entries``/``redactions`` store ``core.assembler.context_assembler.ManifestEntry``/
    ``Redaction`` as JSON-safe dicts (UUIDs stringified) -- see
    ``core.assembler.manifest``'s ``_entry_to_json``/``_redaction_to_json``, the one place
    that shape is defined.
    """

    __tablename__ = "context_manifest"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    viewer_principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    phase: Mapped[str] = mapped_column(String(63), nullable=False)
    entries: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    redactions: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    resolution_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list
    )
    entity_versions: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # Nullable until Phase 2's behavior-profile machinery exists.
    behavior_profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, default=dict)
    rendered_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # which elapsed-history range a resume summary covered, and the hash of the
    # summary text itself -- INV-10 across a resume needs both (the range says which
    # events to re-summarise, the hash says whether you rebuilt the same text). All three
    # NULL on a turn that injected no summary, which is every turn before G4.1.
    history_summary_from_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    history_summary_to_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    history_summary_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "event_seq", name="uq_context_manifest_session_seq"),
        Index("ix_context_manifest_session", "session_id"),
    )
