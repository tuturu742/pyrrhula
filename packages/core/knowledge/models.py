"""Knowledge content model (plan §6.1, §12.3): Source -> Version (immutable,
content-addressed) -> Entry (unit of authorship) -> Chunk (unit of retrieval, derived).

``knowledge_chunk`` isn't mapped here. Like ``vector_store_item`` (T0.3), it needs the
``vector`` column type, which isn't wired into the SQLAlchemy type system — it's created
via raw SQL in the migration and excluded from autogenerate via ``migrations/env.py``'s
``_RAW_SQL_TABLES``. Nothing in A1.1 needs to read or write it (that starts at A1.2/A1.3),
so there's no ORM model to keep in sync with a schema no code touches yet.

Two deliberate deviations from the plan's §12.3 sketch, both required to reconcile
"entries are added to a draft before publish" with CLAUDE.md's append-only rule for
``knowledge_source_version`` (no UPDATE/DELETE grant — see the migration):

- ``KnowledgeEntry.version_id`` is **nullable**, not the implied NOT NULL. NULL means
  "the current mutable draft, not yet part of any published version" — a real row the
  author edits freely. A ``KnowledgeSourceVersion`` row is only ever INSERTed, at publish
  time, with its final ``content_hash`` already computed; it is never created empty and
  then updated in place, because the app role has no UPDATE grant on that table at all.
  Publishing copies the current draft entries into new rows stamped with the new
  ``version_id``, leaving the original draft rows in place as the next round's staging
  area. See ``core.knowledge.authoring.publish_version``.
- ``KnowledgeEntry.knowledge_source_id`` is added (the plan's sketch reaches the source
  only via ``version_id``) because draft entries, by definition, don't have one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from core.tenancy.models import Base


class KnowledgeSource(Base):
    """The book. ``class_`` (mapped to the ``class`` column) is a free string, not a
    CHECK-constrained enum — the plan requires it extensible per tenant beyond the
    built-in rules|lore|misc."""

    __tablename__ = "knowledge_source"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    class_: Mapped[str] = mapped_column("class", String(32), nullable=False)
    owner_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="tenant")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft-delete (migration c4f2a7e1b9d3): NULL = live, a timestamp = archived. Archiving a
    # source also detaches it from workspaces (see core.knowledge.authoring.archive_source),
    # so retrieval drops it; its append-only versions stay intact.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_knowledge_source_tenant_key"),
        CheckConstraint(
            "visibility IN ('private', 'tenant', 'public')", name="ck_knowledge_source_visibility"
        ),
        # use_alter: this and knowledge_source_version FK to each other (a version FKs to
        # its source; the source's "current" pointer FKs to a version). use_alter defers
        # this one to a separate ALTER TABLE (after both tables exist) instead of a
        # circular CREATE TABLE dependency — see the migration.
        ForeignKeyConstraint(
            ["current_version_id"],
            ["knowledge_source_version.id"],
            ondelete="SET NULL",
            use_alter=True,
            name="fk_knowledge_source_current_version",
        ),
    )


class KnowledgeSourceVersion(Base):
    """Immutable, content-addressed (plan §6.1). Append-only in practice, not just
    intent: the migration REVOKEs UPDATE/DELETE from the app role on this table, the same
    control ``audit_log`` uses (T0.7). Only ever INSERTed by
    ``core.knowledge.authoring.publish_version``, with every column already final."""

    __tablename__ = "knowledge_source_version"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # F3.12: set when this version was published from an approved AI-drafted edit
    # proposal rather than a manual edit -- `created_by` still names the human who
    # approved it (an accepted proposal is never anonymous), this just distinguishes how
    # the content was drafted.
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        UniqueConstraint(
            "knowledge_source_id", "version_number", name="uq_knowledge_source_version_number"
        ),
    )


class KnowledgeEntry(Base):
    """The unit of authorship (plan §6.1, §6.4). ``version_id IS NULL`` means "current
    draft"; entry-key uniqueness is therefore two partial unique indexes (one per state) —
    a plain ``UniqueConstraint`` can't express a ``WHERE``-qualified index, so these are
    declared as ``Index(..., postgresql_where=...)`` instead, and created via raw
    ``op.execute`` in the migration (matching the physical DDL, same as every other index
    here)."""

    __tablename__ = "knowledge_entry"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.id", ondelete="CASCADE"),
        nullable=True,
    )
    entry_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    class_: Mapped[str] = mapped_column("class", String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # G4.6: flagged by the import/ingestion injection scan and excluded from retrieval
    # until a human clears it (plan §16.6). Duplicated onto ``knowledge_chunk`` so the
    # retrieval filter stays a chunk-local pushdown; ``approve_quarantined_entry`` clears
    # both in one transaction so they cannot drift.
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    quarantine_reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    quarantine_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keys: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    secondary_keys: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    logic: Mapped[str] = mapped_column(String(8), nullable=False, default="AND")
    use_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    constant: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sticky: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cooldown: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inclusion_group: Mapped[str | None] = mapped_column(String(63), nullable=True)
    # 'before_char' | 'after_char' | 'at_depth_N' (N parametric) — free string, not a
    # CHECK enum, since 'at_depth_N' isn't a fixed value set.
    position: Mapped[str] = mapped_column(String(32), nullable=False, default="before_char")
    insertion_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("logic IN ('AND', 'OR', 'NOT')", name="ck_knowledge_entry_logic"),
        # Partial unique indexes: entry_key is unique within a published version, and
        # separately unique within the current draft — a plain UniqueConstraint can't
        # express this since Postgres treats NULL != NULL, so "unique(version_id,
        # entry_key)" alone would let the same entry_key repeat freely across draft rows
        # (all of which have version_id IS NULL).
        Index(
            "uq_knowledge_entry_published_key",
            "version_id",
            "entry_key",
            unique=True,
            postgresql_where=text("version_id IS NOT NULL"),
        ),
        Index(
            "uq_knowledge_entry_draft_key",
            "knowledge_source_id",
            "entry_key",
            unique=True,
            postgresql_where=text("version_id IS NULL"),
        ),
    )


class WorkspaceKnowledgeAttachment(Base):
    """The workspace's opinion about a shared source (plan §6.1, reqs 3+4) — priority,
    scope, and version pin all live here, never on the source itself, so the same source
    can be attached to two workspaces with different settings."""

    __tablename__ = "workspace_knowledge_attachment"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL = follow the source's current_version_id (plan req 5's "follow_latest").
    version_pin: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_source_version.id", ondelete="SET NULL"),
        nullable=True,
    )
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    priority_weight: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "knowledge_source_id", name="uq_workspace_knowledge_attachment"
        ),
    )


class EntryActivationStateRow(Base):
    """Per-session activation bookkeeping for one knowledge entry.

    ``core.knowledge.activation`` implements ``sticky`` (stay active for N turns after a
    match) and ``cooldown`` (do not re-activate for N turns), and both are computed from a
    ``prior_state`` the caller supplies. Nothing persisted that state, so the assembler
    passed an empty dict every turn and the two fields silently did nothing: an entry
    marked sticky behaved exactly like one that was not.

    Keyed per session because that is the span the semantics are defined over -- "turns"
    means turns of this session, and two sessions in one workspace must not inherit each
    other's cooldowns. Ordinary mutable state, not append-only: it is bookkeeping about
    the conversation, not a record of it.
    """

    __tablename__ = "entry_activation_state"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    entry_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_entry.id", ondelete="CASCADE"), nullable=False
    )
    sticky_until_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cooldown_until_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("session_id", "entry_id", name="uq_entry_activation_state"),)
