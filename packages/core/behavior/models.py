"""Behaviour framework schema (E2.3, plan §8.1, §8.7, §12.4): `axis_definition` (pack
content -- mutable, upserted by key, matching `rule_system`'s shape, not an immutable
history) and `behavior_profile` (‡ append-only versions per agent -- "what disposition was
this agent running when it concealed that?" must be answerable six months later; a mutable
row destroys that).
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


class AxisDefinitionRow(Base):
    """One behavioural axis, as declared by a pack. `bindings` is a JSONB list of
    `{"kind": ..., ...}` objects -- `kind` is validated against the fixed taxonomy
    (`core.behavior.validation.BINDING_KINDS`); everything else in a binding is
    kind-specific and opaque to this schema. `label_key` is overlay-resolved, never a
    literal domain word (CLAUDE.md rule 1) -- the UI renders it through
    `useLabel()`/`label()`, same as every other user-facing noun in this codebase."""

    __tablename__ = "axis_definition"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    pack_id: Mapped[str] = mapped_column(String(63), nullable=False)
    key: Mapped[str] = mapped_column(String(63), nullable=False)
    label_key: Mapped[str] = mapped_column(String(255), nullable=False)
    range_min: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    range_max: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    # Resting value shown in the UI and used when a profile omits this axis. Nullable
    # for rows created before this column existed; the API falls back to the midpoint.
    default_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stakes: Mapped[str] = mapped_column(String(8), nullable=False)
    semantics_md: Mapped[str] = mapped_column(Text, nullable=False)
    bindings: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "pack_id", "key", name="uq_axis_definition_tenant_pack_key"),
        CheckConstraint("stakes IN ('low', 'high')", name="ck_axis_definition_stakes"),
        CheckConstraint("range_min < range_max", name="ck_axis_definition_range"),
    )


class BehaviorProfileRow(Base):
    """Append-only (CLAUDE.md rule 5): a change to an agent's dial settings is always a
    new version, never an edit -- the migration REVOKEs UPDATE/DELETE from the app role
    on this table, the same control every other append-only table in this codebase uses.
    Every `ContextManifest` records the version in effect at generation time (C1.3's
    `behavior_profile_version` column, wired in this task)."""

    __tablename__ = "behavior_profile"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    persona_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persona.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    pack_id: Mapped[str] = mapped_column(String(63), nullable=False)
    axis_values: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("persona_id", "version", name="uq_behavior_profile_agent_version"),
    )
