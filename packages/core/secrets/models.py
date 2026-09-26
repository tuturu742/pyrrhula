"""Secret schema: a first-class record with its own holder set,
disclosure state machine, and provenance — the substrate everything else in Phase 2
(gate, exclusion, overseer) operates on. A secret is a property of a *relationship*
between a fact and its holders, not a field on Entity or Persona: two conspirators
share one row, one holder each, not a copy per holder.

``SecretRow.gist_embedding`` isn't mapped here. Like ``knowledge_chunk.embedding``,
it needs the pgvector ``vector`` type, which isn't wired into the SQLAlchemy type system —
it's added via raw SQL in the migration and excluded from autogenerate via
``migrations/env.py``'s ``_RAW_SQL_COLUMNS`` (a column-level sibling of ``_RAW_SQL_TABLES``:
unlike ``knowledge_chunk``, every other column on ``secret`` needs real ORM-backed CRUD
from day one, so excluding the whole table isn't the right shape here). Nothing in E2.1
populates it — that starts when the disclosure gate or overseer needs
semantic search over gists.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.tenancy.models import Base


class SecretRow(Base):
    """The record itself. ``content_ciphertext`` is routed through the ``Encryptor``
    port at the repo layer (identity impl for now, D11) — never read directly by anything
    outside ``core.secrets.repo``/``core.assembler``/``core.overseer`` (INV-1). ``gist`` is
    the only field the disclosure gate will ever see. ``behavioral_directive`` is
    nullable in the schema but not optional in spirit (E2.2 treats an empty one as a lint
    warning) — it's the field that makes exclusion produce an agent with a
    motivation instead of a lobotomy."""

    __tablename__ = "secret"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    # Polymorphic subject, no per-kind FK -- same shape as audit_log.resource_type/
    # resource_id: the target table varies by subject_kind, so a single FK
    # constraint can't express it.
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    gist: Mapped[str] = mapped_column(Text, nullable=False)
    hint_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    behavioral_directive: Mapped[str | None] = mapped_column(Text, nullable=True)
    disclosure_state: Mapped[str] = mapped_column(String(16), nullable=False, default="undisclosed")
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # May this plaintext leave the deployment in the clear? A fourth question, kept apart
    # from the other three on purpose: ``scope_key`` is who knows in-world,
    # ``disclosure_state``/holders is who has come to know in play, and the
    # ``secret:author``/``secret:inspect`` permissions are who may read as an operator.
    # This one is about publication, and defaults closed -- a character brief written to
    # be shared is a deliberate act, never an oversight.
    publication: Mapped[str] = mapped_column(
        String(16), nullable=False, default="guarded", server_default="guarded"
    )
    authored_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "subject_kind IN ('entity', 'agent', 'workspace', 'knowledge_entry')",
            name="ck_secret_subject_kind",
        ),
        CheckConstraint(
            "disclosure_state IN ('undisclosed', 'hinted', 'partial', 'public')",
            name="ck_secret_disclosure_state",
        ),
        CheckConstraint("publication IN ('guarded', 'publishable')", name="ck_secret_publication"),
    )


class SecretHolderRow(Base):
    """Who knows (D3, brief item 14: the facilitator does *not* see held secrets by
    default -- visibility is an explicit scope opt-in, never a default, so there is no
    facilitator-holder row created implicitly anywhere in this schema or its repo)."""

    __tablename__ = "secret_holder"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    secret_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("secret.id", ondelete="CASCADE"), nullable=False
    )
    holder_principal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE"), nullable=False
    )
    holder_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    acquired_via_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session_event.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("secret_id", "holder_principal_id", name="uq_secret_holder"),
        CheckConstraint(
            "holder_kind IN ('author', 'discovered', 'told')", name="ck_secret_holder_kind"
        ),
    )


class SecretDisclosureEventRow(Base):
    """Append-only (CLAUDE.md rule 5): what actually got said, to whom, and how. The
    migration REVOKEs UPDATE/DELETE from the app role on this table, the same control
    ``audit_log``/``knowledge_source_version`` use."""

    __tablename__ = "secret_disclosure_event"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    secret_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("secret.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    disclosed_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    disclosed_to: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("disclosure_decision.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("message.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "mode IN ('full', 'hint', 'inferred', 'leaked')", name="ck_secret_disclosure_event_mode"
        ),
    )


class DisclosureDecisionRow(Base):
    """Append-only (CLAUDE.md rule 5): the gate's recorded rationale  — evidence,
    not a source of truth for what happened (that's ``secret_disclosure_event``). Q11 (Phase
    5, Legal) owns its retention question; this schema doesn't pre-judge it."""

    __tablename__ = "disclosure_decision"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("session.id", ondelete="CASCADE"), nullable=False
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
    )
    behavior_profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decisions: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agent.id", ondelete="SET NULL"), nullable=True
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
